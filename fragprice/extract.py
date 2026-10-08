"""Claude reads each post and returns structured listings: what fragrance, what size/fill, price, and
whether that specific item actually SOLD (strikethrough, 'SOLD' edits, seller comments, flair)."""
import json
import os
import time

import anthropic

MODEL = os.environ.get("FRAGPRICE_MODEL", "claude-haiku-5-5")
# Bump when the SYSTEM rules change: posts read under an older version get re-read on the next run.
PROMPT_VERSION = 5
_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


SYSTEM = """You extract fragrance sale listings from marketplace posts (Reddit r/fragranceswap, Facebook buy/sell groups, Mercari titles).

For every distinct fragrance item offered for sale, return one entry. Rules:

IDENTITY
- brand: official house name, properly cased (e.g. "Parfums de Marly", "Maison Francis Kurkdjian", "Le Labo", "Creed", "Dior").
- name: official fragrance name. Put the concentration IN the name only when that concentration is a separate,
  differently-priced product (e.g. "Sauvage Elixir", "Bleu de Chanel Parfum", "Baccarat Rouge 540 Extrait").
  Otherwise use the plain name (e.g. "Layton", "Aventus", "Santal 33") and put EDP/EDT/etc. in `concentration`.
- Expand abbreviations: PDM -> Parfums de Marly, MFK -> Maison Francis Kurkdjian, BR540 -> Baccarat Rouge 540,
  YSL -> Yves Saint Laurent, TF -> Tom Ford, JPG -> Jean Paul Gaultier, MM -> Maison Margiela.
- Clones/dupes (Armaf, Alexandria, Montagne, Dua, etc.) are their own brand — never record them as the original.

SIZE / CONDITION
- size_ml: bottle capacity in ml (convert oz: 1 oz = 30 ml, 1.7 oz = 50 ml, 2.5 oz = 75 ml, 3.4 oz = 100 ml, 4.2 oz = 125 ml).
- fill_pct: how full (100 for new/full). "90%", "95/100ml" -> 95, "used 5 sprays" -> ~98. Unknown -> null.
- condition: new_sealed | new_unsealed | used | partial | decant | tester | sample | unknown.
  Decants/samples are small vials split from a bottle (usually 2-30 ml).

PRESENTATION (what comes with the bottle)
- box: "yes" if the original box is included ("with box", "full presentation", "BNIB"), "no" if stated without
  ("no box", "bottle only", "unboxed"), else "unknown". A tester box counts as "yes" but say so in presentation.
- cap: "yes" / "no" (e.g. "no cap", "missing cap", "capless") / "unknown". Assume nothing — only what's stated.
- presentation: a few words exactly as the seller describes it, e.g. "full presentation", "box, no cap",
  "tester box", "damaged box", "bottle + cap only". Null if nothing is said.

QUANTITY (sellers often list several identical bottles on one line)
- qty_sold: how many of THIS line sold. "(x3) SOLD" -> 3; "(x3 2) $345 SOLD" (3 struck, 2 written) -> read the
  final number, 2, unless the text says otherwise; "(6 1 available) ... 5 Sold!!" -> 5. Single bottle sold -> 1.
  Not sold -> 0.
- qty_available: how many are still for sale ("1 available" -> 1; unsold single bottle -> 1; all sold -> 0).
- A line that is partly sold (some sold, some left) is still ONE entry: status "sold", with both numbers filled in.

PRICE
- price: the asking/sold price for THAT item, as a number. If several sizes/decant tiers are listed, make one entry per tier.
- If one price covers several items, set bundle=true on each with the total price on the first and null on the rest.
- price_includes_shipping: true if "shipped"/"incl. shipping"/"+ free ship", false if "+ shipping", else null.
- currency: ISO code (default USD).

SOLD STATUS (be strict — this drives valuations)
- sold: ONLY one of these explicit signals for THIS item:
    1. the item's line is ~~struck through~~ in the post text;
    2. the word "SOLD"/"sold" next to the item in the post text (e.g. "Layton 125ml $180 - SOLD");
    3. the post's flair or title says SOLD (e.g. flair "SOLD", "[SOLD]") — that covers every item in the post;
    4. a comment by the SELLER saying it sold (e.g. "Layton sold", "all sold", "everything is gone");
    5. a comment by anyone else that clearly says THIS item is gone (e.g. "can't believe Night Drive is gone",
       "congrats on selling the Layton", "missed out on the Aventus");
    6. a "PerfumeBot Sale u/X" / "PerfumeBot Buy u/X" comment, but ONLY on a post selling a single bottle.
       On a multi-bottle post PerfumeBot alone does not say which item sold — it needs one of signals 1–5.
  Mercari items are always sold.
  Not sold signals: "I'll take it", "chat sent", "interested", "PM'd" (interest is not a sale).
- pending: "pending", "on hold", "PPD".
- available: explicitly still available, or no sold signal on a post where other items ARE marked sold.
- unknown: no signal either way.
- sold_evidence: short quote/phrase that justified the status.
- confidence: 0-1, your confidence in the item identity + price + status together.

On r/fragranceswap titles start with [WTS]/[WTB]/[WTT] and end with (Bottle)/(Decant).
Ignore WTB / want-to-buy and WTT / want-to-trade-only posts, trade-only items with no price, and non-fragrance items.
Still record decants/samples with condition decant/sample (they are filtered downstream), never as bottles.
If nothing qualifies, return an empty items list."""

