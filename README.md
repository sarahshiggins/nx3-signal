# NX3 Signal — Vertical Market Intelligence

A real-time market intelligence platform built by Nexus3 to evaluate vertical markets against the Nexus3 AI investment thesis, track competitive changes, and surface daily market signals.

---

## What It Does

NX3 Signal is a full-stack market research platform that takes any industry vertical as input and generates a structured intelligence report in seconds. It scores each vertical against Nexus3's 5-criterion investment thesis, maps the competitive landscape, surfaces recent news and funding activity, and — for verticals that pass the bar — generates a venture outline using Nexus3's operating playbook.

Pin the verticals you care about, and NX3 Signal monitors them daily. If something changes — a new AI competitor enters the space, a thesis score shifts, or a notable funding round hits — you get an email. If nothing changed, it stays quiet.

---

## Why It Was Built

Nexus3 builds, invests in, and acquires generative AI vertical SaaS companies. Evaluating new verticals quickly — before committing to deeper diligence — is a recurring need. NX3 Signal automates the first pass: market sizing, competitive scanning, thesis alignment scoring, news monitoring, and initial venture hypothesis, all in a repeatable, structured format that gets smarter over time as it tracks changes.

---

## Features

### Discover
Browse 18 curated verticals — Healthcare, Legal, Insurance, Energy, Pharma, Telecom, Mining, and more — broken into 85+ specific workflow segments. Each segment includes market size estimates and AI penetration scores.

### Analyze
Type in any vertical, workflow, or industry segment (broad or specific) and get a live AI-powered report:
- **Market Overview** — description, TAM, primary workflow being replaced
- **Competitive Landscape** — traditional incumbents, AI-native competitors, whitespace
- **Recent News & Signals** — 3-5 current articles with clickable links to sources
- **Nexus3 Fit Scores** — 5 criteria scored 1-5 with rationale and overall verdict
- **Venture Playbook** — painkiller, beachhead, moat, revenue model, year 1 plan, biggest risk
- **Comparable** — which Nexus3 Tier 1 vertical this most resembles

### Pin & Track
Pin verticals to your watchlist. See pin status across the app — on analysis results, discover cards, and the dedicated My Pins tab. Duplicate pins are prevented automatically.

### Daily Change Detection
Every morning at 8 AM CT, NX3 Signal re-analyzes all pinned verticals and compares against the previous day's data. You only get an email if something meaningful changed:
- Thesis score shifted by 1+ points
- Overall verdict changed (e.g., POSSIBLE FIT → STRONG FIT)
- New AI-native or traditional competitor appeared
- Market size estimate changed significantly
- Notable news or funding activity surfaced

No changes = no email. No spam.

### Email Alerts
- **Pin confirmation** — instant branded email when you pin a vertical, with one-click unpin link
- **Change reports** — daily digest of what moved, with color-coded score arrows and news highlights

---

## Scoring Framework

The 5 criteria map directly to Nexus3's investment thesis:

| # | Criterion | What It Measures |
|---|---|---|
| 1 | **Market Size & Manual Labor** | Is the market >$10B? Are workflows still manual/paper-heavy? |
| 2 | **Regulatory Moat** | Heavily regulated? Domain expertise a real barrier? |
| 3 | **Process Replacement** | Can AI replace entire workflows, not just assist? |
| 4 | **Capital Efficiency** | Path to enterprise contracts? Recurring high-margin revenue? |
| 5 | **Layer 4 Moat** | Deep integrations (EHR, SCADA, core systems, filing APIs)? |

**Verdict thresholds:** STRONG FIT (avg ≥ 4.0) · POSSIBLE FIT (avg 3.0–3.9) · WEAK FIT (avg < 3.0)

---

## How to Use It

1. Open [nx3-signal-production.up.railway.app](https://nx3-signal-production.up.railway.app)
2. Enter your email (first time only — stored in browser)
3. Browse verticals on the Discover tab, or type anything into the Analyze tab
4. Pin verticals you want to track
5. Get daily change detection emails automatically

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Python / Flask on Railway (auto-deploys from GitHub) |
| **AI Engine** | Perplexity sonar model — live web-searched analysis with real-time data |
| **Email** | Resend API with verified nexus3cap.com domain |
| **Database** | SQLite — pins, analysis history, change tracking |
| **Frontend** | Single-page app with Tailwind CSS, Bebas Neue + Inter + JetBrains Mono |
| **Alerts** | Railway cron job runs daily comparison logic at 8 AM CT |
| **Hosting** | Railway (backend + cron) |
| **Source** | GitHub with auto-deploy on push |

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | / | Serve frontend |
| GET | /health | Health check |
| POST | /api/analyze | Run market analysis (with auto-retry) |
| POST | /api/pin | Pin a vertical (with duplicate prevention) |
| GET | /api/pins | Get user's pins |
| GET | /api/pins/check | Check if a vertical is pinned |
| DELETE | /api/pins/\<id\> | Remove a pin |
| GET | /api/unpin?token= | One-click unpin from email |
| GET | /api/history | Analysis history for trend tracking |
| POST | /api/send-alert | Trigger daily change detection scan |

---

*Built by Sarah Higgins, Nexus3.*
