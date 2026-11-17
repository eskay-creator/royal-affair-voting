# THE ROYAL AFFAIR 2026 — USSD voting backend

## Render settings
- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`

## Endpoints
- `GET /` — service information
- `GET /health` — database connectivity check
- `POST /ussd/callback` — Arkesel USSD callback
- `POST /paystack/webhook` — Paystack payment webhook

## Required Render environment variables
- `DATABASE_URL` — Supabase PostgreSQL Session Pooler URI
- `PAYSTACK_SECRET_KEY` — Paystack TEST secret key while testing
- `PAYMENT_EMAIL` — defaults to eskay4760@gmail.com
- Optional: `VOTING_OPENS_AT` (default 2026-11-18T00:00:00+00:00)
- Optional: `VOTING_CLOSES_AT` (default 2026-12-19T00:00:00+00:00; Ghana time, so voting includes 18 December)

## Payment and vote handling
- One vote costs GH₵1.00; the charge amount is sent in pesewas.
- Paystack Ghana Mobile Money provider codes used: MTN `mtn`, Telecel `vod`, AT Money `atl`.
- The voter enters the MoMo phone number and selects the network.
- Telecel's voucher/OTP step is supported through Paystack's submit-OTP endpoint.
- A payment is counted only after the server verifies it with Paystack as successful, with the exact expected amount and GHS currency.
- Each Paystack reference can create only one counted-vote row, preventing duplicate webhook deliveries from double-counting.
- Vote payments and counted votes are stored in Supabase PostgreSQL.

## Important
- Keep the Paystack secret key and database URI only in Render environment variables. Never commit them to GitHub or share them.
- Keep Paystack in TEST mode until real end-to-end tests pass.
- Set the Paystack TEST webhook URL to `https://royal-affair-voting.onrender.com/paystack/webhook`.
- Do not advertise paid voting as live until webhook delivery, all three network flows, duplicate events, database persistence and failure cases have been tested.
- The current Arkesel USSD session state is held in memory; the persistent payment/vote records survive restarts, but a voter mid-menu may need to redial if the service restarts.
- Review Arkesel callback field formats and test every menu path with the actual shortcode before opening voting.
