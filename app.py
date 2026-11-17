import hashlib
import hmac
import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
import requests
from flask import Flask, Response, jsonify, request
from psycopg.types.json import Jsonb

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
SESSIONS = {}

def database_url():
    value = os.getenv("DATABASE_URL", "").strip()
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    return value

@contextmanager
def db():
    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    conn = psycopg.connect(url, connect_timeout=12)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vote_payments (
                    reference TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    network TEXT NOT NULL,
                    provider_code TEXT NOT NULL,
                    category TEXT NOT NULL,
                    nominee TEXT NOT NULL,
                    nominee_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    amount_pesewas INTEGER NOT NULL CHECK (amount_pesewas > 0),
                    status TEXT NOT NULL DEFAULT 'pending',
                    provider_status TEXT,
                    provider_response JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    paid_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS counted_votes (
                    id BIGSERIAL PRIMARY KEY,
                    payment_reference TEXT NOT NULL UNIQUE
                        REFERENCES vote_payments(reference),
                    category TEXT NOT NULL,
                    nominee TEXT NOT NULL,
                    nominee_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

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

def paystack_request(method, path, payload=None):
    secret = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
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
    reference = "RAF26-" + secrets.token_hex(12)
    amount = state["quantity"] * 100  # GH₵1.00 per vote, in pesewas
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO vote_payments
                (reference, session_id, phone, network, provider_code, category,
                 nominee, nominee_code, quantity, amount_pesewas, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'initiating')
            """, (
                reference, session_id, phone, network_name, provider,
                CATEGORIES[state["category_index"]], state["nominee"]["name"],
                str(state["nominee"]["code"]), state["quantity"], amount
            ))
    try:
        result = paystack_request("POST", "/charge", {
            "email": PAYMENT_EMAIL,
            "amount": str(amount),
            "currency": "GHS",
            "reference": reference,
            "mobile_money": {"phone": phone, "provider": provider},
            "metadata": {
                "event": "THE ROYAL AFFAIR 2026",
                "vote_reference": reference,
                "category": CATEGORIES[state["category_index"]],
                "nominee": state["nominee"]["name"],
                "quantity": state["quantity"],
            }
        })
    except Exception:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE vote_payments SET status='init_failed' WHERE reference=%s", (reference,))
        raise
    provider_status = str(result.get("status", "pending"))
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE vote_payments SET status='pending', provider_status=%s,
                provider_response=%s WHERE reference=%s
            """, (provider_status, Jsonb(result), reference))
    return reference, result, provider_status

def verify_and_record(reference):
    verified = paystack_request("GET", "/transaction/verify/" + reference)
    if verified.get("reference") != reference or verified.get("status") != "success":
        return False
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT amount_pesewas, quantity, category, nominee, nominee_code
                FROM vote_payments WHERE reference=%s FOR UPDATE
            """, (reference,))
            row = cur.fetchone()
            if not row:
                return False
            expected_amount, quantity, category, nominee, nominee_code = row
            if int(verified.get("amount", -1)) != expected_amount or verified.get("currency") != "GHS":
                cur.execute("UPDATE vote_payments SET status='verification_mismatch' WHERE reference=%s", (reference,))
                return False
            cur.execute("""
                UPDATE vote_payments SET status='success', provider_status='success',
                paid_at=COALESCE(%s, NOW()) WHERE reference=%s
            """, (verified.get("paid_at"), reference))
            cur.execute("""
                INSERT INTO counted_votes
                (payment_reference, category, nominee, nominee_code, quantity)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (payment_reference) DO NOTHING
            """, (reference, category, nominee, nominee_code, quantity))
    return True

@app.get("/")
def home():
    return jsonify({
        "service": "THE ROYAL AFFAIR 2026 USSD Voting Backend",
        "status": "online",
        "ussd_endpoint": "/ussd/callback",
        "payment_webhook": "/paystack/webhook",
        "payment_email": PAYMENT_EMAIL,
        "mode": "test" if os.getenv("PAYSTACK_SECRET_KEY", "").startswith("sk_test_") else "check Paystack key mode"
    })

@app.get("/health")
def health():
    return jsonify({
        "status": "online",
        "database": "temporarily disabled",
        "mode": "development"
    }), 200

@app.post("/paystack/webhook")
def paystack_webhook():
    secret = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
    signature = request.headers.get("x-paystack-signature", "")
    raw_body = request.get_data(cache=True)
    if not secret or not signature:
        return jsonify({"error": "Missing webhook signature configuration"}), 401
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "Invalid signature"}), 401
    event = request.get_json(silent=True) or {}
    if event.get("event") != "charge.success":
        return jsonify({"received": True}), 200
    reference = (event.get("data") or {}).get("reference")
    if not reference:
        return jsonify({"received": True}), 200
    try:
        verify_and_record(str(reference))
    except Exception:
        app.logger.exception("Could not verify Paystack transaction %s", reference)
        return jsonify({"error": "Temporary verification failure"}), 500
    return jsonify({"received": True}), 200

@app.post("/ussd/callback")
def ussd_callback():
    payload = request.get_json(silent=True) or request.form.to_dict()
    session_id = str(payload.get("sessionID") or payload.get("sessionId") or "")
    user_data = str(payload.get("userData") or payload.get("text") or "").strip()
    new_session = payload.get("newSession", False)
    if not session_id:
        return jsonify({"error": "Missing sessionID"}), 400

    if new_session is True or str(new_session).lower() == "true" or session_id not in SESSIONS:
        SESSIONS[session_id] = {"stage": "main", "page": 0}
    state = SESSIONS[session_id]
    stage = state["stage"]

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

    if stage == "phone":
        phone = normalize_ghana_phone(user_data)
        if not phone:
            return reply(payload, "Invalid Ghana phone number. Enter 10 digits, e.g. 0241234567.")
        try:
            reference, result, provider_status = create_payment(state, session_id, phone, state["network_key"])
        except Exception:
            app.logger.exception("Paystack charge initiation failed")
            SESSIONS.pop(session_id, None)
            return reply(payload, "We could not start your payment. No votes were recorded. Please try again later.", False)
        if provider_status == "send_otp":
            state.update(stage="telecel_otp", reference=reference)
            return reply(payload, "Telecel requires a voucher code. Dial *110# on your phone, generate the code, then enter it here.")
        SESSIONS.pop(session_id, None)
        return reply(payload, "Payment request sent to your MoMo phone. Approve the prompt to pay. Your votes count only after Paystack confirms payment. Ref: " + reference, False)

    if stage == "telecel_otp":
        if not re.fullmatch(r"\d{4,8}", user_data):
            return reply(payload, "Enter the 4-8 digit Telecel voucher code.")
        try:
            result = paystack_request("POST", "/charge/submit_otp", {"otp": user_data, "reference": state["reference"]})
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE vote_payments SET provider_status=%s, provider_response=%s WHERE reference=%s",
                                (str(result.get("status", "pending")), Jsonb(result), state["reference"]))
            if result.get("status") == "success":
                verify_and_record(state["reference"])
        except Exception:
            app.logger.exception("Telecel OTP submission failed")
            SESSIONS.pop(session_id, None)
            return reply(payload, "We could not submit that voucher code. No votes were recorded. Please try again.", False)
        reference = state["reference"]
        SESSIONS.pop(session_id, None)
        return reply(payload, "Voucher submitted. Complete any remaining phone prompt. Votes count only after Paystack confirms payment. Ref: " + reference, False)

    SESSIONS.pop(session_id, None)
    return reply(payload, "Your session has expired. Please dial again.", False)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
