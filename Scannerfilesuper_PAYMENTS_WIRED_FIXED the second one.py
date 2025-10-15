# app.py — Arbexa Profit Finder (Spot Arbitrage Scanner) + Minimal Auth (Gmail+Password)
# ----------------------------------------------------------------------------------------------------------------
# WHAT’S FIXED
# - Single FastAPI app instance (no duplicate `app = FastAPI()` resets).
# - /me returns proper profile data; /opps profile card loads it (and caches to localStorage).
# - Everything else kept the same spirit.
# - NEW: Signup requires a unique recovery sentence; /auth/reset implements password reset via recovery sentence.
# - NEW: Login page now toggles Login / Sign up / Reset views (no signup fields visible by default).
# ----------------------------------------------------------------------------------------------------------------
from __future__ import annotations   # must be first
from uuid import uuid4
import datetime

from dotenv import load_dotenv
load_dotenv()                        # only once
import requests
import json


import math, time, threading, random, os, datetime, re, unicodedata, hashlib
from datetime import datetime as dt
from typing import Dict, List, Any, Optional, Tuple

import ccxt
from fastapi import FastAPI, HTTPException, Depends, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse



# ===== Supabase (PostgREST) minimal helper =====
import json as _json
from collections import deque as _deque

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SUPABASE_SERVICE_KEY", ""))

_sb_queue = _deque(maxlen=2000)   # simple in-memory queue
_sb_last_post_err = None

def _sb_post_rows(table: str, rows: list):
    """Insert rows via Supabase PostgREST. Uses service role key so it bypasses RLS.
    Safe no-op if env vars are missing.
    """
    global _sb_last_post_err
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not rows:
        return False
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        hdrs = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        r = requests.post(url, headers=hdrs, data=_json.dumps(rows), timeout=10)
        if r.status_code >= 300:
            _sb_last_post_err = f"SB insert {table} HTTP {r.status_code}: {r.text[:200]}"
            print("[supabase]", _sb_last_post_err)
            return False
        return True
    except Exception as e:
        _sb_last_post_err = f"SB insert {table} error: {type(e).__name__}: {e}"
        print("[supabase]", _sb_last_post_err)
        return False

def _sb_enqueue(table: str, row: dict):
    # keep tiny & safe
    try:
        _sb_queue.append((table, row))
    except Exception as _e:
        print("[supabase] enqueue error:", _e)


def _sb_upsert(table: str, rows: list, on_conflict: str | None = None):
    """Upsert rows via PostgREST using resolution=merge-duplicates.
    If on_conflict is provided, adds ?on_conflict=col to the endpoint.
    """
    global _sb_last_post_err
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not rows:
        return False
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        if on_conflict:
            url += f"?on_conflict={on_conflict}"
        hdrs = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"
        }
        r = requests.post(url, headers=hdrs, data=_json.dumps(rows), timeout=10)
        if r.status_code >= 300:
            _sb_last_post_err = f"SB upsert {table} HTTP {r.status_code}: {r.text[:200]}"
            print("[supabase]", _sb_last_post_err)
            return False
        return True
    except Exception as e:
        _sb_last_post_err = f"SB upsert {table} error: {type(e).__name__}: {e}"
        print("[supabase]", _sb_last_post_err)
        return False


def _sb_flush(max_batch=100):
    if not _sb_queue:
        return
    # batch by table
    batches = {}
    try:
        while _sb_queue and max_batch>0:
            tbl, row = _sb_queue.popleft()
            batches.setdefault(tbl, []).append(row)
            max_batch -= 1
    except Exception as _:
        pass
    for tbl, rows in batches.items():
        _sb_post_rows(tbl, rows)


# --- tiny helpers for mirroring into Supabase ---
def _sb_enqueue_and_post(table: str, row: dict):
    """Enqueue a single row AND try to post immediately.
    Keeps your current queue semantics, but also best-effort posts right now.
    Safe no-op if Supabase env is missing.
    """
    try:
        _sb_enqueue(table, row)
        # fire-and-forget best-effort direct post (single-row list)
        try:
            _sb_post_rows(table, [row])
        except Exception as e:
            # swallow; periodic flusher will retry
            print("[supabase] enqueue_and_post error:", e)
    except Exception as e:
        print("[supabase] enqueue_and_post failed:", e)

def _sb_get_profile_id(email: str) -> str | None:
    """Resolve Supabase PROFILES.id (UUID) for a given email.
    Tries both quoted-uppercase and lowercase table names to match your DB.
    Returns UUID string or None.
    """
    try:
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not email:
            return None
        base = SUPABASE_URL.rstrip("/") + "/rest/v1/"
        hdrs = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Accept": "application/json",
        }
        for tbl in ("PROFILES", "profiles"):
            url = f"{base}{tbl}?select=id&email=eq.{email}&limit=1"
            try:
                r = requests.get(url, headers=hdrs, timeout=8)
            except Exception:
                continue
            if r.status_code == 200:
                arr = r.json() if r.headers.get("content-type","").startswith("application/json") else []
                if isinstance(arr, list) and arr:
                    rid = arr[0].get("id") or arr[0].get("ID") or arr[0].get("Id")
                    if rid:
                        return str(rid)
            # if 404 for this table name, try the other name
        return None
    except Exception as e:
        print("[supabase] get_profile_id error:", e)
        return None


# ---------- SETTINGS helpers ----------
def _sb_get_or_create_profile_id(email: str, username: str):
    """
    Ensure a PROFILES row exists for the given email, and return its id.
    """
    try:
        pid = _sb_get_profile_id(email=email)
        if pid:
            return pid
    except Exception as _e:
        print("[supabase] lookup profile error:", _e)

    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return None

    try:
        url = SUPABASE_URL.rstrip("/") + "/rest/v1/PROFILES"
        hdrs = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Prefer": "return=representation",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = {
            "email": email,
            "username": username or (email.split("@")[0] if email else "user"),
            "status": "FREE",
            "role": "user",
            "meta": {}
        }
        r = requests.post(url, headers=hdrs, json=data, timeout=10)
        if r.status_code in (200, 201):
            try:
                js = r.json()
            except Exception:
                js = None
            if isinstance(js, list) and js and isinstance(js[0], dict) and "id" in js[0]:
                return js[0]["id"]
            if isinstance(js, dict) and "id" in js:
                return js["id"]
        else:
            print("[supabase] create profile failed:", r.status_code, r.text[:200])
    except Exception as _e:
        print("[supabase] create profile error:", _e)
    return None

# ====== Minimal Auth (DB+JWT) ======
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
import jwt
from sqlalchemy import Column, Integer, String, DateTime, Boolean, create_engine, text, func
from sqlalchemy import select

from sqlalchemy.orm import sessionmaker, declarative_base, Session

# ---------- AUTH SETTINGS ----------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./arbexa_users.db")  # local file db (no setup needed)
JWT_SECRET   = os.getenv("JWT_SECRET", "change_me_super_secret")         # change later
JWT_EXPIRE_MINUTES = 24 * 60
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------- AUTH DB ----------
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, echo=False, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    is_active = Column(Boolean, default=True)
    # username (lowercase letters + digits only)
    username = Column(String(64), unique=True, index=True, nullable=True)
    # public sequential user id (10000+)
    public_id = Column(Integer, unique=True, index=True, nullable=True)
    # recovery sentence fingerprint (sha256 hex, unique)
    recovery_sha = Column(String(64), unique=True, index=True, nullable=True)

Base.metadata.create_all(bind=engine)

# Lightweight migrations for SQLite
def _ensure_username_column():
    try:
        if not DATABASE_URL.startswith("sqlite"):
            return
        with engine.begin() as conn:
            cols = conn.execute(text("PRAGMA table_info('users')")).fetchall()
            have = any((c[1] == "username") for c in cols)
            if not have:
                conn.execute(text("ALTER TABLE users ADD COLUMN username VARCHAR(64)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_unique ON users(username)"))
    except Exception as e:
        print(f"[migrate] username column check/add failed: {e}")

def _ensure_public_id_column():
    try:
        if not DATABASE_URL.startswith("sqlite"):
            return
        with engine.begin() as conn:
            cols = conn.execute(text("PRAGMA table_info('users')")).fetchall()
            have = any((c[1] == "public_id") for c in cols)
            if not have:
                conn.execute(text("ALTER TABLE users ADD COLUMN public_id INTEGER"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_public_id_unique ON users(public_id)"))
    except Exception as e:
        print(f"[migrate] public_id column check/add failed: {e}")

def _ensure_recovery_columns():
    try:
        if not DATABASE_URL.startswith("sqlite"):
            return
        with engine.begin() as conn:
            cols = conn.execute(text("PRAGMA table_info('users')")).fetchall()
            have = any((c[1] == "recovery_sha") for c in cols)
            if not have:
                conn.execute(text("ALTER TABLE users ADD COLUMN recovery_sha VARCHAR(64)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_recovery_sha_unique ON users(recovery_sha)"))
    except Exception as e:
        print(f"[migrate] recovery column check/add failed: {e}")

_ensure_username_column()
_ensure_public_id_column()
_ensure_recovery_columns()


# ---------- CHAT MESSAGES DB ----------
class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    username = Column(String(64), nullable=False)
    text = Column(String(2000), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

# Ensure messages table exists
Base.metadata.create_all(bind=engine)



def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def hash_pw(p: str) -> str:
    return pwd_ctx.hash(p)

def verify_pw(p: str, h: str) -> bool:
    return pwd_ctx.verify(p, h)

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def make_token(user_id: int, email: str) -> str:
    now = int(time.time())
    payload = {"sub": str(user_id), "email": email, "iat": now, "exp": now + JWT_EXPIRE_MINUTES * 60}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])

def current_user_from_auth_header(req: Request, db: Session) -> Optional[User]:
    auth = req.headers.get("authorization") or req.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    try:
        data = decode_token(token)
    except Exception:
        return None
    uid = int(data.get("sub") or 0)
    if uid <= 0:
        return None
    return db.query(User).filter(User.id == uid, User.is_active == True).first()

# ---------- AUTH API ----------
class SignupIn(BaseModel):
    email: EmailStr
    password: str
    username: str               # lowercase letters + digits only
    recovery: str               # ≥20 chars, lowercase letters & digits & spaces allowed

class ResetIn(BaseModel):
    email: EmailStr
    recovery: str
    new_password: str

# ✨ NEW: change-password payload
class ChangePwdIn(BaseModel):
    current_password: str
    new_password: str
    confirm_new_password: str


# ---------- CHAT API MODELS ----------
class ChatIn(BaseModel):
    text: str


from fastapi import APIRouter
auth_router = APIRouter(prefix="/auth", tags=["auth"])

@auth_router.post("/signup")

def signup(data: SignupIn, db: Session = Depends(get_db)):
    email = (data.email or "").lower().strip()
    username = unicodedata.normalize("NFKC", (data.username or "").strip().lower())
    recovery = unicodedata.normalize("NFKC", (data.recovery or "").strip().lower())

    if not email.endswith("@gmail.com"):
        raise HTTPException(status_code=400, detail="Only GMAIL allowed for now.")
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="Password too short (min 6).")
    if not re.fullmatch(r"[a-z0-9]{3,32}", username or ""):
        raise HTTPException(status_code=400, detail="Username must be 3–32 chars, lowercase letters and digits only.")
    if not re.fullmatch(r"[a-z0-9 ]{20,}", recovery or ""):
        raise HTTPException(status_code=400, detail="Recovery sentence must be ≥20 chars, lowercase letters, digits and spaces only.")

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already registered.")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already taken.")

    rec_sha = sha256_hex(recovery)
    if db.query(User).filter(User.recovery_sha == rec_sha).first():
        raise HTTPException(status_code=400, detail="Recovery sentence already in use. Choose another one.")

    # compute next public_id (10000, 10001, ...)
    next_pid = (db.query(func.max(User.public_id)).scalar() or 9999) + 1

    u = User(email=email, username=username, password_hash=hash_pw(data.password),
             public_id=next_pid, recovery_sha=rec_sha)
    try:
        db.add(u); db.commit(); db.refresh(u)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="Signup failed. Username or recovery sentence may already be taken.")

    # --- enqueue PROFILES to Supabase (no FK link) ---
    try:
        _sb_enqueue("PROFILES", {
            "email": u.email,
            "username": u.username,
            "status": "FREE",
            "role": "user",
            "meta": {"source": "local-signup", "local_user_id": u.id}
        })
    except Exception as _e:
        print("[supabase] enqueue profile error:", _e)

    tok = make_token(u.id, u.email)
    return {"ok": True, "user_id": u.id, "email": u.email, "username": u.username, "access_token": tok, "token_type": "bearer"}


