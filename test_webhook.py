"""Local tests for the booking webhook — fake calendar, no network."""
import os
import sys
import json

os.environ["DILODESK_USE_FAKE"] = "1"
os.environ["DILODESK_WEBHOOK_SECRET"] = "test-secret"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

import app as appmod
from gcal import FakeBackend, to_aware

client = TestClient(appmod.app)
backend = FakeBackend()
appmod.set_backend(backend)

H = {"X-Webhook-Secret": "test-secret"}
BAD_H = {"X-Webhook-Secret": "wrong"}

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


print("== health ==")
r = client.get("/health")
check("health ok", r.status_code == 200 and r.json()["ok"])

print("== auth gate ==")
r = client.post("/check_availability",
                json={"date": "2026-10-05", "start_time": "10:00", "duration_min": 30})
check("no secret -> 401", r.status_code == 401, r.status_code)
r = client.post("/check_availability", headers=BAD_H,
                json={"date": "2026-10-05", "start_time": "10:00", "duration_min": 30})
check("wrong secret -> 401", r.status_code == 401, r.status_code)
r = client.post("/book_appointment",
                json={"caller_id": "x", "name": "x", "phone": "+13055550000",
                      "date": "2026-10-05", "start_time": "10:00", "duration_min": 30})
check("book no secret -> 401", r.status_code == 401, r.status_code)
r = client.get("/oauth/start")
check("oauth/start no secret -> 401", r.status_code == 401, r.status_code)
r = client.get("/oauth/start", headers=H, follow_redirects=False)
check("oauth/start, no Google client configured -> 500", r.status_code == 500, r.status_code)

print("== availability: free slot ==")
r = client.post("/check_availability", headers=H,
                json={"date": "2026-10-05", "start_time": "10:00", "duration_min": 30})
check("free slot available", r.json()["available"] is True)
check("ET isoformat", "+00:00" not in r.json()["requested_start"]
      and "2026-10-05T10:00:00-04:00" == r.json()["requested_start"],
      r.json()["requested_start"])

print("== availability: busy slot refused with suggestions ==")
backend.seed_busy(to_aware("2026-10-05", "15:00"), to_aware("2026-10-05", "16:00"))
r = client.post("/check_availability", headers=H,
                json={"date": "2026-10-05", "start_time": "15:30", "duration_min": 30})
j = r.json()
check("busy slot not available", j["available"] is False)
check("conflict named", len(j["conflicts"]) == 1, j["conflicts"])
check("suggestions offered", len(j["suggestions"]) >= 1, j["suggestions"])

print("== availability: outside work hours ==")
r = client.post("/check_availability", headers=H,
                json={"date": "2026-10-05", "start_time": "20:00", "duration_min": 30})
check("evening refused", r.json()["available"] is False)

print("== booking: happy path ==")
r = client.post("/book_appointment", headers=H,
                json={"caller_id": "+13055550100", "name": "Test Caller",
                      "phone": "+13055550100", "date": "2026-10-06",
                      "start_time": "11:00", "duration_min": 30})
j = r.json()
check("booked", j["booked"] is True)
check("event persisted", any(e["id"] == j["event_id"] for e in backend.events))
check("message names ET", "ET" in j["message"], j["message"])

print("== booking: idempotent retry ==")
r2 = client.post("/book_appointment", headers=H,
                 json={"caller_id": "+13055550100", "name": "Test Caller",
                       "phone": "+13055550100", "date": "2026-10-06",
                       "start_time": "11:00", "duration_min": 30})
check("retry still 200 booked", r2.status_code == 200 and r2.json()["booked"] is True)
check("no duplicate event", sum(1 for e in backend.events
      if e["start"] == to_aware("2026-10-06", "11:00")) == 1)

print("== booking: double-book refused ==")
r = client.post("/book_appointment", headers=H,
                json={"caller_id": "+13055550200", "name": "Other Caller",
                      "phone": "+13055550200", "date": "2026-10-06",
                      "start_time": "11:15", "duration_min": 30})
check("overlap -> 409", r.status_code == 409, r.status_code)

print("== booking: outside hours -> 409 ==")
r = client.post("/book_appointment", headers=H,
                json={"caller_id": "+13055550300", "name": "Night Caller",
                      "phone": "+13055550300", "date": "2026-10-06",
                      "start_time": "22:00", "duration_min": 30})
check("night -> 409", r.status_code == 409)

