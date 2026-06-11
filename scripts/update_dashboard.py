#!/usr/bin/env python3
"""
US Pulse — weekly economy & politics dashboard builder.

Pipeline (run weekly by GitHub Actions, or manually):
  1. Pull economic series from the FRED API (free key required).
  2. Ask Claude (with web search) to gather current political indicators.
  3. Append this week's snapshot to data/history.json.
  4. Ask Claude to write the "bottom line" analysis comparing to prior weeks.
  5. Render docs/index.html from scripts/template.html.
  6. Email a short summary with a link to the dashboard.

Usage:
  python scripts/update_dashboard.py            # full live run (needs env vars)
  python scripts/update_dashboard.py --sample   # offline preview with sample data

Environment variables (live mode):
  FRED_API_KEY        required
  ANTHROPIC_API_KEY   required
  GMAIL_ADDRESS       optional (sender; skip email if unset)
  GMAIL_APP_PASSWORD  optional (Gmail app password, not your real password)
  EMAIL_TO            optional (defaults to GMAIL_ADDRESS)
  DASHBOARD_URL       optional (link used in the email)
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = ROOT / "data" / "history.json"
TEMPLATE_PATH = ROOT / "scripts" / "template.html"
OUTPUT_PATH = ROOT / "docs" / "index.html"

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"

MAX_HISTORY_WEEKS = 260  # keep ~5 years of weekly snapshots

# ---------------------------------------------------------------------------
# FRED series configuration
# ---------------------------------------------------------------------------

UNEMPLOYMENT_SERIES = [
    ("UNRATE", "Overall"),
    ("LNS14000003", "White"),
    ("LNS14000006", "Black"),
    ("LNS14000009", "Hispanic"),
    ("LNS14032183", "Asian"),
    ("LNS14000001", "Men"),
    ("LNS14000002", "Women"),
    ("LNS14024887", "Ages 16-24"),
]

INFLATION_SERIES = [
    ("CPIAUCSL", "All items"),
    ("CPILFESL", "Core (ex food & energy)"),
    ("CUSR0000SETB01", "Gasoline"),
    ("CUSR0000SAF11", "Groceries (food at home)"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[us-pulse] {msg}", flush=True)


def fred_fetch(series_id: str, api_key: str, start: str) -> list[tuple[str, float]]:
    """Return [(date, value), ...] for a FRED series, skipping missing values."""
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start,
        "sort_order": "asc",
    }
    for attempt in range(3):
        try:
            r = requests.get(FRED_BASE, params=params, timeout=30)
            r.raise_for_status()
            obs = r.json().get("observations", [])
            out = []
            for o in obs:
                if o.get("value") not in (".", "", None):
                    out.append((o["date"], float(o["value"])))
            return out
        except Exception as e:  # noqa: BLE001
            log(f"FRED {series_id} attempt {attempt + 1} failed: {e}")
            time.sleep(3 * (attempt + 1))
    log(f"WARNING: giving up on FRED series {series_id}; it will be omitted.")
    return []


def yoy_percent(observations: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Convert a monthly index level series into year-over-year % change."""
    by_date = dict(observations)
    out = []
    for d, v in observations:
        dt = datetime.strptime(d, "%Y-%m-%d")
        prior_key = dt.replace(year=dt.year - 1).strftime("%Y-%m-%d")
        if prior_key in by_date and by_date[prior_key] != 0:
            out.append((d, round((v / by_date[prior_key] - 1) * 100, 1)))
    return out


