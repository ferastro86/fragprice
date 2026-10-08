# fragprice — what every fragrance actually sells for

Daily GitHub Actions job that collects **sold** fragrance listings from r/fragranceswap, Facebook buy/sell
groups and Mercari, has Claude (Haiku) read each post to work out *which* items sold and for how much,
stores everything in SQLite, and publishes:

- **`docs/index.html`** — mobile search page (GitHub Pages): median sold price per size/condition, $/ml,
  recent sales with links, and a "value my bottle" calculator (size + fill %).
- **`docs/fragrance_values.xlsx`** — Excel workbook: `Values` (one row per fragrance × size × condition),
  `Sales` (every extracted listing with evidence + link), `About`.
- **`data/fragprice.db`** — the SQLite database (open with DB Browser for SQLite, or pandas).

## How it decides "sold"

| Source | Discovery | Sold signal |
|---|---|---|
| Reddit | Arctic Shift archive (no auth) | Claude reads the **live** post + seller comments at 4 days old and again at 21 days: `~~strikethrough~~`, "SOLD", flair, "sold to u/…" |
| Mercari | **Off for now** — own Playwright scraper kept in `fragprice/sources/mercari.py`, sold-only search | Already sold — Claude only parses the title into brand / name / size |
| Facebook | **Planned** — `fragprice/sources/facebook.py` is a documented placeholder; disabled and hidden from the page | Claude reads post + comments at 4 and 10 days old |

Re-checks skip Claude entirely when the post text hasn't changed, so you only pay for new or edited posts.
Values use **sold** prices only, last 365 days, outliers removed (1.5× IQR), confidence ≥ 0.6.
Asking prices of unsold bottles are shown separately.

## Choosing platforms

- **Search page:** the *Price from* toggles (Reddit · Facebook · Mercari, with sale counts) recompute every value
  live from only the platforms that are on. Each fragrance also has a *By platform* table so you can compare.
  Your choice is remembered on that device.
- **Default:** `valuation.default_off: [mercari]` in `config.yaml` starts Mercari unticked for everyone.
- **Excel:** the Values sheet has Median / # columns per platform; filter the Sales sheet's Source column too.
- **Stop scraping a platform entirely:** set `enabled: false` under it in `config.yaml`.

## Setup (≈10 minutes)

1. Create a GitHub repo and push this folder.
2. **Settings → Secrets and variables → Actions** → add `ANTHROPIC_API_KEY`.
3. **Settings → Pages** → Source: *Deploy from a branch*, branch `main`, folder `/docs`.
   (Free Pages needs a public repo; private works on GitHub Pro.)
4. **Actions → Update fragrance values → Run workflow** with `backfill_days = 365` once to load history.
   After that it runs daily on its own.

## Costs (rough)

- **Claude Haiku via the Message Batches API** (half price, same answers): roughly $0.0002 per post read.
  Only the seller's comments and comments with sale wording are sent. Posts older than the last check age that
  contain no sale wording at all (sold, gone, taken, ~~strikethrough~~, PerfumeBot, ✅…) skip Claude entirely.
  Rough totals: ~$1–2/month ongoing, ~$1.50–2.50 for a one-time 180-day backfill.
- Batch answers usually arrive within minutes; if a run ends first, the next run collects them before doing anything else.
- **Scraping**: free — Reddit via Arctic Shift, Mercari via your own Chromium scraper on the Actions runner.
- Change the model with the `FRAGPRICE_MODEL` env var (default `claude-haiku-5-5`).

## Tuning

Everything lives in `config.yaml`: Mercari keywords (brands/houses to track), check ages, per-run Claude
call caps, valuation window and confidence floor. Run locally with:

```bash
pip install -r requirements.txt
python -m playwright install chromium
export ANTHROPIC_API_KEY=...
python -m fragprice.run --sources reddit          # one source
python -m fragprice.run --export-only             # rebuild page data + Excel from the DB
```

## Layout

```
fragprice/run.py          pipeline + scheduling of checks
fragprice/extract.py      Claude prompt + structured tool schema
fragprice/normalize.py    brand/name canonicalization (fuzzy merges spelling variants)
fragprice/export.py       valuations → docs/data.json + Excel
fragprice/sources/        reddit.py, mercari.py, facebook.py (stub)
fragprice/db.py           SQLite schema
```

## Mercari notes (currently disabled)

Mercari is switched off: in testing (April 2026) Mercari showed automated browsers — Playwright, stealth mode,
even real Chrome driven by Playwright — an empty "No results found" page while the same search worked in normal
Chrome. Everything downstream (database, page toggle, Excel columns) still supports it. To try again set
`mercari.enabled: true`; the workflow then installs Chromium automatically. If results come back empty, the more
promising routes are reading sold searches from your real Chrome (e.g. the Page Change Watcher extension) or a
hosted scraper, plugged in by making `mercari(cfg)` return the same row format.


Mercari renders results with JavaScript and uses rotating CSS class names, so the scraper reads result cards by
their `/us/item/<id>` links and also captures the page's own JSON API responses for status and dates. It runs a real
(non-headless) Chromium under a virtual display. If Mercari starts blocking the GitHub runner's datacenter IPs
(you'll see "possibly blocked" in the log), run just that source from your Mac and push the DB:
`python -m fragprice.run --sources mercari && git add data docs && git commit -m mercari && git push`.

Mercari has no "date sold" on search cards, so a sale is dated the day it's first seen (accurate for daily runs).

## Adding Facebook later

The pipeline, Claude re-checks, database, page toggle and Excel columns already support Facebook. Implement
`facebook(cfg)` in `fragprice/sources/facebook.py` (its docstring gives the exact return format), add group URLs
under `facebook.groups`, and set `facebook.enabled: true`. The Facebook toggle appears on the page automatically.

## Running on your home PC (live reddit.com data)

Reddit blocks GitHub's servers, so a GitHub run only sees the archive copy of each post, taken seconds after it
was posted: later SOLD flair, strikethroughs and "SOLD" edits are invisible. Running on a PC at home fixes that.
One reddit.com request returns the current flair + text of 100 posts, so this stays well under Reddit's limit.

One-time setup on the PC (Windows):
1. Install **Git for Windows** (git-scm.com) and **Python 3.12** (python.org — tick "Add python.exe to PATH").
2. In the repo: **Settings → Actions → Runners → New self-hosted runner → Windows x64**. Open PowerShell, run the
   commands GitHub shows (in e.g. `C:\actions-runner`). When `config.cmd` asks, accept the defaults and answer
   **Y** to "run as service", so it runs even when you're not logged in.
3. In the repo: **Settings → Secrets and variables → Actions → Variables → New variable**: `RUNNER` = `self-hosted`.
4. Power settings: don't let the PC sleep (at least around the daily run, ~5:47 AM Central).

Delete the `RUNNER` variable to move runs back to GitHub's servers. If the PC is off, a run waits up to 24 hours
for it. The first run on the PC re-checks every post already read from the archive; posts whose live copy is
unchanged are skipped for free, and only ones with new SOLD flair/edits are sent to Claude.
