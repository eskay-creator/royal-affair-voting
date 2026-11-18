import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)
GH_TZ = ZoneInfo("Africa/Accra")
PAYSTACK_BASE = "https://api.paystack.co"
PAYMENT_EMAIL = os.getenv("PAYMENT_EMAIL", "eskay4760@gmail.com")
CATEGORIES = [
    "Most Fashionable Male", "Most Fashionable Female", "Perfect Gentleman",
    "Perfect Lady", "Instrumentalist of the Year", "Media Personality of the Year",
    "Youth Executive of the Year", "Teen of the Year", "Most Influential Male",
    "Most Influential Female", "Most Famous Male", "Most Famous Female",
    "Usher of the Year", "Best Dancer of the Year", "Best Singer of the Year",
]
NETWORKS = {
    "1": ("MTN MoMo", "mtn"),
    "2": ("Telecel Cash", "vod"),
    "3": ("AT Money", "atl"),
}
SESSION_TTL_SECONDS = 900

def redis_command(*parts):
    url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if not url or not token:
        raise RuntimeError("Upstash Redis is not configured")
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=list(parts),
        timeout=8
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError("Upstash Redis command failed")
    return body.get("result")


def redis_lock(key, value, ttl_seconds):
    """Atomically claim work so retries cannot process it twice."""
    return redis_command("SET", key, value, "NX", "EX", ttl_seconds) == "OK"


def redis_delete(key):
    redis_command("DEL", key)


def idempotency_key(prefix, reference):
    digest = hashlib.sha256(str(reference).encode("utf-8")).hexdigest()
    return f"royal-affair:{prefix}:{digest}"


class RedisSessionState(dict):
    def __init__(self, session_id, initial=None):
        self.session_id = session_id
        dict.__init__(self, initial or {})

    def persist(self):
        redis_command(
            "SET",
            f"royal-affair:ussd:{self.session_id}",
            json.dumps(self),
            "EX",
            SESSION_TTL_SECONDS
        )

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        self.persist()

    def update(self, *args, **kwargs):
        dict.update(self, *args, **kwargs)
        self.persist()


class RedisSessionStore:
    def key(self, session_id):
        return f"royal-affair:ussd:{session_id}"

    def __contains__(self, session_id):
        return redis_command("EXISTS", self.key(session_id)) == 1

    def __getitem__(self, session_id):
        value = redis_command("GET", self.key(session_id))
        if value is None:
            raise KeyError(session_id)
        return RedisSessionState(session_id, json.loads(value))

    def __setitem__(self, session_id, value):
        state = RedisSessionState(session_id, value)
        state.persist()

    def pop(self, session_id, default=None):
        redis_command("DEL", self.key(session_id))
        return default


SESSIONS = RedisSessionStore()

def voting_window():
    now = datetime.now(GH_TZ)
    opens = datetime.fromisoformat(os.getenv("VOTING_OPENS_AT", "2026-11-18T00:00:00+00:00")).astimezone(GH_TZ)
    closes = datetime.fromisoformat(os.getenv("VOTING_CLOSES_AT", "2026-12-19T00:00:00+00:00")).astimezone(GH_TZ)
    return now, opens, closes

def voting_is_open():
    now, opens, closes = voting_window()
    return opens <= now < closes, now, opens, closes

