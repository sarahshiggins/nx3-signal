# NX3 Signal — Vertical Market Intelligence

A professional internal research tool built for Nexus3 to evaluate vertical markets against the Nexus3 AI investment thesis.

---

## What It Is

NX3 Signal is a browser-based market research tool that takes any industry vertical as input and generates a structured intelligence report in seconds. It scores each vertical against Nexus3's 5-criterion investment thesis, maps the competitive landscape with real incumbents and AI-native players, and — for verticals that pass the bar — generates a venture outline using Nexus3's operating playbook.

Think of it as an AI analyst that's been pre-briefed on exactly how Nexus3 evaluates opportunities.

---

## Why It Was Built

Nexus3 builds, invests in, and acquires generative AI vertical SaaS companies. Evaluating new verticals quickly — before committing to deeper diligence — is a recurring need. NX3 Signal automates the first pass: market sizing, competitive scanning, thesis alignment scoring, and initial venture hypothesis, all in a repeatable, structured format.

---

## How to Use It

1. **Get a Perplexity API key** → [perplexity.ai/settings/api](https://www.perplexity.ai/settings/api) (free tier available)
2. **Open `index.html`** in any modern browser (Chrome, Firefox, Safari, Edge)
3. **Click the ⚙ gear icon** in the top right to open Settings
4. **Paste your API key** and click Save
5. **Type a market vertical** in the search box (e.g. "Veterinary Practice Management", "Construction Permitting", "Freight Brokerage")
6. **Click "RUN ANALYSIS"** — the report generates in ~10–15 seconds

Your API key is stored in `localStorage` and never leaves your browser. No backend, no server, no data collection.

---

## Report Structure

Each analysis returns five sections:

| Section | What You Get |
|---|---|
| **Market Overview** | 2-3 sentence description, estimated market size, primary workflow being replaced |
| **Competitive Landscape** | 3-5 traditional incumbents, 2-3 AI-native competitors, identified whitespace |
| **Nexus3 Fit Scores** | 5 criteria scored 1-5 with rationale, overall verdict (STRONG / POSSIBLE / WEAK FIT) |
| **If We Did It** | Full venture outline (Painkiller, Beachhead, Moat, Revenue Model, Year 1, Biggest Risk) — only appears for POSSIBLE or STRONG FIT |
| **Comparable Ventures** | Which Nexus3 Tier 1 vertical this most resembles and why |

---

## Scoring Framework

The 5 criteria map directly to Nexus3's investment thesis:

1. **Market Size & Manual Labor** — Is the market >$10B? Are workflows still manual/paper-heavy?
2. **Regulatory Moat** — Heavily regulated? Domain expertise a real barrier to generic AI replication?
3. **Process Replacement** — Can AI replace entire workflows, not just assist? Is there a labor budget to capture?
4. **Capital Efficiency** — Path to enterprise contracts? Recurring high-margin revenue potential?
5. **Layer 4 Moat** — Deep integrations (EHR, SCADA, court APIs, core systems) that create switching costs once built?

**Verdict thresholds:** STRONG FIT (avg ≥ 4.0) / POSSIBLE FIT (avg 3.0–3.9) / WEAK FIT (avg < 3.0)

---

## Getting Your API Key

1. Go to [perplexity.ai/settings/api](https://www.perplexity.ai/settings/api)
2. Sign in or create a free account
3. Click **"Generate"** to create a new API key
4. Copy and paste it into the NX3 Signal settings panel

The free tier includes credits sufficient for dozens of analyses.

---

## Tech Stack

- **Single-file app** — `index.html`, no build step, no dependencies to install
- **Tailwind CSS** — utility-first styling via CDN
- **Google Fonts** — Bebas Neue (headers) + Inter (body) + JetBrains Mono (labels/code)
- **Perplexity AI** — `sonar` model for live web-grounded market research
- **Vanilla JS** — zero frameworks, runs entirely in-browser
- **localStorage** — API key persistence, client-side only

---

## Notes

- Analysis quality depends on Perplexity's `sonar` model's web access. Results are best for established verticals with substantial public data.
- This is a research accelerator, not a replacement for human diligence. Use it to generate hypotheses and identify questions, not to make final investment decisions.
- The `sonar` model is instructed to return structured JSON; edge cases with unusual verticals may require a retry if parsing fails.

---

*Built by Sarah Higgins, Nexus3.*

