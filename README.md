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
| Mercari | Own Playwright scraper (`fragprice/sources/mercari.py`), mercari.com search with the sold-only filter | Already sold — Claude only parses the title into brand / name / size |
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

- **Claude Haiku**: ~1–2k tokens per Reddit/Facebook post, Mercari titles batched 25 per call. r/fragranceswap
  is a few hundred posts/week → typically a few dollars a month. The one-time 365-day backfill costs more.
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

## Mercari notes

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