def month_label(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b %y")


def tail(series: list[tuple[str, float]], n: int) -> list[tuple[str, float]]:
    return series[-n:] if series else []


def delta_chip(current: float | None, prior: float | None, decimals: int = 1) -> dict:
    if current is None or prior is None:
        return {"delta": None, "text": "—"}
    diff = round(current - prior, decimals)
    sign = "+" if diff > 0 else ""
    return {"delta": diff, "text": f"{sign}{diff}"}


# ---------------------------------------------------------------------------
# Step 1: economic data
# ---------------------------------------------------------------------------

def fetch_economy(api_key: str) -> dict:
    start_3y = (date.today() - timedelta(days=3 * 365 + 60)).isoformat()
    start_18m = (date.today() - timedelta(days=550)).isoformat()

    log("Fetching unemployment series...")
    unemp = {}
    for sid, label in UNEMPLOYMENT_SERIES:
        s = fred_fetch(sid, api_key, start_3y)
        if s:
            unemp[label] = tail(s, 25)

    log("Fetching CPI series...")
    inflation = {}
    for sid, label in INFLATION_SERIES:
        s = fred_fetch(sid, api_key, start_3y)
        y = yoy_percent(s)
        if y:
            inflation[label] = tail(y, 25)

    log("Fetching payrolls, claims, wages, gas, mortgage, sentiment...")
    payems = fred_fetch("PAYEMS", api_key, start_3y)
    payroll_chg = [
        (payems[i][0], round(payems[i][1] - payems[i - 1][1]))
        for i in range(1, len(payems))
    ]
    claims = tail(fred_fetch("ICSA", api_key, start_18m), 26)
    ahe_yoy = tail(yoy_percent(fred_fetch("CES0500000003", api_key, start_3y)), 25)
    gas = tail(fred_fetch("GASREGW", api_key, start_18m), 52)
    mortgage = tail(fred_fetch("MORTGAGE30US", api_key, start_18m), 52)
    sentiment = tail(fred_fetch("UMCSENT", api_key, start_3y), 25)

    # Real wage growth = wage YoY minus headline CPI YoY, matched by month.
    cpi_map = dict(inflation.get("All items", []))
    real_wages = [
        (d, round(v - cpi_map[d], 1)) for d, v in ahe_yoy if d in cpi_map
    ]

    return {
        "unemployment": unemp,
        "inflation": inflation,
        "payroll_change": tail(payroll_chg, 13),
        "claims": claims,
        "ahe_yoy": ahe_yoy,
        "real_wages": real_wages,
        "gas": gas,
        "mortgage": mortgage,
        "sentiment": sentiment,
    }


# ---------------------------------------------------------------------------
# Step 2 & 4: Claude API calls
# ---------------------------------------------------------------------------

def call_claude(api_key: str, prompt: str, use_search: bool, max_tokens: int = 3000) -> str:
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_search:
        body["tools"] = [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}
        ]
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    for attempt in range(3):
        try:
            r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=300)
            r.raise_for_status()
            blocks = r.json().get("content", [])
            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if text.strip():
                return text
            raise ValueError("Empty text response from Claude")
        except Exception as e:  # noqa: BLE001
            log(f"Claude call attempt {attempt + 1} failed: {e}")
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("Claude API failed after 3 attempts")


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a Claude response, tolerating fences."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in response:\n{text[:500]}")
    return json.loads(text[start : end + 1])


POLITICS_PROMPT = """You are a careful, nonpartisan US politics data researcher. Today is {today}.
Use web search to find the MOST RECENT values for each indicator below. Prefer polling
averages (Silver Bulletin, VoteHub, RealClearPolitics, Decision Desk HQ/The Hill) over
single polls; for demographic crosstabs use the latest Economist/YouGov weekly poll (or
Quinnipiac/Gallup/Marquette if fresher); for House/Senate control odds use Polymarket
and/or Kalshi prediction markets for the 2026 midterms.

Return ONLY a single JSON object, no prose before or after, with exactly this shape
(numbers are percentages; use null if you genuinely cannot find a value):

{{
  "as_of": "YYYY-MM-DD",
  "trump_approval": {{"approve": 0, "disapprove": 0, "source": ""}},
  "trump_econ_approval": {{"approve": 0, "disapprove": 0, "source": ""}},
  "approval_by_group": [
    {{"group": "Men", "approve": 0, "disapprove": 0}},
    {{"group": "Women", "approve": 0, "disapprove": 0}},
    {{"group": "White voters", "approve": 0, "disapprove": 0}},
    {{"group": "Black voters", "approve": 0, "disapprove": 0}},
    {{"group": "Hispanic voters", "approve": 0, "disapprove": 0}},
    {{"group": "18-29", "approve": 0, "disapprove": 0}},
    {{"group": "30-44", "approve": 0, "disapprove": 0}},
    {{"group": "45-64", "approve": 0, "disapprove": 0}},
    {{"group": "65+", "approve": 0, "disapprove": 0}},
    {{"group": "College grads", "approve": 0, "disapprove": 0}},
    {{"group": "No college degree", "approve": 0, "disapprove": 0}},
    {{"group": "Independents", "approve": 0, "disapprove": 0}}
  ],
  "approval_by_group_source": "",
  "generic_ballot": {{"dem": 0, "gop": 0, "source": ""}},
  "house_dem_odds": {{"pct": 0, "source": ""}},
  "senate_dem_odds": {{"pct": 0, "source": ""}},
  "right_track": {{"right_direction": 0, "wrong_track": 0, "source": ""}},
  "special_elections_note": "one sentence on recent special-election over/under-performance",
  "race_ratings_note": "one sentence on current Cook Political Report / Sabato outlook"
}}"""


