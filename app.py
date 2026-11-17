import json
import os
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# IMPORTANT:
# This first deployment is a staging/demo backend.
# Session state is kept in memory and is lost on restart.
# Do not use for live voting until persistent storage and payment verification
# have been added and tested.

CATEGORIES = [
    "Most Fashionable Male",
    "Most Fashionable Female",
    "Perfect Gentleman",
    "Perfect Lady",
    "Instrumentalist of the Year",
    "Media Personality of the Year",
    "Youth Executive of the Year",
    "Teen of the Year",
    "Most Influential Male",
    "Most Influential Female",
    "Most Famous Male",
    "Most Famous Female",
    "Usher of the Year",
    "Best Dancer of the Year",
    "Best Singer of the Year",
]

SESSIONS = {}

def load_nominees():
    try:
        with open("nominees.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

def reply(payload, message, keep_open=True):
    # Arkesel's published USSD examples show legacy form-encoded requests
    # with plain-text CON/END replies. Its newer API material also describes
    # JSON callbacks. Match the incoming request format where possible.
    if request.is_json:
        return jsonify({
            "sessionID": payload.get("sessionID", payload.get("sessionId", "")),
            "userID": payload.get("userID", ""),
            "msisdn": payload.get("msisdn", payload.get("phoneNumber", "")),
            "message": message,
            "continueSession": keep_open
        })
    prefix = "CON " if keep_open else "END "
    return Response(prefix + message, mimetype="text/plain")

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

@app.get("/")
def home():
    return jsonify({
        "service": "THE ROYAL AFFAIR 2026 USSD Voting Backend",
        "status": "online",
        "ussd_endpoint": "/ussd/callback",
        "mode": "staging - payment and persistent database not configured"
    })

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.post("/ussd/callback")
def ussd_callback():
    payload = request.get_json(silent=True) or request.form.to_dict()
    session_id = str(payload.get("sessionID") or payload.get("sessionId") or "")
    user_data = str(payload.get("userData") or payload.get("text") or "").strip()
    new_session = payload.get("newSession", False)
    msisdn = str(payload.get("msisdn") or payload.get("phoneNumber") or "")

    if not session_id:
        return jsonify({"error": "Missing sessionID"}), 400

    if new_session is True or str(new_session).lower() == "true" or session_id not in SESSIONS:
        SESSIONS[session_id] = {"stage": "main", "page": 0}

    state = SESSIONS[session_id]
    stage = state["stage"]

    if stage == "main":
        if user_data == "":
            return reply(payload,
                "WELCOME TO THE ROYAL AFFAIR 2026\n"
                "1. Cast Your Vote\n"
                "2. View Award Categories\n"
                "3. View Nominees & Codes\n"
                "4. Voting Guidelines\n"
                "0. Exit")
        if user_data == "0":
            SESSIONS.pop(session_id, None)
            return reply(payload, "Thank you. Goodbye.", False)
        if user_data == "1":
            state.update(stage="vote_categories", page=0)
            return reply(payload, page_text("SELECT AWARD CATEGORY", CATEGORIES, 0))
        if user_data == "2":
            state.update(stage="view_categories", page=0)
            return reply(payload, page_text("AWARD CATEGORIES", CATEGORIES, 0))
        if user_data == "3":
            state.update(stage="view_nominee_categories", page=0)
            return reply(payload, page_text("SELECT CATEGORY TO VIEW NOMINEES", CATEGORIES, 0))
        if user_data == "4":
            return reply(payload,
                "VOTING GUIDELINES\n"
                "Each vote costs GHS 1.00.\n"
                "Multiple votes are allowed.\n"
                "Payment must be approved before votes count.\n"
                "Votes cannot be reversed.\n"
                "Voting closes 18 Dec 2026.\n"
                "0. Back")
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
            return reply(payload, f"{category}\n0. Back")
        nominees = load_nominees().get(str(category_index + 1), [])
        if not nominees:
            return reply(payload, f"{category}\nNominees and codes have not yet been loaded.\n0. Back")
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
            return reply(payload, f"{nominee['name']} - Code {nominee['code']}\n0. Back", False)
        state.update(stage="quantity", nominee=nominee)
        return reply(payload, f"HOW MANY VOTES?\nEnter number of votes for {nominee['name']}.\nEach vote costs GHS 1.00.")

    if stage == "quantity":
        if not user_data.isdigit() or int(user_data) < 1 or int(user_data) > 999:
            return reply(payload, "Enter a whole number from 1 to 999.")
        state["quantity"] = int(user_data)
        state["amount"] = int(user_data) * 1.00
        state["stage"] = "confirm"
        category = CATEGORIES[state["category_index"]]
        return reply(payload,
            f"CONFIRM YOUR VOTE\nCategory: {category}\n"
            f"Contestant: {state['nominee']['name']}\nVotes: {state['quantity']}\n"
            f"Total: GHS {state['amount']:.2f}\n1. Confirm & Pay\n2. Cancel")

    if stage == "confirm":
        if user_data == "2":
            SESSIONS.pop(session_id, None)
            return reply(payload, "Vote cancelled. No payment was requested and no votes were recorded.", False)
        if user_data == "1":
            # Intentionally disabled until a payment provider is selected and its
            # server-to-server payment verification callback is implemented.
            SESSIONS.pop(session_id, None)
            return reply(payload,
                "Voting payment is not yet enabled. No payment was taken and no votes were recorded. Please try again later.",
                False)
        return reply(payload, "Choose 1 to Confirm & Pay or 2 to Cancel.")

    return reply(payload, "Your session has expired. Please dial again.", False)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
