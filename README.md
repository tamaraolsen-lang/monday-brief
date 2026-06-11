# The Monday Brief

A self-updating weekly dashboard of US economic and political indicators, with
Claude-written analysis and a Monday-morning email summary.

- **Economy:** unemployment (overall + by demographic group), CPI inflation
  (headline, core, gasoline, groceries), payrolls, weekly jobless claims, real
  wage growth, pump gas prices, 30-yr mortgage rates, consumer sentiment — all
  from official sources via the FRED API.
- **Politics:** Trump approval (overall, on the economy, and by demographic),
  generic congressional ballot, Democrats' House/Senate odds from prediction
  markets, right track/wrong track, special-election and race-rating notes —
  gathered weekly by Claude with web search, sources cited.

**Live page:** served from `docs/index.html` via GitHub Pages.
**Schedule:** every Monday 12:00 UTC via `.github/workflows/weekly-update.yml`.
**Setup:** see `SETUP_GUIDE.md`.

```
.github/workflows/weekly-update.yml   the weekly automation
scripts/update_dashboard.py           the whole pipeline (fetch → analyze → render → email)
scripts/template.html                 dashboard design (data injected at build time)
data/history.json                     weekly snapshots (powers the political trend charts)
docs/index.html                       the generated dashboard (GitHub Pages serves this)
```

Preview offline with no keys: `python scripts/update_dashboard.py --sample`
