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
| Facebook | Apify `apify/facebook-groups-scraper`, re-scrapes the last 12 days each run | Claude reads post + comments at 4 and 10 days old |
| Mercari | Apify `devilscrapes/mercari-sold-listings` (sold-only filter) | Already sold — Claude only parses the title into brand / name / size |

Re-checks skip Claude entirely when the post text hasn't changed, so you only pay for new or edited posts.
Values use **sold** prices only, last 365 days, outliers removed (1.5× IQR), confidence ≥ 0.6.
Asking prices of unsold bottles are shown separately.

## Setup (≈10 minutes)

1. Create a GitHub repo and push this folder.
2. **Settings → Secrets and variables → Actions** → add `ANTHROPIC_API_KEY` and `APIFY_TOKEN`.
3. **Settings → Pages** → Source: *Deploy from a branch*, branch `main`, folder `/docs`.
   (Free Pages needs a public repo; private works on GitHub Pro.)
4. Put your Facebook group URLs in `config.yaml` under `facebook.groups` (the Apify scraper can only read
   groups whose posts are visible without logging in).
5. **Actions → Update fragrance values → Run workflow** with `backfill_days = 365` once to load history.
   After that it runs daily on its own.

## Costs (rough)

- **Claude Haiku**: ~1–2k tokens per Reddit/Facebook post, Mercari titles batched 25 per call. r/fragranceswap
  is a few hundred posts/week → typically a few dollars a month. The one-time 365-day backfill costs more.
- **Apify**: Mercari actor ≈ $0.004 per sold item; daily runs only fetch the last 3 days per keyword.
  Facebook groups scraper is billed per result — keep `max_posts_per_group` modest.
- Change the model with the `FRAGPRICE_MODEL` env var (default `claude-haiku-5-5`).

## Tuning

Everything lives in `config.yaml`: Mercari keywords (brands/houses to track), check ages, per-run Claude
call caps, valuation window and confidence floor. Run locally with:

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=... APIFY_TOKEN=...
python -m fragprice.run --sources reddit          # one source
python -m fragprice.run --export-only             # rebuild page data + Excel from the DB
```

## Layout

```
fragprice/run.py          pipeline + scheduling of checks
fragprice/extract.py      Claude prompt + structured tool schema
fragprice/normalize.py    brand/name canonicalization (fuzzy merges spelling variants)
fragprice/export.py       valuations → docs/data.json + Excel
fragprice/sources/        reddit.py, apify_sources.py (Mercari + Facebook)
fragprice/db.py           SQLite schema
```
