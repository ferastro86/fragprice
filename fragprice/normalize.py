"""Turn Claude's brand/name into a stable grouping key, snapping near-duplicate spellings onto
names already in the database ("Parfum de Marly" -> "Parfums de Marly", "Layton Exclusif" stays separate)."""
import re
import unicodedata

from rapidfuzz import fuzz, process


def slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\b(the|eau de parfum|eau de toilette|edp|edt)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


class Canonicalizer:
    def __init__(self, con):
        self.brands = {}  # brand slug -> display brand
        self.names = {}   # brand slug -> {name slug -> display name}
        for r in con.execute("SELECT key, brand, name, COUNT(*) n FROM sales WHERE key IS NOT NULL "
                             "GROUP BY key, brand, name ORDER BY n DESC"):
            b, n = r["key"].split("|", 1)
            self.brands.setdefault(b, r["brand"])
            self.names.setdefault(b, {}).setdefault(n, r["name"])

    def _add(self, brand, name):
        b, n = slug(brand), slug(name)
        if not b or not n:
            return
        self.brands.setdefault(b, brand)
        self.names.setdefault(b, {}).setdefault(n, name)

    def canon(self, brand, name):
        b, n = slug(brand), slug(name)
        if not b or not n:
            return brand, name, None
        if b not in self.brands and self.brands:
            m = process.extractOne(b, list(self.brands), scorer=fuzz.ratio)
            if m and m[1] >= 90:
                b = m[0]
        names = self.names.get(b, {})
        if n not in names and names:
            m = process.extractOne(n, list(names), scorer=fuzz.ratio)
            # high bar: "Layton" vs "Layton Exclusif" must NOT merge
            if m and m[1] >= 94 and abs(len(m[0]) - len(n)) <= 2:
                n = m[0]
        disp_brand = self.brands.get(b, brand)
        disp_name = self.names.get(b, {}).get(n, name)
        self._add(disp_brand, disp_name)
        return disp_brand, disp_name, f"{b}|{n}"


def clean_item(it):
    """Sanity-check numbers coming back from the model."""
    try:
        it["price"] = float(it["price"]) if it.get("price") is not None else None
    except (TypeError, ValueError):
        it["price"] = None
    if it.get("price") is not None and not (1 <= it["price"] <= 5000):
        it["price"] = None
    for k in ("size_ml", "fill_pct"):
        try:
            it[k] = float(it[k]) if it.get(k) is not None else None
        except (TypeError, ValueError):
            it[k] = None
    if it.get("size_ml") is not None and not (0.5 <= it["size_ml"] <= 1000):
        it["size_ml"] = None
    if it.get("fill_pct") is not None:
        it["fill_pct"] = max(0.0, min(100.0, it["fill_pct"]))
    if it.get("condition") in ("new_sealed", "new_unsealed", "decant", "sample") and it.get("fill_pct") is None:
        it["fill_pct"] = 100.0
    return it
