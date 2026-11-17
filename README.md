# THE ROYAL AFFAIR 2026 — USSD voting backend

## Render settings
- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`

## Endpoints
- `GET /` — service information
- `GET /health` — application and Paystack key-mode status (no database)
- `POST /ussd/callback` — Arkesel USSD callback
- `POST /paystack/webhook` — validates Paystack webhook signature and independently verifies successful charges

## Required Render environment variables
- `PAYSTACK_SECRET_KEY` — Paystack TEST secret key during testing; do not expose it
- `PAYMENT_EMAIL` — payment email; defaults to eskay4760@gmail.com
- Optional: `VOTING_OPENS_AT` (default 2026-11-18T00:00:00+00:00)
- Optional: `VOTING_CLOSES_AT` (default 2026-12-19T00:00:00+00:00; Ghana time, so voting includes 18 December)

No database is used. Remove any obsolete `DATABASE_URL` setting from Render; it is no longer read by this application.

## Payment flow and manual vote review
- One vote costs GH₵1.00; the charge amount is sent to Paystack in pesewas.
- Supported Ghana Mobile Money provider codes: MTN `mtn`, Telecel `vod`, AT Money `atl`.
- The voter selects a network and enters the phone number to be charged.
- Telecel's voucher/OTP step is supported through Paystack's submit-OTP endpoint.
- The voter must save the Paystack reference and payment receipt and submit them to the organizers.
- A successful Paystack webhook is signature-checked and the transaction is verified directly with Paystack. The server logs verified payment details for organizer review; it does not automatically count votes.
- Organizers must manually match the submitted reference, verified payment amount, category, nominee and vote quantity before adding votes to the official tally. Do not count screenshots alone without checking the reference in the Paystack dashboard.

## Important limitations and launch checks
- There is no persistent payment or vote database. Application logs are not a durable ledger and may not be sufficient for audit or reconciliation.
- USSD session state is held in memory; voters may need to redial if the service restarts or a request reaches a different instance.
- Keep Paystack in TEST mode until end-to-end tests pass for MTN, Telecel and AT Money, including successful, failed, cancelled and duplicate webhook events.
- Set the Paystack webhook URL to `https://royal-affair-voting.onrender.com/paystack/webhook`.
- Do not announce paid voting as live until the organizers have tested the full flow and established a secure, auditable manual reconciliation process.
- Never commit or share the Paystack secret key.
