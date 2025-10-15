
"""
Scannerfilesuper_PAYMENTS_WIRED_EMAILPATCH_READY.py
--------------------------------------------------------------------
Drop-in FastAPI router to RECORD the initiating user's email + user_id
into Supabase `public.payments` **at checkout start**.

✅ Matches your latest PAYMENTS schema (plan_duration_days, partial unique on sr_invoice_id).
✅ Works with your existing JWT auth (same HS256 secret).
✅ Uses Supabase PostgREST with SERVICE ROLE key (bypasses RLS safely on server).
✅ If the PROFILES row doesn't exist yet for the email, it will create it and use that id.

How to use (two options):
  A) Import and include_router in your existing app.py:
        from Scannerfilesuper_PAYMENTS_WIRED_EMAILPATCH_READY import pro_router
        app.include_router(pro_router)   # after you create `app = FastAPI()`
  B) Run as a tiny standalone app (for testing):
        uvicorn Scannerfilesuper_PAYMENTS_WIRED_EMAILPATCH_READY:app --reload

Endpoints:
  - GET /pro/checkout?plan={weekly|monthly|3m|6m|yearly|3y}
    * Requires Authorization: Bearer <token> (your current JWT with `email` claim)
    * Creates a `payments` row with user_email + user_id (PROFILES.id)
    * Returns JSON with the created row (status 'pending').
    * You can then redirect to CryptoCloud invoice using the response.

NOTE: This file is self-contained and does not modify your other code.
"""

from __future__ import annotations
import os, time, json, re
from typing import Optional, Dict, Any
import requests
import jwt

# --- ENV (must be set in your environment/.env) ---
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SUPABASE_SERVICE_KEY", ""))
JWT_SECRET   = os.getenv("JWT_SECRET", "change_me_super_secret")   # must match your app

# ---- Simple helpers for Supabase PostgREST ----
def _sb_headers(extra: Optional[Dict[str,str]] = None) -> Dict[str,str]:
    h = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }
    if extra:
        h.update(extra)
    return h

def _sb_get_profile_id(email: str) -> Optional[str]:
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and email):
        return None
    base = f"{SUPABASE_URL}/rest/v1"
    for tbl in ("PROFILES", "profiles"):
        url = f"{base}/{tbl}?select=id&email=eq.{email}&limit=1"
        try:
            r = requests.get(url, headers=_sb_headers({"Accept":"application/json"}), timeout=10)
            if r.status_code == 200 and r.headers.get("content-type","").startswith("application/json"):
                arr = r.json()
                if isinstance(arr, list) and arr:
                    rid = arr[0].get("id") or arr[0].get("ID") or arr[0].get("Id")
                    if rid: return str(rid)
        except Exception:
            pass
    return None

def _sb_create_profile(email: str, username: Optional[str] = None) -> Optional[str]:
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and email):
        return None
    base = f"{SUPABASE_URL}/rest/v1/PROFILES"
    data = {
        "email": email,
        "username": (username or (email.split("@")[0]))[:32],
        "status": "FREE",
        "role": "user",
        "meta": {"source": "payments-writer"}
    }
    try:
        r = requests.post(base, headers=_sb_headers({"Content-Type":"application/json","Prefer":"return=representation","Accept":"application/json"}), json=data, timeout=12)
        if r.status_code in (200, 201):
            js = r.json()
            if isinstance(js, list) and js and isinstance(js[0], dict) and "id" in js[0]: return js[0]["id"]
            if isinstance(js, dict) and "id" in js: return js["id"]
    except Exception:
        pass
    return None

def _sb_ensure_profile_id(email: str, username: Optional[str] = None) -> Optional[str]:
    pid = _sb_get_profile_id(email)
    if pid: return pid
    return _sb_create_profile(email, username=username)

# ---- Plan catalog (adjust freely) ----
PLAN_CATALOG = {
    "weekly":  {"plan_name": "Weekly",            "plan_duration_days": 7,   "amount": 10.00},
    "monthly": {"plan_name": "Monthly",           "plan_duration_days": 30,  "amount": 20.00},
    "3m":      {"plan_name": "3 Months",          "plan_duration_days": 90,  "amount": 55.00},
    "6m":      {"plan_name": "6 Months",          "plan_duration_days": 180, "amount": 100.00},
    "yearly":  {"plan_name": "Yearly",            "plan_duration_days": 365, "amount": 180.00},
    "3y":      {"plan_name": "3 Years (Promo)",   "plan_duration_days": 1095,"amount": 450.00},
}

# ---- JWT helpers ----
def _current_email_from_auth(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None
    email = (data.get("email") or "").strip().lower()
    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return None
    return email

# ---- Payment insertion ----
def create_pending_payment(user_email: str, plan_key: str, username_hint: Optional[str] = None) -> Dict[str, Any]:
    """
    Creates a payments row with both `user_email` and `user_id` populated.
    Uses latest schema: plan_duration_days, provider='cryptocloud', status='pending'.
    """
    if plan_key not in PLAN_CATALOG:
        raise ValueError("Unknown plan key.")
    plan = PLAN_CATALOG[plan_key]

    # ensure PROFILES.id
    profile_id = _sb_ensure_profile_id(user_email, username=username_hint)

    row = {
        "user_id": profile_id,                       # UUID or NULL if not resolvable
        "user_email": user_email,                    # plain email (nullable in schema, but we set it)
        "plan_name": plan["plan_name"],
        "plan_duration_days": plan["plan_duration_days"],
        "amount": float(plan["amount"]),
        "currency": "USD",
        "status": "pending",
        "provider": "cryptocloud",
        # provider_invoice_id, sr_invoice_id left NULL until gateway callback
        "note": None,
        "meta": {"source": "checkout-init", "plan_key": plan_key, "ts": int(time.time())},
    }

    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        raise RuntimeError("Supabase env missing: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")

    url = f"{SUPABASE_URL}/rest/v1/payments"
    headers = _sb_headers({"Content-Type":"application/json", "Prefer":"return=representation"})
    r = requests.post(url, headers=headers, data=json.dumps(row), timeout=12)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Supabase insert failed: HTTP {r.status_code} | {r.text[:300]}")
    # return inserted row
    js = r.json()
    if isinstance(js, list) and js:
        return js[0]
    if isinstance(js, dict):
        return js
    return {"ok": True}

# ---- FastAPI Router ----
from fastapi import APIRouter, FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

pro_router = APIRouter(prefix="/pro", tags=["pro"])

@pro_router.get("/checkout")
def start_checkout(request: Request, plan: str, username: str = "user") -> JSONResponse:
    """
    1) Reads user from Authorization Bearer JWT.
    2) Writes a pending row into public.payments with user_email + user_id.
    3) Returns the inserted row JSON.
    """
    email = _current_email_from_auth(request.headers.get("authorization") or request.headers.get("Authorization"))
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        row = create_pending_payment(user_email=email, plan_key=plan, username_hint=username)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create payment row: {type(e).__name__}: {e}")
    return JSONResponse(row)

# Optional: tiny app for standalone testing
app = FastAPI(title="Arbexa Payments Writer")
app.include_router(pro_router)
