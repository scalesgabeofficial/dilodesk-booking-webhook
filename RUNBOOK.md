# D1 "A now" — booking webhook runbook

## What exists (2026-09-30 ~17:45 ET)
- Code: `hidden_files/booking-webhook/` — `app.py` (FastAPI), `gcal.py`
  (GCal + fake backends), `test_webhook.py`, `.venv/`
- Endpoints: `GET /health`, `POST /check_availability`, `POST /book_appointment`
- Local tests: 14/14 pass (fake backend) — availability, busy-refusal +
  suggestions, work-hours guard, idempotent retry, double-book 409, ET
  isoformat on all datetimes.
- Guards: deterministic event ids (retry = no-op), re-validate-before-insert,
  explicit America/New_York on writes, Mon–Fri 9:00–17:00 ET default hours.

## Blocked on (needs Gabe)
1. **Public HTTPS URL.** cloudflared quick tunnel fails in this sandbox
   (TLS-intercepting egress). Needs real hosting: Railway/Render/Fly (~$5/mo)
   or equivalent. His call on provider.
2. **Google OAuth consent for OUR backend** (not Retell — Retell has no native
   GCal OAuth; its built-in scheduling is Cal.com). Needs: GCP OAuth client id
   (created under his Google account) + one consent click granting
   calendar read/write to the backend. token.json lands in this dir (mode 600).
3. After 1+2: attach `check_availability` + `book_appointment` as custom
   function tools on Maya's LLM (`llm_eb9b86b16f8297680f1e5903afbd`) via
   Retell PATCH /update-retell-llm (API key in hand — no dashboard needed),
   then run test-plan Steps 2–4 for real.

## Notes
- A local uvicorn was left running on 127.0.0.1:8471 with the FAKE backend
  for reference; it is NOT public and dies on VM replacement.
- Retell dashboard sign-in is passkey-gated (Face/fingerprint) — cloud browser
  can't complete it. All Retell changes go through the API.
- Test plan: `files/calendar-connect-test-plan-2026-09-30.md`
  (Drive 1JLYsZYpsHNSDgNXO5AgL7KqZwDxNg-88). Steps 2–4 stand; Step 1 mechanism
  corrected (custom tools, not native OAuth).