ANALYSIS_PROMPT = """You are a sharp, nonpartisan analyst writing a weekly briefing on the US
economy and national politics for a smart generalist reader. Today is {today}.

Here is this week's data snapshot:
{current}

Here is last week's snapshot (may be empty on the first run):
{previous}

And the snapshot from ~4 weeks ago (may be empty):
{month_ago}

Write the briefing analysis. Be concrete and numerate (cite the actual figures), focus on
what CHANGED and why it matters, and connect economy to politics where the data supports
it. Plain language, no hype, no partisanship. If history is empty, analyze levels rather
than changes and say trends will build over coming weeks.

Return ONLY a single JSON object, no prose before or after:

{{
  "headline": "one punchy sentence, max 16 words",
  "takeaways": [
    {{"tag": "economy", "text": "1-2 sentences"}},
    {{"tag": "economy", "text": "1-2 sentences"}},
    {{"tag": "economy", "text": "1-2 sentences"}},
    {{"tag": "politics", "text": "1-2 sentences"}},
    {{"tag": "politics", "text": "1-2 sentences"}}
  ],
  "economy_summary": "one short paragraph, max 90 words",
  "politics_summary": "one short paragraph, max 90 words",
  "email_subject": "Monday Brief: <short hook>",
  "email_bullets": ["five very short bullets, max 15 words each"]
}}"""


def fetch_politics(anthropic_key: str) -> dict:
    log("Asking Claude (with web search) for political indicators...")
    text = call_claude(
        anthropic_key,
        POLITICS_PROMPT.format(today=date.today().isoformat()),
        use_search=True,
        max_tokens=4000,
    )
    return extract_json(text)


def build_analysis(anthropic_key: str, current: dict, previous: dict, month_ago: dict) -> dict:
    log("Asking Claude for the weekly analysis...")
    text = call_claude(
        anthropic_key,
        ANALYSIS_PROMPT.format(
            today=date.today().isoformat(),
            current=json.dumps(current, indent=1),
            previous=json.dumps(previous, indent=1) if previous else "{}",
            month_ago=json.dumps(month_ago, indent=1) if month_ago else "{}",
        ),
        use_search=False,
        max_tokens=2500,
    )
    return extract_json(text)


# ---------------------------------------------------------------------------
# Step 3: snapshot history
# ---------------------------------------------------------------------------

def latest(series: list[tuple[str, float]]) -> float | None:
    return series[-1][1] if series else None


def prior(series: list[tuple[str, float]]) -> float | None:
    return series[-2][1] if len(series) >= 2 else None


def make_snapshot(econ: dict, pol: dict) -> dict:
    """Compact weekly snapshot stored in history.json (keeps the file small)."""
    return {
        "date": date.today().isoformat(),
        "economy": {
            "unemployment_overall": latest(econ["unemployment"].get("Overall", [])),
            "unemployment_black": latest(econ["unemployment"].get("Black", [])),
            "unemployment_hispanic": latest(econ["unemployment"].get("Hispanic", [])),
            "cpi_headline": latest(econ["inflation"].get("All items", [])),
            "cpi_core": latest(econ["inflation"].get("Core (ex food & energy)", [])),
            "cpi_gas": latest(econ["inflation"].get("Gasoline", [])),
            "cpi_groceries": latest(econ["inflation"].get("Groceries (food at home)", [])),
            "payrolls_change_k": latest(econ["payroll_change"]),
            "initial_claims": latest(econ["claims"]),
            "real_wage_growth": latest(econ["real_wages"]),
            "gas_price": latest(econ["gas"]),
            "mortgage_rate": latest(econ["mortgage"]),
            "consumer_sentiment": latest(econ["sentiment"]),
        },
        "politics": {
            "approve": (pol.get("trump_approval") or {}).get("approve"),
            "disapprove": (pol.get("trump_approval") or {}).get("disapprove"),
            "econ_approve": (pol.get("trump_econ_approval") or {}).get("approve"),
            "generic_dem": (pol.get("generic_ballot") or {}).get("dem"),
            "generic_gop": (pol.get("generic_ballot") or {}).get("gop"),
            "house_dem_odds": (pol.get("house_dem_odds") or {}).get("pct"),
            "senate_dem_odds": (pol.get("senate_dem_odds") or {}).get("pct"),
            "right_direction": (pol.get("right_track") or {}).get("right_direction"),
        },
    }