def load_nominees():
    try:
        with open("nominees.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

def reply(payload, message, keep_open=True):
    if request.is_json:
        return jsonify({
            "sessionID": payload.get("sessionID", payload.get("sessionId", "")),
            "userID": payload.get("userID", ""),
            "msisdn": payload.get("msisdn", payload.get("phoneNumber", "")),
            "message": message,
            "continueSession": keep_open
        })
    return Response(("CON " if keep_open else "END ") + message, mimetype="text/plain")

def page_text(title, items, page, page_size=5):
    start = page * page_size
    shown = items[start:start + page_size]
    lines = [title]
    for i, item in enumerate(shown, start=1):
        lines.append(f"{i}. {item}")
    if start + page_size < len(items):
        lines.append("8. Next")
    if page > 0:
        lines.append("9. Previous")
    lines.append("0. Back")
    return "\n".join(lines)

def get_paystack_secret():
    """Read and normalize the Paystack secret without exposing it."""
    secret = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
    if len(secret) >= 2 and secret[0] == secret[-1] and secret[0] in ("'", '"'):
        secret = secret[1:-1].strip()
    if secret.lower().startswith("bearer "):
        secret = secret[7:].strip()
    return secret


def paystack_key_mode(secret=None):
    secret = get_paystack_secret() if secret is None else secret
    if secret.startswith("sk_test_"):
        return "test"
    if secret.startswith("sk_live_"):
        return "live"
    if secret.startswith(("pk_test_", "pk_live_")):
        return "public_key_not_secret"
    return "missing" if not secret else "unrecognized_format"


def paystack_request(method, path, payload=None):
    secret = get_paystack_secret()
    if not secret:
        raise RuntimeError("PAYSTACK_SECRET_KEY is not configured")
    response = requests.request(
        method, PAYSTACK_BASE + path,
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        json=payload, timeout=25
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("status"):
        raise RuntimeError(body.get("message", "Paystack request was not successful"))
    return body.get("data") or {}

def normalize_ghana_phone(value):
    phone = re.sub(r"[\s-]", "", value)
    if phone.startswith("+233") and len(phone) == 13:
        phone = "0" + phone[4:]
    if not re.fullmatch(r"0\d{9}", phone):
        return None
    return phone

def create_payment(state, session_id, phone, network_key):
    network_name, provider = NETWORKS[network_key]
    reference = state.get("payment_reference") or ("RAF26-" + secrets.token_hex(12))
    amount = state["quantity"] * 100
    result = paystack_request("POST", "/charge", {
        "email": PAYMENT_EMAIL, "amount": amount, "currency": "GHS",
        "reference": reference, "mobile_money": {"phone": phone, "provider": provider},
        "metadata": {"event": "THE ROYAL AFFAIR 2026", "vote_reference": reference,
            "session_id": session_id, "category": CATEGORIES[state["category_index"]],
            "nominee": state["nominee"]["name"], "nominee_code": str(state["nominee"]["code"]),
            "quantity": state["quantity"], "network": network_name, "phone": phone}
    })
    return reference, result, str(result.get("status", "pending"))


@app.get("/")
def home():
    return jsonify({"service": "THE ROYAL AFFAIR 2026 USSD Voting Backend",
        "status": "online", "ussd_endpoint": "/ussd/callback",
        "payment_webhook": "/paystack/webhook", "payment_email": PAYMENT_EMAIL,
        "vote_recording": "manual verification required",
        "mode": paystack_key_mode()})


@app.get("/health")
def health():
    key = get_paystack_secret()
    mode = paystack_key_mode(key)
    redis_status = "not_configured"
    if os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN"):
        try:
            redis_status = "connected" if redis_command("PING") == "PONG" else "error"
        except Exception:
            app.logger.exception("Upstash Redis health check failed")
            redis_status = "error"
    return jsonify({"status": "online", "database": "upstash_redis",
        "redis_status": redis_status,
        "vote_recording": "manual review required",
        "paystack_key_present": bool(key),
        "paystack_key_mode": mode}), 200


def send_verified_payment_to_sheet(verified):
    """Forward a Paystack-verified payment to the private Google Sheets endpoint."""
    endpoint = os.getenv("GOOGLE_SHEETS_WEBAPP_URL", "").strip()
    shared_secret = os.getenv("GOOGLE_SHEETS_SHARED_SECRET", "")
    if not endpoint or not shared_secret:
        raise RuntimeError("Google Sheets integration is not configured")

    metadata = verified.get("metadata") or {}
    quantity = int(metadata.get("quantity") or 0)
    amount_pesewas = int(verified.get("amount") or 0)
    if quantity < 1 or amount_pesewas != quantity * 100:
        raise RuntimeError("Verified payment amount does not match vote quantity")

    record = {
        "reference": str(verified.get("reference") or ""),
        "phone": str(metadata.get("phone") or ""),
        "category": str(metadata.get("category") or ""),
        "nominee": str(metadata.get("nominee") or ""),
        "nominee_code": str(metadata.get("nominee_code") or ""),
        "votes_purchased": quantity,
        "amount": amount_pesewas / 100,
        "currency": str(verified.get("currency") or ""),
        "network": str(metadata.get("network") or ""),
        "payment_status": "success"
    }
    if not record["reference"] or not record["category"] or not record["nominee_code"]:
        raise RuntimeError("Verified payment is missing required voting metadata")

    response = requests.post(endpoint, json={
        "token": shared_secret,
        "record": record
    }, timeout=25)
    response.raise_for_status()
    result = response.json()
    if not result.get("success"):
        raise RuntimeError("Google Sheets rejected the verified payment record")
    return result


@app.post("/paystack/webhook")
def paystack_webhook():
    secret = get_paystack_secret()
    signature = request.headers.get("x-paystack-signature", "")
    raw_body = request.get_data(cache=True)
    if not secret or not signature:
        return jsonify({"error": "Missing webhook signature configuration"}), 401
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "Invalid signature"}), 401
    event = request.get_json(silent=True) or {}
    if event.get("event") == "charge.success":
        data = event.get("data") or {}
        reference = str(data.get("reference") or "")
        if reference:
            dedupe_key = idempotency_key("webhook", reference)
            try:
                claimed = redis_lock(dedupe_key, "processing", 300)
                if not claimed:
                    return jsonify({"received": True, "duplicate": True, "votes_counted": False}), 200

                verified = paystack_request("GET", "/transaction/verify/" + reference)
                if (verified.get("reference") == reference
                        and verified.get("status") == "success"
                        and verified.get("currency") == "GHS"):
                    send_verified_payment_to_sheet(verified)
                    redis_command("SET", dedupe_key, "completed", "EX", 2592000)
                    app.logger.info("VERIFIED PAYMENT SENT TO REGISTER: reference=%s", reference)
                else:
                    redis_delete(dedupe_key)
                    app.logger.warning("Paystack verification did not match webhook reference %s", reference)
            except Exception:
                try:
                    redis_delete(dedupe_key)
                except Exception:
                    app.logger.exception("Could not release webhook idempotency lock")
                app.logger.exception("Payment verification or register update failed for %s", reference)
                return jsonify({"error": "Temporary payment recording failure"}), 500
    return jsonify({"received": True, "votes_counted": False}), 200


