import os
import logging
import uuid
from datetime import datetime, timezone, timedelta
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

app = FastAPI(title="FHIR Interop Engine", version="2.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["X-API-Key", "X-Idempotency-Key", "Content-Type", "X-Admin-Key", "Authorization"],
)

# ------------------------------
# Supabase & Stripe config
# ------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ADMIN_KEY = os.getenv("ADMIN_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_...")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_...")
stripe.api_key = STRIPE_SECRET_KEY

# Map your actual Stripe Price IDs to credit amounts
PRICE_CREDIT_MAP = {
    "price_1U1xzUIUjoQtwpInIy0o6u1O": 100,    # Pay‑as‑you‑go: $30 for 100 credits
    "price_1U1xzUIUjoQtwpInNFq6TRCU": 10000,  # Volume Pack: $2000 for 10,000 credits
    "price_1U1xzUIUjoQtwpInL8G3EBZX": 75000,  # Enterprise: $9000 for 75,000 credits
}

# ------------------------------
# Rate limiter & helpers
# ------------------------------
rate_limit_store: Dict[str, list] = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 120

def check_rate_limit(api_key: str) -> bool:
    now = datetime.now(timezone.utc)
    if api_key not in rate_limit_store:
        rate_limit_store[api_key] = []
    rate_limit_store[api_key] = [ts for ts in rate_limit_store[api_key] if now - ts < timedelta(seconds=RATE_LIMIT_WINDOW)]
    if len(rate_limit_store[api_key]) >= RATE_LIMIT_MAX:
        return False
    rate_limit_store[api_key].append(now)
    return True

def api_key_exists(api_key: str) -> bool:
    try:
        resp = supabase.table("api_keys").select("key").eq("key", api_key).execute()
        return len(resp.data) > 0
    except Exception as e:
        logger.error(f"Supabase error checking key: {e}")
        return False

def check_idempotency(api_key: str, idem_key: str) -> Optional[dict]:
    try:
        resp = supabase.table("idempotency_store").select("response") \
            .eq("api_key", api_key).eq("idempotency_key", idem_key).execute()
        if resp.data:
            return resp.data[0]["response"]
    except Exception as e:
        logger.error(f"Idempotency check error: {e}")
    return None

def store_idempotency(api_key: str, idem_key: str, response_data: dict):
    try:
        supabase.table("idempotency_store").upsert({
            "api_key": api_key,
            "idempotency_key": idem_key,
            "response": response_data,
        }, on_conflict="api_key,idempotency_key").execute()
    except Exception as e:
        logger.error(f"Idempotency store error: {e}")

def transform_hl7_to_fhir(hl7_message: str) -> dict:
    clean = hl7_message.replace("\n", "\r").strip()
    msg = parse_message(clean)
    pid = msg.PID
    mrn = pid.PID_3[0].value if pid.PID_3 else str(uuid.uuid4())
    if pid.PID_5 and len(pid.PID_5) > 0:
        name_field = pid.PID_5[0]
        family = name_field.PID_5_1.value if name_field.PID_5_1 else "Unknown"
        given = name_field.PID_5_2.value if name_field.PID_5_2 else "Unknown"
    else:
        family, given = "Unknown", "Unknown"
    dob = pid.PID_7.value if pid.PID_7 else "19700101"

    pv1 = msg.PV1 if hasattr(msg, "PV1") else None
    encounter_id = pv1.PV1_19.value if pv1 and pv1.PV1_19 else str(uuid.uuid4())

    patient = Patient(
        id=str(uuid.uuid4()),
        identifier=[Identifier(system="urn:oid:2.16.840.1.113883.19.5", value=str(mrn))],
        name=[HumanName(family=str(family), given=[str(given)])],
        birthDate=datetime.strptime(str(dob), "%Y%m%d").date() if len(str(dob)) == 8 else None
    )
    encounter = Encounter(
        id=str(uuid.uuid4()),
        status="completed",
        subject={"reference": f"Patient/{patient.id}"},
        identifier=[Identifier(system="urn:oid:2.16.840.1.113883.19.5", value=str(encounter_id))]
    )
    entries = [
        BundleEntry(resource=patient, request=BundleEntryRequest(method="POST", url="Patient")),
        BundleEntry(resource=encounter, request=BundleEntryRequest(method="POST", url="Encounter"))
    ]
    bundle = Bundle(type="batch", entry=entries, id=str(uuid.uuid4()))
    return bundle.dict()

# -------------------- Core Endpoints --------------------
@app.get("/health")
async def health():
    try:
        supabase.table("api_keys").select("key").limit(1).execute()
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/credits")
async def check_credits(api_key: str):
    resp = supabase.table("api_keys").select("credits_remaining").eq("key", api_key).execute()
    if resp.data:
        return {"credits_remaining": resp.data[0]["credits_remaining"]}
    raise HTTPException(status_code=404, detail="API key not found")