def load_history() -> dict:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text())
        except Exception:  # noqa: BLE001
            log("WARNING: history.json unreadable; starting fresh.")
    return {"snapshots": []}


def save_history(history: dict) -> None:
    history["snapshots"] = history["snapshots"][-MAX_HISTORY_WEEKS:]
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=1))


# ---------------------------------------------------------------------------
# Step 5: render the dashboard
# ---------------------------------------------------------------------------

def chart_lines(named: dict, value_decimals: int = 1) -> dict:
    """Convert {label: [(date, val)...]} into Chart.js-friendly labels/series."""
    if not named:
        return {"labels": [], "series": []}
    # Use the longest series for labels; align others by date.
    base_label, base = max(named.items(), key=lambda kv: len(kv[1]))
    labels = [d for d, _ in base]
    series = []
    for name, pts in named.items():
        m = dict(pts)
        series.append(
            {"name": name, "data": [round(m[d], value_decimals) if d in m else None for d in labels]}
        )
    return {"labels": [month_label(d) for d in labels], "series": series}


def build_cards(econ: dict, pol: dict, snapshots: list[dict]) -> dict:
    prev = snapshots[-2] if len(snapshots) >= 2 else None

    def pol_prev(key):
        return (prev or {}).get("politics", {}).get(key) if prev else None

    unemp_overall = econ["unemployment"].get("Overall", [])
    cpi = econ["inflation"].get("All items", [])
    cpi_gas = econ["inflation"].get("Gasoline", [])
    cpi_groc = econ["inflation"].get("Groceries (food at home)", [])

    econ_cards = [
        {"label": "Unemployment", "value": latest(unemp_overall), "unit": "%",
         "sub": "vs. prior month", **delta_chip(latest(unemp_overall), prior(unemp_overall)),
         "good_when": "down"},
        {"label": "Inflation (CPI, YoY)", "value": latest(cpi), "unit": "%",
         "sub": "vs. prior month", **delta_chip(latest(cpi), prior(cpi)), "good_when": "down"},
        {"label": "Grocery inflation", "value": latest(cpi_groc), "unit": "%",
         "sub": "vs. prior month", **delta_chip(latest(cpi_groc), prior(cpi_groc)), "good_when": "down"},
        {"label": "Gas inflation", "value": latest(cpi_gas), "unit": "%",
         "sub": "vs. prior month", **delta_chip(latest(cpi_gas), prior(cpi_gas)), "good_when": "down"},
        {"label": "Job growth", "value": latest(econ["payroll_change"]), "unit": "k",
         "sub": "payrolls, vs. prior month",
         **delta_chip(latest(econ["payroll_change"]), prior(econ["payroll_change"]), 0),
         "good_when": "up"},
        {"label": "Jobless claims", "value": latest(econ["claims"]), "unit": "",
         "sub": "weekly, vs. prior week",
         **delta_chip(latest(econ["claims"]), prior(econ["claims"]), 0), "good_when": "down"},
        {"label": "Real wage growth", "value": latest(econ["real_wages"]), "unit": "%",
         "sub": "wages minus inflation, YoY",
         **delta_chip(latest(econ["real_wages"]), prior(econ["real_wages"])), "good_when": "up"},
        {"label": "Gas price", "value": latest(econ["gas"]), "unit": "$",
         "sub": "regular, vs. prior week", **delta_chip(latest(econ["gas"]), prior(econ["gas"]), 2),
         "good_when": "down", "prefix_unit": True},
        {"label": "30-yr mortgage", "value": latest(econ["mortgage"]), "unit": "%",
         "sub": "vs. prior week", **delta_chip(latest(econ["mortgage"]), prior(econ["mortgage"]), 2),
         "good_when": "down"},
        {"label": "Consumer sentiment", "value": latest(econ["sentiment"]), "unit": "",
         "sub": "U. Michigan, vs. prior month",
         **delta_chip(latest(econ["sentiment"]), prior(econ["sentiment"])), "good_when": "up"},
    ]

    appr = pol.get("trump_approval") or {}
    econ_appr = pol.get("trump_econ_approval") or {}
    gb = pol.get("generic_ballot") or {}
    rt = pol.get("right_track") or {}

    pol_cards = [
        {"label": "Trump approval", "value": appr.get("approve"), "unit": "%",
         "sub": f"disapprove {appr.get('disapprove', '—')}% · vs. last week",
         **delta_chip(appr.get("approve"), pol_prev("approve")), "good_when": "neutral"},
        {"label": "Approval on economy", "value": econ_appr.get("approve"), "unit": "%",
         "sub": f"disapprove {econ_appr.get('disapprove', '—')}% · vs. last week",
         **delta_chip(econ_appr.get("approve"), pol_prev("econ_approve")), "good_when": "neutral"},
        {"label": "Generic ballot — Dem", "value": gb.get("dem"), "unit": "%",
         "sub": f"GOP {gb.get('gop', '—')}% · vs. last week",
         **delta_chip(gb.get("dem"), pol_prev("generic_dem")), "good_when": "neutral"},
        {"label": "Dem House odds", "value": (pol.get("house_dem_odds") or {}).get("pct"),
         "unit": "%", "sub": "prediction markets · vs. last week",
         **delta_chip((pol.get("house_dem_odds") or {}).get("pct"), pol_prev("house_dem_odds")),
         "good_when": "neutral"},
        {"label": "Dem Senate odds", "value": (pol.get("senate_dem_odds") or {}).get("pct"),
         "unit": "%", "sub": "prediction markets · vs. last week",
         **delta_chip((pol.get("senate_dem_odds") or {}).get("pct"), pol_prev("senate_dem_odds")),
         "good_when": "neutral"},
        {"label": "Right direction", "value": rt.get("right_direction"), "unit": "%",
         "sub": f"wrong track {rt.get('wrong_track', '—')}% · vs. last week",
         **delta_chip(rt.get("right_direction"), pol_prev("right_direction")),
         "good_when": "neutral"},
    ]
    return {"economy": econ_cards, "politics": pol_cards}


