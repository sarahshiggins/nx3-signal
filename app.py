"""
NX3 Signal — Flask Backend
Nexus3 | Vertical Market Intelligence Platform

Routes:
  GET  /                     → Serve frontend
  POST /api/analyze          → Perplexity-powered market analysis
  POST /api/pin              → Pin a vertical (SQLite, deduped)
  GET  /api/pins             → Get pins by email
  GET  /api/pins/check       → Check if email+vertical is pinned
  DELETE /api/pins/<pin_id>  → Remove a pin
  GET  /api/unpin?token=     → Unpin via email link (token auth)
  POST /api/send-alert       → Send weekly digest emails (cron target)
  GET  /health               → Railway health check
"""

import os
import json
import sqlite3
import datetime
import traceback
import secrets
import threading
from functools import wraps

import requests
from flask import Flask, request, jsonify, render_template, g
from flask_cors import CORS

# ─── App Setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests (for local dev / CDN frontends)

# ─── Config from Environment ──────────────────────────────────────────────────

def _get_env(key, default=""):
    """Read env var at call time — avoids Railway startup race conditions."""
    return os.environ.get(key, default)
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
                unpin_token TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vertical TEXT NOT NULL,
                email TEXT,
                result_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS analysis_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vertical TEXT NOT NULL,
                result_json TEXT NOT NULL,
                scores_json TEXT NOT NULL,
                competitors_json TEXT NOT NULL,
                news_json TEXT NOT NULL,
                analyzed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_history_vertical ON analysis_history(vertical, analyzed_at DESC);
        """)
        # Add unpin_token column to existing databases that don't have it
        try:
            conn.execute("ALTER TABLE pins ADD COLUMN unpin_token TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
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
  "recentNews": [
    {{ "headline": string, "source": string, "date": string (YYYY-MM-DD or "recent"), "url": string (direct URL to the article), "relevance": string (1 sentence on why this matters for AI opportunity) }}
  ],
  "comparable": {{
    "vertical": string,
    "reason": string
  }}
}}

Include 3-5 recent news articles, funding announcements, or market developments from the past 7 days that are relevant to this vertical. Use real sources and dates. Focus on: AI companies entering the space, funding rounds, regulatory changes, major partnerships, and big tech moves.

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
    if not _get_env("PERPLEXITY_API_KEY"):
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
            "Authorization": f"Bearer {_get_env('PERPLEXITY_API_KEY')}",
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

    return robust_json_parse(raw)


def robust_json_parse(raw: str) -> dict:
    """Parse LLM JSON output with progressive repair for common issues."""
    import re

    text = raw.strip()

    # Step 1: Strip markdown code fences
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()

    # Step 2: Extract outermost JSON object
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            raise ValueError(f"No JSON object found in response. Raw: {raw[:300]}")
        text = text[start:]

    # Find matching closing brace
    depth = 0
    end = -1
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end != -1:
        text = text[: end + 1]

    # Step 3: Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Step 4: Fix trailing commas
    repaired = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Step 5: Aggressive repair — fix unescaped quotes, newlines, tabs inside strings
    result = []
    in_string = False
    escaped = False
    for i, ch in enumerate(repaired):
        if escaped:
            result.append(ch)
            escaped = False
            continue
        if ch == "\\":
            result.append(ch)
            escaped = True
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                result.append(ch)
            else:
                # Look ahead to decide if this closes the string
                rest = repaired[i + 1 :].lstrip()
                if not rest or rest[0] in ":,}]":
                    in_string = False
                    result.append(ch)
                else:
                    result.append('\\"')  # escape internal quote
        else:
            if in_string and ch == "\n":
                result.append("\\n")
            elif in_string and ch == "\r":
                result.append("\\r")
            elif in_string and ch == "\t":
                result.append("\\t")
            else:
                result.append(ch)

    try:
        return json.loads("".join(result))
    except json.JSONDecodeError:
        raise ValueError(f"Could not parse Perplexity response as JSON. Raw: {raw[:500]}")



def send_resend_email(to_email: str, subject: str, html_body: str) -> bool:
    """Send an HTML email via the Resend API. Returns True on success."""
    if not _get_env("RESEND_API_KEY"):
        app.logger.warning("RESEND_API_KEY not set — skipping email send.")
        return False

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {_get_env('RESEND_API_KEY')}",
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


# ─── Comparison Logic ─────────────────────────────────────────────────────────

def compare_analyses(previous: dict, current: dict) -> dict:
    """Compare two analyses and return a structured change report."""

    def _compute_verdict(scores: dict) -> str:
        """Compute verdict from scores: avg >= 4 STRONG, >= 3 POSSIBLE, else WEAK."""
        if not scores:
            return "UNKNOWN"
        vals = []
        for dim_data in scores.values():
            if isinstance(dim_data, dict) and "score" in dim_data:
                vals.append(dim_data["score"])
            elif isinstance(dim_data, (int, float)):
                vals.append(dim_data)
        if not vals:
            return "UNKNOWN"
        avg = sum(vals) / len(vals)
        if avg >= 4:
            return "STRONG FIT"
        elif avg >= 3:
            return "POSSIBLE FIT"
        else:
            return "WEAK FIT"

    def _extract_competitor_names(competitors: dict, category: str) -> dict:
        """Extract {name: {description, type}} from competitors dict."""
        result = {}
        for comp in competitors.get(category, []):
            if isinstance(comp, dict) and "name" in comp:
                result[comp["name"]] = {
                    "description": comp.get("description", ""),
                    "type": "ai_native" if category == "aiNative" else "traditional"
                }
        return result

    # Score changes
    score_changes = []
    prev_scores = previous.get("scores", {})
    curr_scores = current.get("scores", {})
    all_dimensions = set(list(prev_scores.keys()) + list(curr_scores.keys()))
    for dim in sorted(all_dimensions):
        prev_val = prev_scores.get(dim, {})
        curr_val = curr_scores.get(dim, {})
        old_score = prev_val.get("score", 0) if isinstance(prev_val, dict) else (prev_val if isinstance(prev_val, (int, float)) else 0)
        new_score = curr_val.get("score", 0) if isinstance(curr_val, dict) else (curr_val if isinstance(curr_val, (int, float)) else 0)
        if abs(new_score - old_score) >= 1:
            score_changes.append({
                "dimension": dim,
                "old_score": old_score,
                "new_score": new_score,
                "direction": "up" if new_score > old_score else "down"
            })

    # Verdict change
    old_verdict = _compute_verdict(prev_scores)
    new_verdict = _compute_verdict(curr_scores)
    verdict_change = {"old": old_verdict, "new": new_verdict} if old_verdict != new_verdict else None

    # Competitor changes
    prev_competitors = previous.get("competitors", {})
    curr_competitors = current.get("competitors", {})
    prev_all = {}
    curr_all = {}
    for cat in ["aiNative", "traditional"]:
        prev_all.update(_extract_competitor_names(prev_competitors, cat))
        curr_all.update(_extract_competitor_names(curr_competitors, cat))

    new_competitor_names = set(curr_all.keys()) - set(prev_all.keys())
    lost_competitor_names = set(prev_all.keys()) - set(curr_all.keys())

    new_competitors = [{"name": n, "description": curr_all[n]["description"], "type": curr_all[n]["type"]} for n in sorted(new_competitor_names)]
    lost_competitors = [{"name": n, "type": prev_all[n]["type"]} for n in sorted(lost_competitor_names)]

    # Market size change
    old_market = (previous.get("overview", {}) or {}).get("marketSize", "")
    new_market = (current.get("overview", {}) or {}).get("marketSize", "")
    market_size_change = {"old": old_market, "new": new_market} if old_market != new_market else None

    # New news — all current news items are "new" since they're from the latest search
    new_news = current.get("recentNews", [])

    has_changes = bool(score_changes or verdict_change or new_competitors or lost_competitors or market_size_change or new_news)

    return {
        "has_changes": has_changes,
        "score_changes": score_changes,
        "verdict_change": verdict_change,
        "new_competitors": new_competitors,
        "lost_competitors": lost_competitors,
        "market_size_change": market_size_change,
        "new_news": new_news,
    }


def build_change_report_email(email: str, changes: list) -> str:
    """Build a dark-themed HTML change report email for daily alerts."""
    sections = ""
    for entry in changes:
        vertical = entry.get("vertical", "Unknown")
        change = entry.get("change_data", {})

        parts = ""

        # Score changes
        if change.get("score_changes"):
            score_rows = ""
            for sc in change["score_changes"]:
                dim_label = sc["dimension"].replace("_", " ").title()
                # camelCase to readable
                for orig, repl in [("marketSize", "Market Size"), ("regulatoryMoat", "Regulatory Moat"), ("processReplacement", "Process Replacement"), ("capitalEfficiency", "Capital Efficiency"), ("layer4Moat", "Layer 4 Moat")]:
                    if sc["dimension"] == orig:
                        dim_label = repl
                        break
                arrow = "↑" if sc["direction"] == "up" else "↓"
                color = "#22c55e" if sc["direction"] == "up" else "#ef4444"
                score_rows += f'<div style="margin-bottom:6px;font-size:14px;color:#F5F0E8">{dim_label}: <span style="color:#a09890">{sc["old_score"]}</span> → <span style="color:{color};font-weight:700">{sc["new_score"]} {arrow}</span></div>'
            parts += f'<div style="margin-bottom:16px"><div style="font-size:11px;letter-spacing:0.1em;color:#DC2626;font-family:monospace;margin-bottom:8px">SCORE CHANGES</div>{score_rows}</div>'

        # Verdict change
        if change.get("verdict_change"):
            vc = change["verdict_change"]
            parts += f'<div style="margin-bottom:16px;padding:12px;background:#0a0a0a;border-radius:4px"><span style="font-size:13px;color:#a09890">Overall:</span> <span style="color:#a09890">{vc["old"]}</span> <span style="color:#F5F0E8">→</span> <span style="color:#DC2626;font-weight:700">{vc["new"]}</span></div>'

        # New competitors
        if change.get("new_competitors"):
            comp_rows = ""
            for comp in change["new_competitors"]:
                type_label = "AI Native" if comp["type"] == "ai_native" else "Traditional"
                comp_rows += f'<div style="margin-bottom:8px;font-size:14px;color:#F5F0E8">🆕 <strong>{comp["name"]}</strong> <span style="font-size:11px;color:#a09890;background:#1a1a1a;padding:2px 6px;border-radius:3px">{type_label}</span><br><span style="font-size:13px;color:#a09890">{comp.get("description", "")}</span></div>'
            parts += f'<div style="margin-bottom:16px"><div style="font-size:11px;letter-spacing:0.1em;color:#DC2626;font-family:monospace;margin-bottom:8px">NEW COMPETITORS</div>{comp_rows}</div>'

        # Lost competitors
        if change.get("lost_competitors"):
            lost_rows = ""
            for comp in change["lost_competitors"]:
                lost_rows += f'<div style="margin-bottom:4px;font-size:13px;color:#a09890">✕ {comp["name"]} ({comp.get("type", "unknown")})</div>'
            parts += f'<div style="margin-bottom:16px"><div style="font-size:11px;letter-spacing:0.1em;color:#DC2626;font-family:monospace;margin-bottom:8px">REMOVED COMPETITORS</div>{lost_rows}</div>'

        # Market size change
        if change.get("market_size_change"):
            ms = change["market_size_change"]
            parts += f'<div style="margin-bottom:16px;font-size:14px;color:#F5F0E8">📊 Market Size: <span style="color:#a09890">{ms["old"]}</span> → <span style="color:#DC2626;font-weight:700">{ms["new"]}</span></div>'

        # News items
        if change.get("new_news"):
            news_rows = ""
            for item in change["new_news"][:5]:
                news_rows += f'<div style="margin-bottom:10px;padding-left:8px;border-left:2px solid #222"><div style="font-size:14px;color:#F5F0E8;font-weight:600">{item.get("headline", "")}</div><div style="font-size:12px;color:#a09890;margin-top:2px">{item.get("source", "")} · {item.get("date", "")}</div><div style="font-size:13px;color:#a09890;margin-top:4px">{item.get("relevance", "")}</div></div>'
            parts += f'<div style="margin-bottom:16px"><div style="font-size:11px;letter-spacing:0.1em;color:#DC2626;font-family:monospace;margin-bottom:8px">RECENT NEWS</div>{news_rows}</div>'

        sections += f"""
        <div style="margin-bottom:32px;padding:20px 24px;background:#111111;border-radius:8px;border:1px solid #222222;border-left:3px solid #DC2626">
          <h2 style="font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-size:18px;font-weight:700;color:#F5F0E8;margin:0 0 4px 0;letter-spacing:0.03em">
            {vertical.upper()}
          </h2>
          <p style="color:#a09890;font-size:12px;font-family:monospace;margin:0 0 14px 0;letter-spacing:0.1em">
            CHANGES DETECTED
          </p>
          {parts}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>NX3 Signal — Change Report</title>
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
        Daily Change Report
      </p>
    </div>

    <!-- Intro -->
    <p style="font-size:14px;color:#a09890;margin:0 0 28px 0;line-height:1.6">
      Changes detected in your pinned verticals since the last analysis.
    </p>

    <!-- Change Sections -->
    {sections}

    <!-- Footer -->
    <div style="border-top:1px solid #222222;padding-top:24px;margin-top:32px">
      <p style="margin:0;font-size:12px;color:#4a4540;line-height:1.6">
        NX3 Signal by Nexus3 · Manage pins at
        <a href="https://signal.nexus3cap.com" style="color:#DC2626;text-decoration:none">signal.nexus3cap.com</a>
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
    return jsonify({
        "status": "ok",
        "env_check": {
            "PERPLEXITY_API_KEY": bool(os.environ.get("PERPLEXITY_API_KEY")),
            "RESEND_API_KEY": bool(os.environ.get("RESEND_API_KEY")),
            "ALERT_SECRET": bool(os.environ.get("ALERT_SECRET")),
        },
        "env_count": len([k for k in os.environ if "PERPLEXITY" in k or "RESEND" in k or "ALERT" in k]),
        "all_env_keys": sorted([k for k in os.environ.keys()])
    })


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

    if not _get_env("PERPLEXITY_API_KEY"):
        return jsonify({"error": "Server is not configured with a Perplexity API key. Contact the admin."}), 503

    try:
        prompt = build_analysis_prompt(vertical)
        # Retry up to 2 times if Perplexity returns unparseable JSON
        last_err = None
        result = None
        for attempt in range(2):
            try:
                result = call_perplexity(prompt)
                break
            except (ValueError, json.JSONDecodeError) as parse_err:
                last_err = parse_err
                app.logger.warning(f"Perplexity parse failed (attempt {attempt+1}): {parse_err}")
                continue
        if result is None:
            raise last_err or ValueError("Analysis failed after retries")

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

        # Store in analysis_history for change tracking
        try:
            db = get_db()
            db.execute(
                "INSERT INTO analysis_history (vertical, result_json, scores_json, competitors_json, news_json) VALUES (?, ?, ?, ?, ?)",
                (vertical, json.dumps(result), json.dumps(result.get('scores', {})), json.dumps(result.get('competitors', {})), json.dumps(result.get('recentNews', []))),
            )
            db.commit()
        except Exception as db_err:
            app.logger.warning(f"Failed to store analysis history: {db_err}")

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
    Pin a vertical for a user. Returns existing pin if already pinned.

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

    # Check for existing pin (duplicate prevention)
    existing = db.execute(
        "SELECT id FROM pins WHERE email = ? AND vertical = ?",
        (email, vertical),
    ).fetchone()

    if existing:
        return jsonify({"status": "already_pinned", "pin_id": existing["id"]})

    # Generate unpin token for email-based unpin links
    unpin_token = secrets.token_urlsafe(16)

    cursor = db.execute(
        "INSERT INTO pins (vertical, email, label, unpin_token) VALUES (?, ?, ?, ?)",
        (vertical, email, label, unpin_token),
    )
    db.commit()

    pin_id = cursor.lastrowid

    # Send confirmation email (non-blocking — don't let failures affect the response)
    try:
        display_name = label or vertical
        unpin_url = f"{request.host_url}api/unpin?token={unpin_token}"
        confirm_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif">
  <div style="max-width:560px;margin:0 auto;padding:32px 16px">
    <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:0.2em;color:#DC2626;font-family:monospace;text-transform:uppercase">Nexus3</p>
    <h1 style="margin:0 0 24px 0;font-size:28px;font-weight:700;color:#F5F0E8;letter-spacing:0.04em">NX3 Signal</h1>
    <div style="padding:20px 24px;background:#111111;border-radius:8px;border:1px solid #222222;border-left:3px solid #DC2626">
      <p style="margin:0 0 12px 0;font-size:16px;color:#F5F0E8;font-weight:600">📌 You pinned {display_name}</p>
      <p style="margin:0 0 16px 0;font-size:14px;color:#a09890;line-height:1.6">You'll receive weekly analysis updates for this vertical as part of your NX3 Signal digest.</p>
      <p style="margin:0;font-size:13px;color:#4a4540;line-height:1.6">Changed your mind? <a href="{unpin_url}" style="color:#DC2626;text-decoration:none">Unpin this vertical</a>.</p>
    </div>
    <p style="margin:24px 0 0;font-size:11px;color:#4a4540;font-family:monospace">NX3 Signal · Nexus3 · nexus3cap.com</p>
  </div>
