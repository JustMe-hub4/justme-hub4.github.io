import os
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, status, Response
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from hl7apy.parser import parse_message
from fhir.resources.patient import Patient
from fhir.resources.encounter import Encounter
from fhir.resources.bundle import Bundle, BundleEntry, BundleEntryRequest
from fhir.resources.humanname import HumanName
from fhir.resources.identifier import Identifier
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("fhir-interop")

app = FastAPI(title="FHIR Interop Engine", version="2.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["X-API-Key", "X-Idempotency-Key", "Content-Type", "X-Admin-Key"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ADMIN_KEY = os.getenv("ADMIN_KEY", "")

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
from fastapi.security import HTTPBearer
from fastapi import Depends

security = HTTPBearer()

async def get_current_user(request: Request):
    """Validate Supabase JWT from Authorization header and return user."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth_header.split(" ")[1]
    try:
        # Use Supabase to get user from JWT
        user = supabase.auth.get_user(token)
        return user
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.get("/portal/me")
async def portal_me(request: Request, user = Depends(get_current_user)):
    """Return current user's API key and credit balance."""
    user_id = user.user.id
    # Get API key from users table
    user_resp = supabase.table("users").select("api_key").eq("id", user_id).execute()
    if not user_resp.data:
        raise HTTPException(status_code=404, detail="User not found")
    api_key = user_resp.data[0]["api_key"]
    # Get credits
    credit_resp = supabase.table("api_keys").select("credits_remaining").eq("key", api_key).execute()
    credits = credit_resp.data[0]["credits_remaining"] if credit_resp.data else 0
    return {"api_key": api_key, "credits_remaining": credits}

@app.get("/portal/usage")
async def portal_usage(request: Request, user = Depends(get_current_user)):
    """Return daily usage counts for the last 7 days."""
    user_id = user.user.id
    user_resp = supabase.table("users").select("api_key").eq("id", user_id).execute()
    if not user_resp.data:
        raise HTTPException(status_code=404, detail="User not found")
    api_key = user_resp.data[0]["api_key"]
    # Query translation_logs for the last 7 days, grouped by date
    result = supabase.table("translation_logs") \
        .select("created_at", count="exact") \
        .eq("api_key", api_key) \
        .gte("created_at", "now() - interval '7 days'") \
        .order("created_at", desc=False) \
        .execute()
    # Simple grouping in Python (Supabase may not do date grouping easily)
    from collections import defaultdict
    daily = defaultdict(int)
    for row in result.data:
        date_str = row["created_at"][:10]  # YYYY-MM-DD
        daily[date_str] += 1
    return {"daily_usage": dict(sorted(daily.items()))}