TOOL = {
    "name": "record_listings",
    "description": "Record every fragrance item for sale found in the post(s).",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "description": "Post id the item came from (batched input only)."},
                        "brand": {"type": "string"},
                        "name": {"type": "string"},
                        "concentration": {"type": ["string", "null"], "enum": ["EDT", "EDP", "Parfum", "Extrait", "Cologne", "Elixir", "Other", None]},
                        "size_ml": {"type": ["number", "null"]},
                        "fill_pct": {"type": ["number", "null"]},
                        "condition": {"type": "string", "enum": ["new_sealed", "new_unsealed", "used", "partial", "decant", "tester", "sample", "unknown"]},
                        "box": {"type": "string", "enum": ["yes", "no", "unknown"]},
                        "cap": {"type": "string", "enum": ["yes", "no", "unknown"]},
                        "presentation": {"type": ["string", "null"]},
                        "price": {"type": ["number", "null"]},
                        "currency": {"type": "string"},
                        "price_includes_shipping": {"type": ["boolean", "null"]},
                        "bundle": {"type": "boolean"},
                        "qty_sold": {"type": ["integer", "null"]},
                        "qty_available": {"type": ["integer", "null"]},
                        "status": {"type": "string", "enum": ["sold", "available", "pending", "unknown"]},
                        "sold_evidence": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                    },
                    "required": ["brand", "name", "condition", "price", "status", "confidence"],
                },
            }
        },
        "required": ["items"],
    },
}


def _params(user_text, max_tokens=16000):  # room for 100+ item lists; only tokens used are billed
    return dict(
        model=MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_listings"},
        messages=[{"role": "user", "content": user_text}],
    )


def _items(msg):
    for block in msg.content:
        if block.type == "tool_use":
            items = block.input.get("items", []) or []
            if isinstance(items, str):  # occasionally the list comes back JSON-encoded as a string
                try:
                    items = json.loads(items)
                except ValueError:
                    items = []
            return items if isinstance(items, list) else []
    return []


def _call(user_text, max_tokens=16000, retries=4):
    for attempt in range(retries):
        try:
            return _items(client().messages.create(**_params(user_text, max_tokens)))
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            status = getattr(e, "status_code", None)
            if status and 400 <= status < 500 and status != 429:
                raise
            time.sleep(2 ** attempt * 5)
    raise RuntimeError("Claude API failed after retries")


def post_text(source, title, body, comments=None, flair=None, seller=None):
    parts = [f"SOURCE: {source}"]
    if seller:
        parts.append(f"SELLER (post author): {seller}")
    if flair:
        parts.append(f"FLAIR: {flair}")
    parts.append(f"TITLE: {title or ''}")
    parts.append(f"BODY:\n{(body or '')[:12000]}")
    if comments:
        parts.append("COMMENTS (author: text):\n" + "\n".join(c[:400] for c in comments[:60]))
    return "\n\n".join(parts)


def extract_post(source, title, body, comments=None, flair=None, seller=None):
    """One marketplace post (Reddit / Facebook) -> list of item dicts (immediate, full price)."""
    return _call(post_text(source, title, body, comments, flair, seller))


# ---------- Message Batches API: same model and instructions, half price, answers within minutes–hours ----------

def submit_batch(texts):
    """texts: {custom_id: user_text}. Returns the batch id."""
    reqs = [{"custom_id": cid, "params": _params(t)} for cid, t in texts.items()]
    return client().messages.batches.create(requests=reqs).id


def batch_status(batch_id):
    return client().messages.batches.retrieve(batch_id).processing_status  # "in_progress" | "canceling" | "ended"


def batch_results(batch_id):
    """{custom_id: list of items} for successes, {custom_id: Exception} for failures."""
    out = {}
    for entry in client().messages.batches.results(batch_id):
        if entry.result.type == "succeeded":
            out[entry.custom_id] = _items(entry.result.message)
        else:
            out[entry.custom_id] = RuntimeError(f"batch item {entry.result.type}")
    return out


def extract_titles(rows):
    """Batch of already-sold Mercari listings: [{'ref','title','price','desc'}] -> items tagged with ref."""
    lines = [
        json.dumps({"ref": r["ref"], "title": r["title"], "price": r["price"], "desc": (r.get("desc") or "")[:300]})
        for r in rows
    ]
    text = (
        "SOURCE: mercari (every listing below is SOLD; price is the sold price in USD).\n"
        "Return one item per listing that is a fragrance, with `ref` set to its ref.\n\n" + "\n".join(lines)
    )
    return _call(text, max_tokens=8000)
