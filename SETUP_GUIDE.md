# The Monday Brief — Setup Guide

One-time setup: about 20 minutes. After that, the dashboard updates itself every
Monday at 8 AM Eastern and emails you a summary. Total running cost: roughly
$0.15–0.30 per week in Claude API usage (everything else is free).

---

## Step 1 — Get your two API keys (5 min)

**FRED key (free, instant):**
1. Go to https://fred.stlouisfed.org/docs/api/api_key.html
2. Sign in / create a free account → "Request API Key" → describe use as "personal dashboard"
3. Copy the 32-character key.

**Anthropic key:**
1. Go to https://console.anthropic.com → API Keys → Create Key.
2. Add a small amount of credit ($5 lasts months at this usage level).
3. Copy the key (starts with `sk-ant-`).

## Step 2 — Create a Gmail app password (3 min)

This lets the script send you email without your real password. Requires 2-step
verification on your Google account.
1. Go to https://myaccount.google.com/apppasswords
2. App name: "Monday Brief" → Create → copy the 16-character password.

## Step 3 — Create the GitHub repo and upload the files (5 min)

1. At https://github.com, click **New repository**. Name it `monday-brief`
   (or anything). **Public** is required for free GitHub Pages; note that the
   dashboard page and data history will be visible to anyone with the URL.
2. On the new repo page: **uploading an existing file** → drag in the entire
   contents of this folder (keep the folder structure: `.github/workflows/`,
   `scripts/`, `data/`, `docs/`, `requirements.txt`, `README.md`).
   - If the web uploader fights you on the `.github` folder, the easy fix is
     GitHub Desktop or `git push` from the command line — say the word and
     Claude can walk you through it.
3. Commit.

## Step 4 — Add your secrets (3 min)

In the repo: **Settings → Secrets and variables → Actions → New repository secret.**
Add these six (names must match exactly):

| Name | Value |
|---|---|
| `FRED_API_KEY` | from Step 1 |
| `ANTHROPIC_API_KEY` | from Step 1 |
| `GMAIL_ADDRESS` | your Gmail address |
| `GMAIL_APP_PASSWORD` | from Step 2 (16 chars, no spaces) |
| `EMAIL_TO` | where the summary should go (can equal GMAIL_ADDRESS) |
| `DASHBOARD_URL` | fill in after Step 5, e.g. `https://YOURNAME.github.io/monday-brief/` |

## Step 5 — Turn on GitHub Pages (2 min)

1. Repo **Settings → Pages**.
2. Source: **Deploy from a branch** → Branch: `main` → Folder: `/docs` → Save.
3. After a minute, your URL appears at the top:
   `https://YOURNAME.github.io/monday-brief/`
4. Go back and set the `DASHBOARD_URL` secret to this URL.
5. Bookmark it; on iPhone, Share → **Add to Home Screen** makes it feel like an app.

## Step 6 — Run it once manually (2 min)

1. Repo **Actions** tab → "Weekly dashboard update" → **Run workflow**.
2. Watch it run (~2–4 minutes). Green check = your dashboard is live and the
   first email is in your inbox.
3. From now on it runs automatically every Monday at 8 AM ET. You can always
   force a fresh run from the Actions tab.

---

## Good to know

- **Trend charts for politics build over time.** Economic charts are full of
  history from day one (FRED supplies it). Approval/ballot/odds trends add one
  point per week, so they appear after the second run and get interesting
  after a month.
- **Monthly vs. weekly data.** Jobless claims, gas prices, mortgage rates,
  polling, and odds genuinely change weekly. Unemployment, CPI, payrolls,
  wages, and sentiment update when the government releases them (roughly
  monthly) — the dashboard always shows the latest available.
- **Accuracy.** Economic figures come straight from official APIs. Political
  figures are gathered by Claude via web search from polling averages and
  prediction markets — sources are named on the page, but spot-check anything
  you plan to repeat or rely on.
- **Preview without keys:** `python scripts/update_dashboard.py --sample`
  then open `docs/index.html`.
- **GitHub Actions note:** GitHub pauses scheduled workflows on repos with no
  activity for 60 days — but since this workflow commits weekly, it keeps
  itself alive.

## Troubleshooting

- **Workflow fails on "Build dashboard"** → open the run log. A FRED error
  means the key is wrong; a 401 from api.anthropic.com means the Anthropic key
  is wrong or out of credit.
- **No email but dashboard updated** → email is non-fatal by design; check the
  log for the SMTP error (usually an app-password typo).
- **Page shows old data** → hard-refresh (the browser caches it), or check the
  Actions tab to confirm Monday's run succeeded.
- **Want a different schedule?** Edit the cron line in
  `.github/workflows/weekly-update.yml` (it's in UTC).