from fastapi.security import OAuth2PasswordRequestForm
@auth_router.post("/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    email = (form.username or "").strip().lower()
    if not email.endswith("@gmail.com"):
        raise HTTPException(status_code=400, detail="Only GMAIL allowed for now.")
    u = db.query(User).filter(User.email == email).first()
    if not u or not verify_pw(form.password, u.password_hash):
        raise HTTPException(status_code=400, detail="Invalid email or password.")
    tok = make_token(u.id, u.email)
    return {"access_token": tok, "token_type": "bearer"}

@auth_router.post("/logout")
def logout():
    return {"ok": True}

@auth_router.post("/reset")
def reset_password(data: ResetIn, db: Session = Depends(get_db)):
    email = (data.email or "").strip().lower()
    recovery = unicodedata.normalize("NFKC", (data.recovery or "").strip().lower())
    if not email.endswith("@gmail.com"):
        raise HTTPException(status_code=400, detail="Only GMAIL allowed for now.")
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password too short (min 6).")
    if not re.fullmatch(r"[a-z0-9 ]{20,}", recovery or ""):
        raise HTTPException(status_code=400, detail="Recovery sentence format invalid.")
    u = db.query(User).filter(User.email == email).first()
    if not u or not u.recovery_sha:
        raise HTTPException(status_code=400, detail="Account not found or recovery not set.")
    if sha256_hex(recovery) != u.recovery_sha:
        raise HTTPException(status_code=400, detail="Recovery sentence incorrect.")
    u.password_hash = hash_pw(data.new_password)
    db.add(u); db.commit()
    return {"ok": True}

# ✨ NEW: change password (AUTH REQUIRED)
@auth_router.post("/change-password")
def change_password(data: ChangePwdIn, request: Request, db: Session = Depends(get_db)):
    u = current_user_from_auth_header(request, db)
    if not u:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if len(data.new_password or "") < 6:
        raise HTTPException(status_code=400, detail="New password too short (min 6).")
    if (data.new_password or "") != (data.confirm_new_password or ""):
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    if not verify_pw(data.current_password or "", u.password_hash):
        raise HTTPException(status_code=400, detail="Current password incorrect.")
    u.password_hash = hash_pw(data.new_password)
    db.add(u); db.commit()
    return {"ok": True}
# ===== FastAPI app (SINGLE INSTANCE) =====

# --- Supabase flush background ---
def _supabase_flush_loop():
    # runs every ~5 seconds
    while True:
        try:
            _sb_flush(200)  # up to 200 rows per tick
        except Exception as _:
            pass
        time.sleep(5)



app = FastAPI()
@app.on_event("startup")
def _sb_force_start():
    try:
        import threading as _th
        _th.Thread(target=_supabase_flush_loop, daemon=True).start()
        print("[supabase] flush worker started (startup hook)")
    except Exception as _e:
        print("[supabase] failed to start flush worker:", _e)

app.include_router(auth_router)
# ---- current user profile (for Profile ▾ card) ----
@app.get("/me", response_class=JSONResponse)
def me(request: Request, db: Session = Depends(get_db)):
    u = current_user_from_auth_header(request, db)
    if not u:
        raise HTTPException(status_code=401, detail="Not authenticated")

    public_id = u.public_id or (10000 + (u.id or 0))
    joined_iso = (u.created_at or dt.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")

    uname = (u.username or "").strip()
    if not uname:
        base = (u.email or "").split("@", 1)[0].lower()
        base = re.sub(r"[^a-z0-9]", "", base) or "user"
        uname = base[:32]

    return JSONResponse({
        "email": u.email,
        "username": uname,
        "date_joined": joined_iso,
        "status": "free",
        "user_id": public_id,
    })

# ====== SCANNER ======
EXCHANGE_IDS = [
    "binance", "bybit", "mexc", "gateio", "bitget", "bitmart", "cryptocom",
    "bingx", "coinex", "whitebit", "ascendex", "bitrue", "lbank",
    "xt", "bitstamp", "hitbtc",
]
QUOTE = "USDT"
EDGE_MIN = 1.0
EDGE_MAX = 26.0
ORDERBOOK_LEVELS = 15
SLIPPAGE_BAND = 0.002
DEPTH_BAND_UI = 0.003
VOLUME_CAP_FRACTION = 0.0005
SCAN_INTERVAL = 10
MAX_OPPS = 1000
TIMEOUT_MS = 15000
RETRIES = 5

OPP_REFRESH_TTL = 20
OPP_REFRESH_LIMIT = 20

EX_LOGO_DOMAIN: Dict[str, str] = {
    "binance": "binance.com", "bybit": "bybit.com", "mexc": "mexc.com", "gateio": "gate.io",
    "bitget": "bitget.com", "bitmart": "bitmart.com", "cryptocom": "crypto.com", "bingx": "bingx.com",
    "coinex": "coinex.com", "whitebit": "whitebit.com", "ascendex": "ascendex.com", "bitrue": "bitrue.com",
    "lbank": "lbank.com", "xt": "xt.com", "bitstamp": "bitstamp.net", "hitbtc": "hitbtc.com",
}
EX_LOGO_URL = {
    "coinex": "/static/logo-coinex.svg",
    "gateio": "https://www.gate.io/favicon.ico",
}

# ------------- STATE -------------
lock = threading.Lock()
cycle_no = 0
last_cycle_summary: Dict[str, Any] = {"passed": 0, "failed": 0, "failed_names": []}
ex_states: Dict[str, Dict[str, Any]] = {}
snapshots: Dict[str, Dict[str, Dict[str, float]]] = {}
opps_cache: Dict[str, Dict[str, Any]] = {}
running = True

# ------------- UTILS -------------
def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None: return None
        return float(x)
    except Exception:
        return None

def compute_quote_volume_usd(t: Dict[str, Any]) -> float:
    qv = safe_float(t.get("quoteVolume"))
    if qv and qv > 0: return qv
    base_v = safe_float(t.get("baseVolume"))
    last = safe_float(t.get("last"))
    if base_v and last: return base_v * last
    info = t.get("info", {}) or {}
    for k in ("qv","quoteVolume","quote_volume","volValue","volUsd","quoteVolume24h","vol24hQuote"):
        v = safe_float(info.get(k))
        if v and v > 0: return v
    return 0.0
def within_slippage_capacity(levels: List[List[float]], band_ratio: float, is_ask: bool) -> float:
    if not levels: return 0.0
    best = levels[0][0]
    if not best or best <= 0: return 0.0
    total = 0.0
    if is_ask:
        limit = best * (1.0 + band_ratio)
        for price, amount in levels:
            if price and amount and price <= limit: total += price * amount
            else: break
    else:
        limit = best * (1.0 - band_ratio)
        for price, amount in levels:
            if price and amount and price >= limit: total += price * amount
            else: break
    return total

def suggested_trade_range_usd(qv_buy: float,
                              qv_sell: float,
                              ob_asks: List[List[float]],
                              ob_bids: List[List[float]]) -> Tuple[float, float]:
    cap_buy_slip  = within_slippage_capacity(ob_asks or [], SLIPPAGE_BAND, True)
    cap_sell_slip = within_slippage_capacity(ob_bids or [], SLIPPAGE_BAND, False)
    cap_vol = min(qv_buy or 0.0, qv_sell or 0.0) * VOLUME_CAP_FRACTION
    max_cap = max(0.0, min(cap_buy_slip, cap_sell_slip, cap_vol))
    if max_cap <= 0: return (0.0, 0.0)
    hi = max(0.0, max_cap * 0.9)
    lo = max(50.0, hi * 0.1)
    if lo > hi: lo = hi
    return (float(int(lo)), float(int(hi)))

def liquidity_score_v2(qv_buy: float,
                       qv_sell: float,
                       ob_asks: List[List[float]],
                       ob_bids: List[List[float]]) -> int:
    def volume_bucket_score(v_usd: float) -> int:
        if v_usd < 50_000:         return 40
        if v_usd < 100_000:        return 50
        if v_usd < 300_000:        return 65
        if v_usd < 1_000_000:      return 75
        if v_usd < 3_000_000:      return 80
        if v_usd < 10_000_000:     return 85
        if v_usd < 30_000_000:     return 90
        if v_usd < 100_000_000:    return 95
        return 100

    base = volume_bucket_score(
        (2.0 / (1.0/(qv_buy or 1e-9) + 1.0/(qv_sell or 1e-9))) if (qv_buy and qv_sell) else (qv_buy or qv_sell or 0.0)
    )
    cap_ask = within_slippage_capacity(ob_asks or [], DEPTH_BAND_UI, True)
    cap_bid = within_slippage_capacity(ob_bids or [], DEPTH_BAND_UI, False)
    depth_usd = min(cap_ask, cap_bid)

    def depth_factor(u: float) -> float:
        if u < 2_000:       return 0.20
        if u < 5_000:       return 0.35
        if u < 10_000:      return 0.50
        if u < 25_000:      return 0.60
        if u < 50_000:      return 0.70
        if u < 100_000:     return 0.80
        if u < 200_000:     return 0.90
        return 1.00

    score = int(round(base * depth_factor(depth_usd)))
    return max(0, min(100, score))

def best_bid_ask_for_symbol(symbol: str) -> Optional[Tuple[str, float, str, float, float]]:
    best_ask_ex, best_ask = None, float("inf")
    best_bid_ex, best_bid = None, 0.0
    last_prices = []
    for ex, m in snapshots.items():
        row = m.get(symbol)
        if not row: continue
        a = row.get("ask"); b = row.get("bid"); l = row.get("last")
        if l: last_prices.append(l)
        if a and a > 0 and a < best_ask:
            best_ask_ex, best_ask = ex, a
        if b and b > 0 and b > best_bid:
            best_bid_ex, best_bid = ex, b
    if best_ask_ex and best_bid_ex:
        mid = (sum(last_prices)/len(last_prices)) if last_prices else (best_bid + best_ask)/2.0
        return best_ask_ex, best_ask, best_bid_ex, best_bid, mid
    return None

def edge_pct(buy_price: float, sell_price: float) -> float:
    if not buy_price or buy_price <= 0: return 0.0
    return (sell_price - buy_price) / buy_price * 100.0

def build_exchange(ex_id: str):
    try:
        if ex_id not in ccxt.exchanges: return None
        klass = getattr(ccxt, ex_id)
        return klass({"enableRateLimit": True, "timeout": TIMEOUT_MS, "options": {"defaultType": "spot"}})
    except Exception:
        return None
# ------------- BACKGROUND SCAN -------------
def init_states():
    for ex_id in EXCHANGE_IDS:
        ex = build_exchange(ex_id)
        ex_states[ex_id] = {"ex": ex, "retries": 0, "backoff_until": 0.0, "symbols": [], "ok": False}
        snapshots[ex_id] = {}

def load_symbols_for_exchange(ex_id: str):
    st = ex_states[ex_id]; ex = st["ex"]
    if not ex: st["ok"] = False; return
    try:
        ex.load_markets(reload=False)
        st["symbols"] = [sym for sym, m in ex.markets.items() if m.get("spot") and sym.endswith("/" + QUOTE)]
        st["ok"] = True
    except Exception as e:
        st["ok"] = False; st["retries"] += 1
        if st["retries"] >= RETRIES: st["backoff_until"] = time.time() + 60
        print(f"[load_symbols] {ex_id} ERROR: {e}")

def fetch_tickers_for_exchange(ex_id: str) -> bool:
    st = ex_states[ex_id]; ex = st["ex"]
    if not ex: return False
    if time.time() < st["backoff_until"]: return False
    try:
        try:
            tickers = ex.fetch_tickers()
        except Exception:
            tickers = {}
            probe = random.sample(st["symbols"], min(len(st["symbols"]), 200)) if st["symbols"] else []
            for sym in probe:
                try: tickers[sym] = ex.fetch_ticker(sym)
                except Exception: continue
        data = {}
        for sym, t in tickers.items():
            if not sym.endswith("/" + QUOTE): continue
            bid = safe_float(t.get("bid")); ask = safe_float(t.get("ask")); last = safe_float(t.get("last"))
            qv = compute_quote_volume_usd(t)
            if bid or ask or last or qv:
                data[sym] = {"bid": bid, "ask": ask, "last": last, "qvol": qv}
        snapshots[ex_id] = data
        st["ok"] = True; st["retries"] = 0
        return True
    except Exception as e:
        st["ok"] = False; st["retries"] += 1
        if st["retries"] >= RETRIES: st["backoff_until"] = time.time() + 60
        print(f"[tickers] {ex_id} ERROR: {e}")
        return False

def fetch_orderbook(ex_id: str, symbol: str) -> Optional[Dict[str, Any]]:
    st = ex_states[ex_id]; ex = st["ex"]
    if not ex or time.time() < st["backoff_until"]: return None
    try:
        ob = ex.fetch_order_book(symbol, limit=ORDERBOOK_LEVELS)
        bids = ob.get("bids") or []; asks = ob.get("asks") or []
        bids = [[safe_float(p), safe_float(a)] for p, a in bids[:ORDERBOOK_LEVELS]]
        asks = [[safe_float(p), safe_float(a)] for p, a in asks[:ORDERBOOK_LEVELS]]
        return {"bids": bids, "asks": asks}
    except Exception as e:
        st["retries"] += 1
        if st["retries"] >= RETRIES: st["backoff_until"] = time.time() + 60
        print(f"[orderbook] {ex_id} {symbol} ERROR: {e}")
        return None

def fetch_one_ticker(ex_id: str, symbol: str) -> Dict[str, Optional[float]]:
    st = ex_states.get(ex_id)
    ex = st["ex"] if st else None
    out = {"bid": None, "ask": None, "last": None, "qvol": None}
    if not ex or time.time() < (st.get("backoff_until") or 0): return out
    try:
        t = ex.fetch_ticker(symbol)
        out["bid"] = safe_float(t.get("bid"))
        out["ask"] = safe_float(t.get("ask"))
        out["last"] = safe_float(t.get("last"))
        out["qvol"] = compute_quote_volume_usd(t)
        m = snapshots.setdefault(ex_id, {})
        m[symbol] = {"bid": out["bid"], "ask": out["ask"], "last": out["last"], "qvol": out["qvol"] or 0.0}
    except Exception as e:
        print(f"[ticker-refresh] {ex_id} {symbol} ERROR: {e}")
    return out

def try_build_opps_incremental():
    counts: Dict[str, int] = {}
    for ex, m in snapshots.items():
        for sym in m.keys():
            counts[sym] = counts.get(sym, 0) + 1
    candidates = [s for s, n in counts.items() if n >= 2]
    random.shuffle(candidates)
    for sym in candidates:
        res = best_bid_ask_for_symbol(sym)
        if not res: continue
        ex_buy, ask, ex_sell, bid, mid = res
        if not ex_buy or not ex_sell or ex_buy == ex_sell: continue
        e = edge_pct(ask, bid)
        if e < EDGE_MIN - 1e-8 or e > EDGE_MAX + 1e-8: continue
        ob_buy = fetch_orderbook(ex_buy, sym)
        ob_sell = fetch_orderbook(ex_sell, sym)
        if not ob_buy or not ob_sell: continue
        qv_buy = snapshots[ex_buy].get(sym, {}).get("qvol", 0.0)
        qv_sell = snapshots[ex_sell].get(sym, {}).get("qvol", 0.0)
        ls = liquidity_score_v2(qv_buy, qv_sell, ob_buy["asks"], ob_sell["bids"])
        lo, hi = suggested_trade_range_usd(qv_buy, qv_sell, ob_buy["asks"], ob_sell["bids"])
        row = {
            "symbol": sym, "price": mid, "buy_ex": ex_buy, "sell_ex": ex_sell,
            "buy_ask": ask, "sell_bid": bid, "edge": e, "qv_buy": qv_buy, "qv_sell": qv_sell,
            "liquidity": ls, "sugg_lo": lo, "sugg_hi": hi,
            "example_profit_1000": max(0.0, 1000.0 * e / 100.0),
            "ob_buy_asks": ob_buy["asks"], "ob_buy_bids": ob_buy["bids"],
            "ob_sell_asks": ob_sell["asks"], "ob_sell_bids": ob_sell["bids"],
            "ts": time.time(),
        }
        with lock:
            prev = opps_cache.get(sym)
            if (not prev) or row["edge"] > prev["edge"]:
                opps_cache[sym] = row
                # === enqueue to Supabase OPPORTUNITIES ===
                try:
                    _base, _quote = (sym.split('/') + [None])[:2]
                    _row_db = {
                        "pair": sym,
                        "base": _base,
                        "quote": _quote,
                        "exchange_buy": ex_buy,
                        "exchange_sell": ex_sell,
                        "price_buy": ask,
                        "price_sell": bid,
                        "spread_pct": e,
                        "volume_24h_usd": float(min(qv_buy or 0.0, qv_sell or 0.0)),
                        "liquidity": ls,
                        "est_profit_usd": float(max(0.0, 1000.0 * e / 100.0)),
                        "raw": row  # full blob for later analysis
                        # user_id: left NULL (server-side analytics/global)
                    }
                    _sb_enqueue("opportunities", _row_db)
                
                    _ok = _sb_post_rows("opportunities", [_row_db])  # direct write
                    print("[supabase] direct insert", sym, "->", _ok)
                except Exception as _e:
                    print("[supabase] enqueue opp error:", _e)

            if len(opps_cache) > MAX_OPPS:

                    top = sorted(opps_cache.values(), key=lambda r: r["edge"], reverse=True)[:MAX_OPPS]
                    opps_cache.clear()
                    opps_cache.update({r["symbol"]: r for r in top})

def refresh_existing_opps():
    now = time.time()
    with lock:
        symbols = list(opps_cache.keys())
    refreshed = 0
    for sym in symbols:
        with lock:
            r = opps_cache.get(sym)
        if not r: continue
        if now - r.get("ts", 0) < OPP_REFRESH_TTL:
            continue
        buy_ex = r["buy_ex"]; sell_ex = r["sell_ex"]

        t_buy  = fetch_one_ticker(buy_ex,  sym)
        t_sell = fetch_one_ticker(sell_ex, sym)

        new_ask = t_buy["ask"]  if t_buy["ask"]  is not None else r["buy_ask"]
        new_bid = t_sell["bid"] if t_sell["bid"] is not None else r["sell_bid"]
        qv_buy  = t_buy["qvol"]  if t_buy["qvol"]  is not None else r["qv_buy"]
        qv_sell = t_sell["qvol"] if t_sell["qvol"] is not None else r["qv_sell"]

        ob_buy  = fetch_orderbook(buy_ex,  sym) or {"asks": r["ob_buy_asks"],  "bids": r["ob_buy_bids"]}
        ob_sell = fetch_orderbook(sell_ex, sym) or {"asks": r["ob_sell_asks"], "bids": r["ob_sell_bids"]}

        mid = (new_bid + new_ask)/2.0 if (new_bid and new_ask) else r["price"]
        e = edge_pct(new_ask or r["buy_ask"], new_bid or r["sell_bid"])
        ls = liquidity_score_v2(qv_buy, qv_sell, ob_buy["asks"], ob_sell["bids"])
        lo, hi = suggested_trade_range_usd(qv_buy, qv_sell, ob_buy["asks"], ob_sell["bids"])

        new_row = {
            **r,
            "price": mid, "buy_ask": new_ask, "sell_bid": new_bid, "edge": e,
            "qv_buy": qv_buy, "qv_sell": qv_sell, "liquidity": ls,
            "sugg_lo": lo, "sugg_hi": hi,
            "example_profit_1000": max(0.0, 1000.0 * e / 100.0),
            "ob_buy_asks": ob_buy["asks"], "ob_buy_bids": ob_buy["bids"],
            "ob_sell_asks": ob_sell["asks"], "ob_sell_bids": ob_sell["bids"],
            "ts": now,
        }
        with lock:
            opps_cache[sym] = new_row
        refreshed += 1
        if refreshed >= OPP_REFRESH_LIMIT:
            break

def update_opportunities_stream():
    global cycle_no, last_cycle_summary
    while running:
        try:
            start = time.time()
            cycle_no += 1
            passed, failed = 0, 0
            failed_names: List[str] = []

            print(f"\n=== 🔁 Cycle #{cycle_no} — {dt.utcnow().isoformat()}Z ===")
            for ex_id in EXCHANGE_IDS:
                st = ex_states.get(ex_id)
                if st and st["symbols"]:
                    continue
                print(f"   ⏳ Loading symbols for {ex_id} ...", end="", flush=True)
                load_symbols_for_exchange(ex_id)
                print(" OK" if ex_states[ex_id]["ok"] else " FAIL")

            for ex_id in EXCHANGE_IDS:
                st = ex_states[ex_id]
                if time.time() < st["backoff_until"]:
                    print(f"   ⏭️  {ex_id} in backoff, skipping.")
                    failed += 1; failed_names.append(ex_id); continue
                print(f"   📡 Fetching tickers: {ex_id} ...", end="", flush=True)
                ok = fetch_tickers_for_exchange(ex_id)
                if ok: passed += 1; print(" OK")
                else: failed += 1; failed_names.append(ex_id); print(" FAIL")
                try_build_opps_incremental()

            try_build_opps_incremental()
            refresh_existing_opps()

            dur = time.time() - start
            last_cycle_summary = {"passed": passed, "failed": failed, "failed_names": failed_names}
            print(f"=== ✅ Cycle #{cycle_no} done in {dur:.1f}s | Passed: {passed} | Failed: {failed} | FailedEx: {failed_names} ===")
            time.sleep(max(1.0, SCAN_INTERVAL - (time.time() - start)))
        except Exception as e:
            print(f"[scanner-loop] CRASH GUARD: {type(e).__name__}: {e}", flush=True)
            time.sleep(1.0)

@app.on_event("startup")
def _startup():
    init_states()
    t = threading.Thread(target=update_opportunities_stream, daemon=True)
    t.start()
    # start supabase flush worker
    threading.Thread(target=_supabase_flush_loop, daemon=True).start()
# ------------- HTTP -------------
# LOGIN PAGE (front door) — starts in LOGIN mode; toggle to SIGNUP or RESET

PRO_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arbexa — Go Pro</title>
<style>
:root{--bg:#0b1220;--card:#101a33;--txt:#e7eefc;--muted:#9bb0d6;--acc:#2bd576;--line:#23345f}
*{box-sizing:border-box} html,body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu}
a{color:var(--acc);text-decoration:none}
.wrap{min-height:100svh;display:grid;grid-template-rows:auto 1fr}
.header{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;
  padding:10px 12px;border-bottom:1px solid #132042;background:linear-gradient(180deg,#0b1220 85%,transparent)}
.brand{display:flex;align-items:center;gap:10px}
.brand img{height:32px}
.back{display:inline-flex;align-items:center;gap:8px;height:36px;padding:0 12px;border-radius:10px;border:1px solid #23345f;
  background:#0e1a35;color:#e7eefc;font-weight:900;cursor:pointer}
.back:hover{filter:brightness(1.06)}
.main{max-width:900px;margin:0 auto;padding:16px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}
.h1{font-size:22px;margin:0 0 6px;font-weight:900;letter-spacing:.4px}
.lead{color:var(--muted);font-weight:600}
.perks{display:grid;gap:8px;margin-top:6px}
.perks li{margin-left:22px}
.plans{display:grid;gap:12px}
.plan{display:grid;gap:6px;background:#0f1730;border:1px solid #23345f;border-radius:14px;padding:12px}
.gopro{display:inline-flex;align-items:center;justify-content:center;height:36px;padding:0 14px;border-radius:10px;
  border:1px solid #846200;background:linear-gradient(135deg,#ffe089,#efb800);color:#2b1e00;font-weight:900;letter-spacing:.2px;cursor:pointer}
.gopro:hover{filter:brightness(1.05)}
.info{font-size:14px;color:#cfe0ff}
.mono{font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace}
</style>
</head><body>
<div class="wrap">
  <div class="header">
    <div class="brand">
      <img src="/brandlogo" alt="Arbexa">
      <div style="font-weight:900;letter-spacing:.6px">Go Pro</div>
    </div>
    <a class="back" href="/opps" aria-label="Back to opportunities">Back →</a>
  </div>

  <div class="main">
    <div class="card">
      <div class="h1">Information</div>
      <div class="info">
        After you pay, Pro activates automatically. Most payments confirm in 30-90 seconds. During busy network periods it can take up to 2-5 minutes (rarely longer). You don't need to do anything- just wait and then refresh your dashboard.
      </div>
    </div>

    <div class="card">
      <div class="h1">Perks</div>
      <ul class="perks">
        <li>opportunities more than 2% profit</li>
        <li>Prioritized Refresh</li>
        <li>Neat Orderbooks with market quantity displayed in dollars($),so you don’t stress yourself calculating!</li>
        <li>Trade size suggestion for smoother trades</li>
        <li>Liquidity Score For smooth <span class="mono">trade grading</span></li>
        <li>Advanced filtering</li>
        <li>Special Guide Straight to your registered email</li>
        <li>Chat access to connect with other pro users</li>
        <li>Bluetick on your username</li>
      </ul>
    </div>

    <div class="card">
      <div class="h1">Plans</div>
      <div class="plans">
        <div class="plan">
          <div><b>Weekly</b></div>
          <div>Duration: 1 week</div>
          <div>Amount: <b>$10</b></div>
          <a class="gopro" href="/pro/checkout?plan=weekly" >GO PRO👑</a>
        </div>
        <div class="plan">
          <div><b>Monthly</b></div>
          <div>Duration: 1 month</div>
          <div>Amount: <b>$20</b></div>
          <a class="gopro" href="/pro/checkout?plan=monthly" >GO PRO👑</a>
        </div>
        <div class="plan">
          <div><b>3 Months</b></div>
          <div>Duration: 3 months</div>
          <div>Amount: <b>$55</b></div>
          <a class="gopro" href="/pro/checkout?plan=3m" >GO PRO👑</a>
        </div>
        <div class="plan">
          <div><b>6 Months</b></div>
          <div>Duration: 6 months</div>
          <div>Amount: <b>$100</b></div>
          <a class="gopro" href="/pro/checkout?plan=6m" >GO PRO👑</a>
        </div>
        <div class="plan">
          <div><b>Yearly</b></div>
          <div>Duration: 12 months</div>
          <div>Amount: <b>$180</b></div>
          <a class="gopro" href="/pro/checkout?plan=yearly" >GO PRO👑</a>
        </div>
        <div class="plan">
          <div><b>3 Years (Early Bird Promo)</b></div>
          <div>Duration: 36 months</div>
          <div>Amount: <b>$450</b></div>
          <a class="gopro" href="/pro/checkout?plan=3y" >GO PRO👑</a>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
(function(){
  try {
    var u = localStorage.getItem('arbexa_username') || localStorage.getItem('arbexa_user') || 'anon';
    document.querySelectorAll('a.gopro').forEach(function(a){
      try {
        var url = new URL(a.getAttribute('href'), window.location.origin);
        url.searchParams.set('username', u);
        a.setAttribute('href', url.pathname + url.search);
      } catch(e){}
    });
  } catch(e){}
})();
</script>

</body></html>"""

LOGIN_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arbexa Profit Finder — Login</title>
<style>
:root{--bg:#0b1220;--card:#111a2e;--muted:#7a8aa0;--txt:#e7eefc;--acc:#2bd576;--warn:#ffcf5a}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu}
.wrap{min-height:100svh;display:grid;place-items:center;padding:24px}
.box{width:min(520px,92vw);background:var(--card);border:1px solid #1a2547;border-radius:16px;padding:16px 18px}
.brand{display:flex;gap:12px;align-items:center;justify-content:center;margin-bottom:8px}
.brand img{height:42px}.brand h1{margin:0;font-size:20px;letter-spacing:.8px;text-transform:uppercase}
.msg{color:#cfe0ff;text-align:center;margin-bottom:12px;font-weight:800}
.f{display:grid;gap:10px;margin-top:6px}
.label{font-size:12px;color:#9fb2d9;letter-spacing:1px}
.inp{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #24345d;background:#0e1a35;color:#e7eefc;font-weight:600}
.row{display:flex;gap:10px;align-items:center;justify-content:space-between;margin-top:8px}
.btn{flex:1;height:38px;border-radius:10px;border:1px solid #26345e;background:#0e1a35;color:#e7eefc;font-weight:800;cursor:pointer}
.btn:hover{filter:brightness(1.06)}
.btn.primary{background:var(--acc);border-color:#1b9d5b;color:#04120a}
.link{color:var(--acc);font-weight:800;cursor:pointer;text-decoration:none}
.help{font-size:12px;color:#7a8aa0;text-align:center;margin-top:8px}
.notice{margin-top:10px;font-size:12px;color:#ffcf5a;text-align:center}
.small{font-size:12px;color:#9fb2d9}
.italic{font-style:italic;color:var(--acc)}
.hide{display:none}
.succpop{position:fixed; top:16px; right:16px; z-index:99999; background:#2bd576; color:#ffffff;
  border:1px solid #1b9d5b; border-radius:12px; padding:12px 14px; font-weight:900; letter-spacing:.6px;
  text-transform:uppercase; box-shadow:0 10px 24px rgba(0,0,0,.35)}
.succpop.hide{display:none}
#miniOverlay{position:fixed; inset:0; display:none; align-items:center; justify-content:center; flex-direction:column; gap:12px;
  background:rgba(7,12,22,.75); z-index:99998}
#miniOverlay.show{display:flex}
.logoPulse{width:min(520px,70vw);height:auto;filter:drop-shadow(0 6px 22px rgba(43,213,118,.25));animation:pulse 1.3s ease-in-out infinite}
@keyframes pulse{0%{transform:scale(.96);opacity:.8}50%{transform:scale(1.03);opacity:1}100%{transform:scale(.96);opacity:.8}}
</style>
</head><body>
<div class="wrap">
  <div class="box">
    <div class="brand">
      <img src="/brandlogo" alt="Arbexa">
      <h1>ARBEXA PROFIT FINDER</h1>
    </div>
    <div id="title" class="msg">LOGIN TO CONTINUE</div>

    <!-- LOGIN VIEW -->
    <div id="viewLogin" class="f">
      <div><div class="label">GMAIL</div><input id="emailL" class="inp" placeholder="YOURGMAIL@GMAIL.COM" autocomplete="email"></div>
      <div><div class="label">PASSWORD</div><input id="pwL" class="inp" type="password" placeholder="ENTER PASSWORD" autocomplete="current-password"></div>
      <div class="row"><button id="btnLogin" class="btn primary">LOGIN</button></div>
      <div class="small italic"><a id="linkForgot" class="link">Forgot password?</a></div>
      <div class="help">If you haven’t registered, <a id="linkToSignup" class="link">sign up here</a>.</div>
      <div class="small italic" style="color:#22c55e;margin-top:10px">Having Issues Signing In Or Signing Up? Click <a href="https://t.me/ArbexaProfitFinderSupport" target="_blank" rel="noopener" class="link" style="color:#22c55e;font-weight:700">SUPPORT</a></div>
      <div class="notice">Note: Only GMAIL addresses are allowed for now.</div>
    </div>

    <!-- SIGNUP VIEW -->
    <div id="viewSignup" class="f hide">
      <div><div class="label">GMAIL</div><input id="emailS" class="inp" placeholder="YOURGMAIL@GMAIL.COM" autocomplete="email"></div>
      <div><div class="label">PASSWORD</div><input id="pwS" class="inp" type="password" placeholder="ENTER PASSWORD (min 6)" autocomplete="new-password"></div>
      <div><div class="label">CONFIRM PASSWORD</div><input id="pwS2" class="inp" type="password" placeholder="RE-ENTER PASSWORD" autocomplete="new-password"></div>
      <div><div class="label">USERNAME</div><input id="username" class="inp" placeholder="lowercase & digits only, e.g. arbexa123" autocomplete="username"></div>
      <div><div class="label">RECOVERY SENTENCE</div><input id="recovery" class="inp" placeholder="at least 20 chars; lowercase & digits; spaces allowed"></div>
      <div class="small">This is your <strong>only recovery phrase</strong> to change your password. Keep it safe. Without it, your account cannot be recovered.</div>
      <div class="row"><button id="btnSignup" class="btn primary">SIGN UP</button></div>
      <div class="help">Have an account? <a id="linkToLogin" class="link">log in</a>.</div>
      <div class="small italic" style="color:#22c55e;margin-top:10px">Having Issues Signing In Or Signing Up? Click <a href="https://t.me/ArbexaProfitFinderSupport" target="_blank" rel="noopener" class="link" style="color:#22c55e;font-weight:700">SUPPORT</a></div>
    </div>

    <!-- RESET VIEW -->
    <div id="viewReset" class="f hide">
      <div><div class="label">GMAIL</div><input id="emailR" class="inp" placeholder="YOURGMAIL@GMAIL.COM" autocomplete="email"></div>
      <div><div class="label">RECOVERY SENTENCE</div><input id="recoveryR" class="inp" placeholder="type exactly as you set it (lowercase/digits/spaces)"></div>
      <div><div class="label">NEW PASSWORD</div><input id="pwR" class="inp" type="password" placeholder="NEW PASSWORD (min 6)" autocomplete="new-password"></div>
      <div><div class="label">CONFIRM NEW PASSWORD</div><input id="pwR2" class="inp" type="password" placeholder="RE-ENTER NEW PASSWORD" autocomplete="new-password"></div>
      <div class="row"><button id="btnReset" class="btn primary">RESET PASSWORD</button></div>
      <div class="help"><a id="linkBackLogin" class="link">Back to login</a></div>
    </div>
  </div>
</div>

<div id="miniOverlay" aria-hidden="true">
  <img class="logoPulse" src="/brandlogo" alt="Loading">
</div>
<div id="succpop" class="succpop hide" role="status"></div>

<script>
const SOUND_KEY='arbexa_sound';
function soundEnabled(){ try{ return localStorage.getItem(SOUND_KEY)!=='0'; }catch(_){ return true; } }
let _actx=null;
function _ctx(){ if(!_actx){ _actx=new (window.AudioContext||window.webkitAudioContext)(); } return _actx; }
function _beep(freq=440, dur=120, type='sine', vol=0.08, delayMs=0){
  if(!soundEnabled()) return;
  try{
    const ctx=_ctx(); const t=ctx.currentTime + (delayMs||0)/1000;
    const o=ctx.createOscillator(); const g=ctx.createGain();
    o.type=type; o.frequency.setValueAtTime(freq, t);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.0001 + vol, t+0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur/1000);
    o.connect(g).connect(ctx.destination);
    o.start(t); o.stop(t + dur/1000 + 0.05);
  }catch(_){}
}
function playSfx(kind){
  switch(kind){
    case 'success': _beep(440,120,'sine',0.07,0); _beep(660,140,'sine',0.07,80); break;
    case 'tap': _beep(300,70,'square',0.05,0); break;
  }
}

function $(s){return document.querySelector(s)}
function toast(msg){alert(msg)}
function setToken(t){ try{ if(t) localStorage.setItem('arbexa_token', t); else localStorage.removeItem('arbexa_token'); }catch(_){ } }

function showSuccess(text, ms){
  const el = $('#succpop'); if(!el) return;
  el.textContent = String(text||'SUCCESS');
  el.classList.remove('hide');
  const timer = setTimeout(()=>{ el.classList.add('hide'); }, Math.max(400, ms||1200));
  return ()=>{ try{ clearTimeout(timer); el.classList.add('hide'); }catch(_){} };
}
function showMiniOverlay(show=true){
  const o = $('#miniOverlay'); if(!o) return;
  if(show) o.classList.add('show'); else o.classList.remove('show');
}

async function apiLogin(email, password){
  const form = new URLSearchParams({ username: email, password: password });
  const r = await fetch('/auth/login', { method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body: form });
  const j = await r.json().catch(()=>({}));
  if(!r.ok || !j.access_token) throw new Error(j.detail||('Login failed ('+r.status+')'));
  return j.access_token;
}
async function apiSignup(email, password, username, recovery){
  const r = await fetch('/auth/signup', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({email, password, username, recovery})
  });
  const j = await r.json().catch(()=>({}));
  if(!r.ok || !j.access_token) throw new Error(j.detail||('Signup failed ('+r.status+')'));
  return j.access_token; // auto-login token
}
async function apiReset(email, recovery, newpw){
  const r = await fetch('/auth/reset', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({email, recovery, new_password:newpw})
  });
  const j = await r.json().catch(()=>({}));
  if(!r.ok || !j.ok) throw new Error(j.detail||('Reset failed ('+r.status+')'));
  return true;
}

function goApp(){ location.href = '/opps'; }

function switchMode(mode){
  const t = $('#title');
  $('#viewLogin').classList.add('hide');
  $('#viewSignup').classList.add('hide');
  $('#viewReset').classList.add('hide');
  if(mode==='login'){ $('#viewLogin').classList.remove('hide'); if(t) t.textContent='LOGIN TO CONTINUE'; $('#emailL').focus(); }
  if(mode==='signup'){ $('#viewSignup').classList.remove('hide'); if(t) t.textContent='CREATE YOUR ACCOUNT'; $('#emailS').focus(); }
  if(mode==='reset'){ $('#viewReset').classList.remove('hide'); if(t) t.textContent='RESET PASSWORD'; $('#emailR').focus(); }
  window.scrollTo({top:0,left:0,behavior:'instant'});
}

async function doLogin(){
  const email = $('#emailL').value.trim().toLowerCase();
  const pw = $('#pwL').value;
  if(!email || !pw) return toast('Enter email and password.');
  try{
    showMiniOverlay(true);
    const tok = await apiLogin(email, pw);
    setToken(tok);
    showSuccess('Successful Login', 1200);
    playSfx('success');
    setTimeout(goApp, 1200);
  }catch(e){ toast(e.message||String(e)); }
  finally{ showMiniOverlay(false); }
}

function validUsername(u){ return /^[a-z0-9]{3,32}$/.test(u||''); }
function validRecovery(s){ return /^[a-z0-9 ]{20,}$/.test((s||'').trim()); }

async function doSignup(){
  const email = $('#emailS').value.trim().toLowerCase();
  const pw = $('#pwS').value;
  const pw2 = $('#pwS2').value;
  const username = ($('#username').value||'').trim().toLowerCase();
  const recovery = ($('#recovery').value||'').trim().toLowerCase();
  if(!email || !pw || !pw2 || !username || !recovery) return toast('Complete all fields.');
  if(pw !== pw2) return toast('Passwords do not match.');
  if(pw.length < 6) return toast('Password too short (min 6).');
  if(!validUsername(username)) return toast('Username must be 3–32 chars, lowercase letters and digits only.');
  if(!validRecovery(recovery)) return toast('Recovery sentence must be ≥20 chars; lowercase letters, digits and spaces only.');
  try{
    showMiniOverlay(true);
    const tok = await apiSignup(email, pw, username, recovery);
    setToken(tok);
    showSuccess('Welcome To Arbexa🎉', 5000);
    playSfx('success');
    setTimeout(goApp, 5000);
  }catch(e){ toast(e.message||String(e)); }
  finally{ showMiniOverlay(false); }
}

async function doReset(){
  const email = $('#emailR').value.trim().toLowerCase();
  const recovery = ($('#recoveryR').value||'').trim().toLowerCase();
  const pw = $('#pwR').value, pw2 = $('#pwR2').value;
  if(!email || !recovery || !pw || !pw2) return toast('Complete all fields.');
  if(pw !== pw2) return toast('Passwords do not match.');
  if(!validRecovery(recovery)) return toast('Recovery sentence format invalid.');
  try{
    showMiniOverlay(true);
    await apiReset(email, recovery, pw);
    showSuccess('Password changed', 1600);
    switchMode('login');
  try{ if(location && location.pathname==='/signup'){ switchMode('signup'); } }catch(_){}
  try{ if(location && location.pathname==='/signup'){ switchMode('signup'); } }catch(_){}
  }catch(e){ toast(e.message||String(e)); }
  finally{ showMiniOverlay(false); }
}

document.addEventListener('DOMContentLoaded', ()=>{
  // already logged in? go to app
  try{ const t = localStorage.getItem('arbexa_token'); if(t){ goApp(); return; } }catch(_){}

  switchMode('login');
  try{ if(location && location.pathname==='/signup'){ switchMode('signup'); } }catch(_){}
  try{ if(location && location.pathname==='/signup'){ switchMode('signup'); } }catch(_){}
  $('#btnLogin').addEventListener('click', doLogin);
  $('#linkForgot').addEventListener('click', ()=>switchMode('reset'));
  $('#linkToSignup').addEventListener('click', ()=>switchMode('signup'));
  $('#linkToLogin').addEventListener('click', ()=>switchMode('login'));
  $('#linkBackLogin').addEventListener('click', ()=>switchMode('login'));
  $('#btnSignup').addEventListener('click', doSignup);
  $('#btnReset').addEventListener('click', doReset);

  document.addEventListener('keydown', (ev)=>{ if(ev.key==='Enter' && !$('#viewLogin').classList.contains('hide')) doLogin(); });
});
</script>

<script>
(function(){
  try {
    var u = localStorage.getItem('arbexa_username') || localStorage.getItem('arbexa_user') || 'anon';
    document.querySelectorAll('a.gopro').forEach(function(a){
      try {
        var url = new URL(a.getAttribute('href'), window.location.origin);
        url.searchParams.set('username', u);
        a.setAttribute('href', url.pathname + url.search);
      } catch(e){}
    });
  } catch(e){}
})();
</script>

</body></html>
"""

# --- ADD: LANDING_HTML (right below LOGIN_HTML) ---
LANDING_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arbexa Profit Finder — Spot Arbitrage, Simplified</title>
<style>
:root{--bg:#0b1220;--card:#101a33;--txt:#e7eefc;--muted:#91a5cc;--acc:#2bd576;--line:#1a2547;--chip:#0e1a35}
*{box-sizing:border-box} html,body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu}
a{color:var(--acc);text-decoration:none}
.nav{position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-bottom:1px solid #132042;background:linear-gradient(180deg,#0b1220 85%,transparent)}
.brand{display:flex;align-items:center;gap:10px}
.brand img{height:36px}
.brand .tag{display:none}
@media(min-width:720px){.brand .tag{display:inline-block;padding:3px 8px;border-radius:8px;border:1px solid #23345f;background:#0e1a35;color:#2bd576;font-weight:800;letter-spacing:.5px}}
.nav .links{display:flex;align-items:center;gap:8px}
.btn{display:inline-flex;align-items:center;justify-content:center;height:36px;padding:0 12px;border-radius:10px;border:1px solid #23345f;background:#0e1a35;color:#e7eefc;font-weight:800;cursor:pointer}
.btn:hover{filter:brightness(1.06)}
.btn.primary{background:var(--acc);border-color:#1a9b5a;color:#04120a}
.hero{display:grid;grid-template-columns:1.4fr 1fr;gap:18px;align-items:center;padding:30px 16px 18px;max-width:1100px;margin:0 auto}
@media(max-width:920px){.hero{grid-template-columns:1fr}}
.h1{font-size:36px;line-height:1.1;margin:6px 0 8px}
.p{color:var(--muted);font-weight:600;margin:0 0 14px}
.cta{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.small{font-size:12px;color:#9fb2d9}
.cons{margin-top:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}
.float{box-shadow:0 18px 50px rgba(0,0,0,.35)}
.hl{display:grid;gap:8px}
.hl .row{display:flex;align-items:center;gap:8px}
.hl .chip{background:var(--chip);border:1px solid #23345f;padding:4px 8px;border-radius:999px;font-size:12px;color:#bdd2ff;display:inline-flex;gap:6px;align-items:center}
.sec{padding:24px 16px;max-width:1100px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media(max-width:920px){.grid{grid-template-columns:1fr}}
.box{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px}
.box h3{margin:0 0 8px}
.steps{display:grid;gap:10px}
.logos{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;align-items:center}
@media(max-width:920px){.logos{grid-template-columns:repeat(3,1fr)}}
.logo{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;border:1px solid #22335d;background:#0e1a35;border-radius:12px;padding:8px}
.logo img{height:26px}
.lname{font-size:12px;color:#cfe0ff;font-weight:900;text-transform:none;letter-spacing:.3px;text-align:center}
.pricing{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media(min-width:980px){.pricing{grid-template-columns:repeat(6,1fr)}}
.plan{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;display:grid;gap:8px}
.plan .t{font-weight:900;letter-spacing:.3px}
.plan .p{color:#cfe0ff}
.plan.rec{outline:2px solid #2bd57622}
.socials{display:flex;flex-wrap:wrap;gap:10px}
.sicon{display:inline-flex;align-items:center;justify-content:center;width:40px;height:40px;border-radius:10px;border:1px solid #23345f;background:#0e1a35}
.sicon img{height:22px; width:22px; display:block}
.footer{padding:18px 16px;border-top:1px solid #132042;color:#8ea3c8}
.footer .row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;max-width:1100px;margin:0 auto}
.fade{opacity:0;transform:translateY(10px);transition:opacity .6s ease, transform .6s ease}
.fade.show{opacity:1;transform:none}
.pulse{animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%{transform:scale(.98)}50%{transform:scale(1.02)}100%{transform:scale(.98)}}
</style>
</head><body>
<header class="nav">
  <div class="brand">
    <img src="/brandlogo" alt="Arbexa">
    <span class="tag">ARBEXAPROFITFINDER.COM</span>
  </div>
  <div class="links">
    <a class="btn" href="https://www.dropbox.com/scl/fi/paqtorxp2q2couih5z5vs/Guide-All-You-Need-To-Know-From-ArbexaProfitFinder.pdf?dl=0" target="_blank" rel="noopener">Guide</a>
    <a class="btn" href="/signup">Sign up</a>
    <a class="btn primary" href="/login">Login</a>
  </div>
</header>

<section class="hero">
  <div class="fade">
    <div class="chip" style="display:inline-flex;gap:8px;align-items:center;background:#0e1a35;border:1px solid #23345f;padding:6px 10px;border-radius:999px">
      <span>🔎 Spot Arbitrage Scanner</span><span style="opacity:.6">•</span><span>Multi-exchange</span><span style="opacity:.6">•</span><span>Orderbooks ×15</span>
    </div>
    <h1 class="h1">Cryptocurrency Arbitrage <span style="color:var(--acc)">Made Easy</span></h1>
    <p class="p">Arbexa scans leading spot exchanges in real-time, surfaces buy/sell gaps, shows depth-aware liquidity and suggests sensible trade sizes — no API keys required to view.</p>
    <div class="cta">
      <a class="btn primary" href="/login">Get started — Sign up / Login</a>
      <a class="btn" href="#pricing">See pricing</a>
    </div>
    <div class="small cons">By signing up, you agree to our
      <a href="https://www.dropbox.com/scl/fi/aw3wca53knh4n89m8t2ec/Arbexa_Terms_And_Conditions_And-Privacy-Policy.pdf?dl=0" target="_blank" rel="noopener">Terms &amp; Conditions</a>
      and <a href="https://www.dropbox.com/scl/fi/aw3wca53knh4n89m8t2ec/Arbexa_Terms_And_Conditions_And-Privacy-Policy.pdf?dl=0" target="_blank" rel="noopener">Privacy Policy</a>.
    </div>
  </div>

  <div class="card float fade">
    <div class="hl">
      <div class="row"><span class="chip">📈 Edge filter: 1–26%</span><span class="chip">🧪 Liquidity score</span></div>
      <div class="row"><span class="chip">📊 24h Volume check</span><span class="chip">📚 Orderbooks ×15</span></div>
      <div class="row"><span class="chip">💡 Suggested size</span><span class="chip">⚡ 10s refresh</span></div>
      <div class="row"><span class="chip">🏦 Multi-exchange</span><span class="chip">🔔 Optional sound</span></div>
    </div>
  </div>
</section>

<section class="sec fade" id="exchanges">
  <h3 style="margin:0 0 10px">Supported exchanges</h3>
  <div class="logos" id="logos"><div class="logo pulse">Loading…</div></div>
  
</section>

<section class="sec grid fade">
  <div class="box">
    <h3>How it works</h3>
    <div class="steps small">
      <div>1) Buy on the cheaper exchange.</div>
      <div>2) Transfer on a fast, low-fee network (e.g. TRC20/BEP20).</div>
      <div>3) Sell on the higher-priced exchange.</div>
      <div class="small" style="opacity:.9">Full walkthrough in the <a href="https://www.dropbox.com/scl/fi/paqtorxp2q2couih5z5vs/Guide-All-You-Need-To-Know-From-ArbexaProfitFinder.pdf?dl=0" target="_blank" rel="noopener">Guide</a>.</div>
    </div>
  </div>
  <div class="box">
    <h3>Why Arbexa</h3>
    <ul class="small" style="margin:10px 0 0 25px">
      <li>Real-time scanner across popular spot markets</li>
      <li>Depth-aware liquidity &amp; suggested size</li>
      <li>No API keys to view opportunities</li>
      <li>Clean UI, sensible defaults, mobile-friendly</li>
    </ul>
  </div>
  <div class="box">
    <h3>Stay updated</h3>
    <p class="small" style="margin-top:6px">Follow our socials for the latest updates and join the Telegram Channel &amp; Chat to get the best of Arbexa and connect with other users.</p>
    <div class="socials">
      <a class="sicon" target="_blank" rel="noopener" href="https://x.com/arbexascanner?s=21"><img alt="X" src="https://logo.clearbit.com/x.com"></a>
      <a class="sicon" target="_blank" rel="noopener" href="https://www.youtube.com/@ArbexaProfitFinder"><img alt="YouTube" src="https://logo.clearbit.com/youtube.com"></a>
      <a class="sicon" target="_blank" rel="noopener" href="https://www.tiktok.com/@arbexaprofitfinder"><img alt="TikTok" src="https://logo.clearbit.com/tiktok.com"></a>
      <a class="sicon" target="_blank" rel="noopener" href="https://t.me/ArbexaProfitFinderSupport"><img alt="Telegram Channel" src="https://logo.clearbit.com/telegram.org"></a>
      <a class="sicon" target="_blank" rel="noopener" href="https://t.me/ArbexaProfitFinderSupport"><img alt="Telegram Chat" src="https://logo.clearbit.com/telegram.org"></a>
      <a class="sicon" target="_blank" rel="noopener" href="https://t.me/ArbexaProfitFinderSupport"><img alt="Telegram Support" src="https://logo.clearbit.com/telegram.org"></a>
    </div>
  </div>
</section>

<section class="sec fade" id="pricing">
  <h3 style="margin:0 0 10px">Pricing</h3>
  <div class="pricing">
    <div class="plan"><div class="t">Weekly</div><div class="p">$10</div><a class="btn" href="/login">Get Started</a></div>
    <div class="plan"><div class="t">Monthly</div><div class="p">$20</div><a class="btn" href="/login">Get Started</a></div>
    <div class="plan"><div class="t">3 Months</div><div class="p">$55 <span class="small" style="opacity:.8">(~8% off)</span></div><a class="btn" href="/login">Get Started</a></div>
    <div class="plan"><div class="t">6 Months</div><div class="p">$100 <span class="small" style="opacity:.8">(~17% off • $16.7/mo)</span></div><a class="btn" href="/login">Get Started</a></div>
    <div class="plan rec"><div class="t">Yearly</div><div class="p">$180 <span class="small" style="opacity:.8">(Recommended • $15/mo)</span></div><a class="btn primary" href="/login">Get Started</a></div>
    <div class="plan"><div class="t">3 Years</div><div class="p">$450 <span class="small" style="opacity:.8">(~38% off • $12.5/mo)</span></div><a class="btn" href="/login">Get Started</a></div>
  </div>
  <div class="small cons" style="margin-top:10px">By signing up, you agree to our
    <a href="https://www.dropbox.com/scl/fi/aw3wca53knh4n89m8t2ec/Arbexa_Terms_And_Conditions_And-Privacy-Policy.pdf?dl=0" target="_blank" rel="noopener">Terms &amp; Conditions</a>
    and <a href="https://www.dropbox.com/scl/fi/aw3wca53knh4n89m8t2ec/Arbexa_Terms_And_Conditions_And-Privacy-Policy.pdf?dl=0" target="_blank" rel="noopener">Privacy Policy</a>.
  </div>
</section>

<footer class="footer">
  <div class="row">
    <div class="small">© <span id="y"></span> Arbexa Profit Finder. No guaranteed profits; fees, latency and slippage apply.</div>
    <div class="small">
      <a href="https://www.dropbox.com/scl/fi/aw3wca53knh4n89m8t2ec/Arbexa_Terms_And_Conditions_And-Privacy-Policy.pdf?dl=0" target="_blank" rel="noopener">Terms &amp; Conditions</a> •
      <a href="https://www.dropbox.com/scl/fi/aw3wca53knh4n89m8t2ec/Arbexa_Terms_And_Conditions_And-Privacy-Policy.pdf?dl=0" target="_blank" rel="noopener">Privacy</a>
    </div>
  </div>
</footer>

<script>
/* Reveal on scroll */
const obs=new IntersectionObserver(es=>es.forEach(e=>{ if(e.isIntersecting){ e.target.classList.add('show'); obs.unobserve(e.target); } }),{threshold:.12});
document.querySelectorAll('.fade').forEach(el=>obs.observe(el));
/* Year */
document.getElementById('y').textContent = String(new Date().getFullYear());
/* Logos from /logos (reusing your config + Clearbit fallbacks) */
function exLogoSrc(ex, dom, url){ return url && url[ex] ? url[ex] : (dom && dom[ex] ? ('https://logo.clearbit.com/'+dom[ex]) : ''); }
(async ()=>{
  try{
    const r = await fetch('/logos',{cache:'no-store'}); const j = await r.json();
    const arr = (j.exchanges||[]).slice(0);
    const dom = j.logos || {}; const url = j.logo_urls || {};
    const wrap = document.getElementById('logos');
    if(!wrap) return;
    const NAMES = {
  binance:'Binance', bybit:'Bybit', mexc:'MEXC', gateio:'Gate.io', bitget:'Bitget', bitmart:'BitMart',
  cryptocom:'Crypto.com', bingx:'BingX', coinex:'CoinEx', whitebit:'WhiteBIT', ascendex:'AscendEX',
  bitrue:'Bitrue', lbank:'LBank', xt:'XT.com', bitstamp:'Bitstamp', hitbtc:'HitBTC'
};
wrap.innerHTML = arr.map(ex=>{
  const src = exLogoSrc(ex, dom, url);
  const name = NAMES[ex] || (ex.charAt(0).toUpperCase()+ex.slice(1));
  return `<div class="logo"><img alt="${ex}" src="${src}" onerror="this.style.display='none'"><div class="lname"><b>${name}</b></div></div>`;
}).join('');
  }catch(_){}
})();
</script>
</body></html>"""

@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(LANDING_HTML)

@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(LOGIN_HTML)

LOGO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" width="260" height="40" viewBox="0 0 520 80">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2bd576"/><stop offset="1" stop-color="#1e78ff"/></linearGradient></defs>
  <rect width="100%" height="100%" fill="none"/>
  <path d="M20,60 L70,20 L110,50 L150,18" stroke="url(#g)" stroke-width="10" fill="none" stroke-linecap="round"/>
  <text x="166" y="52" font-size="34" font-family="Segoe UI, Arial, sans-serif" fill="#e7eefc" font-weight="700">Arbexa</text>
  <rect x="165" y="58" rx="6" ry="6" width="330" height="20" fill="#0e1a35" stroke="#26345e" stroke-width="1"/>
  <text x="172" y="73" font-size="14" font-family="Segoe UI, Arial, sans-serif" fill="#2bd576" letter-spacing="1.2">ARBEXAPROFITFINDER.COM</text>
</svg>
""".strip()



@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return HTMLResponse(LOGIN_HTML)


@app.get("/pro", response_class=HTMLResponse)
def pro_page(request: Request):
    return HTMLResponse(PRO_HTML)



# --- PRO checkout -> create CryptoCloud invoice and redirect ---
PRO_PRICE_MAP = {'weekly':10,'monthly':20,'3m':55,'6m':100,'yearly':180,'3y':450}

@app.get("/pro/checkout")
def pro_checkout(plan: str = 'monthly', username: str | None = None):
    import os, time
    API = os.environ.get('CRYPTOCLOUD_API_KEY')
    SHOP = os.environ.get('CRYPTOCLOUD_SHOP_ID')
    if not (API and SHOP):
        return HTMLResponse("<script>alert('CryptoCloud not configured.');location='/pro';</script>", status_code=501)

    amount = PRO_PRICE_MAP.get(plan)
    if amount is None:
        return HTMLResponse(f"<h3>Unknown plan: {plan}</h3>", status_code=400)

    order_id = f"{(username or 'anon')}:{plan}:{int(time.time())}"
    payload = {
        "shop_id": SHOP,
        "amount": float(amount),
        "currency": "USD",
        "order_id": order_id,
        "add_fields": {"username": username or "anon", "plan": plan
    },
    }
    headers = {"Authorization": f"Token {API}"}
    try:
        r = requests.post("https://api.cryptocloud.plus/v2/invoice/create",
                          json=payload, headers=headers, timeout=20)
        # --- payments: ensure user_id, provider_invoice_id, sr_invoice_id ---
        try:
            u = current_user_from_auth_header(request, db)
            supa_uid = None
            if u:
                try:
                    supa_uid = _sb_get_or_create_profile_id(email=u.email, username=getattr(u, "username", None))
                except Exception as _e:
                    print("[payments] profile resolve error:", _e)
            if not supa_uid:
                try:
                    supa_uid = _sb_get_or_create_profile_id(email=None, username=username if "username" in locals() else "anon")
                except Exception as _e:
                    print("[payments] fallback profile resolve error:", _e)
        
            _prov_id = None
            try:
                _j = r.json()
            except Exception:
                _j = {}
            if isinstance(_j, dict):
                for k in ("invoice_id", "id"):
                    if k in _j and isinstance(_j[k], (str, int)):
                        _prov_id = str(_j[k])
                for nest in ("result", "data", "payload"):
                    d = _j.get(nest) if isinstance(_j.get(nest), dict) else None
                    if d:
                        for k in ("invoice_id", "id"):
                            if k in d and isinstance(d[k], (str, int)):
                                _prov_id = str(d[k])
        
            try:
                order_id  # noqa
            except NameError:
                order_id = "sr_" + datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S") + "_" + uuid4().hex[:8]
        
            try:
                _duration = {"weekly":7,"monthly":30,"3m":90,"6m":180,"yearly":365,"3y":1095}.get(str(plan), 30)
            except Exception:
                _duration = 30
        
            now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        
            row = {
                "user_id": supa_uid,
                "plan_name": str(plan) if "plan" in locals() else "PLAN",
                "plan_duration": int(_duration),
                "amount": float(amount) if "amount" in locals() else 0.0,
                "currency": "USD",
                "status": "pending",
                "provider": "cryptocloud",
                "provider_invoice_id": _prov_id,
                "sr_invoice_id": order_id,
                "started_at": now_iso,
                "paid_at": None,
                "expires_at": None,
                "note": None,
                "meta": _j if isinstance(_j, dict) else {}
            }
            try:
                _sb_enqueue("payments", row)
            except Exception as _e:
                print("[supabase] payments enqueue error:", _e)
            try:
                _sb_post_rows("payments", [row])
            except Exception as _e:
                print("[supabase] payments direct post error:", _e)
        except Exception as _e:
            print("[payments] finalize row error:", _e)

    except Exception as e:
        return HTMLResponse(f"<h3>Failed to reach CryptoCloud API: {e}</h3>", status_code=502)

    if r.status_code != 200:
        return HTMLResponse(f"<h3>CryptoCloud error {r.status_code}: {r.text}</h3>", status_code=502)
    # --- enqueue PAYMENTS (pending) to Supabase ---
    try:
        _res = r.json() or {}
        _res_result = _res.get("result") or {}
        _prov_id = _res_result.get("id") or _res_result.get("invoice_id") or None
        _sb_enqueue("payments", {
            "user_id": None,
            "plan_name": plan,
            "plan_duration": {"weekly":7, "monthly":30, "3m":90, "6m":180, "yearly":365, "3y":1095}.get(plan),
            "amount": float(amount),
            "currency": "USD",
            "status": "pending",
            "provider": "cryptocloud",
            "provider_invoice_id": _prov_id,
            "started_at": datetime.datetime.utcnow().isoformat(),
            "note": None,
            "meta": _res
        })
    except Exception as _e:
        print("[supabase] enqueue payment(pending) error:", _e)
    
    link = (r.json().get("result") or {}).get("link")
    if not link:
        return HTMLResponse(f"<h3>Unexpected API response: {r.text}</h3>", status_code=502)
    
    return RedirectResponse(link, status_code=307)



@app.post("/webhooks/cryptocloud", response_class=JSONResponse)
def cryptocloud_webhook(req: Request):
    # Minimal webhook: trust payload (you can add signature checks later).
    try:
        payload = req.json()
    except Exception:
        try:
            payload = json.loads(req.body().decode("utf-8"))
        except Exception:
            payload = {}
    # Expected fields: status, invoice_id/id, amount, currency, add_fields{plan, username}, paid_at, expired_at
    status = (payload.get("status") or "").lower()
    result = payload.get("result") or {}
    inv_id = payload.get("invoice_id") or result.get("id") or result.get("invoice_id")
    addf = payload.get("add_fields") or result.get("add_fields") or {}
    plan = addf.get("plan") or "monthly"
    # Upsert payment with new status
    try:
        _sb_upsert("payments", [{
            "provider_invoice_id": inv_id,
            "status": status if status in ("paid","failed","canceled","expired","pending") else "pending",
            "paid_at": datetime.datetime.utcnow().isoformat() if status=="paid" else None,
            "meta": payload
        }], on_conflict="provider_invoice_id")
    except Exception as _e:
        print("[supabase] webhook upsert error:", _e)
    # If paid, create subscription row
    if status == "paid":
        days = {"weekly":7,"monthly":30,"3m":90,"6m":180,"yearly":365,"3y":1095}.get(plan, 30)
        start = datetime.datetime.utcnow()
        end = start + datetime.timedelta(days=days)
        try:
            _sb_enqueue("subscription", [{
                "status":"active",
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "note": f"Auto from CryptoCloud webhook; plan={plan}; invoice={inv_id}"
            }][0])
        except Exception as _e:
            print("[supabase] enqueue subscription error:", _e)
    return JSONResponse({"ok": True})


@app.get("/brandlogo")
def brandlogo():
    return Response(content=LOGO_SVG, media_type="image/svg+xml")

@app.get("/static/logo-coinex.svg")
def logo_coinex():
    svg = """
<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <defs>
    <linearGradient id="cxg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#09C372"/>
      <stop offset="1" stop-color="#00A6C8"/>
    </linearGradient>
  </defs>
  <rect width="256" height="256" rx="48" fill="#0b1220"/>
  <g transform="translate(28,28)">
    <circle cx="100" cy="100" r="96" fill="none" stroke="url(#cxg)" stroke-width="24"/>
    <path d="M160 74a54 54 0 1 0 0 52" fill="none" stroke="url(#cxg)" stroke-width="24" stroke-linecap="round"/>
  </g>
</svg>
    """.strip()
    return Response(content=svg, media_type="image/svg+xml")

@app.get("/time", response_class=JSONResponse)
def time_now():
    return JSONResponse({"serverTimeUTC": dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")})
# ------------- PAGE (HTML) -------------
_OPPS_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arbexa Profit Finder — /opps</title>
<style>
:root{--bg:#0b1220;--card:#111a2e;--muted:#7a8aa0;--txt:#e7eefc;--acc:#2bd576;--warn:#ffcf5a;--bad:#ff6b6b;--chip:#1a2440;}
*{box-sizing:border-box} body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,'Helvetica Neue',Arial,'Apple Color Emoji','Segoe UI Emoji';background:var(--bg);color:var(--txt)}
a{color:var(--acc);text-decoration:none}
header{position:sticky;top:0;z-index:5;background:linear-gradient(180deg,#0b1220 70%,transparent);padding:6px 12px 6px;border-bottom:1px solid #182241}
.brandrow{position:relative;display:flex;align-items:center;gap:14px;padding:4px 0 8px;flex-wrap:wrap}
.brandlogo{height:36px;width:auto;display:block}
.brandurl{text-decoration:none;text-transform:uppercase;letter-spacing:.8px;font-weight:700;color:#2bd576;border:1px solid #26345e;background:#0e1a35;padding:4px 8px;border-radius:8px}
.lastbox{display:inline-flex;align-items:baseline;gap:8px;background:#0e1a3500;padding:2px 6px;border-radius:8px}
.lastbox .lastlabel{font-size:12px;color:var(--muted)}
.lastbox .lasttime{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums;letter-spacing:1px;font-size:20px;font-weight:800;color:#e7eefc;white-space:nowrap}
.lastbox .oppcount{font-size:12px;color:var(--muted);font-style:oblique;opacity:.9;white-space:nowrap}
.menu-anchor{position:absolute; right:12px; top:2px; display:flex; gap:8px; align-items:center;}
.menu-anchor details{display:inline-block}
.refresh-btn{width:36px;height:36px;border-radius:10px;border:1px solid #26345e;background:#0e1a35;display:inline-flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 1px 0 rgba(0,0,0,.2) inset}
.refresh-btn:focus{outline:2px solid #2bd57644;outline-offset:2px}
.refresh-btn:hover{filter:brightness(1.05)}
.refresh-btn svg{width:22px;height:22px;display:block}
.auth-anchor{display:flex; gap:8px; align-items:center;}
.auth-btn{height:36px; padding:0 10px; border-radius:10px; border:1px solid #26345e; background:#0e1a35; color:#e7eefc; font-weight:700; cursor:pointer; display:inline-flex; align-items:center; justify-content:center;}
.auth-btn:hover{filter:brightness(1.05)}
.auth-btn.primary{background:#2bd576; color:#04120a; border-color:#1b9d5b}
.menu-panel{min-width:780px; max-width:90vw; background:var(--card); border:1px solid #23345f; border-radius:12px; padding:10px; box-shadow:0 6px 24px rgba(0,0,0,.45)}
.menu-panel .group{margin-bottom:10px; border:1px solid #182241; border-radius:12px; padding:8px; background:#0f1a33}
.menu-panel .group summary{font-weight:600}
.menu-list a{display:flex; align-items:center; gap:8px; padding:6px 8px; border-radius:8px}
.menu-list a:hover{background:#132042}
.menu-list img{width:16px;height:16px;border-radius:4px;object-fit:contain;vertical-align:-2px}
.set-filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
.set-filters .block{background:#0f1a33;border:1px solid #182241;padding:8px 10px;border-radius:12px;display:flex;gap:8px;align-items:center}
.set-ex h4{margin:4px 0 8px;font-size:14px;color:#b8c8e8}
.exgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.extable{width:100%;border-collapse:collapse;font-size:13px}
.extable td{border:1px solid #23345f;padding:6px 8px}
.extable td label{display:flex;align-items:center;gap:8px}
.extable img.exlogo{width:16px;height:16px;object-fit:contain;border-radius:4px;vertical-align:-2px;}
.ex-toolbar{display:flex;gap:8px;margin-top:8px}
.btnpdf{border:1px solid #23345f;background:#0e1a35;color:#b8c8e8;border-radius:10px;padding:6px 10px;cursor:pointer;display:inline-flex;gap:8px;align-items:center;font-weight:600}
.btnpdf:hover{filter:brightness(1.1)}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px}
.filters .block{background:var(--card);border:1px solid #182241;padding:8px 10px;border-radius:12px;display:flex;gap:8px;align-items:center}
#tblwrap{padding:8px 10px 18px}
table{width:100%;border-collapse:separate;border-spacing:0 10px}
thead th{text-align:left;font-size:12px;color:var(--muted);padding:0 10px}
tbody tr{background:var(--card);border:1px solid #1a2547}
tbody td{padding:12px 10px;vertical-align:top;border-top:1px solid #1a2547;border-bottom:1px solid #1a2547}
tbody td:first-child{border-left:1px solid #1a2547;border-top-left-radius:14px;border-bottom-left-radius:14px}
tbody td:last-child{border-right:1px solid #1a2547;border-top-right-radius:14px;border-bottom-right-radius:14px}
.badge{display:inline-flex;align-items:center;gap:6px;background:#0e1a35;border:1px solid #23345f;padding:4px 8px;border-radius:999px;font-size:12px;color:#b8c8e8}
.edge{font-weight:700}.edge.good{color:var(--acc)}.edge.warn{color:var(--warn)}
.mononu{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.kv{display:flex;flex-wrap:wrap;gap:8px 14px;color:#c5d3f2}.kv div{display:flex;gap:6px;align-items:center}
.details summary{cursor:pointer;color:#b8c8e8}
.obtbl{width:100%;border-collapse:collapse;margin-top:6px;font-size:12px}
.obtbl th,.obtbl td{border:1px solid #203058;padding:4px 6px;text-align:right}
.obtbl th:first-child,.obtbl td:first-child{text-align:left}
.obtbl.ob-ask td:nth-child(1), .obtbl.ob-ask td:nth-child(2){ color:#2bd576; }
.obtbl.ob-bid  td:nth-child(1), .obtbl.ob-bid  td:nth-child(2){ color:#ff6b6b; }
.obtbl td:nth-child(3){ color:#e7eefc; }
footer{color:#8ea3c8;font-size:12px;padding:6px 12px 18px}
.grid-cards{display:none}
@media(max-width:920px){table{display:none}.grid-cards{display:grid;gap:10px;padding:10px;grid-template-columns:1fr}.card{background:var(--card);border:1px solid #1a2547;border-radius:16px;padding:10px}.card h3{margin:4px 0 8px;font-size:16px}.kv{gap:6px 10px;font-size:13px}}
.cell-ob .stack details+details{margin-top:6px}
.disc{color:#cfe0ff}.disc ul{margin:8px 0 0 0; padding-left:18px}.disc li{margin:4px 0}
.tradecontent{ max-width:460px; max-height:60vh; overflow:auto; padding-right:6px;}
.tradecontent iframe{ width:100%; min-height:320px; height:60vh; border:1px solid #23345f; border-radius:10px; background:#0e1a35;}
.emptymsg{ font-style: italic; font-weight: 700; }
#loadOverlay{position:fixed;inset:0;background:rgba(7,12,22,.75);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;z-index:9998}
#loadOverlay.hidden{display:none}
.logoWrap{display:grid;place-items:center}
.logoPulse{width:min(520px,70vw);height:auto;filter:drop-shadow(0 6px 22px rgba(43,213,118,.25));animation:pulse 1.3s ease-in-out infinite}
@keyframes pulse{0%{transform:scale(.96);opacity:.8}50%{transform:scale(1.03);opacity:1}100%{transform:scale(.96);opacity:.8}}
.loadCaption{color:#e7eefc;font-weight:800;letter-spacing:.6px;text-shadow:0 1px 0 #04120a}
.loadBar{width:min(520px,80vw);height:10px;border-radius:999px;background:#0e1a35;border:1px solid #26345e;overflow:hidden}
.loadBarFill{height:100%;width:100%;transform-origin:left center;transform:scaleX(0);background:linear-gradient(90deg,#2bd576,#22c35e);box-shadow:0 0 16px rgba(43,213,118,.45) inset, 0 0 10px rgba(43,213,118,.25)}
.modal{position:fixed;inset:0;background:rgba(7,12,22,.7);display:flex;align-items:center;justify-content:center;z-index:9999}
.modal.hidden{display:none}
.modal .box{background:var(--card);border:1px solid #23345f;border-radius:14px;max-width:460px;width:calc(100% - 40px);padding:16px 18px;color:#e7eefc;box-shadow:0 6px 30px rgba(0,0,0,.45);text-align:center}
.modal .box p{margin:0 0 12px 0;font-weight:700}
.modal .actions{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
img.exlogo{width:16px;height:16px;object-fit:contain;border-radius:4px;vertical-align:-2px;}

.dash-tip{font-style:italic;color:#2bd576;text-align:center;margin:8px 12px;font-weight:700}
.chatfab{position:fixed;right:14px;bottom:18px;width:52px;height:52px;border-radius:999px;border:1px solid #23345f;background:#0e1a35;z-index:9999;display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 10px 24px rgba(0,0,0,.35)}
.chatfab:hover{filter:brightness(1.06)}
.chatfab .dot{font-size:22px;line-height:1}
@media (max-width:520px){.chatfab{width:48px;height:48px}}

.btn-pro{display:inline-flex;align-items:center;justify-content:center;height:34px;padding:0 12px;border-radius:10px;
  border:1px solid #846200;background:linear-gradient(135deg,#ffe089,#efb800);color:#2b1e00;font-weight:900;letter-spacing:.2px;cursor:pointer;white-space:nowrap}
.btn-pro:hover{filter:brightness(1.05)}
@media(max-width:520px){.btn-pro{height:32px;padding:0 10px;font-size:12px}}
.free-banner{margin:8px 12px 0 12px; color:#22c55e; font-style:italic; font-size:14px}
.free-banner.hide{display:none}
</style>
</head><body>
<header>
  <div class="brandrow">
    <img src="/brandlogo" alt="Arbexa Profit Finder" class="brandlogo">
    <a class="brandurl" href="https://arbexaprofitfinder.com" target="_blank" rel="noopener">ARBEXAPROFITFINDER.COM</a>
    <div class="lastbox" id="lastBox">
      <span class="lastlabel">Last updated</span>
      <span id="lastUTCtime" class="lasttime">--:-- AM</span>
      <span id="oppCount" class="oppcount">· -- possible opportunities</span>
    </div>

    <div class="menu-anchor">
      <details id="menuDD">
        <summary class="btnpdf" title="Menu">☰ Menu ▾</summary>
        <div class="menu-panel">
          <details id="settingsDD" class="group">
            <summary class="btnpdf">⚙️ Settings ▾</summary>
            <div>
              <div class="set-filters">
                <div class="block"><button id="soundToggle" class="btnpdf" type="button" aria-pressed="true">🔊 Sound: ON</button></div>
                <div class="block"><span>📈 Edge %</span><input id="minEdge" type="number" step="0.1" value="1" style="width:70px"><span>–</span><input id="maxEdge" type="number" step="0.1" value="25" style="width:70px"></div>
                <div class="block"><span>🔎 Pair</span><input id="q" placeholder="e.g. BTC/USDT" style="width:160px"></div>
                <div class="block"><span>💧 Min $24h Vol</span><input id="minVol" type="number" step="1000" value="0" style="width:120px"></div>
                <div class="block"><span>🧪 Min Liquidity</span><input id="minLiq" type="number" min="0" max="100" step="1" value="0" style="width:70px"></div>
                <div class="block"><span>💡 Trade Size $</span><input id="tsMin" type="number" step="1" placeholder="100" style="width:90px"><span>–</span><input id="tsMax" type="number" step="1" placeholder="6500" style="width:90px"></div>
              </div>
              <div class="set-ex">
                <h4>🏦 Exchanges</h4>
                <div class="exgrid">
                  <table class="extable" id="extable1"></table>
                  <table class="extable" id="extable2"></table>
                </div>
                <div class="ex-toolbar">
                  <button type="button" id="exAll" class="btnpdf">Select All</button>
                  <button type="button" id="exNone" class="btnpdf">None</button>
                  <button type="button" id="exApply" class="btnpdf">Apply</button>
                  <button type="button" id="exDefault" class="btnpdf" title="Tick all exchanges and reset filters to Edge 1–26">Default</button>
                  <!-- ✨ NEW: Change Password button (beside Default) -->
                  <button type="button" id="exChangePassword" class="btnpdf" title="Change your password (logged-in only)">Change Password</button>
                </div>
              </div>
            </div>
          </details>

          <details id="socialsDD" class="group" open>
            <summary class="btnpdf">👥 Socials ▾</summary>
            <div class="menu-list">
              <a href="https://www.tiktok.com/@arbexaprofitfinder" target="_blank" rel="noopener"><img src="https://www.tiktok.com/favicon.ico" alt=""> TikTok</a>
              <a href="https://x.com/arbexascanner?s=21" target="_blank" rel="noopener"><img src="https://x.com/favicon.ico" alt=""> Twitter / X</a>
              <a href="https://www.youtube.com/@ArbexaProfitFinder" target="_blank" rel="noopener"><img src="https://www.youtube.com/favicon.ico" alt=""> YouTube</a>
              <a href="https://t.me/ArbexaProfitFinderSupport" target="_blank" rel="noopener"><img src="https://telegram.org/favicon.ico" alt=""> Telegram Channel</a>
              <a href="https://t.me/ArbexaProfitFinderSupport" target="_blank" rel="noopener"><img src="https://telegram.org/favicon.ico" alt=""> Telegram Chat</a>
              <a href="https://t.me/ArbexaProfitFinderSupport" target="_blank" rel="noopener"><img src="https://telegram.org/favicon.ico" alt=""> Telegram Support</a>
            </div>
          </details>

          <details id="tncDD" class="group">
            <summary class="btnpdf">T&amp;C ▾</summary>
            <div class="menu-list">
              <a id="tncLink" href="https://www.dropbox.com/scl/fi/aw3wca53knh4n89m8t2ec/Arbexa_Terms_And_Conditions_And-Privacy-Policy.pdf?dl=0" target="_blank" rel="noopener">📄 Terms &amp; Conditions / Privacy Policy</a>
              <div style="padding:6px 8px"><button id="agreeBtn" class="btnpdf" type="button">Agreed?</button></div>
            </div>
          </details>
        </div>
      </details>

      <button id="refreshNow" class="refresh-btn" title="Refresh now" aria-label="Refresh now">
        <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 12a9 9 0 1 1-2.64-6.36"/>
          <polyline points="21 3 21 9 15 9"/>
        </svg>
      </button>

      <!-- Profile dropdown -->
      <details id="profileDD">
        <summary class="btnpdf" title="Profile">👤 Profile ▾</summary>
        <div id="profileCard" class="menu-panel" style="max-width:420px; padding:12px">
          <div style="color:#9fb2d9">Loading…</div>
        </div>
      </details>

      <div class="auth-anchor">
        <button id="btnSignup" class="auth-btn" type="button" title="Create account">Sign up</button>
        <button id="btnLogin"  class="auth-btn primary" type="button" title="Login">Log in</button>
        <button id="btnLogout" class="auth-btn" type="button" title="Logout" style="display:none">Log out</button>
      </div>
    </div>
  </div>

  <div class="filters">
    <div class="block"><a id="extDoc" class="btnpdf" href="#" target="_blank" rel="noopener" style="display:none"></a></div>
    <div class="block"><details id="tradeDD"><summary class="btnpdf">TRADE DETAILS‼️</summary><div class="tradecontent" id="tradeContent"></div></details></div>
    <div class="block">
      <details id="msgDD"><summary id="msgSummary" class="btnpdf">📩Message</summary>
        <div class="tradecontent">Make sure to apply the ⚠️Trade Cautions before each trade!</div>
      </details>
    </div>
  </div>

  <a id="btnGoPro" class="btn-pro" href="/pro" title="Upgrade to Pro">GO PRO👑</a>
</header>
<div id="freeBanner" class="free-banner hide">You’re currently on the free plan with limited features, subscribe to unlock full potentials.</div>
<div class="dash-tip">Most Profitable and executable Opportunities last no more than 10-15 Minutes so act fast,but carefully!</div>


<!-- Loading overlay -->
<div id="loadOverlay" class="hidden" aria-hidden="true">
  <div class="logoWrap"><img class="logoPulse" src="/brandlogo" alt="Loading…" /></div>
  <div class="loadCaption">Loading latest opportunities…</div>
  <div class="loadBar" aria-hidden="true"><div id="loadBarFill" class="loadBarFill"></div></div>
</div>

<!-- Reminder modal -->
<div id="remindModal" class="modal hidden" role="dialog" aria-modal="true" aria-labelledby="remindText">
  <div class="box"><p id="remindText">⚠️Make sure to apply trade details before each trade!</p>
    <div class="actions"><button id="remindOk" class="btnpdf" type="button">Okay</button><button id="remindSkip" class="btnpdf" type="button">Do not remind me today</button></div>
  </div>
</div>

<!-- ✨ NEW: Change Password modal -->
<div id="cpModal" class="modal hidden" role="dialog" aria-modal="true" aria-labelledby="cpTitle">
  <div class="box">
    <p id="cpTitle">Change Password</p>
    <div style="display:grid;gap:8px;margin:8px 0">
      <input id="cpCur" type="password" placeholder="Current password">
      <input id="cpNew" type="password" placeholder="New password (min 6)">
      <input id="cpNew2" type="password" placeholder="Confirm new password">
    </div>
    <div class="actions">
      <button id="cpSubmit" class="btnpdf" type="button">Change Password</button>
      <button id="cpCancel" class="btnpdf" type="button">Cancel</button>
    </div>
  </div>
</div>
<div id="tblwrap">
  <table id="opptable">
    <thead><tr>
      <th>Pair 🔁</th><th>Edge% 📈</th><th>Buy @ 🛒</th><th>Sell @ 💸</th>
      <th>$24h Vol (Buy/Sell) 💧</th>
      <th title="Higher score reflects stronger 24h volume and tighter order-book depth near the top price.">Liquidity score 🧪</th>
      <th>Suggest Size 💡</th><th>Example Profit 🤑</th><th>Price ($)</th><th>Best Ask 🔼</th><th>Best Bid 🔽</th><th>Details 📚</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <div class="grid-cards" id="cards"></div>
</div>

<footer><div class="kv"><div>⏱️ Auto-refresh: every 10s</div><div>⚠️ Examples only; fees, latency and slippage apply.</div></div></footer>

<script>
try{
  const _t = localStorage.getItem('arbexa_token');
  if(!_t){ location.replace('/'); }
}catch(_){ location.replace('/'); }

const SOUND_KEY='arbexa_sound';
function soundEnabled(){ try{ return localStorage.getItem(SOUND_KEY)!=='0'; }catch(_){ return true; } }
let _actx=null;
function _ctx(){ if(!_actx){ _actx=new (window.AudioContext||window.webkitAudioContext)(); } return _actx; }
function _beep(freq=440, dur=120, type='sine', vol=0.08, delayMs=0){
  if(!soundEnabled()) return;
  try{
    const ctx=_ctx(); const t=ctx.currentTime + (delayMs||0)/1000;
    const o=ctx.createOscillator(); const g=ctx.createGain();
    o.type=type; o.frequency.setValueAtTime(freq, t);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.0001 + vol, t+0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur/1000);
    o.connect(g).connect(ctx.destination);
    o.start(t); o.stop(t + dur/1000 + 0.05);
  }catch(_){}
}
function playSfx(kind){ if(kind==='tap'){ _beep(300,70,'square',0.05,0); } }

function qs(s){return document.querySelector(s)} function qsa(s){return Array.from(document.querySelectorAll(s))}
function cap(s){return s.charAt(0).toUpperCase()+s.slice(1)}

const EXT_DOC_URL="https://www.dropbox.com/scl/fi/paqtorxp2q2couih5z5vs/Guide-All-You-Need-To-Know-From-ArbexaProfitFinder.pdf?dl=0";
const EXT_DOC_LABEL="Guide,All You Need To Know From ArbexaProfitFinder";
function initExtDoc(){const el=qs('#extDoc'); if(!el) return; let url=EXT_DOC_URL?EXT_DOC_URL.trim():""; if(url){el.href=url; el.textContent=`🔗 ${EXT_DOC_LABEL}`; el.style.display='inline-flex';}}

window._uiState={open:{},scrollY:0};
function rememberUI(){_uiState.scrollY=window.scrollY; _uiState.open={}; document.querySelectorAll('tr[data-sym]').forEach(tr=>{const k=tr.dataset.sym,ds=tr.querySelectorAll('.cell-ob details'); _uiState.open[k]=[!!(ds[0]?.open),!!(ds[1]?.open)];});}
function restoreUI(){
  let applied=false;
  document.querySelectorAll('tr[data-sym]').forEach(tr=>{
    const st=_uiState.open[tr.dataset.sym]; if(!st) return;
    const ds=tr.querySelectorAll('.cell-ob details');
    if(st[0]&&ds[0]) ds[0].open=true;
    if(st[1]&&ds[1]) ds[1].open=true;
    applied = applied || !!(st[0]||st[1]);
  });
  requestAnimationFrame(()=>window.scrollTo({top:_uiState.scrollY,left:0,behavior:'instant'}));
  return applied;
}

function numFull(x){if(x===null||x===undefined||isNaN(x)) return '-'; return Number(x).toLocaleString(undefined,{useGrouping:false,maximumFractionDigits:12});}
function usdFull(x){if(x===null||x===undefined||isNaN(x)) return '$0'; return '$'+Number(x).toLocaleString(undefined,{useGrouping:false,maximumFractionDigits:12});}

function disclaimerHTML(){return `<div class="disc"><div><strong>Disclaimer‼</strong></div><ul>
<li>Make your re-search on the exchanges you choose to use before investing.</li>
<li>Verify deposit/withdrawal working (not temporarily suspended).</li>
<li><strong>Volume &amp; Depth</strong>: 24h volume &gt; $500k for the pairs you trade; tighter spreads; deeper books.</li>
<li>Speed: TRON/BSC networks for cheap/fast transfers; beware ERC-20 delays &amp; fees.</li>
<li>Reliability: Exchanges with predictable deposit credit times and clear status pages.</li>
<li>Look out for liquidity, usually 24 hours Volume (USDT) should be 1000X your trade size, for comfortable and smooth trade.</li>
<li>Same ticker does not guarantee same asset — always verify full name &amp; contract.</li>
<li>Confirm network type (TRC20/BEP20/ERC20/Solana/etc.) matches both ways.</li>
<li>Note min withdraw, max per transaction, and expected confirmation count.</li>
<li>Mis-matching networks causes lost funds or long recovery tickets.</li>
<li>Always confirm contact addresses for the network you’ll be using for deposit and withdrawal in both exchanges.</li>
<li>Some coins exist on multiple chains with different contract addresses.</li>
<li>Deposits may need memos (e.g., XRP, XLM); forgetting them delays credit.</li>
<li>Check estimated arrival on the withdrawing exchange, along with fees and network.</li>
<li>Trading fee at the buy exchange. (Spot — usually 0.1–0.4%)</li>
<li>Withdrawal/Network Fee from buy exchange. (Confirm at exchange)</li>
<li>Trading fee at the sell exchange. (Spot — usually 0.1–0.4%)</li>
</ul><div style="margin-top:6px;"><em>We do not provide guaranteed profit, we only help you spot opportunities, and how to utilize them.</em></div></div>`;}
const FORCE_TRADE_TEXT=disclaimerHTML();
function renderTradeDetails(){const box=qs('#tradeContent'); if(box) box.innerHTML=FORCE_TRADE_TEXT;}

/* ====== SERVER CLOCK: AFRICA/LAGOS (no seconds) ====== */
let baseServerMs=null, baseClientMs=null, clockTicker=null;
function ensureLastBox(){let b=qs('#lastBox'), t=qs('#lastUTCtime'); if(!b){const br=qs('.brandrow'); if(!br) return; b=document.createElement('div'); b.className='lastbox'; b.id='lastBox'; b.innerHTML=`<span class="lastlabel">Last updated</span><span id="lastUTCtime" class="lasttime">--:-- AM</span><span id="oppCount" class="oppcount">· -- possible opportunities</span>`; br.appendChild(b); t=qs('#lastUTCtime');} return t||qs('#lastUTCtime');}
function fmtLagos(d){
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone:'Africa/Lagos',
    year:'numeric', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', hour12:true
  }).formatToParts(d);
  const map = Object.fromEntries(parts.map(p => [p.type, p.value]));
  return `${map.year}-${map.month}-${map.day} ${map.hour}:${map.minute} ${String(map.dayPeriod||'').toUpperCase()}`;
}
function setLastUpdatedUTCFromISO(iso){
  const el=ensureLastBox(); if(!el) return;
  const d=new Date(iso); baseServerMs=d.getTime(); baseClientMs=Date.now();
  el.textContent=fmtLagos(d);
  if(clockTicker) clearInterval(clockTicker);
  clockTicker=setInterval(()=>{
    if(baseServerMs==null||baseClientMs==null) return;
    const now=new Date(baseServerMs + (Date.now()-baseClientMs));
    el.textContent=fmtLagos(now);
  },1000);
}
function setOppCount(n){const el=qs('#oppCount'); if(!el) return; const num = (typeof n==='number' && isFinite(n)) ? n : 0; el.textContent = `· ${num} possible opportunities`; }
async function seedServerTime(){try{const r=await fetch('/time',{cache:'no-store'}); const j=await r.json(); if(j&&j.serverTimeUTC) setLastUpdatedUTCFromISO(j.serverTimeUTC);}catch(_){}}

function exLogoSrc(ex){
  const u = (window._logoUrl && window._logoUrl[ex]) || null;
  if(u) return u;
  const dom = window._logoDom && window._logoDom[ex];
  return dom ? `https://logo.clearbit.com/${dom}` : '';
}

let _progRAF=null;
const PROG_CAP=0.97;
function setBar(p){const f=qs('#loadBarFill'); if(!f) return; const clamped=Math.max(0, Math.min(1, p)); f.style.transform=`scaleX(${clamped})`;}
function startProgress(){cancelProgress(); setBar(0); const start=performance.now(); const speed=480; function tick(now){const t=now - start; const p = 1 - Math.exp(-t / speed); setBar(Math.min(PROG_CAP, p)); _progRAF = requestAnimationFrame(tick);} _progRAF = requestAnimationFrame(tick);}
function completeProgressThen(cb){cancelProgress(); setBar(1); setTimeout(()=>{ if(typeof cb==='function') cb(); setBar(0); }, 140);}
function cancelProgress(){if(_progRAF){ cancelAnimationFrame(_progRAF); _progRAF=null; }}
function showOverlay(){const o=qs('#loadOverlay'); if(!o) return; o.classList.remove('hidden'); startProgress();}
function hideOverlay(){const o=qs('#loadOverlay'); if(!o) return; completeProgressThen(()=>{ o.classList.add('hidden'); });}

const FILTERS_KEY='arbexa_filters_v1';
function collectFilters(){
  const get = id => qs('#'+id)?.value ?? '';
  const exSel = qsa('input[name="ex"]').filter(cb=>cb.checked).map(cb=>cb.value);
  return { minEdge:get('minEdge'), maxEdge:get('maxEdge'), q:get('q'), minVol:get('minVol'),
           minLiq:get('minLiq'), tsMin:get('tsMin'), tsMax:get('tsMax'), ex: exSel };
}
function saveFilters(){ try{ localStorage.setItem(FILTERS_KEY, JSON.stringify(collectFilters())); }catch(e){} }
function applyFiltersToUI(f){
  if(!f) return;
  const set = (id,val)=>{ const el=qs('#'+id); if(el!=null && val!=null && val!=='') el.value=val; };
  set('minEdge',f.minEdge); set('maxEdge',f.maxEdge); set('q',f.q);
  set('minVol',f.minVol); set('minLiq',f.minLiq); set('tsMin',f.tsMin); set('tsMax',f.tsMax);
  if(Array.isArray(f.ex) && f.ex.length>0){ qsa('input[name="ex"]').forEach(cb=>cb.checked = f.ex.includes(cb.value)); }
}
function wireFilterAutosave(){
  ['minEdge','maxEdge','q','minVol','minLiq','tsMin','tsMax'].forEach(id=>{
    const el=qs('#'+id); if(el){ el.addEventListener('change', saveFilters); }
  });
  qsa('input[name="ex"]').forEach(cb=>cb.addEventListener('change', saveFilters));
}
window.addEventListener('beforeunload', ()=>{ try{ saveFilters(); sessionStorage.setItem('arbexa_manual_reload','1'); }catch(e){} });
(function(){
  let manual=false;
  try{ manual = sessionStorage.getItem('arbexa_manual_reload')==='1'; sessionStorage.removeItem('arbexa_manual_reload'); }catch(e){}
  window._wasManualReload = manual;
  if(manual){ showOverlay(); }
})();

/* Persist per-row expanders */
const EXPAND_KEY='arbexa_expand_v1';
function readExpandStore(){ try{ return JSON.parse(localStorage.getItem(EXPAND_KEY)||'{}'); }catch(e){ return {}; } }
function writeExpandStore(s){ try{ localStorage.setItem(EXPAND_KEY, JSON.stringify(s)); }catch(e){} }
function applySavedExpandState(){
  const store=readExpandStore(); let applied=false;
  document.querySelectorAll('tr[data-sym]').forEach(tr=>{
    const sym=tr.dataset.sym; const st=store[sym]; if(!st) return;
    const ds=tr.querySelectorAll('.cell-ob details');
    if(ds[0]) ds[0].open = !!st[0];
    if(ds[1]) ds[1].open = !!st[1];
    applied = applied || !!st[0] || !!st[1];
  });
  document.querySelectorAll('.card[data-sym]').forEach(card=>{
    const sym=card.dataset.sym; const st=store[sym]; if(!st) return;
    const ds=card.querySelectorAll('details');
    if(ds[0]) ds[0].open = !!st[0];
    if(ds[1]) ds[1].open = !!st[1];
    applied = applied || !!st[0] || !!st[1];
  });
  return applied;
}
function wireExpandPersistence(){
  const store=readExpandStore();
  const attach=(rootSel, detailsSel)=>{
    document.querySelectorAll(rootSel).forEach(root=>{
      const sym=root.getAttribute('data-sym'); if(!sym) return;
      const ds=root.querySelectorAll(detailsSel);
      ds.forEach((d,i)=>{
        d.addEventListener('toggle', ()=>{
          const cur = store[sym] || [false,false];
          cur[i] = d.open;
          store[sym] = cur;
          writeExpandStore(store);
        });
      });
    });
  };
  attach('tr[data-sym]', '.cell-ob details');
  attach('.card[data-sym]', 'details');
}

/* Persist top bar dropdown open/close */
const DD_KEY='arbexa_dd_v1';
function applySavedDD(){
  try{
    const s = JSON.parse(localStorage.getItem(DD_KEY)||'{}')||{};
    ['menuDD','settingsDD','socialsDD','tncDD','profileDD','tradeDD','msgDD'].forEach(id=>{
      const el=document.getElementById(id); if(el && typeof s[id]==='boolean') el.open = s[id];
    });
  }catch(_){}
}
function wireDDSave(){
  try{
    const s = JSON.parse(localStorage.getItem(DD_KEY)||'{}')||{};
    ['menuDD','settingsDD','socialsDD','tncDD','profileDD','tradeDD','msgDD'].forEach(id=>{
      const el=document.getElementById(id); if(!el) return;
      el.addEventListener('toggle', ()=>{ s[id]=el.open; try{ localStorage.setItem(DD_KEY, JSON.stringify(s)); }catch(_){} });
    });
  }catch(_){}
}

function obTable(title, rows, isAsks){
  if(!rows||rows.length===0){return `<div style="margin-top:8px"><div class="badge">${title}</div><div class="mononu" style="margin-top:6px">No levels</div></div>`;}
  const lines=rows.map(([p,a])=>{const usd=(p&&a)?(p*a):0; return `<tr><td>${numFull(p)}</td><td>${numFull(a)}</td><td class="mononu">${usdFull(usd)}</td></tr>`;}).join('');
  const cls=isAsks?'obtbl ob-ask':'obtbl ob-bid'; const subtitle=isAsks?'Asks (buy levels)':'Bids (sell levels)';
  return `<div style="margin-top:8px"><div class="badge">${title} — ${subtitle}</div><table class="${cls}"><thead><tr><th>Price $</th><th>Qty</th><th>≈ USD</th></tr></thead><tbody>${lines}</tbody></table></div>`;
}
function obHTML(r){return obTable('🛒 '+cap(r.buy_ex)+' '+r.symbol,r.ob_buy_asks,true)+obTable('💸 '+cap(r.sell_ex)+' '+r.symbol,r.ob_sell_bids,false);}

function render(rows){
  rows.sort((a,b)=>b.edge-a.edge);
  const tb=qs('#opptable tbody'), cards=qs('#cards');
  if(!rows||rows.length===0){
    const msg=`<div class="emptymsg"><em><strong>Filter too strict, try default settings for opportunities.</strong></em></div>`;
    tb.innerHTML=`<tr><td colspan="12">${msg}</td></tr>`; cards.innerHTML=`<div class="card">${msg}</div>`;
    setOppCount(0);
    return;
  }
  setOppCount(rows.length);
  tb.innerHTML=rows.map(r=>{
    const edgeClass=(r.edge>=10?'good':(r.edge>=5?'warn':''));
    const sugg=(r.sugg_hi>0)?`${usdFull(r.sugg_lo)} – ${usdFull(r.sugg_hi)}`:'n/a';
    const lb = exLogoSrc(r.buy_ex)  ? `<img class="exlogo" src="${exLogoSrc(r.buy_ex)}" onerror="this.style.display='none'">` : '';
    const ls = exLogoSrc(r.sell_ex) ? `<img class="exlogo" src="${exLogoSrc(r.sell_ex)}" onerror="this.style.display='none'">` : '';
    const drops=`<div class="stack"><details><summary>📚 Orderbooks (15)</summary>${obHTML(r)}</details><details><summary>📊 Volume &amp; Disclaimer</summary>${disclaimerHTML()}</details></div>`;
    return `<tr data-sym="${r.symbol}">
      <td class="mononu"><span class="badge">🔁 ${r.symbol}</span></td>
      <td class="mononu edge ${edgeClass}">${r.edge.toFixed(2)}%</td>
      <td><span class="badge">🛒 ${lb} ${cap(r.buy_ex)}</span><div class="mononu">${usdFull(r.buy_ask)}</div></td>
      <td><span class="badge">💸 ${ls} ${cap(r.sell_ex)}</span><div class="mononu">${usdFull(r.sell_bid)}</div></td>
      <td class="mononu">$${Number(r.qv_buy||0).toLocaleString()} / $${Number(r.qv_sell||0).toLocaleString()}</td>
      <td class="mononu"><span title="Higher score reflects stronger 24h volume and tighter depth near the top price.">${r.liquidity}</span></td>
      <td class="mononu">${sugg}<div><small class="muted">Example guidance only</small></div></td>
      <td class="mononu">${usdFull(r.example_profit_1000)} <small class="muted">(on $1000)</small></td>
      <td class="mononu">${usdFull(r.price)}</td>
      <td class="mononu">${usdFull(r.buy_ask)}</td>
      <td class="mononu">${usdFull(r.sell_bid)}</td>
      <td class="cell-ob">${drops}</td></tr>`;
  }).join('');

  cards.innerHTML=rows.map(r=>{
    const sugg=(r.sugg_hi>0)?`${usdFull(r.sugg_lo)} – ${usdFull(r.sugg_hi)}`:'n/a';
    const lb = exLogoSrc(r.buy_ex)  ? `<img class="exlogo" src="${exLogoSrc(r.buy_ex)}" onerror="this.style.display='none'">` : '';
    const ls = exLogoSrc(r.sell_ex) ? `<img class="exlogo" src="${exLogoSrc(r.sell_ex)}" onerror="this.style.display='none'">` : '';
    return `<div class="card" data-sym="${r.symbol}">
      <h3>🔁 ${r.symbol} · <span class="edge">${r.edge.toFixed(2)}%</span></h3>
      <div class="kv">
        <div>🛒 Buy: ${lb} ${cap(r.buy_ex)} @ ${usdFull(r.buy_ask)}</div>
        <div>💸 Sell: ${ls} ${cap(r.sell_ex)} @ ${usdFull(r.sell_bid)}</div>
        <div>💧 Vol: $${Number(r.qv_buy||0).toLocaleString()} / $${Number(r.qv_sell||0).toLocaleString()}</div>
        <div><span title="Higher score reflects stronger 24h volume and tighter depth near the top price.">🧪 Liq score:</span> ${r.liquidity}</div>
        <div>💡 Suggest: ${sugg}</div>
        <div>🤑 ${usdFull(r.example_profit_1000)} on $1000</div>
        <div>💵 Price: ${usdFull(r.price)}</div>
      </div>
      <details class="details"><summary>📚 Orderbooks (15)</summary>${obHTML(r)}</details>
      <details class="details" style="margin-top:6px"><summary>📊 Volume &amp; Disclaimer</summary>${disclaimerHTML()}</details>
    </div>`;
  }).join('');

  wireExpandPersistence();
}

async function buildExTables(){
  const res=await fetch('/logos',{cache:'no-store'}); const data=await res.json();
  window._logoDom = data.logos || {};
  window._logoUrl = data.logo_urls || {};
  const all = (data.exchanges||[]).slice(0);
  const half = Math.ceil(all.length/2);
  const col1 = all.slice(0,half);
  const col2 = all.slice(half);

  function rows(of){
    return of.map(ex=>{
      return `<tr><td><label><input type="checkbox" name="ex" value="${ex}" checked>
        <img class="exlogo" src="${exLogoSrc(ex)}" onerror="this.style.display='none'">
        ${ex.charAt(0).toUpperCase()+ex.slice(1)}</label></td></tr>`;
    }).join('');
  }

  const t1=qs('#extable1'), t2=qs('#extable2');
  if(t1) t1.innerHTML = rows(col1);
  if(t2) t2.innerHTML = rows(col2);

  ['minEdge','maxEdge','q','minVol','minLiq','tsMin','tsMax'].forEach(id=>{
    const el=qs('#'+id); if(el){ el.addEventListener('change', saveFilters); }
  });
  qsa('input[name="ex"]').forEach(cb=>cb.addEventListener('change', saveFilters));

  qs('#exAll').onclick=()=>{qsa('input[name="ex"]').forEach(cb=>cb.checked=true); saveFilters();};
  qs('#exNone').onclick=()=>{qsa('input[name="ex"]').forEach(cb=>cb.checked=false); saveFilters();};

  qs('#exApply').onclick=async()=>{
    playSfx('tap');
    saveFilters();
    showOverlay();
    await load(false);
    hideOverlay();
    const dd=qs('#settingsDD'); if(dd) dd.open=false;
  };

  const defBtn = qs('#exDefault');
  if(defBtn){
    defBtn.onclick = async ()=>{
      playSfx('tap');
      qsa('input[name="ex"]').forEach(cb=>cb.checked=true);
      ['q','minVol','minLiq','tsMin','tsMax'].forEach(id=>{ const el=qs('#'+id); if(el) el.value=''; });
      const minE = qs('#minEdge'), maxE = qs('#maxEdge');
      if(minE) minE.value = '1';
      if(maxE) maxE.value = '26';
      saveFilters();
      showOverlay();
      await load(false);
      hideOverlay();
      const dd=qs('#settingsDD'); if(dd) dd.open=false;
    };
  }

  // ✨ NEW: wire Change Password button
  const cpBtn = qs('#exChangePassword');
  if(cpBtn){
    cpBtn.onclick = ()=>{
      playSfx('tap');
      const m = qs('#cpModal');
      if(m) m.classList.remove('hidden');
    };
  }

  const soundBtn = qs('#soundToggle');
  if(soundBtn){
    function paint(){ const on=soundEnabled(); soundBtn.textContent = on ? '🔊 Sound: ON' : '🔈 Sound: OFF'; soundBtn.setAttribute('aria-pressed', on?'true':'false'); }
    soundBtn.addEventListener('click', ()=>{ const on=soundEnabled(); try{ localStorage.setItem(SOUND_KEY, on?'0':'1'); }catch(_){ } paint(); playSfx('tap'); });
    paint();
  }

  try{ const saved = JSON.parse(localStorage.getItem(FILTERS_KEY)||'null'); if(saved) applyFiltersToUI(saved); }catch(_){}
}

/* AUTH helpers on /opps */
function getToken(){ try{ return localStorage.getItem('arbexa_token')||''; }catch(_) { return ''; } }
function setToken(t){ try{ if(t) localStorage.setItem('arbexa_token', t); else localStorage.removeItem('arbexa_token'); }catch(_){ } }
async function apiLogin(email, password){
  const form = new URLSearchParams({ username: email, password: password });
  const r = await fetch('/auth/login', { method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body: form });
  const j = await r.json().catch(()=>({}));
  if(!r.ok || !j.access_token){ throw new Error(j.detail||('Login failed ('+r.status+')')); }
  return j.access_token;
}
function updateAuthUI(){
  const has = !!getToken();
  const bS = qs('#btnSignup'), bL = qs('#btnLogin'), bO = qs('#btnLogout');
  if(bS) bS.style.display = has ? 'none' : 'inline-flex';
  if(bL) bL.style.display = has ? 'none' : 'inline-flex';
  if(bO) bO.style.display = has ? 'inline-flex' : 'none';
}
function wireAuthButtons(){
  const bS = qs('#btnSignup'), bL = qs('#btnLogin'), bO = qs('#btnLogout');
  if(bS){
    bS.addEventListener('click', async ()=>{
      const email = prompt('Enter Gmail to sign up:'); if(!email) return;
      const pw = prompt('Enter a password (min 6 chars):'); if(!pw) return;
      const un = prompt('Choose a username (lowercase letters and digits, 3–32 chars):'); if(!un) return;
      const rec = prompt('Enter a recovery sentence (>=20 chars; lowercase/digits/spaces only):'); if(!rec) return;
      try{
        playSfx('tap');
        showOverlay();
        const r = await fetch('/auth/signup', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ email: email.toLowerCase(), password: pw, username: (un||'').toLowerCase(), recovery: (rec||'').toLowerCase() })
        });
        const j = await r.json().catch(()=>({}));
        if(!r.ok || !j.access_token) throw new Error(j.detail||('Signup failed ('+r.status+')'));
        setToken(j.access_token); updateAuthUI();
        alert('Signed up & logged in!');
        await load(true);
      }catch(e){ alert(e.message||String(e)); }
      hideOverlay();
    });
  }
  if(bL){
    bL.addEventListener('click', async ()=>{
      const email = prompt('Enter your Gmail:'); if(!email) return;
      const pw = prompt('Enter your password:'); if(!pw) return;
      try{
        playSfx('tap');
        showOverlay();
        const tok = await apiLogin(email.toLowerCase(), pw);
        setToken(tok); updateAuthUI();
        alert('Logged in!');
        await load(true);
      }catch(e){ alert(e.message||String(e)); }
      hideOverlay();
    });
  }
  if(bO){
    bO.addEventListener('click', ()=>{
      setToken(''); updateAuthUI();
      alert('Logged out.');
      location.replace('/');
    });
  }
}
function authFetch(url, opts={}){
  const t = getToken();
  const hdrs = Object.assign({}, opts.headers||{});
  if(t) hdrs['Authorization'] = 'Bearer ' + t;
  return fetch(url, {...opts, headers: hdrs});
}

/* ✨ NEW: Change Password modal wiring */
(function(){
  const modal = qs('#cpModal');
  if(!modal) return;
  const cancel = qs('#cpCancel');
  const submit = qs('#cpSubmit');

  function close(){ modal.classList.add('hidden'); }
  if(cancel) cancel.onclick = close;

  async function doChange(){
    const cur = (qs('#cpCur')?.value||'').trim();
    const nw  = (qs('#cpNew')?.value||'').trim();
    const nw2 = (qs('#cpNew2')?.value||'').trim();
    if(!cur || !nw || !nw2){ alert('Complete all fields.'); return; }
    if(nw.length < 6){ alert('New password too short (min 6).'); return; }
    if(nw !== nw2){ alert('New passwords do not match.'); return; }
    try{
      showOverlay();
      const r = await authFetch('/auth/change-password', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ current_password: cur, new_password: nw, confirm_new_password: nw2 })
      });
      const j = await r.json().catch(()=>({}));
      if(!r.ok || !j.ok){ throw new Error(j.detail||('Change failed ('+r.status+')')); }
      alert('Password changed successfully.');
      close();
    }catch(e){
      alert(e.message||String(e));
    }finally{
      hideOverlay();
    }
  }
  if(submit) submit.onclick = doChange;
})();

/* Profile card */
function fmtDateNice(iso){
  try{
    const d = new Date(iso);
    return d.toLocaleString(undefined, {year:'numeric', month:'short', day:'2-digit', hour:'2-digit', minute:'2-digit'});
  }catch(_){ return iso||'-'; }
}
function profileCardHTML(p){
  const rows = [
    ['Gmail', p.email || '-'],
    ['Username', p.username || '-'],
    ['Date joined', p.date_joined ? fmtDateNice(p.date_joined) : '-'],
    ['Status', (p.status||'free').toUpperCase()],
    ['User ID', (p.user_id!=null ? String(p.user_id) : '-')],
  ];
  const items = rows.map(([k,v])=>`<div style="display:flex;justify-content:space-between;gap:10px;padding:8px 10px;border:1px solid #182241;border-radius:10px;background:#0f1a33">
    <div style="color:#9fb2d9;font-weight:700">${k}</div>
    <div class="mononu" style="font-weight:800">${v}</div>
  </div>`).join('');
  return `<div style="display:grid;gap:8px"><div style="display:flex;align-items:center;gap:8px">
      <span class="badge">👤 Profile</span><span style="color:#b8c8e8">Card preview (read-only)</span></div>${items}</div>`;
}
async function loadProfileIntoCard(){
  const card = document.getElementById('profileCard');
  if(!card) return;
  try{
    const res = await authFetch('/me', {cache:'no-store'});
    if(res.status === 401){ card.innerHTML = `<div style="color:#ffcf5a">Please log in to view your profile.</div>`; return; }
    const p = await res.json();
    card.innerHTML = profileCardHTML(p);
    try{ localStorage.setItem('arbexa_profile', JSON.stringify(p)); }catch(_){}
  }catch(e){
    try{
      const cached = JSON.parse(localStorage.getItem('arbexa_profile')||'null');
      card.innerHTML = cached ? profileCardHTML(cached) : `<div style="color:#ff6b6b">Failed to load profile.</div>`;
    }catch(_){
      card.innerHTML = `<div style="color:#ff6b6b">Failed to load profile.</div>`;
    }
  }
}

/* Manual data load */
async function load(auto=true){
  const p={minEdge:parseFloat(qs('#minEdge').value||'1'),maxEdge:parseFloat(qs('#maxEdge').value||'25'),
           q:(qs('#q').value||'').trim(),minVol:parseFloat(qs('#minVol').value||'0'),minLiq:parseFloat(qs('#minLiq').value||'0'),
           tsMin:parseFloat(qs('#tsMin').value||'0'),tsMax:parseFloat(qs('#tsMax').value||'0'),
           ex:Array.from(document.querySelectorAll('input[name="ex"]:checked')).map(x=>x.value).join(',')};
  const qsParams=new URLSearchParams(p); qsParams.set('_ts',String(Date.now()));
  rememberUI();
  try{
    const res=await authFetch('/data?'+qsParams.toString(),{cache:'no-store'});
    if(res.status===401){ location.replace('/'); return; }
    const data=await res.json();
    render(data.rows||[]);
    const usedEphemeral = restoreUI();
    if(!usedEphemeral){ applySavedExpandState(); }
    if(data.serverTimeUTC) setLastUpdatedUTCFromISO(data.serverTimeUTC);
  }catch(e){
    setTimeout(hideOverlay, 1200);
  }
}

/* Reminder modal */
function maybeShowReminder(){
  const now=Date.now(); const until=parseInt(localStorage.getItem('tradeDetailsReminderUntil')||'0',10);
  if(until && now<until) return;
  const modal=qs('#remindModal'); if(!modal) return;
  const ok=qs('#remindOk'), skip=qs('#remindSkip');
  modal.classList.remove('hidden');
  ok.onclick=()=>{localStorage.setItem('tradeDetailsReminderUntil',String(Date.now()+3600000)); modal.classList.add('hidden');};
  skip.onclick=()=>{localStorage.setItem('tradeDetailsReminderUntil',String(Date.now()+24*3600000)); modal.classList.add('hidden');};
}

/* T&C button */
function initTnC(){
  const btn = qs('#agreeBtn'); if(!btn) return;
  const KEY='tncAgreed';
  function paint(){ btn.textContent = (localStorage.getItem(KEY)==='1') ? 'Agreed ✓' : 'Agreed?'; }
  btn.addEventListener('click', ()=>{ localStorage.setItem(KEY, localStorage.getItem(KEY)==='1' ? '0' : '1'); paint(); });
  paint();
}

/* Message dropdown label toggle */
(function(){
  const dd = document.getElementById('msgDD');
  const sum = document.getElementById('msgSummary');
  if(dd && sum){ dd.addEventListener('toggle', ()=>{ sum.textContent = dd.open ? '📨Opened' : '📩Message'; }); }
})();

/* Visibility refresh */
document.addEventListener('visibilitychange',()=>{ if(!document.hidden){ load(true); }});

/* Wire buttons + profile toggle */
document.addEventListener('DOMContentLoaded', ()=>{
  const btn = qs('#refreshNow');
  if(btn){ btn.addEventListener('click', async ()=>{ playSfx('tap'); showOverlay(); await load(false); hideOverlay(); }); }
  wireAuthButtons(); updateAuthUI();

  const prof = document.getElementById('profileDD');
  if(prof){
    prof.addEventListener('toggle', ()=>{
      if(prof.open){ loadProfileIntoCard(); }
    });
  }

  applySavedDD(); wireDDSave();
});

/* Boot */
buildExTables().then(async ()=>{
  renderTradeDetails(); initExtDoc(); initTnC(); maybeShowReminder(); seedServerTime();
  wireFilterAutosave();
  await load(true);
  hideOverlay();
});
setInterval(()=>load(true),10000);
</script>

<button id="chatFab" class="chatfab" title="Open chat" aria-label="Open chat"><span class="dot">💬</span></button>
<script>
(function(){
  const fab=document.getElementById('chatFab'); if(!fab) return;
  let dragging=false,sx=0,sy=0,left=0,top=0;
  function setPos(x,y){ fab.style.right='auto'; fab.style.bottom='auto'; fab.style.left=x+'px'; fab.style.top=y+'px'; }
  function down(e){ dragging=true; const r=fab.getBoundingClientRect(); left=r.left; top=r.top;
    sx=(e.touches?e.touches[0].clientX:e.clientX); sy=(e.touches?e.touches[0].clientY:e.clientY); e.preventDefault(); }
  function move(e){ if(!dragging) return; const x=(e.touches?e.touches[0].clientX:e.clientX), y=(e.touches?e.touches[0].clientY:e.clientY); setPos(left+(x-sx), top+(y-sy)); }
  function up(){ dragging=false; }
  fab.addEventListener('mousedown', down); fab.addEventListener('touchstart', down, {passive:false});
  window.addEventListener('mousemove', move); window.addEventListener('touchmove', move, {passive:false});
  window.addEventListener('mouseup', up); window.addEventListener('touchend', up);
  fab.addEventListener('click', function(){ if(dragging){ dragging=false; return; }
    try{ const t=localStorage.getItem('arbexa_token'); if(!t){ location.href='/login'; return; } }catch(_){}
    location.href='/chat'; });
})();
</script>

<script>
(function(){
  try{
    var prof=null; try{prof=JSON.parse(localStorage.getItem('arbexa_profile')||'null');}catch(_){}
    var s = (prof && (prof.status||prof.plan)) ? String(prof.status||prof.plan).toLowerCase() : 'free';
    var isPro = s.includes('pro') || s.includes('premium') || s==='paid';
    var el = document.getElementById('freeBanner'); if(el){ if(isPro) el.classList.add('hide'); else el.classList.remove('hide'); }
  }catch(e){}
})();
</script>
</body></html>
"""
@app.get("/opps", response_class=HTMLResponse)
def opps_page():
    return HTMLResponse(_OPPS_HTML)

@app.get("/logos", response_class=JSONResponse)
def logos():
    return JSONResponse({"exchanges": EXCHANGE_IDS, "logos": EX_LOGO_DOMAIN, "logo_urls": EX_LOGO_URL})
# Server-side protection: /data requires valid bearer token
from fastapi import status

@app.get("/data", response_class=JSONResponse)
def data(request: Request,
         minEdge: float = EDGE_MIN, maxEdge: float = EDGE_MAX,
         q: str = "", minVol: float = 0.0, minLiq: int = 0,
         tsMin: float = 0.0, tsMax: float = 0.0, ex: str = "",
         db: Session = Depends(get_db)):
    # --- SETTINGS persist (safe/fallbacks) ---
    try:
        qp = request.query_params
    
        # ensure current user for settings persist
        u = current_user_from_auth_header(request, db)
        def _num(name, default=None):
            v = qp.get(name)
            if v is None or v == "":
                return default
            try:
                return float(v)
            except Exception:
                return default
    
        # exchanges (ex=binance,bybit,...)
        ex_param = qp.get("ex") or ""
        ex_list = [e.strip() for e in ex_param.split(",") if e.strip()]  # ensure defined
    
        # resolve/create PROFILES.id for this user
        supa_uid = _sb_get_or_create_profile_id(
            email=u.email,
            username=getattr(u, "username", None)
        )
    
        if supa_uid:
            row = {
                "user_id": supa_uid,
                "edge_pct_min": _num("minEdge", 1.0),
                "volume_24h_usd_min": _num("minVol", 0.0),
                "liquidity_min": _num("minLiq", 0.0),
                "pair_filter": qp.get("q") or None,
                "trade_size_min_usd": _num("tsMin", None),
                "trade_size_max_usd": _num("tsMax", None),
                "sound_on": True,
                "exchanges_enabled": ex_list,
                "updated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            try:
                _sb_enqueue("SETTINGS", row)
            except Exception as _e:
                print("[supabase] SETTINGS enqueue error:", _e)
            try:
                _sb_post_rows("SETTINGS", [row])
            except Exception as _e:
                print("[supabase] SETTINGS direct post error:", _e)
        else:
            print("[supabase] SETTINGS persist via /data skip: no user_id")
    
    except Exception as _e:
        print("[supabase] SETTINGS persist via /data error:", _e)

    user = current_user_from_auth_header(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    ex_filter = set([e for e in ex.split(",") if e]) if ex else set()
    with lock:
        rows = list(opps_cache.values())
    qlow = q.lower().strip()
    out = []
    for r in rows:
        if r["edge"] < minEdge or r["edge"] > maxEdge: continue
        if qlow and qlow not in r["symbol"].lower(): continue
        if minVol > 0 and (r["qv_buy"] < minVol or r["qv_sell"] < minVol): continue
        if r["liquidity"] < minLiq: continue
        if ex_filter and (r["buy_ex"] not in ex_filter or r["sell_ex"] not in ex_filter): continue
        if (tsMin > 0 or tsMax > 0) and (r["sugg_lo"] <= 0 or r["sugg_hi"] <= 0): continue
        if tsMin > 0 and r["sugg_lo"] < tsMin: continue
        if tsMax > 0 and r["sugg_hi"] > tsMax: continue
        out.append({
            "symbol": r["symbol"], "price": r["price"], "buy_ex": r["buy_ex"], "sell_ex": r["sell_ex"],
            "buy_ask": r["buy_ask"], "sell_bid": r["sell_bid"], "edge": r["edge"],
            "qv_buy": r["qv_buy"], "qv_sell": r["qv_sell"], "liquidity": r["liquidity"],
            "sugg_lo": r["sugg_lo"], "sugg_hi": r["sugg_hi"], "example_profit_1000": r["example_profit_1000"],
            "ob_buy_asks": r["ob_buy_asks"], "ob_buy_bids": r["ob_buy_bids"],
            "ob_sell_asks": r["ob_sell_asks"], "ob_sell_bids": r["ob_sell_bids"],
        })
    out.sort(key=lambda x: x["edge"], reverse=True)
    server_time_utc_iso = dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return JSONResponse({"rows": out[:MAX_OPPS], "cycle": cycle_no, "summary": last_cycle_summary, "serverTimeUTC": server_time_utc_iso})

# ------------- CHAT API -------------
@app.get("/api/chat/messages", response_class=JSONResponse)
def list_chat_messages(request: Request, db: Session = Depends(get_db)):
    u = current_user_from_auth_header(request, db)
    if not u:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = db.query(Message).order_by(Message.created_at.asc()).limit(300).all()
    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "user_id": r.user_id,
            "username": r.username,
            "text": r.text,
            "created_at": (r.created_at or datetime.datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return JSONResponse({"ok": True, "messages": out})

@app.post("/api/chat/messages", response_class=JSONResponse)
def post_chat_message(data: ChatIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    u = current_user_from_auth_header(request, db)
    if not u:
        raise HTTPException(status_code=401, detail="Not authenticated")
    text_in = (data.text or "").strip()
    if not text_in:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    if len(text_in) > 2000:
        raise HTTPException(status_code=400, detail="Message too long (max 2000 chars).")

    uname = (u.username or "")
    if not uname:
        import re as _re
        base = (u.email or "").split("@", 1)[0].lower()
        uname = _re.sub(r"[^a-z0-9]", "", base) or "user"

    msg = Message(user_id=u.id, username=uname[:64], text=text_in[:2000])
    db.add(msg); db.commit(); db.refresh(msg)


    # --- Mirror to Supabase CHAT (best-effort) ---
    try:
        _pid = _sb_get_profile_id(u.email)
        if _pid:
            _sb_enqueue_and_post("CHAT", {
                "user_id": _pid,
                "room_id": "global",
                "content": text_in[:2000],
                "reply_to": None,
                "is_system": False,
                "meta": {"source": "local-chat", "local_message_id": msg.id, "username": msg.username}
            })
        else:
            print("[supabase] CHAT skip (no PROFILES UUID found for user)")
    except Exception as _e:
        print("[supabase] CHAT mirror error:", _e)
    return JSONResponse({
        "ok": True,
        "message": {
            "id": msg.id,
            "user_id": msg.user_id,
            "username": msg.username,
            "text": msg.text,
            "created_at": (msg.created_at or datetime.datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    })

CHAT_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arbexa — Chat</title>
<style>
:root{--bg:#0b1220;--card:#101a33;--txt:#e7eefc;--muted:#91a5cc;--acc:#2bd576;--line:#1a2547;--chip:#0e1a35;--danger:#ff6b6b}
*{box-sizing:border-box} html,body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu}
a{color:var(--acc);text-decoration:none}
.header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;padding:8px 10px;border-bottom:1px solid #132042;background:linear-gradient(180deg,#0b1220 85%,transparent)}
.back{display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 10px;border-radius:10px;border:1px solid #23345f;background:#0e1a35;color:#e7eefc;font-weight:800;cursor:pointer}
.back:hover{filter:brightness(1.06)}
.brand{margin-left:auto;display:flex;align-items:center;gap:8px}
.brand img{height:24px}
.wrap{display:grid;grid-template-rows:auto 1fr auto; height:calc(100svh);}
.rules{padding:10px 12px;background:#0e1a35;border-bottom:1px solid #23345f;color:#bcd2ff;font-size:12px}
.rules strong{color:#2bd576}
.list{overflow:auto;padding:10px 12px;display:grid;gap:10px}
.msg{display:grid;gap:4px;background:#101a33;border:1px solid #23345f;border-radius:12px;padding:8px 10px}
.meta{font-size:12px;color:#9fb2d9;display:flex;align-items:center;gap:8px;justify-content:space-between}
.text{white-space:pre-wrap;word-break:break-word}
.me{border-color:#2bd57644;background:#0f1f19}
.input{display:flex;gap:8px;align-items:flex-end;padding:10px;border-top:1px solid #132042;background:#0b1220}
.ta{flex:1;min-height:42px;max-height:140px;resize:vertical;padding:10px 12px;border-radius:10px;border:1px solid #23345f;background:#0e1a35;color:#e7eefc;font-weight:600}
.send{height:42px;padding:0 14px;border-radius:10px;border:1px solid #1b9d5b;background:#2bd576;color:#04120a;font-weight:900;cursor:pointer}
.send:hover{filter:brightness(1.07)}
.warn{color:var(--danger);font-size:12px;margin-left:10px}
</style>
</head><body>
<div class="wrap">
  <div class="header">
    <a class="back" href="/opps" aria-label="Back to opportunities">← Back</a>
    <div class="brand"><img src="/brandlogo" alt="Arbexa"><span style="font-weight:900;letter-spacing:.6px">Chat</span></div>
  </div>
  <div class="rules">
    <strong>Rules:</strong> Be respectful • No spam, scams, or financial advice claims • Keep messages on-topic • Admin may remove content and suspend access for abuse.
  </div>
  <div id="list" class="list" aria-live="polite" aria-busy="true"></div>
  <form id="form" class="input" autocomplete="off">
    <textarea id="ta" class="ta" placeholder="Type a message..." maxlength="2000" required></textarea>
    <button class="send" id="btnSend" type="submit">Send</button>
    <span id="warn" class="warn" style="display:none"></span>
  </form>
</div>

<script>
const LS_KEY='arbexa_chat_messages';
const LS_SYNC='arbexa_chat_sync';
function $(s){return document.querySelector(s)}
function token(){ try{ return localStorage.getItem('arbexa_token')||''; }catch(_){ return ''; } }
function fmt(ts){ try{ const d=new Date(ts); return d.toLocaleString(); }catch(_){ return ts; } }
function lsGet(){ try{ return JSON.parse(localStorage.getItem(LS_KEY)||'[]'); }catch(_){ return []; } }
function lsSet(arr){ try{ localStorage.setItem(LS_KEY, JSON.stringify(arr||[])); localStorage.setItem(LS_SYNC, String(Date.now())); }catch(_){ } }
function draw(messages){
  const list = $('#list');
  list.innerHTML = (messages||[]).map(m => {
    const me = (m.username||'').toLowerCase() === (window.meUser||'').toLowerCase();
    return `<div class="msg ${me ? 'me':''}">
      <div class="meta"><span><strong>${m.username||'user'}</strong></span><span>${fmt(m.created_at||'')}</span></div>
      <div class="text"></div>
    </div>`;
  }).join('');
  const els = list.querySelectorAll('.msg .text');
  (messages||[]).forEach((m,i)=>{ els[i].textContent = m.text || ''; });
  list.scrollTop = list.scrollHeight + 200;
}
async function loadMessages(){
  draw(lsGet());
  try{
    const r = await fetch('/api/chat/messages',{headers:{'Authorization':'Bearer '+token()}});
    const j = await r.json().catch(()=>({}));
    if(!r.ok || !j.ok){ return; }
    lsSet(j.messages||[]);
    draw(j.messages||[]);
  }catch(_){}
}
async function me(){
  try{
    const r = await fetch('/me',{headers:{'Authorization':'Bearer '+token()}});
    const j = await r.json().catch(()=>({}));
    if(r.ok){ window.meUser = j.username || ''; }
  }catch(_){}
}
async function sendMessage(ev){
  ev.preventDefault();
  const ta = $('#ta'); const warn = $('#warn');
  const txt = (ta.value||'').trim();
  if(!txt){ warn.textContent='Write something first.'; warn.style.display='inline'; return; }
  warn.style.display='none';
  try{
    const r = await fetch('/api/chat/messages', {
      method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+token()},
      body: JSON.stringify({text: txt})
    });
    const j = await r.json().catch(()=>({}));
    if(!r.ok || !j.ok){ alert(j.detail||('Send failed ('+r.status+')')); return; }
    const arr = lsGet(); arr.push(j.message); lsSet(arr);
    ta.value='';
    draw(arr);
  }catch(e){ alert(e.message||String(e)); }
}
document.addEventListener('DOMContentLoaded', async ()=>{
  try{ if(!token()){ location.href='/login'; return; } }catch(_){}
  await me();
  await loadMessages();
  setInterval(loadMessages, 6000);
  document.getElementById('form').addEventListener('submit', sendMessage);
});
</script>
</body></html>"""

@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    return HTMLResponse(CHAT_HTML)


# ---------- PAYMENTS endpoints (minimal insert to Supabase) ----------
from typing import Optional as _Optional

class PaymentIn(BaseModel):
    plan_name: str
    plan_duration_days: int
    amount: float
    currency: str = "USD"
    note: _Optional[str] = None

def _payments_insert_row(u, payload: dict):
    # Builds and inserts a PAYMENTS row linked to Supabase PROFILES.user_id
    supa_uid = _sb_get_or_create_profile_id(email=u.email, username=getattr(u, "username", None))
    if not supa_uid:
        raise HTTPException(status_code=400, detail="Could not resolve Supabase user_id")

    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {
        "user_id": supa_uid,
        "plan_name": payload.get("plan_name"),
        "plan_duration_days": int(payload.get("plan_duration_days") or 0),
        "amount": float(payload.get("amount") or 0.0),
        "currency": (payload.get("currency") or "USD").upper(),
        "status": "pending",
        "provider": "cryptocloud",
        "provider_invoice_id": None,
        "started_at": now,
        "paid_at": None,
        "expires_at": None,
        "note": payload.get("note"),
        "meta": {"source": "api.payments.create"},
        "sr_invoice_id": None,
    }
    try:
        _sb_enqueue("PAYMENTS", row)
    except Exception as _e:
        print("[supabase] PAYMENTS enqueue error:", _e)
    try:
        _sb_post_rows("PAYMENTS", [row])
    except Exception as _e:
        print("[supabase] PAYMENTS direct post error:", _e)
    return row

@app.post("/api/payments/create", response_class=JSONResponse)
def payments_create(data: PaymentIn, request: Request, db: Session = Depends(get_db)):
    u = current_user_from_auth_header(request, db)
    if not u:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if (data.amount or 0) <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")
    row = _payments_insert_row(u, data.dict())
    return JSONResponse({"ok": True, "saved": True, "payment": row})

@app.get("/api/payments", response_class=JSONResponse)
def payments_list(request: Request, db: Session = Depends(get_db)):
    u = current_user_from_auth_header(request, db)
    if not u:
        raise HTTPException(status_code=401, detail="Not authenticated")
    supa_uid = _sb_get_or_create_profile_id(email=u.email, username=getattr(u, "username", None))
    if not supa_uid or not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return JSONResponse({"items": []})

    try:
        base = SUPABASE_URL.rstrip("/") + "/rest/v1/PAYMENTS"
        params = f"?select=*&user_id=eq.{supa_uid}&order=created_at.desc&limit=25"
        hdrs = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Accept": "application/json",
        }
        r = requests.get(base + params, headers=hdrs, timeout=10)
        if r.status_code == 200 and r.headers.get("content-type","").startswith("application/json"):
            arr = r.json() or []
            return JSONResponse({"items": arr})
        print("[supabase] PAYMENTS fetch failed:", r.status_code, r.text[:200])
        return JSONResponse({"items": []})
    except Exception as _e:
        print("[supabase] PAYMENTS fetch error:", _e)
        return JSONResponse({"items": []})

