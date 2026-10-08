"""Reddit via Arctic Shift (archive, no auth) for discovery, plus a live re-fetch at check time so we
see the seller's later 'SOLD' edits and comments, which the archive may have captured too early."""
import re
import time

import requests

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
UA = {"User-Agent": "fragprice/1.0 (fragrance price research)"}
WTB = re.compile(r"\[\s*wtb\s*\]|\bwtb\b|\[w\]\s*[^\[]*\[h\]\s*(paypal|\$|cash)", re.I)


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
        if len(batch) < 100 or not new:
            break
        last = int(batch[-1]["created_utc"])
        cur = last if last > cur else cur + 1
        time.sleep(0.5)
    return out


def is_candidate(post):
    """Cheap pre-filter so we don't pay Claude to read want-to-buy posts."""
    title = post.get("title") or ""
    if WTB.search(title):
        return False
    return not post.get("is_video")


def _flatten(children, out, depth=0):
    for c in children or []:
        d = c.get("data", {})
        if c.get("kind") == "t1" and d.get("body"):
            out.append(f"{d.get('author')}: {d['body']}")
            rep = d.get("replies")
            if isinstance(rep, dict) and depth < 3:
                _flatten(rep.get("data", {}).get("children"), out, depth + 1)


def live(post_id):
    """Current post text, flair, author and comments. Tries reddit.com, falls back to Arctic Shift."""
    data = _get(f"https://www.reddit.com/comments/{post_id}.json", {"raw_json": 1, "limit": 100})
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
