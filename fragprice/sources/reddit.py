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
# Any wording that could mean an item sold. Deliberately wide: a false hit only costs one Claude read,
# a miss would lose a sale. Used to skip old posts with no hint of a sale, and to pick comments worth sending.
SALE_HINT = re.compile(
    r"~~|\bsold\b|\bgone\b|\btaken\b|spoken for|no longer|perfume ?bot|fragrance ?bot|\bsale\b|"
    r"congrat|missed|bye ?bye|\bgrab|\bclaimed\b|off the market|✅|✔|☑",
    re.I)
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


def live_info(post_ids, pause=7.0):
    """CURRENT title, text, flair and author for many posts straight from reddit.com — 100 posts per request,
    so a few thousand posts take a few minutes even under Reddit's ~10 requests/minute limit for apps without
    an API key. This is what sees SOLD flair and later strikethrough/SOLD edits; the archive copy is a snapshot
    taken seconds after posting. Only works from a home connection (Reddit blocks cloud servers like GitHub's).
    Returns {} when reddit.com refuses us, so callers fall back to the archive."""
    ids, out = list(post_ids), {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = _get("https://www.reddit.com/api/info.json",
                    {"id": ",".join(f"t3_{x}" for x in chunk), "raw_json": 1}, tries=3)
        if data is None:
            if not out:
                print("  reddit.com not answering — using the archive copy (flair/edits may be out of date)")
                return {}
            print(f"  reddit.com stopped answering after {len(out)} posts; the rest use the archive copy")
            return out
        for ch in (data.get("data") or {}).get("children") or []:
            d = ch.get("data") or {}
            out[d.get("id")] = {"title": d.get("title"), "text": d.get("selftext"),
                                "flair": d.get("link_flair_text"), "author": d.get("author")}
        if (i // 100) % 10 == 9:
            print(f"  live from reddit.com: {len(out)}/{len(ids)} posts")
        if i + 100 < len(ids):
            time.sleep(pause)
    return out


def live(post_id, info=None):
    """Post text, flair, author + comments. `info` is the live reddit.com copy (from live_info) when we have it;
    comments always come from the archive, which stores each comment as it's posted."""
    c = _get(f"{ARCTIC}/comments/search", {"link_id": f"t3_{post_id}", "limit": 100})
    comments = [f"{x.get('author')}: {x.get('body')}" for x in (c or {}).get("data") or []]
    if info:
        return dict(info, comments=comments, live=True)
    p = _get(f"{ARCTIC}/posts/ids", {"ids": post_id})
    p = ((p or {}).get("data") or [None])[0]
    if not p:
        return None
    return {"title": p.get("title"), "text": p.get("selftext"), "flair": p.get("link_flair_text"),
            "author": p.get("author"), "comments": comments, "live": False}


def has_sale_hint(*texts):
    return any(t and SALE_HINT.search(t) for t in texts)


def relevant_comments(comments, seller):
    """Keep every comment by the seller, plus anyone's comment with sale wording. Others ('chat sent',
    'still available?') can never mark an item sold, so they're not worth paying for."""
    seller = (seller or "").lower()
    keep = []
    for c in comments or []:
        who, _, body = c.partition(": ")
        if (seller and who.lower() == seller) or SALE_HINT.search(body):
            keep.append(c)
    return keep
