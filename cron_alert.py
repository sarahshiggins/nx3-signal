"""
NX3 Signal — Daily Alert Cron Job
Runs daily at 8 AM CT via Railway cron.
Calls the /api/send-alert endpoint to check for changes in pinned verticals.
"""
import os
import requests
import sys

ALERT_URL = os.environ.get("ALERT_URL", "https://nx3-signal-production.up.railway.app/api/send-alert")
ALERT_SECRET = os.environ.get("ALERT_SECRET", "")

if not ALERT_SECRET:
    print("ERROR: ALERT_SECRET env var not set")
    sys.exit(1)

print(f"Triggering daily alert scan...")
try:
    resp = requests.post(
        ALERT_URL,
        headers={
            "X-Alert-Secret": ALERT_SECRET,
            "Content-Type": "application/json",
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"SUCCESS: {data.get('sent', 0)} emails sent, {data.get('total_users', 0)} users, {data.get('total_verticals_analyzed', 0)} verticals analyzed")
    if data.get("errors"):
        print(f"ERRORS: {data['errors']}")
except Exception as e:
    print(f"FAILED: {e}")
    sys.exit(1)
