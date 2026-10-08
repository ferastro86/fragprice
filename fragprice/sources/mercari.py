"""Mercari US sold-listing scraper (no Apify). Headless-ish Chromium via Playwright.

Robust to Mercari's hashed CSS classes:
  1. DOM: every result card is an <a href="/us/item/<id>/">; title comes from the image alt / aria-label,
     price from the "$123" text inside the card.
  2. Network: the page's own JSON API responses are captured while it loads; any object that looks like
     an item (id + name + price) enriches the DOM result with status and sold/updated time.
The search URL already filters to sold items (itemStatuses=2).
"""
import json
import re
import time
from urllib.parse import quote_plus

SEARCH = "https://www.mercari.com/search/?keyword={kw}&itemStatuses=2"
ITEM_HREF = re.compile(r"/us/item/(m\d+)")
PRICE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

EXTRACT_JS = """
() => {
  const out = {};
  for (const a of document.querySelectorAll('a[href*="/us/item/"]')) {
    const m = a.getAttribute('href').match(/\\/us\\/item\\/(m\\d+)/);
    if (!m || out[m[1]]) continue;
    const img = a.querySelector('img');
    const label = a.getAttribute('aria-label') || '';
    out[m[1]] = {
      id: m[1],
      href: a.href,
      title: (img && img.alt) || label || '',
      text: (a.innerText || '') + ' ' + label,
    };
  }
  return Object.values(out);
}
"""


def _walk(obj, found):
    """Collect dicts that look like Mercari items from arbitrary API JSON."""
    if isinstance(obj, dict):
        iid = obj.get("id")
        if isinstance(iid, str) and re.fullmatch(r"m\d+", iid) and ("name" in obj or "price" in obj):
            found[iid] = obj
        for v in obj.values():
            _walk(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, found)


def _ts(v):
    try:
        v = float(v)
        return int(v / 1000 if v > 1e12 else v)
    except (TypeError, ValueError):
        return None


def scrape_keyword(page, kw, max_items=150, max_scrolls=25):
    api_items = {}

    def on_response(resp):
        if "mercari.com" not in resp.url or "json" not in (resp.headers.get("content-type") or ""):
            return
        try:
            _walk(resp.json(), api_items)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(SEARCH.format(kw=quote_plus(kw)), wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector('a[href*="/us/item/"]', timeout=25000)
        except Exception:
            title = page.title()
            print(f"  mercari '{kw}': no results rendered (page title: {title!r}) — possibly blocked")
            return []
        cards, stale = {}, 0
        for _ in range(max_scrolls):
            for c in page.evaluate(EXTRACT_JS):
                cards.setdefault(c["id"], c)
            if len(cards) >= max_items:
                break
            before = len(cards)
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(1500)
            stale = stale + 1 if len(cards) == before else 0
            if stale >= 3:
                break
    finally:
        page.remove_listener("response", on_response)

    rows = []
    for iid, c in list(cards.items())[:max_items]:
        api = api_items.get(iid, {})
        m = PRICE.search(c["text"])
        price = float(m.group(1).replace(",", "")) if m else None
        if price is None and isinstance(api.get("price"), (int, float)):
            price = api["price"] / 100  # Mercari's API reports cents
        title = c["title"] or api.get("name") or ""
        status = str(api.get("status") or "").lower()
        if not title or price is None or status in ("on_sale", "active"):
            continue
        rows.append({
            "post_id": iid,
            "url": f"https://www.mercari.com/us/item/{iid}/",
            "title": title,
            "desc": (api.get("description") or "")[:300],
            "price": price,
            "created_utc": _ts(api.get("updated")) or int(time.time()),
        })
    print(f"  mercari '{kw}': {len(rows)} sold items ({len(api_items)} enriched from API)")
    return rows


def mercari(cfg, backfill_days=None):
    from playwright.sync_api import sync_playwright

    max_items = cfg["max_items_per_keyword"] * (5 if backfill_days else 1)
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.get("headless", False),
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="en-US",
                                  timezone_id="America/Chicago")
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.new_page()
        for kw in cfg["keywords"]:
            try:
                for r in scrape_keyword(page, kw, max_items):
                    out[r["post_id"]] = r
            except Exception as e:
                print(f"  mercari '{kw}' failed: {e}")
            page.wait_for_timeout(4000)  # pace requests between searches
        browser.close()
    return list(out.values())
