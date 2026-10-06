"""DiloDesk booking webhook — D1 "A now" live slot feed (production).

Exposes two function-tool endpoints for the Retell voice agent:
  POST /check_availability  -> is the slot free? if not, suggest alternatives
  POST /book_appointment    -> book it, idempotently (retries are no-ops)

  GET /health               -> liveness probe (open)
  GET /oauth/start          -> begin Google consent (secret-gated, one click for Gabe)
  GET /oauth/callback       -> Google redirects here; token stored server-side

Production guards:
  1. Every booking/tool endpoint and /oauth/start require the shared webhook
     secret (header X-Webhook-Secret or ?key=). /health stays open.
     /oauth/callback is reached by Google's redirect without our secret, so it
     is guarded instead by a one-time server-issued `state` (15-min TTL,
     single use, burned on arrival).
  2. book_appointment holds an exclusive file lock around its final
     check-then-insert, so two simultaneous callers can't take one slot
     (single-replica safe).
  3. Deterministic event ids: a retried call inserts the same id; Google
     answers 409 and we treat it as success instead of a second event.
  4. All times resolved in America/New_York with explicit timeZone on writes.
  5. OAuth requests the narrow calendar scope only.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import secrets as _secrets
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from gcal import (BUSINESS_TZ, CalendarBackend, FakeBackend, GCalBackend,
                  _AlreadyExists, _Conflict, deterministic_event_id, to_aware)

app = FastAPI(title="DiloDesk booking webhook")

# ---- config (env-overridable) ----
CALENDAR_ID = os.environ.get("DILODESK_CALENDAR_ID", "primary")
SECRETS_DIR = os.environ.get("DILODESK_SECRETS_DIR",
                             os.path.dirname(os.path.abspath(__file__)))
WORK_START_HOUR = int(os.environ.get("DILODESK_WORK_START", "9"))
WORK_END_HOUR = int(os.environ.get("DILODESK_WORK_END", "17"))
WEBHOOK_SECRET = os.environ.get("DILODESK_WEBHOOK_SECRET", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
LOCK_PATH = os.path.join(SECRETS_DIR, ".book.lock")
TOKEN_PATH = os.path.join(SECRETS_DIR, "token.json")
STATE_PATH = os.path.join(SECRETS_DIR, ".oauth_states.json")
STATE_TTL = 15 * 60  # pending OAuth states live 15 minutes

_backend: CalendarBackend | None = None


def backend() -> CalendarBackend:
    global _backend
    if _backend is None:
        if os.environ.get("DILODESK_USE_FAKE", "") == "1":
            _backend = FakeBackend()
        else:
            _backend = GCalBackend(CALENDAR_ID, SECRETS_DIR)
    return _backend


def set_backend(b: CalendarBackend) -> None:
    global _backend
    _backend = b


def reset_backend() -> None:
    """Drop the cached backend so the next call re-reads token.json (post-consent)."""
    global _backend
    _backend = None


def require_secret(request: Request) -> None:
    if not WEBHOOK_SECRET:
        raise HTTPException(500, "server misconfigured: webhook secret not set")
    got = request.headers.get("x-webhook-secret") or request.query_params.get("key")
    if not _secrets.compare_digest(got or "", WEBHOOK_SECRET):
        raise HTTPException(401, "bad webhook secret")


@contextmanager
def booking_lock():
    """Exclusive lock around check-then-insert (single replica)."""
    os.makedirs(SECRETS_DIR, exist_ok=True)
    with open(LOCK_PATH, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---- models ----
class AvailabilityRequest(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")          # YYYY-MM-DD
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")          # HH:MM (ET)
    duration_min: int = Field(default=30, ge=15, le=240)


class AvailabilityResponse(BaseModel):
    available: bool
    requested_start: str
    requested_end: str
    conflicts: list[str] = []
    suggestions: list[dict] = []


class BookRequest(BaseModel):
    caller_id: str = Field(min_length=1)                       # stable per caller (phone or name+phone)
    name: str = Field(min_length=1)
    phone: str = Field(min_length=7)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")          # HH:MM (ET)
    duration_min: int = Field(default=30, ge=15, le=240)
    notes: str = ""


class BookResponse(BaseModel):
    booked: bool
    event_id: str
    start: str
    end: str
    message: str


# ---- helpers ----
def within_work_hours(start: datetime, end: datetime) -> bool:
    if start.weekday() >= 5:  # Sat/Sun
        return False
    day_start = start.replace(hour=WORK_START_HOUR, minute=0, second=0, microsecond=0)
    day_end = start.replace(hour=WORK_END_HOUR, minute=0, second=0, microsecond=0)
    return start >= day_start and end <= day_end


def slot_free(b: CalendarBackend, start: datetime, end: datetime) -> list[dict]:
    return b.list_events(start, end)


def next_suggestions(b: CalendarBackend, start: datetime, duration: timedelta,
                     n: int = 3) -> list[dict]:
    """Next n free slots of `duration`, scanning forward up to 5 business days."""
    out, cursor = [], start
    deadline = start + timedelta(days=7)
    step = timedelta(minutes=30)
    while len(out) < n and cursor < deadline:
        cand_end = cursor + duration
        if within_work_hours(cursor, cand_end) and not slot_free(b, cursor, cand_end):
            out.append({"date": cursor.strftime("%Y-%m-%d"),
                        "start_time": cursor.strftime("%H:%M"),
                        "end_time": cand_end.strftime("%H:%M")})
        cursor += step
        # jump to next workday morning when past work hours
        if cursor.hour >= WORK_END_HOUR:
            cursor = (cursor + timedelta(days=1)).replace(hour=WORK_START_HOUR, minute=0,
                                                         second=0, microsecond=0)
    return out


AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _load_states() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_states(states: dict) -> None:
    os.makedirs(SECRETS_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(states, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def _prune_states(states: dict) -> dict:
    now = time.time()
    fresh = {}
    for s, v in states.items():
        ts = v.get("ts") if isinstance(v, dict) else None
        if ts is not None and now - ts < STATE_TTL:
            fresh[s] = v
    return fresh


def _issue_state() -> tuple[str, str]:
    """Mint (state, PKCE code_verifier) for one OAuth round-trip, server-side."""
    states = _prune_states(_load_states())
    state = _secrets.token_urlsafe(32)
    verifier = _b64url(_secrets.token_bytes(32))
    states[state] = {"ts": time.time(), "verifier": verifier}
    _save_states(states)
    return state, verifier


def _consume_state(state: str) -> str:
    """Validate and burn a state token; returns its PKCE verifier."""
    states = _load_states()
    entry = states.pop(state, None)
    _save_states(states)
    verifier = entry.get("verifier") if isinstance(entry, dict) else None
    ts = entry.get("ts") if isinstance(entry, dict) else None
    if not verifier or ts is None or time.time() - ts >= STATE_TTL:
        raise HTTPException(403, "invalid or expired OAuth state")
    return verifier


def _check_oauth_config() -> None:
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        raise HTTPException(500, "Google OAuth client not configured on server")


def _redirect_uri(request: Request) -> str:
    host = request.headers.get("host", "")
    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
    return f"{scheme}://{host}/oauth/callback"


# ---- routes ----
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/oauth/start")
def oauth_start(request: Request):
    """Gabe's one tap: secret-gated link -> Google consent -> token stored server-side."""
    require_secret(request)
    _check_oauth_config()
    state, verifier = _issue_state()
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(AUTH_URI + "?" + urllib.parse.urlencode(params))


