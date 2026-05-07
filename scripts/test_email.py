"""scripts/test_email.py -- one-shot Resend email verification.
Run: python scripts/test_email.py
"""
import json
import os
import urllib.request
import urllib.error

API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_TO = os.environ.get("TRIAL_QUEUE_EMAIL_TO", "")
EMAIL_FROM = "onboarding@resend.dev"

if not API_KEY or not EMAIL_TO:
    raise SystemExit("ERROR: RESEND_API_KEY and TRIAL_QUEUE_EMAIL_TO must be set")

print(f"FROM : {EMAIL_FROM}")
print(f"TO   : {EMAIL_TO}")
print(f"KEY  : {API_KEY[:8]}...")

payload = json.dumps({
    "from": EMAIL_FROM,
    "to": [EMAIL_TO],
    "subject": "test: crypto-bot email verification",
    "text": "Email wired up correctly.",
}).encode()

req = urllib.request.Request(
    "https://api.resend.com/emails",
    data=payload,
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "crypto-bot/1.0",
    },
)
try:
    with urllib.request.urlopen(req) as r:
        body = json.loads(r.read().decode())
        print(f"OK {r.status} -- id: {body.get('id')}")
except urllib.error.HTTPError as e:
    print(f"FAILED {e.code}: {e.read().decode()}")