@app.post("/ussd/callback")
def ussd_callback():
    payload = request.get_json(silent=True) or request.form.to_dict()
    session_id = str(payload.get("sessionID") or payload.get("sessionId") or "")
    user_data = str(payload.get("userData") or payload.get("text") or "").strip()
    new_session = payload.get("newSession", False)
    if not session_id:
        return jsonify({"error": "Missing sessionID"}), 400

    is_new_session = new_session is True or str(new_session).lower() == "true"
    if is_new_session or session_id not in SESSIONS:
        SESSIONS[session_id] = {"stage": "main", "page": 0}

    state = SESSIONS[session_id]
    stage = state["stage"]

    # Arkesel may send the dialled shortcode in userData on the first request.
    # Always display the welcome menu for a newly opened session.
    if is_new_session:
        return reply(payload, "WELCOME TO THE ROYAL AFFAIR 2026\n1. Cast Your Vote\n2. View Award Categories\n3. View Nominees & Codes\n4. Voting Guidelines\n0. Exit")

    if stage == "main":
        if user_data == "":
            return reply(payload, "WELCOME TO THE ROYAL AFFAIR 2026\n1. Cast Your Vote\n2. View Award Categories\n3. View Nominees & Codes\n4. Voting Guidelines\n0. Exit")
        if user_data == "0":
            SESSIONS.pop(session_id, None)
            return reply(payload, "Thank you. Goodbye.", False)
        if user_data == "1":
            opened, now, opens, closes = voting_is_open()
            if not opened:
                if now < opens:
                    return reply(payload, "Voting opens on 18 November 2026. Please dial again then.", False)
                return reply(payload, "Voting closed on 18 December 2026. Thank you.", False)
            state.update(stage="vote_categories", page=0)
            return reply(payload, page_text("SELECT AWARD CATEGORY", CATEGORIES, 0))
        if user_data == "2":
            state.update(stage="view_categories", page=0)
            return reply(payload, page_text("AWARD CATEGORIES", CATEGORIES, 0))
        if user_data == "3":
            state.update(stage="view_nominee_categories", page=0)
            return reply(payload, page_text("SELECT CATEGORY TO VIEW NOMINEES", CATEGORIES, 0))
        if user_data == "4":
            return reply(payload, "VOTING GUIDELINES\nEach vote costs GHS 1.00.\nMultiple votes are allowed.\nPayment must be approved before votes count.\nVoting closes 18 Dec 2026.\n0. Back")
        return reply(payload, "Invalid selection. Please choose an option shown.\n0. Back")

    if stage in ("vote_categories", "view_categories", "view_nominee_categories"):
        if user_data == "0":
            state.update(stage="main", page=0)
            return reply(payload, "WELCOME TO THE ROYAL AFFAIR 2026\n1. Cast Your Vote\n2. View Award Categories\n3. View Nominees & Codes\n4. Voting Guidelines\n0. Exit")
        if user_data == "8" and (state["page"] + 1) * 5 < len(CATEGORIES):
            state["page"] += 1
            return reply(payload, page_text("SELECT AWARD CATEGORY", CATEGORIES, state["page"]))
        if user_data == "9" and state["page"] > 0:
            state["page"] -= 1
            return reply(payload, page_text("SELECT AWARD CATEGORY", CATEGORIES, state["page"]))
        if not user_data.isdigit() or not 1 <= int(user_data) <= 5:
            return reply(payload, "Invalid selection. Choose 1-5, 8 for Next, 9 for Previous, or 0 for Back.")
        category_index = state["page"] * 5 + int(user_data) - 1
        if category_index >= len(CATEGORIES):
            return reply(payload, "Invalid category selection. Please try again.")
        state["category_index"] = category_index
        category = CATEGORIES[category_index]
        if stage == "view_categories":
            state.update(stage="main", page=0)
            return reply(payload, f"{category}\n0. Back", False)
        nominees = load_nominees().get(str(category_index + 1), [])
        if not nominees:
            return reply(payload, f"{category}\nNominees have not been loaded.\n0. Back", False)
        state.update(stage="nominees_view" if stage == "view_nominee_categories" else "choose_nominee", nominee_page=0)
        return reply(payload, page_text(f"CONTESTANTS - {category}", [f"{n['name']} - Code {n['code']}" for n in nominees], 0))

    if stage in ("choose_nominee", "nominees_view"):
        if user_data == "0":
            state.update(stage="vote_categories" if stage == "choose_nominee" else "view_nominee_categories", page=0)
            return reply(payload, page_text("SELECT AWARD CATEGORY", CATEGORIES, 0))
        nominees = load_nominees().get(str(state["category_index"] + 1), [])
        if not user_data.isdigit() or not 1 <= int(user_data) <= len(nominees):
            return reply(payload, "Invalid nominee selection. Please try again or enter 0 to go back.")
        nominee = nominees[int(user_data) - 1]
        if stage == "nominees_view":
            state.update(stage="main", page=0)
            return reply(payload, f"{nominee['name']} - Code {nominee['code']}", False)
        state.update(stage="quantity", nominee=nominee)
        return reply(payload, f"HOW MANY VOTES?\nEnter number of votes for {nominee['name']}.\nEach vote costs GHS 1.00.")

    if stage == "quantity":
        if not user_data.isdigit() or int(user_data) < 1 or int(user_data) > 999:
            return reply(payload, "Enter a whole number from 1 to 999.")
        state["quantity"] = int(user_data)
        state["stage"] = "confirm"
        total = state["quantity"]
        return reply(payload, f"CONFIRM YOUR VOTE\nCategory: {CATEGORIES[state['category_index']]}\nContestant: {state['nominee']['name']}\nVotes: {total}\nTotal: GHS {total:.2f}\n1. Continue\n2. Cancel")

    if stage == "confirm":
        if user_data == "2":
            SESSIONS.pop(session_id, None)
            return reply(payload, "Vote cancelled. No payment was requested and no votes were recorded.", False)
        if user_data == "1":
            state["stage"] = "network"
            return reply(payload, "SELECT MoMo NETWORK\n1. MTN MoMo\n2. Telecel Cash\n3. AT Money")
        return reply(payload, "Choose 1 to continue or 2 to cancel.")

    if stage == "network":
        if user_data not in NETWORKS:
            return reply(payload, "Choose 1 for MTN, 2 for Telecel, or 3 for AT Money.")
        state["network_key"] = user_data
        state["stage"] = "phone"
        return reply(payload, "Enter the MoMo phone number to charge (e.g. 0241234567).")

    if stage == "payment_processing":
        return reply(payload, "Your payment request is already being processed. Please wait for the MoMo prompt and do not submit the request again.", False)

    if stage == "phone":
        phone = normalize_ghana_phone(user_data)
        if not phone:
            return reply(payload, "Invalid Ghana phone number. Enter 10 digits, e.g. 0241234567.")
        reference = "RAF26-" + secrets.token_hex(12)
        payment_lock = idempotency_key("ussd-payment", session_id)
        try:
            if not redis_lock(payment_lock, reference, 120):
                return reply(payload, "Your payment request is already being processed. Please wait for the MoMo prompt. Do not retry yet.", False)
            state.update(stage="payment_processing", payment_reference=reference)
            reference, result, provider_status = create_payment(state, session_id, phone, state["network_key"])
        except Exception:
            try:
                redis_delete(payment_lock)
            except Exception:
                app.logger.exception("Could not release USSD payment lock")
            app.logger.exception("Paystack charge initiation failed")
            SESSIONS.pop(session_id, None)
            return reply(payload, "We could not start your payment. No vote has been recorded. Please try again later.", False)
        if provider_status == "send_otp":
            SESSIONS.pop(session_id, None)
            return reply(payload, "This network requires an additional verification step that this USSD service does not support. Do not enter voucher or verification codes here. Please contact the organizers for assistance. Ref: " + reference, False)
        SESSIONS.pop(session_id, None)
        return reply(payload, "Payment request sent to your MoMo phone. Approve it. Save your Paystack reference and receipt, then submit both to the organizers for manual verification. Payment does not automatically count as a vote. Ref: " + reference, False)

    SESSIONS.pop(session_id, None)
    return reply(payload, "Your session has expired. Please dial again.", False)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
