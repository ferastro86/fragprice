"""Pipeline entry point.

    python -m fragprice.run                      # daily run: all enabled sources
    python -m fragprice.run --sources reddit     # just one source
    python -m fragprice.run --backfill-days 365  # pull a year of Reddit history first
    python -m fragprice.run --export-only        # rebuild data.json + Excel from the DB
"""
import argparse
import time
from pathlib import Path

import yaml

from . import db, export
from .extract import extract_post, extract_titles
from .normalize import Canonicalizer, clean_item

ROOT = Path(__file__).resolve().parent.parent


def load_config():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def store_items(con, canon, source, post, items, default_status=None, exclude_conditions=()):
    keep = []
    for it in items:
        it = clean_item(it)
        if it.get("condition") in exclude_conditions:
            continue  # e.g. decants/samples on a bottle post
        if default_status:
            it["status"] = default_status
        _, _, key = canon.canon(it.get("brand"), it.get("name"))
        if not key:
            continue
        it["key"] = key  # raw brand/name kept as written; export shows the majority spelling per key
        keep.append(it)
    db.replace_sales(con, source, post["post_id"], post["url"], post["created_utc"], keep)
    return len(keep)


# ---------------- per-source ----------------

def run_reddit(con, cfg, canon, backfill_days=None, budget=None):
    from .sources import reddit

    for sub in cfg["subreddits"]:
        mk = f"reddit_cursor_{sub}"
        now = int(time.time())
        start = now - backfill_days * 86400 if backfill_days else db.get_meta(con, mk, now - cfg["first_run_days"] * 86400)
        posts = reddit.discover(sub, start, now)
        n, skipped = 0, {}
        for p in posts:
            why = reddit.skip_reason(p, cfg.get("skip_decant_posts", True))
            if why:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            db.upsert_post(con, "reddit", p["id"], f"https://www.reddit.com{p.get('permalink', '')}",
                           p.get("title"), p.get("selftext"), p.get("created_utc"))
            n += 1
        con.commit()
        db.set_meta(con, mk, max(now - 3600, db.get_meta(con, mk, 0)))
        print(f"reddit r/{sub}: discovered {len(posts)} posts, {n} candidates, skipped {skipped}")

    due = db.due_posts(con, "reddit", cfg["check_ages_days"], limit=budget or cfg["max_claude_calls"])
    print(f"reddit: {len(due)} posts due for a Claude read ({cfg.get('workers', 6)} at a time)")
    deadline = time.time() + cfg.get("max_minutes", 40) * 60
    excl = set(cfg.get("exclude_conditions") or [])

    def work(row):
        """Fetch + Claude read in a worker thread. Returns (row, hash, items|None, error|None)."""
        if time.time() > deadline:
            return row, None, None, "deadline"
        cur = reddit.live(row["post_id"]) or {"title": row["title"], "text": row["text"], "flair": None,
                                             "author": None, "comments": []}
        if cur["text"] in ("[deleted]", "[removed]", None, ""):
            cur["text"] = row["text"]  # keep archived copy if seller deleted after selling
        blob = f"{cur['title']}\n{cur['text']}\n{cur.get('flair')}\n" + "\n".join(cur["comments"])
        h = db.text_hash(blob)
        if row["checks"] > 0 and h == row["text_hash"]:
            return row, h, None, None  # unchanged since last read: skip Claude
        try:
            items = extract_post("reddit r/fragranceswap", cur["title"], cur["text"], cur["comments"],
                                 cur.get("flair"), seller=f"u/{cur['author']}" if cur.get("author") else None)
            return row, h, items, None
        except Exception as e:
            return row, h, None, str(e)[:300]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    done = late = 0
    with ThreadPoolExecutor(max_workers=cfg.get("workers", 6)) as pool:
        for fut in as_completed([pool.submit(work, r) for r in due]):
            row, h, items, err = fut.result()   # all DB writes happen here, on the main thread
            if err == "deadline":
                late += 1
                continue
            if err:
                con.execute("UPDATE posts SET error=? WHERE source='reddit' AND post_id=?", (err, row["post_id"]))
                print(f"  {row['post_id']} failed: {err}")
            elif items is None:
                db.mark_checked(con, "reddit", row["post_id"], h)
            else:
                k = store_items(con, canon, "reddit", dict(row), items, exclude_conditions=excl)
                db.mark_checked(con, "reddit", row["post_id"], h)
                done += 1
                if done % 25 == 0:
                    print(f"  read {done}/{len(due)} posts")
            con.commit()
    print(f"reddit: read {done} posts" + (f"; time budget reached, {late} wait for the next run" if late else ""))