@app.post("/v1/translate")
async def translate(request: Request):
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header missing")

    if not check_rate_limit(api_key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    idem_key = request.headers.get("X-Idempotency-Key")
    if idem_key:
        cached = check_idempotency(api_key, idem_key)
        if cached:
            return Response(content=cached, media_type="application/json", headers={"X-Idempotency-Replay": "true"})

    if not api_key_exists(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")

    try:
        deduct_resp = supabase.rpc("deduct_healthcare_credit", {"target_key": api_key}).execute()
        if not deduct_resp.data:
            raise HTTPException(status_code=402, detail="Insufficient credits or key invalid")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Deduction failed: {e}")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable (credit check)")

    try:
        body = await request.body()
        if len(body) > 1_048_576:
            supabase.rpc("refund_healthcare_credit", {"target_key": api_key}).execute()
            raise HTTPException(status_code=413, detail="Payload too large (max 1 MB)")

        try:
            hl7_text = body.decode("utf-8")
        except UnicodeDecodeError:
            hl7_text = body.decode("latin-1")
            logger.warning(f"Non-UTF-8 payload from key {api_key[:4]}...")

        fhir_output = transform_hl7_to_fhir(hl7_text)

        supabase.table("translation_logs").insert({
            "api_key": api_key,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "msg_type": hl7_text[:3],
            "success": True
        }).execute()

        if idem_key:
            store_idempotency(api_key, idem_key, fhir_output)

        return fhir_output

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Translation error: {e}")
        try:
            supabase.rpc("refund_healthcare_credit", {"target_key": api_key}).execute()
        except Exception as refund_error:
            logger.critical(f"Refund failed: {refund_error}")
        raise HTTPException(status_code=422, detail=f"HL7 transformation failed: {str(e)}")

@app.post("/admin/cleanup")
async def cleanup_logs(request: Request):
    admin_key = request.headers.get("X-Admin-Key")
    if not admin_key or admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        supabase.table("translation_logs").delete().lt("created_at", "now() - interval '7 days'").execute()
        supabase.table("idempotency_store").delete().lt("created_at", "now() - interval '1 day'").execute()
        return {"status": "ok", "message": "Cleanup completed"}
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail="Cleanup failed")

# -------------------- Portal Endpoints --------------------
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

class CheckoutRequest(BaseModel):
    price_id: str

@app.get("/portal/me")
async def portal_me(user = Depends(get_current_user)):
    user_id = user.user.id
    key_resp = supabase.table("api_keys").select("key,credits_remaining") \
        .eq("user_id", user_id).eq("active", True).execute()
    if not key_resp.data:
        raise HTTPException(status_code=404, detail="No active API key found")
    active_key = key_resp.data[0]
    return {"api_key": active_key["key"], "credits_remaining": active_key["credits_remaining"]}

@app.post("/portal/rotate-key")
async def rotate_key(user = Depends(get_current_user)):
    user_id = user.user.id
    supabase.table("api_keys").update({"active": False}).eq("user_id", user_id).eq("active", True).execute()
    new_key = os.urandom(16).hex()
    supabase.table("api_keys").insert({
        "key": new_key,
        "credits_remaining": 0,
        "user_id": user_id,
        "active": True
    }).execute()
    supabase.table("users").update({"api_key": new_key}).eq("id", user_id).execute()
    return {"new_api_key": new_key}

@app.get("/portal/usage")
async def portal_usage(user = Depends(get_current_user)):
    user_id = user.user.id
    key_resp = supabase.table("api_keys").select("key") \
        .eq("user_id", user_id).eq("active", True).execute()
    if not key_resp.data:
        return {"daily_usage": {}}
    api_key = key_resp.data[0]["key"]
    # Compute date string for 7 days ago
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    result = supabase.table("translation_logs") \
        .select("created_at") \
        .eq("api_key", api_key) \
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
    try:
        session = stripe.checkout.Session.create(
            line_items=[{"price": price_id, "quantity": 1}],
            mode="payment",
            success_url="https://justme-hub4.github.io/?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://justme-hub4.github.io/",
            customer_email=customer_email,
            metadata={"user_id": user_id, "credits": str(credits)}
        )
        return {"url": session.url}
    except Exception as e:
        logger.error(f"Stripe session error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

@app.post("/portal/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        logger.error(f"Webhook payload error: {e}")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Webhook signature error: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.error(f"Webhook unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Webhook processing error")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        # session is a Stripe object – use attribute access, not .get()
        metadata = session.get("metadata") or {}
        user_id = metadata.get("user_id")
        credits_str = metadata.get("credits", "0")
        logger.info(f"Webhook received: user_id={user_id}, credits={credits_str}")
        if not user_id:
            return {"status": "ignored"}
        credits_to_add = int(credits_str)

        # Try RPC first
        try:
            supabase.rpc("add_credits", {
                "target_user_id": user_id,
                "amount": credits_to_add
            }).execute()
            logger.info(f"RPC add_credits succeeded for {user_id}: +{credits_to_add}")
        except Exception as e:
            logger.error(f"RPC add_credits failed: {e}")
            # Fallback: direct update
            supabase.table("api_keys").update({
                "credits_remaining": supabase.raw(f"credits_remaining + {credits_to_add}")
            }).eq("user_id", user_id).eq("active", True).execute()
            logger.info(f"Fallback direct update for {user_id}: +{credits_to_add}")

        # Record payment
        supabase.table("stripe_payments").insert({
            "user_id": user_id,
            "stripe_checkout_session_id": session["id"],
            "amount_total": session["amount_total"],
            "credits_purchased": credits_to_add,
            "status": "completed"
        }).execute()
        logger.info(f"Payment recorded for user {user_id}")

    return {"status": "success"}

@app.get("/portal/invoices")
async def portal_invoices(user = Depends(get_current_user)):
    user_id = user.user.id
    resp = supabase.table("stripe_payments").select("*") \
        .eq("user_id", user_id).order("created_at", desc=True).execute()
    return {"invoices": resp.data}
