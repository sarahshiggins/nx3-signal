"""
NX3 Signal — Flask Backend
Nexus3 | Vertical Market Intelligence Platform

Routes:
  GET  /                     → Serve frontend
  POST /api/analyze          → Perplexity-powered market analysis
  POST /api/pin              → Pin a vertical (SQLite)
  GET  /api/pins             → Get pins by email
  DELETE /api/pins/<pin_id>  → Remove a pin
  POST /api/send-alert       → Send weekly digest emails (cron target)
  GET  /health               → Railway health check
"""

import os
import json
import sqlite3
import datetime
import traceback
from functools import wraps

import requests
from flask import Flask, request, jsonify, render_template, g
from flask_cors import CORS

# ─── App Setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests (for local dev / CDN frontends)

# ─── Config from Environment ──────────────────────────────────────────────────

PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
ALERT_SECRET = os.environ.get("ALERT_SECRET", "change-me-in-production")
DATABASE_PATH = os.environ.get("DATABASE_URL", "nx3signal.db")

# Strip sqlite:/// prefix if someone passes a full URL
if DATABASE_PATH.startswith("sqlite:///"):
    DATABASE_PATH = DATABASE_PATH[len("sqlite:///"):]

# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    """Get a database connection, creating one per request if needed."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row  # Return rows as dict-like objects
    return g.db


@app.teardown_appcontext
def close_db(error):
    """Close database connection at end of request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Initialize the database schema on startup."""
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vertical TEXT NOT NULL,
                email TEXT NOT NULL,
                label TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vertical TEXT NOT NULL,
                email TEXT,
                result_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()


# ─── Prompt Builder ───────────────────────────────────────────────────────────

def build_analysis_prompt(vertical: str) -> str:
    """Build the Nexus3 thesis-scoring prompt for a given vertical."""
    return f"""You are analyzing the "{vertical}" market vertical for Nexus3, an AI venture studio.

Nexus3's Tier 1 portfolio verticals for context: Energy/Utilities (OT/SCADA/FERC/PUC regulatory), Legal (document-heavy, LangGraph segments), Healthcare RCM (denials/appeals, payer integrations), Insurance (claims adjudication, carrier core systems).

Respond ONLY with valid JSON (no markdown, no extra text) matching this exact schema:

{{
  "vertical": string,
  "overview": {{
    "description": string (2-3 sentences about the vertical),
    "marketSize": string (e.g. "$45B TAM"),
    "primaryWorkflow": string (the main workflow AI would replace)
  }},
  "competitors": {{
    "traditional": [
      {{ "name": string, "description": string }}
    ],
    "aiNative": [
      {{ "name": string, "description": string }}
    ],
    "whitespace": string (1-2 sentences on the gap/opportunity)
  }},
  "scores": {{
    "marketSize": {{ "score": number (1-5), "reason": string (1 sentence) }},
    "regulatoryMoat": {{ "score": number (1-5), "reason": string (1 sentence) }},
    "processReplacement": {{ "score": number (1-5), "reason": string (1 sentence) }},
    "capitalEfficiency": {{ "score": number (1-5), "reason": string (1 sentence) }},
    "layer4Moat": {{ "score": number (1-5), "reason": string (1 sentence) }}
  }},
  "venture": {{
    "painkiller": string,
    "beachhead": string,
    "moat": string,
    "revenueModel": string,
    "yearOneLooksLike": [string, string, string],
    "biggestRisk": string
  }},
  "comparable": {{
    "vertical": string,
    "reason": string
  }}
}}

Scoring criteria:
1. marketSize: Is market >$10B? Are segments still manual/paper-heavy? Score 1-5.
2. regulatoryMoat: Heavily regulated? Domain expertise a real barrier? Score 1-5.
3. processReplacement: Can AI replace entire segments (not just assist)? Score 1-5.
4. capitalEfficiency: Path to enterprise contracts? Recurring high-margin revenue? Score 1-5.
5. layer4Moat: Deep integrations (EHR, SCADA, core systems, filing APIs)? Score 1-5.

Be specific, data-driven, and honest. Use real company names for competitors."""