def run_facebook(con, cfg, canon):
    from .sources.facebook import facebook

    if not cfg.get("groups"):
        print("facebook: no groups configured, skipping")
        return
    for p in facebook(cfg):
        db.upsert_post(con, "facebook", p["post_id"], p["url"], p["title"], p["text"], p["created_utc"])
    con.commit()
    due = db.due_posts(con, "facebook", cfg["check_ages_days"], limit=cfg["max_claude_calls"])
    print(f"facebook: {len(due)} posts due")
    for row in due:
        h = db.text_hash(row["text"])
        if row["checks"] > 0 and h == row["text_hash"]:
            db.mark_checked(con, "facebook", row["post_id"], h)
            continue
        try:
            seller = (row["title"] or "").replace("[FB]", "").strip() or None
            items = extract_post("facebook buy/sell group", "", row["text"], seller=seller)
            store_items(con, canon, "facebook", dict(row), items)
            db.mark_checked(con, "facebook", row["post_id"], h)
        except Exception as e:
            print(f"  fb {row['post_id']} failed: {e}")
        con.commit()


def run_mercari(con, cfg, canon, backfill_days=None):
    from .sources.mercari import mercari

    rows = mercari(cfg, backfill_days)
    new = []
    for r in rows:
        exists = con.execute("SELECT 1 FROM posts WHERE source='mercari' AND post_id=?", (r["post_id"],)).fetchone()
        db.upsert_post(con, "mercari", r["post_id"], r["url"], r["title"], r["desc"], r["created_utc"])
        if not exists:
            new.append(r)
    con.commit()
    print(f"mercari: {len(rows)} sold listings, {len(new)} new")
    batch = cfg.get("titles_per_call", 25)
    for i in range(0, len(new), batch):
        chunk = new[i:i + batch]
        try:
            items = extract_titles([{"ref": r["post_id"], "title": r["title"], "price": r["price"], "desc": r["desc"]}
                                    for r in chunk])
        except Exception as e:
            print(f"  mercari batch failed: {e}")
            continue
        grouped = {}
        for it in items:
            grouped.setdefault(str(it.get("ref")), []).append(it)
        for r in chunk:
            pid = r["post_id"]
            its = grouped.get(pid, [])
            for it in its:  # the scraped price is authoritative for single-item listings
                if len(its) == 1:
                    it["price"] = r["price"]
            store_items(con, canon, "mercari", r, its, default_status="sold")
            db.mark_checked(con, "mercari", pid, db.text_hash(r["title"]))
        con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default=None, help="comma list: reddit,mercari,facebook")
    ap.add_argument("--backfill-days", type=int, default=None)
    ap.add_argument("--export-only", action="store_true")
    a = ap.parse_args()

    cfg = load_config()
    con = db.connect()
    if not a.export_only:
        canon = Canonicalizer(con)
        wanted = a.sources.split(",") if a.sources else [s for s in ("reddit", "mercari", "facebook") if cfg[s]["enabled"]]
        for s in wanted:
            print(f"=== {s} ===")
            try:
                if s == "reddit":
                    run_reddit(con, cfg["reddit"], canon, a.backfill_days)
                elif s == "mercari":
                    run_mercari(con, cfg["mercari"], canon, a.backfill_days)
                elif s == "facebook":
                    run_facebook(con, cfg["facebook"], canon)
            except Exception as e:  # keep other sources + export going
                print(f"{s} crashed: {e}")
    vcfg = dict(cfg["valuation"], active=[s for s in ("reddit", "facebook", "mercari") if cfg[s]["enabled"]])
    export.build(con, vcfg)
    con.close()


if __name__ == "__main__":
    main()
