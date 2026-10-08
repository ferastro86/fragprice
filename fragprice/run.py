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
from . import extract
from .extract import PROMPT_VERSION, extract_post, extract_titles
from .normalize import Canonicalizer, clean_item

ROOT = Path(__file__).resolve().parent.parent
RUN_START = time.time()


def load_config():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def _int(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


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
        sold_n = _int(it.get("qty_sold"))
        left_n = _int(it.get("qty_available"))
        if it.get("status") == "sold" and sold_n and left_n:
            # partly sold line, e.g. "6 → 1 available, 5 sold": one sold row (x5) + one still-listed row (x1)
            keep.append(dict(it, qty=sold_n))
            keep.append(dict(it, status="available", qty=left_n, sold_evidence=None))
            continue
        it["qty"] = (sold_n if it.get("status") == "sold" else left_n) or 1
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

    read_reddit(con, cfg, canon, budget)


def _apply(con, cfg, canon, row, h, items, excl):
    """Save one post's Claude result and advance its check count (main thread only)."""
    store_items(con, canon, "reddit", dict(row), items, exclude_conditions=excl)
    _checked(con, cfg, row, h)


def _checked(con, cfg, row, h):
    db.mark_checked(con, "reddit", row["post_id"], h)
    # a post already older than the last check age has had all the time it needs for SOLD edits:
    # one read is enough, so don't schedule the 21-day re-read
    last_age = max(cfg["check_ages_days"])
    con.execute("UPDATE posts SET checks=? WHERE source='reddit' AND post_id=? AND checks>0 AND created_utc<=?",
                (len(cfg["check_ages_days"]), row["post_id"], time.time() - last_age * 86400))


def _wait_for_batch(con, cfg, canon, pending, deadline, excl):
    """Poll a submitted batch until it ends or time runs out. Returns True if its results were saved."""
    while extract.batch_status(pending["id"]) != "ended":
        if time.time() > deadline:
            print(f"  batch {pending['id']} still processing — its results are collected on the next run")
            return False
        time.sleep(30)
    results = extract.batch_results(pending["id"])
    rows = {r["post_id"]: r for r in con.execute(
        f"SELECT * FROM posts WHERE source='reddit' AND post_id IN ({','.join('?' * len(pending['posts']))})",
        list(pending["posts"]))}
    ok = bad = 0
    for pid, h in pending["posts"].items():
        res, row = results.get(pid), rows.get(pid)
        if row is None:
            continue
        if isinstance(res, list):
            _apply(con, cfg, canon, row, h, res, excl)
            ok += 1
        else:  # failed or missing: leave unchecked so it's retried next run
            con.execute("UPDATE posts SET error=? WHERE source='reddit' AND post_id=?", (str(res)[:300], pid))
            bad += 1
    con.commit()
    db.set_meta(con, "reddit_batch", None)
    print(f"  batch done: saved {ok} posts" + (f", {bad} failed (retried next run)" if bad else ""))
    return True


def read_reddit(con, cfg, canon, budget=None):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .sources import reddit

    excl = set(cfg.get("exclude_conditions") or [])
    use_batch = cfg.get("batch", True)
    fetch_deadline = RUN_START + cfg.get("max_minutes", 150) * 60  # counted from the start of the run
    wait_deadline = fetch_deadline + cfg.get("batch_wait_minutes", 90) * 60

    # 1. finish a batch left over from the previous run before starting a new one
    pending = db.get_meta(con, "reddit_batch")
    if pending:
        print(f"reddit: collecting batch from last run ({len(pending['posts'])} posts)")
        if not _wait_for_batch(con, cfg, canon, pending, wait_deadline, excl):
            return

    due = db.due_posts(con, "reddit", cfg["check_ages_days"], limit=budget or cfg["max_claude_calls"])
    print(f"reddit: {len(due)} posts due ({'batch, half price' if use_batch else 'direct'})")
    last_age = max(cfg["check_ages_days"])

    def fetch(row):
        """Worker thread: get the post's current text + comments and decide what to do with it."""
        if time.time() > fetch_deadline:
            return row, "late", None, None
        cur = reddit.live(row["post_id"]) or {"title": row["title"], "text": row["text"], "flair": None,
                                             "author": None, "comments": []}
        if cur["text"] in ("[deleted]", "[removed]", None, ""):
            cur["text"] = row["text"]  # keep archived copy if seller deleted after selling
        blob = f"{cur['title']}\n{cur['text']}\n{cur.get('flair')}\n" + "\n".join(cur["comments"])
        h = db.text_hash(blob)
        if row["checks"] > 0 and h == row["text_hash"]:
            return row, "same", h, None  # unchanged since last read
        old = row["created_utc"] <= time.time() - last_age * 86400
        if old and cfg.get("skip_old_without_sale_hint", True) and not reddit.has_sale_hint(
                cur["title"], cur["text"], cur.get("flair"), *cur["comments"]):
            return row, "nohint", h, None  # old post with no hint of any sale: nothing to learn
        comments = reddit.relevant_comments(cur["comments"], cur.get("author"))
        text = extract.post_text("reddit r/fragranceswap", cur["title"], cur["text"], comments,
                                 cur.get("flair"), seller=f"u/{cur['author']}" if cur.get("author") else None)
        return row, "read", h, text

    to_read, counts = {}, {"same": 0, "nohint": 0, "late": 0}
    with ThreadPoolExecutor(max_workers=cfg.get("workers", 8)) as pool:
        for i, fut in enumerate(as_completed([pool.submit(fetch, r) for r in due]), 1):
            row, what, h, text = fut.result()
            if what == "read":
                to_read[row["post_id"]] = (row, h, text)
            else:
                counts[what] += 1
                if what in ("same", "nohint"):
                    _checked(con, cfg, row, h)
            if i % 250 == 0:
                con.commit()
                print(f"  fetched {i}/{len(due)}")
    con.commit()
    print(f"reddit: {len(to_read)} posts for Claude; skipped {counts['nohint']} old posts with no sale hint, "
          f"{counts['same']} unchanged" + (f"; {counts['late']} left for next run (time limit)" if counts["late"] else ""))
    if not to_read:
        return

    if use_batch:
        batch_id = extract.submit_batch({pid: t for pid, (_, _, t) in to_read.items()})
        pending = {"id": batch_id, "posts": {pid: h for pid, (_, h, _) in to_read.items()}}
        db.set_meta(con, "reddit_batch", pending)  # saved, so the next run can collect it if we run out of time
        print(f"  submitted batch {batch_id}; waiting for results")
        _wait_for_batch(con, cfg, canon, pending, wait_deadline, excl)
        return

    def call(item):
        pid, (row, h, text) = item
        try:
            return row, h, extract._call(text), None
        except Exception as e:
            return row, h, None, str(e)[:300]

    done = 0
    with ThreadPoolExecutor(max_workers=cfg.get("workers", 8)) as pool:
        for fut in as_completed([pool.submit(call, it) for it in to_read.items()]):
            row, h, items, err = fut.result()
            if err:
                con.execute("UPDATE posts SET error=? WHERE source='reddit' AND post_id=?", (err, row["post_id"]))
            else:
                _apply(con, cfg, canon, row, h, items, excl)
                done += 1
            con.commit()
    print(f"reddit: read {done} posts")


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
        if db.get_meta(con, "prompt_version", 1) != PROMPT_VERSION:
            n = con.execute("UPDATE posts SET checks=0, text_hash=NULL WHERE checks>0 AND source!='mercari'").rowcount
            db.set_meta(con, "prompt_version", PROMPT_VERSION)
            print(f"reading rules changed (v{PROMPT_VERSION}): {n} posts queued to be re-read")
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
