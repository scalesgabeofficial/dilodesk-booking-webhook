#!/usr/bin/env python3
"""Attach D1 booking function tools to Maya's Retell LLM.

Reads the webhook secret from disk (never prints it). Idempotent: replaces
tools with the same name if they already exist.
"""
import json
import sys
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
import dynamic_credentials as dc

BASE = "https://api.retellai.com"
HOSTS = ["api.retellai.com"]
CRED = "custom.retell"
LLM_ID = "llm_eb9b86b16f8297680f1e5903afbd"
AGENT_ID = "agent_af0b299bfe9872dccd3b609b44"
WEBHOOK = "https://webhook-production-54b0.up.railway.app"


def call(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    dc.add_surrogate_to_request(req, CRED, allowed_hosts=HOSTS)
    resp = urllib.request.urlopen(req, timeout=60)
    return dc.read_json_response(resp)


def main():
    with open("/home/hatch/workspace/goals/ai-receptionist-agency/hidden_files/booking-webhook/.secrets/webhook_secret") as f:
        secret = f.read().strip()
    if not secret:
        print("FAIL: empty secret file")
        sys.exit(1)

    llm = call("GET", f"/get-retell-llm/{LLM_ID}")
    existing = llm.get("general_tools") or []
    print("existing tools:", [(t.get("type"), t.get("name")) for t in existing])

    def tool(name, description, path, params, exec_msg):
        return {
            "type": "custom",
            "name": name,
            "description": description,
            "url": WEBHOOK + path,
            "speak_during_execution": True,
            "speak_after_execution": True,
            "execution_message_description": exec_msg,
            "parameters": params,
            "headers": {"X-Webhook-Secret": secret},
        }

    new_tools = [
        tool(
            "check_availability",
            "Check whether a requested date/time slot is free on the DiloDesk booking calendar. "
            "Always call this before offering a specific appointment time. All times are America/New_York. "
            "Business hours are Monday-Friday 9:00 AM to 5:00 PM Eastern.",
            "/check_availability",
            {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date as YYYY-MM-DD, e.g. 2026-10-07"},
                    "start_time": {"type": "string", "description": "Start time as HH:MM in 24-hour Eastern Time, e.g. 14:30"},
                    "duration_min": {"type": "integer", "description": "Length in minutes, default 30", "default": 30},
                },
                "required": ["date", "start_time"],
            },
            "Let me check the calendar for that time.",
        ),
        tool(
            "book_appointment",
            "Book a consultation on the DiloDesk booking calendar. Only call after check_availability "
            "returned available true for the exact slot, and only after the caller confirmed the date and time. "
            "Collect the caller's full name and phone number before calling.",
            "/book_appointment",
            {
                "type": "object",
                "properties": {
                    "caller_id": {"type": "string", "description": "Stable identifier for the caller, e.g. their phone number"},
                    "name": {"type": "string", "description": "Caller's full name"},
                    "phone": {"type": "string", "description": "Caller's phone number"},
                    "date": {"type": "string", "description": "Date as YYYY-MM-DD, e.g. 2026-10-07"},
                    "start_time": {"type": "string", "description": "Start time as HH:MM in 24-hour Eastern Time, e.g. 14:30"},
                    "duration_min": {"type": "integer", "description": "Length in minutes, default 30", "default": 30},
                    "notes": {"type": "string", "description": "Optional notes, e.g. practice name or reason for visit"},
                },
                "required": ["caller_id", "name", "phone", "date", "start_time"],
            },
            "Booking that for you now.",
        ),
    ]

    names = {t["name"] for t in new_tools}
    merged = [t for t in existing if t.get("name") not in names] + new_tools

    updated = call("PATCH", f"/update-retell-llm/{LLM_ID}", {"general_tools": merged})
    got = [(t.get("type"), t.get("name")) for t in (updated.get("general_tools") or [])]
    print("tools after PATCH:", got)
    ok = names.issubset({n for _, n in got})
    print("ATTACH_OK" if ok else "ATTACH_MISMATCH")

    if ok:
        pub = call("POST", f"/publish-agent/{AGENT_ID}", {})
        print("publish-agent response:", json.dumps(pub)[:200])


if __name__ == "__main__":
    main()