def build_alert_prompt(vertical: str) -> str:
    """Build a concise recent-news prompt for weekly alert digests."""
    return f"""You are a market intelligence analyst for Nexus3.

Search for the most recent news, developments, and signals in the "{vertical}" market vertical from the past 7 days.

Focus on:
- New AI-native companies entering this space (funding rounds, launches)
- Regulatory changes affecting this vertical
- Major incumbent moves (acquisitions, new products)
- Notable pain points or customer complaints emerging
- Market size or growth data updates

Respond ONLY with valid JSON:
{{
  "vertical": "{vertical}",
  "period": "past 7 days",
  "developments": [
    {{"headline": string, "detail": string, "signal": "bullish" | "bearish" | "neutral"}}
  ],
  "summary": string (2-3 sentence executive summary)
}}

Provide 3-5 developments. Be specific with company names, dollar amounts, and dates when available."""


# ─── Helpers ─────────────────────────────────────────────────────────────────

def call_perplexity(prompt: str, system_msg: str = None) -> dict:
    """
    Call the Perplexity sonar API and return the parsed JSON response.
    Raises ValueError if the API key is missing or the response can't be parsed.
    Raises requests.HTTPError on API failures.
    """
    if not PERPLEXITY_API_KEY:
        raise ValueError("PERPLEXITY_API_KEY environment variable is not set.")

    if system_msg is None:
        system_msg = (
            "You are a market research analyst for Nexus3. "
            "Always respond with valid JSON matching the exact schema provided. "
            "No markdown, no code blocks, pure JSON only."
        )

    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "sonar",
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()

    raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")

    # Try parsing directly first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences if present
    import re
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Last resort: find the first {...} block
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        return json.loads(match.group(0))

    raise ValueError(f"Could not parse Perplexity response as JSON. Raw: {raw[:300]}")


def send_resend_email(to_email: str, subject: str, html_body: str) -> bool:
    """Send an HTML email via the Resend API. Returns True on success."""
    if not RESEND_API_KEY:
        app.logger.warning("RESEND_API_KEY not set — skipping email send.")
        return False

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": "NX3 Signal <signal@nexus3cap.com>",
            "to": [to_email],
            "subject": subject,
            "html": html_body,
        },
        timeout=30,
    )
    if not resp.ok:
        app.logger.error(f"Resend error for {to_email}: {resp.status_code} {resp.text}")
        return False
    return True