@app.get("/oauth/callback")
def oauth_callback(request: Request, code: str = "", error: str = "",
                   state: str = ""):
    # NOT secret-gated: Google redirects here without our secret. The one-time
    # server-issued `state` is the CSRF/replay guard instead.
    verifier = _consume_state(state)
    if error:
        raise HTTPException(400, f"Google OAuth error: {error}")
    if not code:
        raise HTTPException(400, "missing authorization code")
    _check_oauth_config()
    import requests
    try:
        r = requests.post(TOKEN_URI, data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": _redirect_uri(request),
        }, timeout=20)
    except Exception as e:
        raise HTTPException(502, f"token exchange request failed: {e}")
    if r.status_code != 200:
        raise HTTPException(502, f"token exchange failed: {r.text[:200]}")
    tok = r.json()
    if "refresh_token" not in tok:
        raise HTTPException(502, "no refresh token returned; re-run consent")
    creds = {"type": "authorized_user",
             "client_id": GOOGLE_CLIENT_ID,
             "client_secret": GOOGLE_CLIENT_SECRET,
             "refresh_token": tok["refresh_token"]}
    os.makedirs(SECRETS_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        json.dump(creds, f)
    os.chmod(TOKEN_PATH, 0o600)
    reset_backend()
    return HTMLResponse(
        "<h3>DiloDesk calendar connected.</h3>"
        "<p>You can close this tab — no further action needed.</p>")


@app.post("/check_availability", response_model=AvailabilityResponse)
def check_availability(req: AvailabilityRequest, request: Request):
    require_secret(request)
    b = backend()
    try:
        start = to_aware(req.date, req.start_time)
    except ValueError:
        raise HTTPException(400, "bad date/time format")
    end = start + timedelta(minutes=req.duration_min)

    if not within_work_hours(start, end):
        return AvailabilityResponse(
            available=False, requested_start=start.isoformat(),
            requested_end=end.isoformat(),
            conflicts=["outside business hours (Mon-Fri 9:00-17:00 ET)"],
            suggestions=next_suggestions(b, start, timedelta(minutes=req.duration_min)))

    conflicts = slot_free(b, start, end)
    if conflicts:
        return AvailabilityResponse(
            available=False, requested_start=start.isoformat(),
            requested_end=end.isoformat(),
            conflicts=[f"busy "
                       f"{c['start'].astimezone(BUSINESS_TZ).strftime('%H:%M')}-"
                       f"{c['end'].astimezone(BUSINESS_TZ).strftime('%H:%M')} ET"
                       for c in conflicts],
            suggestions=next_suggestions(b, start, timedelta(minutes=req.duration_min)))
    return AvailabilityResponse(available=True, requested_start=start.isoformat(),
                                requested_end=end.isoformat())


@app.post("/book_appointment", response_model=BookResponse)
def book_appointment(req: BookRequest, request: Request):
    require_secret(request)
    b = backend()
    try:
        start = to_aware(req.date, req.start_time)
    except ValueError:
        raise HTTPException(400, "bad date/time format")
    end = start + timedelta(minutes=req.duration_min)

    if not within_work_hours(start, end):
        raise HTTPException(409, "requested slot is outside business hours (Mon-Fri 9:00-17:00 ET)")

    # Exclusive lock: the final re-validation + insert is atomic against
    # simultaneous callers. Our own earlier booking (same deterministic id)
    # is not a conflict.
    event_id = deterministic_event_id(req.caller_id, start)
    with booking_lock():
        if [e for e in slot_free(b, start, end) if e["id"] != event_id]:
            raise HTTPException(409, "slot was just taken — please pick another time")

        summary = f"DiloDesk consult — {req.name}"
        desc = (f"Booked by Maya (DiloDesk AI receptionist).\nName: {req.name}\n"
                f"Phone: {req.phone}\nNotes: {req.notes}")
        try:
            b.insert_event(event_id, summary, start, end, desc)
        except _AlreadyExists:
            pass  # idempotent retry: the event is already ours -> success
        except _Conflict:
            raise HTTPException(409, "slot was just taken — please pick another time")

    when = start.strftime("%A, %B %d at %I:%M %p ET")
    return BookResponse(booked=True, event_id=event_id,
                        start=start.isoformat(), end=end.isoformat(),
                        message=f"Booked for {when}.")