def build_payload(econ: dict, pol: dict, analysis: dict, history: dict, sample: bool) -> dict:
    snaps = history["snapshots"]
    pol_history = {
        "dates": [s["date"] for s in snaps],
        "approve": [s["politics"].get("approve") for s in snaps],
        "disapprove": [s["politics"].get("disapprove") for s in snaps],
        "generic_dem": [s["politics"].get("generic_dem") for s in snaps],
        "generic_gop": [s["politics"].get("generic_gop") for s in snaps],
        "house_dem_odds": [s["politics"].get("house_dem_odds") for s in snaps],
        "senate_dem_odds": [s["politics"].get("senate_dem_odds") for s in snaps],
    }
    claims_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%b %d") for d, _ in econ["claims"]]
    gas_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%b %d") for d, _ in econ["gas"]]

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%B %d, %Y · %H:%M UTC"),
        "week_of": date.today().strftime("Week of %B %d, %Y"),
        "sample": sample,
        "analysis": analysis,
        "cards": build_cards(econ, pol, snaps),
        "charts": {
            "unemployment": chart_lines(econ["unemployment"]),
            "inflation": chart_lines(econ["inflation"]),
            "payrolls": {
                "labels": [month_label(d) for d, _ in econ["payroll_change"]],
                "data": [v for _, v in econ["payroll_change"]],
            },
            "claims": {"labels": claims_labels, "data": [v for _, v in econ["claims"]]},
            "gas": {"labels": gas_labels, "data": [v for _, v in econ["gas"]]},
            "wages": chart_lines(
                {"Wage growth (YoY)": econ["ahe_yoy"], "Real wage growth": econ["real_wages"]}
            ),
        },
        "politics": {"current": pol, "history": pol_history},
    }


def render(payload: dict) -> None:
    template = TEMPLATE_PATH.read_text()
    html = template.replace("/*__DATA__*/null", json.dumps(payload))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html)
    log(f"Wrote {OUTPUT_PATH}")


