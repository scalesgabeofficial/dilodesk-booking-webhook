"""Google Calendar backend for the DiloDesk booking webhook.

Two implementations behind one interface:
  - GCalBackend: real Google Calendar API (needs OAuth token.json)
  - FakeBackend: in-memory, for local tests (no network, no credentials)

All datetimes are resolved in BUSINESS_TZ (America/New_York) and sent to
Google with an explicit IANA timeZone on both start and end.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/New_York")


def to_aware(date_str: str, time_str: str) -> datetime:
    """'2026-10-02' + '14:30' -> aware datetime in BUSINESS_TZ."""
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=BUSINESS_TZ)


def deterministic_event_id(caller_id: str, start: datetime) -> str:
    """Stable Google event id so a retried booking is a no-op, not a duplicate.

    Google ids must be base32hex, 5-1024 chars. A retry inserts the same id and
    Google answers 409 alreadyExists, which we treat as success.
    """
    raw = f"{caller_id}|{start.isoformat()}".encode()
    return hashlib.sha1(raw).hexdigest()[:32]


class CalendarBackend:
    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        raise NotImplementedError

    def insert_event(self, event_id: str, summary: str, start: datetime,
                     end: datetime, description: str = "") -> dict:
        raise NotImplementedError


class FakeBackend(CalendarBackend):
    """In-memory backend for tests. busy_blocks: list of (start, end) aware datetimes."""

    def __init__(self):
        self.events: list[dict] = []

    def seed_busy(self, start: datetime, end: datetime, summary: str = "BUSY"):
        self.events.append({"id": f"seed-{len(self.events)}", "summary": summary,
                            "start": start, "end": end})

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        return [e for e in self.events if e["start"] < end and e["end"] > start]

    def insert_event(self, event_id: str, summary: str, start: datetime,
                     end: datetime, description: str = "") -> dict:
        for e in self.events:
            if e["id"] == event_id:
                raise _AlreadyExists(event_id)  # idempotent retry -> 409 equivalent
            if e["start"] < end and e["end"] > start:
                raise _Conflict("slot overlaps an existing event")
        ev = {"id": event_id, "summary": summary, "start": start,
              "end": end, "description": description}
        self.events.append(ev)
        return ev


class _AlreadyExists(Exception):
    pass


class _Conflict(Exception):
    pass


class GCalBackend(CalendarBackend):
    """Real Google Calendar API backend. token.json must exist (created at consent time)."""

    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self, calendar_id: str, secrets_dir: str):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        import os

        token_path = os.path.join(secrets_dir, "token.json")
        if not os.path.exists(token_path):
            raise RuntimeError(
                f"token.json not found in {secrets_dir} — run the OAuth consent flow first.")
        creds = Credentials.from_authorized_user_file(token_path, self.SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                raise RuntimeError("Stored credentials invalid and not refreshable — re-run consent.")
        self.service = build("calendar", "v3", credentials=creds)
        self.calendar_id = calendar_id

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        res = self.service.events().list(
            calendarId=self.calendar_id,
            timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime").execute()
        out = []
        for e in res.get("items", []):
            s = e["start"].get("dateTime")
            en = e["end"].get("dateTime")
            if not s or not en:
                continue  # all-day events don't block booking slots
            out.append({"id": e["id"], "summary": e.get("summary", ""),
                        "start": datetime.fromisoformat(s), "end": datetime.fromisoformat(en)})
        return out

    def insert_event(self, event_id: str, summary: str, start: datetime,
                     end: datetime, description: str = "") -> dict:
        from googleapiclient.errors import HttpError
        body = {"id": event_id, "summary": summary, "description": description,
                "start": {"dateTime": start.isoformat(), "timeZone": "America/New_York"},
                "end": {"dateTime": end.isoformat(), "timeZone": "America/New_York"}}
        try:
            e = self.service.events().insert(calendarId=self.calendar_id, body=body).execute()
        except HttpError as ex:
            if ex.resp.status == 409:
                raise _AlreadyExists(event_id)
            raise
        return {"id": e["id"], "summary": e.get("summary", ""), "start": start, "end": end}
