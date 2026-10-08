"""Mercari (sold listings) and Facebook buy/sell groups, both through Apify actors.

Actor output schemas differ between actors, so every field is read defensively. Actor IDs and their
input templates live in config.yaml, so you can swap actors without touching code."""
import os
import time
from datetime import datetime
from urllib.parse import quote_plus

from apify_client import ApifyClient


def _client():
    return ApifyClient(os.environ["APIFY_TOKEN"])


def _run(actor_id, run_input, max_items):
    run = _client().actor(actor_id).call(run_input=run_input, timeout_secs=1800)
    if not run:
        return []
    items = []
    for it in _client().dataset(run["defaultDatasetId"]).iterate_items():
        items.append(it)
        if len(items) >= max_items:
            break
    return items


def _first(d, *keys):
    for k in keys:
        cur = d
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, "", []):
            return cur
    return None


def _ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v / 1000 if v > 1e12 else v)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _fill(template, **kw):
    """Recursively substitute {placeholders} in the config's input template."""
    if isinstance(template, str):
        if template.startswith("{") and template.endswith("}") and template[1:-1] in kw:
            return kw[template[1:-1]]  # keep ints as ints
        return template.format(**kw)
    if isinstance(template, list):
        return [_fill(x, **kw) for x in template]
    if isinstance(template, dict):
        return {k: _fill(v, **kw) for k, v in template.items()}
    return template


# ---------------- Mercari ----------------

MERCARI_SOLD_URL = "https://www.mercari.com/search/?keyword={kw}&itemStatuses=2&sortBy=2"


def mercari(cfg, sold_within_days=None):
    """Yield dicts {post_id, url, title, desc, price, created_utc} for sold Mercari listings."""
    out = {}
    days = min(365, sold_within_days or cfg.get("sold_within_days", 3))
    for kw in cfg["keywords"]:
        url = MERCARI_SOLD_URL.format(kw=quote_plus(kw))
        max_items = cfg["max_items_per_keyword"] if days <= 7 else max(cfg["max_items_per_keyword"], 1000)
        run_input = _fill(cfg["actor_input"], search_url=url, keyword=kw, max_items=max_items,
                          sold_within_days=days)
        try:
            items = _run(cfg["actor"], run_input, max_items)
        except Exception as e:  # one bad keyword shouldn't kill the run
            print(f"  mercari '{kw}' failed: {e}")
            continue
        for it in items:
            pid = _first(it, "item_id", "id", "itemId", "productId")
            title = _first(it, "title", "name", "productName")
            price = _first(it, "price", "itemPrice", "price.amount")
            if not pid or not title or price is None:
                continue
            try:
                price = float(str(price).replace("$", "").replace(",", "")) / cfg.get("price_divisor", 1)
            except ValueError:
                continue
            status = str(_first(it, "status", "itemStatus") or "sold_out").lower()
            if it.get("sold") is False or status in ("on_sale", "stop", "cancel", "active", "available"):
                continue  # only completed sales count
            desc = _first(it, "description", "desc") or ""
            if it.get("condition"):
                desc = f"Mercari condition: {it['condition']}. {desc}"
            out[str(pid)] = {
                "post_id": str(pid),
                "url": _first(it, "item_url", "url", "itemUrl") or f"https://www.mercari.com/us/item/{pid}/",
                "title": title,
                "desc": desc,
                "price": price,
                "created_utc": _ts(_first(it, "sold_date", "updated", "soldAt", "updatedAt", "scraped_at")) or int(time.time()),
            }
        print(f"  mercari '{kw}': {len(items)} items")
    return list(out.values())


# ---------------- Facebook groups ----------------

def facebook(cfg):
    """Yield dicts {post_id, url, title, text, created_utc, author} from configured groups."""
    out = {}
    for group in cfg["groups"]:
        run_input = _fill(cfg["actor_input"], group_url=group, max_items=cfg["max_posts_per_group"],
                          newer_than=cfg["lookback"])
        try:
            items = _run(cfg["actor"], run_input, cfg["max_posts_per_group"])
        except Exception as e:
            print(f"  facebook {group} failed: {e}")
            continue
        for it in items:
            pid = _first(it, "postId", "id", "legacyId")
            text = _first(it, "text", "message", "postText") or ""
            if not pid or not text:
                continue
            comments = []
            for c in _first(it, "topComments", "comments") or []:
                if isinstance(c, dict):
                    who = _first(c, "profileName", "authorName", "author.name") or "?"
                    comments.append(f"{who}: {_first(c, 'text', 'message') or ''}")
            author = _first(it, "user.name", "authorName", "author.name")
            body = text
            if comments:
                body += "\n\n---COMMENTS---\n" + "\n".join(comments)
            out[str(pid)] = {
                "post_id": str(pid),
                "url": _first(it, "url", "postUrl", "facebookUrl"),
                "title": f"[FB] {author or ''}".strip(),
                "text": body,
                "created_utc": _ts(_first(it, "time", "timestamp", "date", "createdAt")) or int(time.time()),
                "author": author,
            }
        print(f"  facebook {group}: {len(items)} posts")
    return list(out.values())