def build_alert_email_html(email: str, verticals_data: list[dict]) -> str:
    """
    Render a clean HTML digest email for a user's pinned verticals.
    `verticals_data` is a list of dicts with keys: vertical, label, developments, summary
    """
    sections = ""
    for item in verticals_data:
        label = item.get("label") or item["vertical"]
        developments = item.get("developments", [])

        bullets = ""
        for dev in developments[:5]:
            signal = dev.get("signal", "neutral")
            signal_color = {"bullish": "#22c55e", "bearish": "#ef4444"}.get(signal, "#a09890")
            signal_dot = f'<span style="color:{signal_color};font-weight:700">●</span>'
            bullets += f"""
            <li style="margin-bottom:10px;padding-left:4px">
              {signal_dot} <strong style="color:#F5F0E8">{dev.get('headline','')}</strong><br>
              <span style="color:#a09890;font-size:13px">{dev.get('detail','')}</span>
            </li>"""

        sections += f"""
        <div style="margin-bottom:32px;padding:20px 24px;background:#111111;border-radius:8px;border:1px solid #222222;border-left:3px solid #DC2626">
          <h2 style="font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-size:18px;font-weight:700;color:#F5F0E8;margin:0 0 4px 0;letter-spacing:0.03em">
            {label.upper()}
          </h2>
          <p style="color:#a09890;font-size:12px;font-family:monospace;margin:0 0 14px 0;letter-spacing:0.1em">
            VERTICAL MARKET UPDATE — PAST 7 DAYS
          </p>
          <ul style="margin:0 0 14px 0;padding:0;list-style:none;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-size:14px;color:#F5F0E8;line-height:1.6">
            {bullets}
          </ul>
          <p style="margin:0;padding:12px;background:#0a0a0a;border-radius:4px;font-size:13px;color:#a09890;font-style:italic;line-height:1.5">
            {item.get('summary','')}
          </p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>NX3 Signal — Weekly Market Update</title>
</head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif">
  <div style="max-width:640px;margin:0 auto;padding:0 16px 40px">

    <!-- Header -->
    <div style="padding:32px 0 24px;border-bottom:1px solid #222222;margin-bottom:32px">
      <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:0.2em;color:#DC2626;font-family:monospace;text-transform:uppercase">
        Nexus3
      </p>
      <h1 style="margin:0;font-size:36px;font-weight:700;color:#F5F0E8;letter-spacing:0.04em">
        NX3 Signal
      </h1>
      <p style="margin:8px 0 0;font-size:12px;color:#a09890;font-family:monospace;letter-spacing:0.1em;text-transform:uppercase">
        Weekly Market Intelligence Digest
      </p>
    </div>

    <!-- Intro -->
    <p style="font-size:14px;color:#a09890;margin:0 0 28px 0;line-height:1.6">
      Here's what moved in your watchlisted verticals this week.
    </p>

    <!-- Vertical Sections -->
    {sections}

    <!-- Footer -->
    <div style="border-top:1px solid #222222;padding-top:24px;margin-top:32px">
      <p style="margin:0;font-size:12px;color:#4a4540;line-height:1.6">
        You're receiving this because you pinned these verticals in 
        <a href="https://signal.nexus3cap.com" style="color:#DC2626;text-decoration:none">NX3 Signal</a>.<br>
        To unsubscribe, reply to this email with "unsubscribe" or remove your pins from the app.
      </p>
      <p style="margin:12px 0 0;font-size:11px;color:#4a4540;font-family:monospace">
        NX3 Signal · Nexus3 · nexus3cap.com
      </p>
    </div>

  </div>
</body>
</html>"""


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the main frontend."""
    return render_template("index.html")


@app.route("/health")
def health():
    """Railway health check endpoint."""
    return jsonify({"status": "ok"})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """
    Run a market analysis via Perplexity.

    Request body:
        { "vertical": "workers comp insurance" }

    Note: API key is now server-side via PERPLEXITY_API_KEY env var.
    The `api_key` field in the request body is accepted but ignored.
    """
    data = request.get_json(silent=True) or {}
    vertical = (data.get("vertical") or "").strip()

    if not vertical:
        return jsonify({"error": "vertical is required"}), 400

    if not PERPLEXITY_API_KEY:
        return jsonify({"error": "Server is not configured with a Perplexity API key. Contact the admin."}), 503

    try:
        prompt = build_analysis_prompt(vertical)
        result = call_perplexity(prompt)

        # Optionally cache analysis to DB
        try:
            email = data.get("email")
            db = get_db()
            db.execute(
                "INSERT INTO analyses (vertical, email, result_json) VALUES (?, ?, ?)",
                (vertical, email, json.dumps(result)),
            )
            db.commit()
        except Exception as db_err:
            app.logger.warning(f"Failed to cache analysis: {db_err}")

        return jsonify(result)

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else 500
        msg = f"Perplexity API error {status}"
        try:
            msg = e.response.json().get("error", {}).get("message", msg)
        except Exception:
            pass
        app.logger.error(f"Perplexity HTTP error: {e}")
        return jsonify({"error": msg}), 502

    except ValueError as e:
        app.logger.error(f"Parsing error: {e}")
        return jsonify({"error": str(e)}), 502

    except Exception as e:
        app.logger.error(f"Unexpected error in /api/analyze: {traceback.format_exc()}")
        return jsonify({"error": "Internal server error. Please try again."}), 500


@app.route("/api/pin", methods=["POST"])
def pin_vertical():
    """
    Pin a vertical for a user.

    Request body:
        {
            "vertical": "workers comp insurance",
            "email": "tim@nexus3cap.com",
            "label": "Workers Comp"   (optional)
        }
    """
    data = request.get_json(silent=True) or {}
    vertical = (data.get("vertical") or "").strip()
    email = (data.get("email") or "").strip()
    label = (data.get("label") or "").strip() or None

    if not vertical or not email:
        return jsonify({"error": "vertical and email are required"}), 400

    db = get_db()
    cursor = db.execute(
        "INSERT INTO pins (vertical, email, label) VALUES (?, ?, ?)",
        (vertical, email, label),
    )
    db.commit()

    return jsonify({"success": True, "pin_id": cursor.lastrowid})


@app.route("/api/pins", methods=["GET"])
def get_pins():
    """
    Get all pins for an email address.

    Query params:
        email=tim@nexus3cap.com
    """
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"error": "email query parameter is required"}), 400

    db = get_db()
    rows = db.execute(
        "SELECT id, vertical, email, label, created_at FROM pins WHERE email = ? ORDER BY created_at DESC",
        (email,),
    ).fetchall()

    return jsonify([dict(row) for row in rows])


@app.route("/api/pins/<int:pin_id>", methods=["DELETE"])
def delete_pin(pin_id):
    """Delete a pin by ID."""
    db = get_db()
    result = db.execute("DELETE FROM pins WHERE id = ?", (pin_id,))
    db.commit()

    if result.rowcount == 0:
        return jsonify({"error": "Pin not found"}), 404

    return jsonify({"success": True})


@app.route("/api/send-alert", methods=["POST"])
def send_alert():
    """
    Send weekly market digest emails to all users with pins.

    Security: Requires X-Alert-Secret header matching ALERT_SECRET env var.
    Designed to be called by a cron job (e.g., Railway cron, GitHub Actions).

    Process:
        1. Load all pins, grouped by email
        2. For each pin, fetch recent Perplexity news
        3. Build digest email per user
        4. Send via Resend
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    secret = request.headers.get("X-Alert-Secret", "")
    if secret != ALERT_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    db = get_db()
    all_pins = db.execute(
        "SELECT id, vertical, email, label FROM pins ORDER BY email, vertical"
    ).fetchall()

    if not all_pins:
        return jsonify({"message": "No pins found — nothing to send.", "sent": 0})

    # ── Group by email ────────────────────────────────────────────────────────
    from collections import defaultdict
    pins_by_email = defaultdict(list)
    for pin in all_pins:
        pins_by_email[pin["email"]].append(dict(pin))

    sent_count = 0
    errors = []

    for email, pins in pins_by_email.items():
        user_verticals = []

        for pin in pins:
            vertical = pin["vertical"]
            label = pin.get("label") or vertical

            try:
                app.logger.info(f"Fetching alert data for '{vertical}' ({email})")
                alert_data = call_perplexity(build_alert_prompt(vertical))
                user_verticals.append({
                    "vertical": vertical,
                    "label": label,
                    "developments": alert_data.get("developments", []),
                    "summary": alert_data.get("summary", ""),
                })
            except Exception as e:
                app.logger.error(f"Failed to fetch alert for '{vertical}': {e}")
                errors.append({"email": email, "vertical": vertical, "error": str(e)})
                # Still include the vertical with empty data rather than skip it
                user_verticals.append({
                    "vertical": vertical,
                    "label": label,
                    "developments": [],
                    "summary": f"Unable to fetch recent data for {vertical} this week.",
                })

        if not user_verticals:
            continue

        html = build_alert_email_html(email, user_verticals)
        subject = "NX3 Signal — Weekly Market Update"

        try:
            success = send_resend_email(email, subject, html)
            if success:
                sent_count += 1
                app.logger.info(f"Alert sent to {email} ({len(user_verticals)} verticals)")
            else:
                errors.append({"email": email, "error": "Email send failed (check RESEND_API_KEY)"})
        except Exception as e:
            app.logger.error(f"Failed to send email to {email}: {e}")
            errors.append({"email": email, "error": str(e)})

    return jsonify({
        "success": True,
        "sent": sent_count,
        "total_users": len(pins_by_email),
        "errors": errors,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    })


# ─── Startup ──────────────────────────────────────────────────────────────────

# Initialize DB when the app starts
with app.app_context():
    init_db()
    app.logger.info(f"NX3 Signal backend started. DB: {DATABASE_PATH}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