# ---------------------------------------------------------------------------
# Step 6: email summary
# ---------------------------------------------------------------------------

def send_email(analysis: dict) -> None:
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("EMAIL_TO") or sender
    url = os.environ.get("DASHBOARD_URL", "")
    if not sender or not password:
        log("Email credentials not set; skipping email.")
        return

    subject = analysis.get("email_subject", "Monday Brief")
    bullets = analysis.get("email_bullets", [])
    headline = analysis.get("headline", "")

    text_body = headline + "\n\n" + "\n".join(f"• {b}" for b in bullets)
    if url:
        text_body += f"\n\nFull dashboard: {url}"

    bullets_html = "".join(f"<li style='margin:6px 0'>{b}</li>" for b in bullets)
    link_html = (
        f"<p><a href='{url}' style='color:#0E7C66;font-weight:600'>Open the full dashboard →</a></p>"
        if url else ""
    )
    html_body = f"""
    <div style="font-family:Georgia,serif;max-width:560px;margin:auto;color:#16212E">
      <p style="font-size:11px;letter-spacing:2px;color:#8A93A0">THE MONDAY BRIEF</p>
      <h2 style="font-size:20px;line-height:1.3">{headline}</h2>
      <ul style="font-family:Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;padding-left:18px">
        {bullets_html}
      </ul>
      {link_html}
    </div>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(sender, password)
            server.sendmail(sender, [to], msg.as_string())
        log(f"Email sent to {to}")
    except Exception as e:  # noqa: BLE001
        log(f"WARNING: email failed ({e}); dashboard was still updated.")


# ---------------------------------------------------------------------------
# Sample mode (offline preview, no keys needed)
# ---------------------------------------------------------------------------

def sample_data() -> tuple[dict, dict, dict]:
    """Plausible placeholder data so the dashboard design can be previewed offline."""
    months = []
    today = date.today().replace(day=1)
    for i in range(24, -1, -1):
        m = (today.month - i - 1) % 12 + 1
        y = today.year + (today.month - i - 1) // 12
        months.append(f"{y:04d}-{m:02d}-01")

    def walk(start, drift, wiggle, decimals=1):
        import random
        random.seed(hash(start) % 1000)
        v, out = start, []
        for d in months:
            v += drift + random.uniform(-wiggle, wiggle)
            out.append((d, round(v, decimals)))
        return out

    weeks = [(date.today() - timedelta(weeks=i)).isoformat() for i in range(51, -1, -1)]
    econ = {
        "unemployment": {
            "Overall": walk(4.1, 0.012, 0.05), "White": walk(3.6, 0.01, 0.05),
            "Black": walk(6.9, 0.02, 0.12), "Hispanic": walk(5.0, 0.015, 0.1),
            "Asian": walk(3.4, 0.01, 0.08), "Men": walk(4.0, 0.012, 0.05),
            "Women": walk(3.9, 0.012, 0.05), "Ages 16-24": walk(9.0, 0.02, 0.2),
        },
        "inflation": {
            "All items": walk(3.4, -0.025, 0.08), "Core (ex food & energy)": walk(3.6, -0.02, 0.06),
            "Gasoline": walk(2.0, -0.15, 1.2), "Groceries (food at home)": walk(2.6, -0.01, 0.15),
        },
        "payroll_change": [(d, round(160 + (hash(d) % 120) - 60)) for d in months[-13:]],
        "claims": [(w, 215000 + (hash(w) % 30000) - 15000) for w in weeks[-26:]],
        "ahe_yoy": walk(4.0, -0.01, 0.06),
        "real_wages": [],
        "gas": [(w, round(3.05 + ((hash(w) % 50) - 25) / 100, 2)) for w in weeks],
        "mortgage": [(w, round(6.4 + ((hash(w) % 60) - 30) / 100, 2)) for w in weeks],
        "sentiment": walk(62, 0.15, 1.5),
    }
    cpi_map = dict(econ["inflation"]["All items"])
    econ["real_wages"] = [(d, round(v - cpi_map[d], 1)) for d, v in econ["ahe_yoy"] if d in cpi_map]

    pol = {
        "as_of": date.today().isoformat(),
        "trump_approval": {"approve": 42, "disapprove": 54, "source": "Sample (avg of averages)"},
        "trump_econ_approval": {"approve": 39, "disapprove": 57, "source": "Sample"},
        "approval_by_group": [
            {"group": "Men", "approve": 48, "disapprove": 49},
            {"group": "Women", "approve": 37, "disapprove": 59},
            {"group": "White voters", "approve": 50, "disapprove": 47},
            {"group": "Black voters", "approve": 15, "disapprove": 80},
            {"group": "Hispanic voters", "approve": 34, "disapprove": 61},
            {"group": "18-29", "approve": 31, "disapprove": 63},
            {"group": "30-44", "approve": 40, "disapprove": 55},
            {"group": "45-64", "approve": 46, "disapprove": 51},
            {"group": "65+", "approve": 47, "disapprove": 51},
            {"group": "College grads", "approve": 37, "disapprove": 60},
            {"group": "No college degree", "approve": 46, "disapprove": 50},
            {"group": "Independents", "approve": 36, "disapprove": 56},
        ],
        "approval_by_group_source": "Sample (Economist/YouGov-style crosstabs)",
        "generic_ballot": {"dem": 46, "gop": 43, "source": "Sample average"},
        "house_dem_odds": {"pct": 71, "source": "Sample (Polymarket-style)"},
        "senate_dem_odds": {"pct": 24, "source": "Sample (Polymarket-style)"},
        "right_track": {"right_direction": 31, "wrong_track": 61, "source": "Sample"},
        "special_elections_note": "Sample: Democrats have overperformed partisan lean by ~6 points across recent specials.",
        "race_ratings_note": "Sample: Cook and Sabato both rate the House a toss-up leaning Democratic; Senate leans Republican.",
    }
    analysis = {
        "headline": "Sample data: run the live workflow to replace everything on this page.",
        "takeaways": [
            {"tag": "economy", "text": "This is placeholder analysis. The live run pulls real BLS/FRED data and writes real takeaways here."},
            {"tag": "economy", "text": "Grocery and gas inflation get their own lines below, exactly as they will with live CPI data."},
            {"tag": "economy", "text": "Weekly jobless claims and gas prices update every week; monthly series update when BLS releases them."},
            {"tag": "politics", "text": "Approval, the generic ballot, and midterm odds will be pulled fresh each Monday with sources cited."},
            {"tag": "politics", "text": "Political trend charts build one point per week — after a month you'll see real movement."},
        ],
        "economy_summary": "Placeholder. The live version writes a short paragraph here comparing this week's economic data to last week and last month.",
        "politics_summary": "Placeholder. The live version summarizes approval, generic-ballot, and prediction-market movement here.",
        "email_subject": "Monday Brief: sample run",
        "email_bullets": ["Sample bullet one", "Sample bullet two", "Sample bullet three"],
    }
    return econ, pol, analysis


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="offline preview with sample data")
    ap.add_argument("--no-email", action="store_true", help="skip the email step")
    args = ap.parse_args()

    history = load_history()

    if args.sample:
        log("SAMPLE MODE — no API calls, no email.")
        econ, pol, analysis = sample_data()
        payload = build_payload(econ, pol, analysis, history, sample=True)
        render(payload)
        log("Open docs/index.html in a browser to preview.")
        return 0

    fred_key = os.environ.get("FRED_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not fred_key or not anthropic_key:
        log("ERROR: FRED_API_KEY and ANTHROPIC_API_KEY must be set (or use --sample).")
        return 1

    econ = fetch_economy(fred_key)
    if not econ["unemployment"]:
        log("ERROR: no economic data retrieved; aborting without changes.")
        return 1

    pol = fetch_politics(anthropic_key)

    snapshot = make_snapshot(econ, pol)
    snaps = history["snapshots"]
    # Replace today's snapshot if re-run same day; otherwise append.
    if snaps and snaps[-1]["date"] == snapshot["date"]:
        snaps[-1] = snapshot
    else:
        snaps.append(snapshot)

    previous = snaps[-2] if len(snaps) >= 2 else {}
    month_ago = snaps[-5] if len(snaps) >= 5 else {}
    analysis = build_analysis(anthropic_key, snapshot, previous, month_ago)

    save_history(history)
    payload = build_payload(econ, pol, analysis, history, sample=False)
    render(payload)

    if not args.no_email:
        send_email(analysis)

    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
