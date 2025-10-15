"""
sb_test_insert.py — Minimal Supabase PostgREST insert test

Usage (PowerShell or CMD in same folder):
  python sb_test_insert.py
Requires env vars:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""
import os, sys, json, time
import requests

URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or ""

if not URL or not KEY:
    print("[ERROR] Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment.")
    sys.exit(2)

row = {
    "pair": "TEST/USDT",
    "base": "TEST",
    "quote": "USDT",
    "exchange_buy": "debug_buy",
    "exchange_sell": "debug_sell",
    "price_buy": 1.11,
    "price_sell": 1.23,
    "spread_pct": 1.08,
    "volume_24h_usd": 98765.0,
    "liquidity": 7.7,
    "est_profit_usd": 10.8,
    "raw": {"src": "sb_test_insert.py", "t": time.time()}
}

headers = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

try:
    resp = requests.post(f"{URL}/rest/v1/opportunities", headers=headers, data=json.dumps([row]), timeout=15)
    print("HTTP", resp.status_code)
    if resp.status_code >= 300:
        print("Body:", resp.text[:500])
        sys.exit(1)
    print("OK: Inserted 1 row into opportunities.")
except Exception as e:
    print("Request error:", type(e).__name__, e)
    sys.exit(3)
