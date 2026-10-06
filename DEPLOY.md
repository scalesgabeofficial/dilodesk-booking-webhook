# D1 Webhook — Railway Deploy Runbook

**Choice (Flint, 2026-09-30): Railway, Hobby plan $5/mo.** Verified pricing 2026-09-30
(Render Starter is $7; Fly is usage-based with more ops surface). One tiny always-on
FastAPI service fits comfortably inside the $5 usage credit.

## Gabe's taps (3 total, ~12 min combined)

### Tap 1 — Railway account (~5 min)
1. Sign up at railway.app (use any email; card required for Hobby).
2. Upgrade the workspace to **Hobby ($5/mo)**.
3. Create an API token: Account Settings → Tokens → create, copy it.
4. Paste the token into the Secure Vault link Flint sends. (Token never goes in chat.)

### Tap 2 — Google Cloud OAuth client (~6 min)
1. console.cloud.google.com → new project `dilodesk-booking`.
2. Enable the **Google Calendar API** (APIs & Services → Library).
3. OAuth consent screen → **External** → fill app name `DiloDesk booking`,
   add your own Gmail as a test user.
4. Credentials → Create Credentials → **OAuth client ID** → type **Web application**.
5. Authorized redirect URI: `https://<railway-domain>/oauth/callback`
   (Flint supplies the exact domain after deploy — come back and add it here.)
6. Copy the **client ID** and **client secret** → Railway project → service →
   **Variables** tab → add `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

### Tap 3 — one-click consent (~1 min)
Click the `/oauth/start?key=…` link Flint sends → **Allow** calendar access → done.
Token is stored server-side; nothing to copy.

## Flint's run (after Tap 1)
1. `npm i -g @railway/cli`; `RAILWAY_TOKEN=… railway …` — new project `dilodesk-booking`,
   new service from this directory (Dockerfile build).
2. Add a **volume** mounted at `/data` (token.json + booking lock survive redeploys).
3. Set variables:
   - `DILODESK_WEBHOOK_SECRET` = generated 32-byte hex (Flint-generated, never in chat)
   - `DILODESK_SECRETS_DIR` = `/data` (already in Dockerfile ENV; override if needed)
   - `DILODESK_CALENDAR_ID` = `primary` (default; see calendar note below)
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — only after Tap 2
4. Deploy → public `https://<service>.up.railway.app` → `GET /health` smoke test.
5. After Tap 3: verify `token.json` landed (indirectly: `/check_availability` with
   secret returns real availability instead of a token error).
6. Attach the two function tools to Maya via the Retell API; run test-plan Steps 2–4.

## Calendar choice (KING Soft B — confirm at Tap 3)
- Default: `primary` calendar on whichever Google account consents.
- Preferred if it exists: a dedicated **DiloDesk booking calendar** — create it in
  Google Calendar first, then set `DILODESK_CALENDAR_ID` to its calendar ID
  (Settings → Integrate calendar). Keeps client bookings isolated from personal events.
- Scopes requested: `https://www.googleapis.com/auth/calendar` only. If Google ever
  shows broader scopes, STOP and re-check.

## Env var reference
| Var | Set by | Notes |
|---|---|---|
| `DILODESK_WEBHOOK_SECRET` | Flint | required; gates all tool + oauth endpoints |
| `DILODESK_CALENDAR_ID` | Flint | `primary` default |
| `DILODESK_SECRETS_DIR` | Dockerfile | `/data` (Railway volume) |
| `DILODESK_USE_FAKE` | — | `1` for pre-consent smoke tests only |
| `GOOGLE_CLIENT_ID` | Gabe (Tap 2) | Railway Variables |
| `GOOGLE_CLIENT_SECRET` | Gabe (Tap 2) | Railway Variables |
| `PORT` | Railway | auto |

## Rollback / revoke (test-plan Step 4)
- Revoke: Google Account → Security → Third-party access → remove `DiloDesk booking`.
  Webhook then fails closed (token invalid → 500, no bookings). Delete `token.json`
  from the volume to fully clear.
- Tear down: Railway dashboard → delete service; cancel Hobby to stop billing.