</body>
</html>"""
        # Send in background thread so the pin response is instant
        def _send_confirm():
            try:
                send_resend_email(email, f"NX3 Signal \u2014 You pinned {display_name}", confirm_html)
            except Exception as ex:
                app.logger.error(f"Failed to send pin confirmation email to {email}: {ex}")
        threading.Thread(target=_send_confirm, daemon=True).start()
    except Exception as e:
        app.logger.error(f"Failed to prepare pin confirmation email for {email}: {e}")

    return jsonify({"success": True, "pin_id": pin_id})


@app.route("/api/pins/check", methods=["GET"])
def check_pin():
    """
    Check if a user has pinned a specific vertical.

    Query params:
        email=tim@nexus3cap.com
        vertical=workers comp insurance
    """
    email = (request.args.get("email") or "").strip()
    vertical = (request.args.get("vertical") or "").strip()

    if not email or not vertical:
        return jsonify({"error": "email and vertical query parameters are required"}), 400

    db = get_db()
    row = db.execute(
        "SELECT id FROM pins WHERE email = ? AND vertical = ?",
        (email, vertical),
    ).fetchone()

    if row:
        return jsonify({"pinned": True, "pin_id": row["id"]})
    return jsonify({"pinned": False, "pin_id": None})


@app.route("/api/unpin", methods=["GET"])
def unpin_via_token():
    """
    Unpin a vertical via a unique token (for email unpin links).
    No auth required — the token serves as proof of ownership.

    Query params:
        token=<unpin_token>
    """
    token = (request.args.get("token") or "").strip()

    if not token:
        return "<html><body style='background:#0a0a0a;color:#F5F0E8;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0'><div style='text-align:center'><h1>Invalid Link</h1><p style='color:#a09890'>No unpin token provided.</p></div></div></body></html>", 400

    db = get_db()
    row = db.execute(
        "SELECT id, vertical, email FROM pins WHERE unpin_token = ?",
        (token,),
    ).fetchone()

    if not row:
        return "<html><body style='background:#0a0a0a;color:#F5F0E8;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0'><div style='text-align:center'><h1>Not Found</h1><p style='color:#a09890'>This unpin link is invalid or has already been used.</p></div></div></body></html>", 404

    vertical = row["vertical"]
    db.execute("DELETE FROM pins WHERE id = ?", (row["id"],))
    db.commit()

    return f"""<html>
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Unpinned — NX3 Signal</title></head>
<body style="background:#0a0a0a;color:#F5F0E8;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0">
  <div style="text-align:center;max-width:480px;padding:32px">
    <p style="font-size:11px;letter-spacing:0.2em;color:#DC2626;font-family:monospace;text-transform:uppercase;margin:0 0 8px 0">Nexus3</p>
    <h1 style="font-size:28px;font-weight:700;margin:0 0 24px 0">Unpinned Successfully</h1>
    <div style="padding:20px 24px;background:#111111;border-radius:8px;border:1px solid #222222">
      <p style="margin:0 0 8px 0;font-size:16px;color:#F5F0E8">You've unpinned <strong>{vertical}</strong>.</p>
      <p style="margin:0;font-size:14px;color:#a09890">You'll no longer receive weekly updates for this vertical.</p>
    </div>
    <p style="margin:24px 0 0;font-size:13px;color:#4a4540"><a href="/" style="color:#DC2626;text-decoration:none">Back to NX3 Signal →</a></p>
  </div>
