# THE ROYAL AFFAIR 2026 — USSD backend (staging)

This is an initial staging backend for the COP Heman Assembly event.

## Render settings
- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`

## Endpoints
- `GET /` — service status
- `GET /health` — health check
- `POST /ussd/callback` — Arkesel USSD callback

## Arkesel request/response
The callback accepts Arkesel's documented JSON fields:
`sessionID`, `userID`, `newSession`, `msisdn`, `userData`, `network`.

It responds with `sessionID`, `userID`, `msisdn`, `message`, and `continueSession`.

## Important limitations — do not use for live voting yet
1. Session state is currently in memory and is lost on restart/redeploy.
2. Nominee lists are empty placeholders in `nominees.json`.
3. Payment initiation and server-to-server payment verification are NOT implemented.
4. Votes are deliberately NOT recorded and payment is deliberately disabled.
5. Add persistent database storage, real nominee data, payment provider integration,
   callback verification, duplicate-transaction protection, closing-time enforcement,
   and security review before production.

## Adding nominees
Edit `nominees.json`. Use category numbers 1–15 as keys. Example:

```json
{
  "1": [
    {"name": "Nominee One", "code": "FM01"},
    {"name": "Nominee Two", "code": "FM02"}
  ],
  "2": []
}
```

Keep nominee codes unique within each category. Do not add real payment credentials to this file or commit secrets to GitHub.
