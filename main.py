import os
import logging
import uuid
import hashlib
import secrets
from datetime import datetime, date, timezone, timedelta
from typing import Optional, Dict
from collections import defaultdict

from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, status, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer
from supabase import create_client, Client
from hl7apy.parser import parse_message
from fhir.resources.patient import Patient
from fhir.resources.encounter import Encounter
from fhir.resources.bundle import Bundle, BundleEntry, BundleEntryRequest
from fhir.resources.humanname import HumanName
from fhir.resources.identifier import Identifier
from dotenv import load_dotenv
import stripe

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("fhir-interop")

app = FastAPI(title="FHIR Interop Engine", version="4.0.0", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Config
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ADMIN_KEY = os.getenv("ADMIN_KEY", "")
ADMIN_CRON_SECRET = os.getenv("ADMIN_CRON_SECRET", ADMIN_KEY)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_...")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_...")
stripe.api_key = STRIPE_SECRET_KEY

PRICE_CREDIT_MAP = {
    "price_1U1xzUIUjoQtwpInIy0o6u1O": 100,
    "price_1U1xzUIUjoQtwpInNFq6TRCU": 10000,
    "price_1U1xzUIUjoQtwpInL8G3EBZX": 75000,
}

# Helpers
def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()

def api_key_hash_exists(key_hash: str) -> bool:
    try:
        resp = supabase.table("api_keys").select("key_hash").eq("key_hash", key_hash).execute()
        return len(resp.data) > 0
    except Exception as e:
        logger.error(f"Supabase error checking key hash: {e}")
        return False

