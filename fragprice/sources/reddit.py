"""Reddit via Arctic Shift (archive, no auth) for discovery, plus a live re-fetch at check time so we
see the seller's later 'SOLD' edits and comments, which the archive may have captured too early."""
import re
import time

import requests

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
UA = {"User-Agent": "fragprice/1.0 (fragrance price research)"}
# r/fragranceswap titles: "[WTS] Guerlain Vetiver 147/150ml (Bottle)" with flair WTS / WTB / WTT.
TAG = re.compile(r"\[\s*(wts|wtb|wtt|fs|ft|iso)\s*\]", re.I)
TYPE = re.compile(r"\(([^)]*\b(?:bottles?|decants?|samples?|splits?)\b[^)]*)\)", re.I)
DECANT = re.compile(r"\b(decants?|splits?|samples?|vials?)\b", re.I)
BOTTLE = re.compile(r"\b(bottles?|partials?|tester)\b", re.I)
SIZE = re.compile(r"(\d+(?:\.\d+)?)\s?(ml|oz)\b", re.I)


def _get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 404):
                return None
        except requests.RequestException:
            pass
        time.sleep(3 * (i + 1))
    return None


def discover(subreddit, after_ts, before_ts=None):
    """All posts in [after_ts, before_ts) from the archive."""
    before_ts = before_ts or int(time.time())
    seen, out, cur = set(), [], int(after_ts)
    while cur < before_ts:
        data = _get(f"{ARCTIC}/posts/search", {
            "subreddit": subreddit, "after": cur, "before": before_ts, "limit": 100, "sort": "asc",
        })
        batch = (data or {}).get("data") or []
        new = [p for p in batch if p["id"] not in seen]
        for p in new:
            seen.add(p["id"])
            out.append(p)
        if len(out) % 1000 < len(new):
            print(f"  collected {len(out)} posts so far")
        if len(batch) < 100 or not new:
            break
        last = int(batch[-1]["created_utc"])
        cur = last if last > cur else cur + 1
        time.sleep(0.5)
    return out


def _mentions_bottle(text):
    """Bottle words, or any size >= 30 ml (1 oz) — '10ml' / '5ml' are decant sizes."""
    if BOTTLE.search(text):
        return True
    return any(float(n) * (30 if u.lower() == "oz" else 1) >= 30 for n, u in SIZE.findall(text))


def skip_reason(post, skip_decant_posts=True):
    """Cheap pre-filter so Claude never reads posts we don't want. Returns a reason, or None to keep.

    - Keep anything tagged WTS (title tag or flair), including combined "[WTS][WTT]" posts.
    - Skip WTB / WTT / ISO posts that aren't also selling.
    - Skip decant-only posts: the "(Decant)" type tag (or title) with no bottle in it.
      Posts selling both, e.g. "(Bottle & Decant)", go to Claude, which keeps only the bottles.
    """
    title = post.get("title") or ""
    flair = (post.get("link_flair_text") or "").lower()
    if post.get("is_video"):
        return "video"
    tags = {t.lower() for t in TAG.findall(title)}
    tags |= {t for t in ("wts", "wtb", "wtt") if t in flair}
    selling = bool(tags & {"wts", "fs"}) or "sell" in flair
    if not selling and tags & {"wtb", "iso"}:
        return "wtb"
    if not selling and tags & {"wtt", "ft"}:
        return "wtt"
    if not selling and re.search(r"\b(wtb|wtt|iso|looking for)\b", title, re.I):
        return "wtb/wtt"
    if not tags and not flair:
        return "untagged"  # mod / bot posts (e.g. PerfumeBot announcements)
    if skip_decant_posts:
        kind = " ".join(TYPE.findall(title)) or title  # prefer the (Bottle)/(Decant) tag when present
        if DECANT.search(kind) and not _mentions_bottle(kind):
            return "decants"
    return None


def is_candidate(post, skip_decant_posts=True):
    return skip_reason(post, skip_decant_posts) is None


def _flatten(children, out, depth=0):
    for c in children or []:
        d = c.get("data", {})
        if c.get("kind") == "t1" and d.get("body"):
            out.append(f"{d.get('author')}: {d['body']}")
            rep = d.get("replies")
            if isinstance(rep, dict) and depth < 3:
                _flatten(rep.get("data", {}).get("children"), out, depth + 1)


_reddit_blocked = False


def live(post_id):
    """Current post text, flair, author and comments. Tries reddit.com, falls back to Arctic Shift.
    If reddit.com refuses us once, stop trying it for the rest of the run (no slow retries per post)."""
    global _reddit_blocked
    data = None if _reddit_blocked else _get(f"https://www.reddit.com/comments/{post_id}.json",
                                             {"raw_json": 1, "limit": 100}, tries=2)
    if data is None and not _reddit_blocked:
        _reddit_blocked = True
        print("  reddit.com not answering from this runner — using the Arctic Shift archive for the rest of the run")
    if isinstance(data, list) and data:
        p = data[0]["data"]["children"][0]["data"]
        comments = []
        _flatten(data[1]["data"]["children"] if len(data) > 1 else [], comments)
        return {
            "title": p.get("title"), "text": p.get("selftext"), "flair": p.get("link_flair_text"),
            "author": p.get("author"), "comments": comments,
        }
    p = _get(f"{ARCTIC}/posts/ids", {"ids": post_id})
    p = ((p or {}).get("data") or [None])[0]
    c = _get(f"{ARCTIC}/comments/search", {"link_id": f"t3_{post_id}", "limit": 100})
    comments = [f"{x.get('author')}: {x.get('body')}" for x in (c or {}).get("data") or []]
    if not p:
        return None
    return {
        "title": p.get("title"), "text": p.get("selftext"), "flair": p.get("link_flair_text"),
        "author": p.get("author"), "comments": comments,
    }