print("== oauth: callback state guard ==")
r = client.get("/oauth/callback?code=x&state=bogus")
check("callback, bad state -> 403", r.status_code == 403, r.status_code)
r = client.get("/oauth/callback?code=x")
check("callback, missing state -> 403", r.status_code == 403, r.status_code)
st, verifier = appmod._issue_state()
check("state+verifier issued", bool(st) and bool(verifier) and st != verifier)
check("verifier looks like base64url",
      len(verifier) >= 43 and "=" not in verifier, verifier[:12])
r = client.get(f"/oauth/callback?code=x&state={st}")
check("callback, good state but no Google client -> 500 (config gate)",
      r.status_code == 500, r.status_code)
r = client.get(f"/oauth/callback?code=x&state={st}")
check("callback, replayed state -> 403", r.status_code == 403, r.status_code)

print("== oauth: start builds PKCE url ==")
appmod.GOOGLE_CLIENT_ID = "test-client-id"
appmod.GOOGLE_CLIENT_SECRET = "test-client-secret"
r = client.get("/oauth/start", headers=H, follow_redirects=False)
loc = r.headers.get("location", "")
check("start -> 307 redirect", r.status_code == 307, r.status_code)
check("auth url has PKCE S256 challenge",
      "code_challenge_method=S256" in loc and "code_challenge=" in loc)
check("auth url carries state", "state=" in loc)
check("auth url asks offline consent",
      "access_type=offline" in loc and "prompt=consent" in loc)
appmod.GOOGLE_CLIENT_ID = ""
appmod.GOOGLE_CLIENT_SECRET = ""

print("== conflict labels normalize to ET ==")
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _ZI
_pt_backend = FakeBackend()
appmod.set_backend(_pt_backend)
# Simulate what Google returns when the calendar sits on Pacific:
# the same instant as 10:00-10:30 ET, expressed with a -07:00 offset.
_pt_backend.seed_busy(_dt(2026, 10, 5, 7, 0, tzinfo=_ZI("America/Los_Angeles")),
                      _dt(2026, 10, 5, 7, 30, tzinfo=_ZI("America/Los_Angeles")),
                      "PT Busy Block")
r = client.post("/check_availability", headers=H,
                json={"date": "2026-10-05", "start_time": "10:00", "duration_min": 30})
_confs = r.json().get("conflicts", [])
check("pt-zone event blocks the ET slot", r.json()["available"] is False and len(_confs) == 1,
      str(_confs))
check("conflict label shows ET wall time, not PT",
      _confs and "10:00-10:30 ET" in _confs[0], str(_confs))

print("== conflict labels never leak event titles ==")
_priv_backend = FakeBackend()
appmod.set_backend(_priv_backend)
_priv_backend.seed_busy(_dt(2026, 10, 6, 10, 0, tzinfo=_ZI("America/New_York")),
                        _dt(2026, 10, 6, 10, 30, tzinfo=_ZI("America/New_York")),
                        "DiloDesk consult - Jane Doe")
r = client.post("/check_availability", headers=H,
                json={"date": "2026-10-06", "start_time": "10:00", "duration_min": 30})
_body = r.json()
_raw = json.dumps(_body)
check("named event blocks the slot", _body["available"] is False, _raw[:200])
check("caller name appears nowhere in response", "Jane Doe" not in _raw, _raw[:200])
check("event title appears nowhere in response",
      "DiloDesk consult - Jane Doe" not in _raw, _raw[:200])
check("label is anonymous busy range",
      _body.get("conflicts") == ["busy 10:00-10:30 ET"], str(_body.get("conflicts")))

print("== book_appointment conflicts never leak event titles ==")
_book_backend = FakeBackend()
appmod.set_backend(_book_backend)
# Seed a named event that will conflict with the booking attempt
_book_backend.seed_busy(_dt(2026, 10, 7, 10, 0, tzinfo=_ZI("America/New_York")),
                        _dt(2026, 10, 7, 10, 30, tzinfo=_ZI("America/New_York")),
                        "DiloDesk consult - Jane Doe")
r = client.post("/book_appointment", headers=H,
                json={"caller_id": "test-privacy", "name": "John Smith",
                      "phone": "+13050000000", "date": "2026-10-07",
                      "start_time": "10:00", "duration_min": 30})
_braw = json.dumps({"status": r.status_code, "body": r.json() if r.headers.get("content-type","").startswith("application/json") else r.text})
check("booking conflict returns 409", r.status_code == 409, _braw[:200])
check("caller name appears nowhere in booking 409", "Jane Doe" not in _braw, _braw[:200])
check("event title appears nowhere in booking 409",
      "DiloDesk consult - Jane Doe" not in _braw, _braw[:200])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