def json_safe(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [json_safe(i) for i in obj]
    return obj

# FHIR transformation
def transform_hl7_to_fhir(hl7_message: str) -> dict:
    # ... (same as before, but we'll keep it concise)
    # (Full implementation omitted for brevity – assume existing working code)
    pass

# Core endpoint
@app.post("/v1/translate")
async def translate(request: Request):
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header missing")

    if not check_rate_limit(api_key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    key_hash = hash_api_key(api_key)

    if not api_key_hash_exists(key_hash):
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Deduct credit
    deduct_resp = supabase.rpc("deduct_healthcare_credit", {"target_key_hash": key_hash}).execute()
    if not deduct_resp.data:
        raise HTTPException(status_code=402, detail="Insufficient credits or key invalid")

    try:
        body = await request.body()
        if len(body) > 1_048_576:
            supabase.rpc("refund_healthcare_credit", {"target_key_hash": key_hash}).execute()
            raise HTTPException(status_code=413, detail="Payload too large")

        hl7_text = body.decode("utf-8")
        fhir_output = json_safe(transform_hl7_to_fhir(hl7_text))

        # Get user_id for logging
        user_id_resp = supabase.table("api_keys").select("user_id").eq("key_hash", key_hash).execute()
        user_id = user_id_resp.data[0]["user_id"] if user_id_resp.data else None

        supabase.table("translation_logs").insert({
            "key_hash": key_hash,
            "user_id": user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "msg_type": hl7_text[:3],
            "success": True
        }).execute()

        return fhir_output
    except Exception as e:
        supabase.rpc("refund_healthcare_credit", {"target_key_hash": key_hash}).execute()
        raise HTTPException(status_code=422, detail=f"Translation failed: {str(e)}")

# Admin cleanup
@app.post("/admin/cleanup")
async def cleanup_logs(request: Request):
    admin_secret = request.headers.get("X-ADMIN-CRON-SECRET")
    if not admin_secret or admin_secret != ADMIN_CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    supabase.table("translation_logs").delete().lt("created_at", "now() - interval '7 days'").execute()
    supabase.table("idempotency_store").delete().lt("created_at", "now() - interval '1 day'").execute()
    return {"status": "ok", "message": "Cleanup completed"}

# Portal auth
security = HTTPBearer()
async def get_current_user(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth_header.split(" ")[1]
    try:
        user = supabase.auth.get_user(token)
        return user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

# Portal endpoints
class CheckoutRequest(BaseModel):
    price_id: str

@app.get("/portal/me")
async def portal_me(user = Depends(get_current_user)):
    user_id = user.user.id
    active_key = supabase.table("api_keys").select("key_hash", "credits_remaining") \
        .eq("user_id", user_id).eq("active", True).execute()

    if active_key.data:
        return {
            "api_key": "********",
            "credits_remaining": active_key.data[0]["credits_remaining"],
            "is_new_key": False
        }
    else:
        new_plaintext = secrets.token_urlsafe(32)
        new_hash = hash_api_key(new_plaintext)
        supabase.table("api_keys").insert({
            "key_hash": new_hash,
            "credits_remaining": 1000,
            "user_id": user_id,
            "active": True
        }).execute()
        return {
            "api_key": new_plaintext,
            "credits_remaining": 1000,
            "is_new_key": True
        }

@app.post("/portal/rotate-key")
async def rotate_key(request: Request, user = Depends(get_current_user)):
    user_id = user.user.id
    old = supabase.table("api_keys").select("credits_remaining") \
        .eq("user_id", user_id).eq("active", True).execute()
    old_credits = old.data[0]["credits_remaining"] if old.data else 0

    supabase.table("api_keys").update({"active": False}).eq("user_id", user_id).eq("active", True).execute()

    new_plaintext = secrets.token_urlsafe(32)
    new_hash = hash_api_key(new_plaintext)
    supabase.table("api_keys").insert({
        "key_hash": new_hash,
        "credits_remaining": old_credits,
        "user_id": user_id,
        "active": True
    }).execute()

    return {"new_api_key": new_plaintext, "credits_remaining": old_credits}

@app.get("/portal/usage")
async def portal_usage(user = Depends(get_current_user)):
    user_id = user.user.id
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    result = supabase.table("translation_logs").select("created_at") \
        .eq("user_id", user_id) \
        .gte("created_at", seven_days_ago) \
        .order("created_at", desc=False) \
        .execute()
    daily = defaultdict(int)
    for row in result.data:
        date_str = row["created_at"][:10]
        daily[date_str] += 1
    return {"daily_usage": dict(sorted(daily.items()))}

@app.post("/portal/create-checkout-session")
async def create_checkout_session(req: CheckoutRequest, user = Depends(get_current_user)):
    price_id = req.price_id
    if price_id not in PRICE_CREDIT_MAP:
        raise HTTPException(status_code=400, detail="Invalid price ID")
    credits = PRICE_CREDIT_MAP[price_id]
    user_id = user.user.id
    user_info = supabase.auth.admin.get_user_by_id(user_id)
    customer_email = user_info.user.email if user_info else "partner@example.com"
    session = stripe.checkout.Session.create(
        line_items=[{"price": price_id, "quantity": 1}],
        mode="payment",
        success_url="https://justme-hub4.github.io/?session_id={CHECKOUT_SESSION_ID}",
        cancel_url="https://justme-hub4.github.io/",
        customer_email=customer_email,
        metadata={"user_id": user_id, "credits": str(credits)}
    )
    return {"url": session.url}

@app.post("/portal/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    if event["type"] == "checkout.session.completed":
        session = event.data.object.to_dict()
        metadata = session.get("metadata", {})
        user_id = metadata.get("user_id")
        credits_str = metadata.get("credits", "0")
        if user_id:
            supabase.rpc("add_credits", {"target_user_id": user_id, "amount": int(credits_str)}).execute()
            supabase.table("stripe_payments").insert({
                "user_id": user_id,
                "stripe_checkout_session_id": session["id"],
                "amount_total": session["amount_total"],
                "credits_purchased": int(credits_str),
                "status": "completed"
            }).execute()
    return {"status": "success"}

@app.get("/portal/invoices")
async def portal_invoices(user = Depends(get_current_user)):
    user_id = user.user.id
    resp = supabase.table("stripe_payments").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return {"invoices": resp.data}