</body>
</html>"""


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
    Send daily change detection emails to all users with pins.

    Security: Requires X-Alert-Secret header matching ALERT_SECRET env var.
    Designed to be called by a cron job (e.g., Railway cron, GitHub Actions).

    Process:
        1. Load all pins, grouped by email
        2. For each pin, run fresh analysis via Perplexity
        3. Compare against most recent previous analysis from analysis_history
        4. Store current analysis in analysis_history
        5. If changes detected, add to user's change report
        6. Send change report emails via Resend
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    secret = request.headers.get("X-Alert-Secret", "")
    if secret != _get_env("ALERT_SECRET", "change-me-in-production"):
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

    # Cache analyses per vertical so we don't re-analyze the same vertical for multiple users
    analysis_cache = {}  # vertical -> {"result": dict, "change_data": dict or None}

    sent_count = 0
    analyzed_count = 0
    errors = []

    for email, pins in pins_by_email.items():
        user_changes = []

        for pin in pins:
            vertical = pin["vertical"]
            label = pin.get("label") or vertical

            try:
                # Use cached analysis if we already analyzed this vertical
                if vertical not in analysis_cache:
                    app.logger.info(f"Running fresh analysis for '{vertical}'")

                    # Run fresh analysis
                    prompt = build_analysis_prompt(vertical)
                    last_err = None
                    result = None
                    for attempt in range(2):
                        try:
                            result = call_perplexity(prompt)
                            break
                        except (ValueError, json.JSONDecodeError) as parse_err:
                            last_err = parse_err
                            app.logger.warning(f"Analysis parse failed for '{vertical}' (attempt {attempt+1}): {parse_err}")
                            continue
                    if result is None:
                        raise last_err or ValueError(f"Analysis failed for {vertical} after retries")

                    analyzed_count += 1

                    # Fetch previous analysis from history
                    prev_row = db.execute(
                        "SELECT result_json FROM analysis_history WHERE vertical = ? ORDER BY analyzed_at DESC LIMIT 1",
                        (vertical,),
                    ).fetchone()

                    # Compare if previous exists
                    change_data = None
                    if prev_row:
                        try:
                            previous = json.loads(prev_row["result_json"])
                            change_data = compare_analyses(previous, result)
                        except Exception as cmp_err:
                            app.logger.warning(f"Comparison failed for '{vertical}': {cmp_err}")
                            # Treat as has_changes so user still gets fresh data
                            change_data = {
                                "has_changes": True,
                                "score_changes": [],
                                "verdict_change": None,
                                "new_competitors": [],
                                "lost_competitors": [],
                                "market_size_change": None,
                                "new_news": result.get("recentNews", []),
                            }
                    else:
                        # First analysis ever — treat as "new" with all news
                        change_data = {
                            "has_changes": True,
                            "score_changes": [],
                            "verdict_change": None,
                            "new_competitors": [],
                            "lost_competitors": [],
                            "market_size_change": None,
                            "new_news": result.get("recentNews", []),
                        }

                    # Store current analysis in history
                    try:
                        db.execute(
                            "INSERT INTO analysis_history (vertical, result_json, scores_json, competitors_json, news_json) VALUES (?, ?, ?, ?, ?)",
                            (vertical, json.dumps(result), json.dumps(result.get('scores', {})), json.dumps(result.get('competitors', {})), json.dumps(result.get('recentNews', []))),
                        )
                        db.commit()
                    except Exception as hist_err:
                        app.logger.warning(f"Failed to store analysis history for '{vertical}': {hist_err}")

                    analysis_cache[vertical] = {"result": result, "change_data": change_data}

                # Check if there are changes worth reporting
                cached = analysis_cache[vertical]
                if cached["change_data"] and cached["change_data"].get("has_changes"):
                    user_changes.append({
                        "vertical": label,
                        "change_data": cached["change_data"],
                    })

            except Exception as e:
                app.logger.error(f"Failed to process '{vertical}' for {email}: {e}")
                errors.append({"email": email, "vertical": vertical, "error": str(e)})

        # Send change report if there are changes for this user
        if user_changes:
            n_changes = len(user_changes)
            subject = f"NX3 Signal \u2014 {n_changes} change{'s' if n_changes != 1 else ''} in your pinned verticals"
            html = build_change_report_email(email, user_changes)

            try:
                success = send_resend_email(email, subject, html)
                if success:
                    sent_count += 1
                    app.logger.info(f"Change report sent to {email} ({n_changes} verticals with changes)")
                else:
                    errors.append({"email": email, "error": "Email send failed (check RESEND_API_KEY)"})
            except Exception as e:
                app.logger.error(f"Failed to send change report to {email}: {e}")
                errors.append({"email": email, "error": str(e)})

    return jsonify({
        "success": True,
        "sent": sent_count,
        "total_users": len(pins_by_email),
        "total_verticals_analyzed": analyzed_count,
        "errors": errors,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    })


@app.route("/api/history", methods=["GET"])
def get_history():
    """
    Get analysis history for a vertical.

    Query params:
        vertical=workers comp insurance  (required)
        limit=7                          (optional, default 7)

    Returns the last N analysis_history entries for trend display.
    """
    vertical = (request.args.get("vertical") or "").strip()
    if not vertical:
        return jsonify({"error": "vertical query parameter is required"}), 400

    try:
        limit = int(request.args.get("limit", 7))
    except (ValueError, TypeError):
        limit = 7
    limit = max(1, min(limit, 50))  # Clamp between 1 and 50

    db = get_db()
    rows = db.execute(
        "SELECT id, vertical, result_json, scores_json, competitors_json, news_json, analyzed_at FROM analysis_history WHERE vertical = ? ORDER BY analyzed_at DESC LIMIT ?",
        (vertical, limit),
    ).fetchall()

    results = []
    for row in rows:
        entry = {
            "id": row["id"],
            "vertical": row["vertical"],
            "analyzed_at": row["analyzed_at"],
        }
        # Parse stored JSON fields
        for field in ["result_json", "scores_json", "competitors_json", "news_json"]:
            try:
                entry[field.replace("_json", "")] = json.loads(row[field])
            except (json.JSONDecodeError, TypeError):
                entry[field.replace("_json", "")] = {}
        results.append(entry)

    return jsonify(results)


# ─── Startup ──────────────────────────────────────────────────────────────────

# Initialize DB when the app starts
with app.app_context():
    init_db()
    app.logger.info(f"NX3 Signal backend started. DB: {DATABASE_PATH}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
