from __future__ import annotations

import os
import re
import time
from difflib import SequenceMatcher
from functools import lru_cache

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
RAMI_CATALOG = "https://www.rami-levy.co.il/api/catalog"
STORE_ID = os.getenv("RAMI_STORE_ID", "331")
TIMEOUT = 12

FOOD_HINTS = {"חלב","ביצ","גבינ","יוגורט","סלט","ירקות","פירות","בשר","עוף","דגים","קפוא","לחם","מאפ","קטניות","אורז","פסטה","דגנים","שימורים","רטבים","תבלינים","ממתקים","חטיפים","שתיה","משקאות","קפה","תה","יין","בירה","מזון","מכולת","אפיה","שמן","קמח","סוכר","דבש","ממרחים","מרקים","תחליף חלב"}
NON_FOOD_HINTS = {"ניקיון","כביסה","טואלטיקה","היגיינה","שמפו","סבון","מגבונים","חיתולים","חד פעמי","כלי בית","נייר","פארם","קוסמטיקה","בישום","חשמל","צעצוע","טקסטיל","נרות","סוללות"}
STOP = {"מחיר","סהכ","סה כ","הנחה","מבצע","לתשלום","מעמ","עודף","אשראי","מזומן","פריט","כמות","ברקוד","קופה","חשבונית","לקוח"}

# Common receipt abbreviations / OCR normalizations. These are deliberately
# conservative: they expand likely grocery abbreviations, not arbitrary words.
EXPAND = {
    "תנ": "תנובה", "תנו": "תנובה", "שט": "שטראוס", "טרה": "טרה",
    "גב": "גבינה", "גבנ": "גבינה", "יוג": "יוגורט", "חלבו": "חלב",
    "שוקו": "שוקו", "לחמ": "לחם", "קורנ": "קורנפלקס", "טונ": "טונה",
    "סלמ": "סלמון", "עגב": "עגבניות", "מלפ": "מלפפון", "תפו": "תפוחי אדמה",
}

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


def _norm(text: str) -> str:
    text = (text or "").lower().replace("״", '"').replace("׳", "'")
    # frequent OCR confusions around Hebrew receipts / digits
    text = text.replace("%", " % ")
    text = re.sub(r"[^0-9a-zא-ת% ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = [EXPAND.get(w, w) for w in text.split() if w not in STOP]
    return " ".join(words)


def _category_text(product: dict) -> str:
    out = []
    for key in ("department", "group", "subGroup"):
        val = product.get(key) or {}
        if isinstance(val, dict):
            out.append(str(val.get("name") or ""))
    return " ".join(out)


def is_food(product: dict) -> bool:
    n = _norm(_category_text(product))
    if any(_norm(x) in n for x in NON_FOOD_HINTS):
        return False
    if any(_norm(x) in n for x in FOOD_HINTS):
        return True
    gs = product.get("gs") or {}
    return bool(gs.get("Ingredient_Sequence_and_Name") or gs.get("Nutritional_Values"))


def search_tags(product: dict) -> list[str]:
    tags = []
    if product.get("name"):
        tags.append(str(product["name"]))
    gs = product.get("gs") or {}
    for key in ("name", "short_name", "Product_Description_English", "BrandName"):
        if gs.get(key):
            tags.append(str(gs[key]))
    for tag in gs.get("Search_Tags") or []:
        tags.append(str(tag))
    if product.get("barcode"):
        tags.append(str(product["barcode"]))
    return list(dict.fromkeys(tags))


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\b\d+(?:[.,]\d+)?\b", text or ""))


def _important_tokens(text: str) -> list[str]:
    toks = [t for t in _norm(text).split() if len(t) >= 2 and not re.fullmatch(r"\d+(?:[.,]\d+)?", t)]
    return toks


def similarity(line: str, tag: str) -> float:
    a, b = _norm(line), _norm(tag)
    if not a or not b:
        return 0.0
    at, bt = set(_important_tokens(a)), set(_important_tokens(b))
    overlap = len(at & bt)
    coverage = overlap / max(len(at), 1)
    jaccard = overlap / max(len(at | bt), 1)
    seq = SequenceMatcher(None, a, b).ratio()
    partial = 0.0
    for x in at:
        partial = max(partial, max((SequenceMatcher(None, x, y).ratio() for y in bt), default=0.0))
    score = 42 * coverage + 18 * jaccard + 25 * seq + 15 * partial
    # matching percentage / volume / weight is a strong signal
    nums_a, nums_b = _numbers(a), _numbers(b)
    if nums_a and nums_b:
        common = nums_a & nums_b
        score += min(12, 6 * len(common))
    if a in b or b in a:
        score += 8
    return min(100.0, score)


def clean_receipt_line(raw: str) -> str:
    q = _norm(raw)
    # remove likely terminal price at end, e.g. "12.90"
    q = re.sub(r"\s+\d{1,4}[.,]\d{2}\s*$", "", q)
    # remove quantity prefixes like 2x / 2 x
    q = re.sub(r"^\s*\d{1,2}\s*[x×]\s*", "", q)
    return q.strip()


def extract_barcodes(raw: str) -> list[str]:
    vals = re.findall(r"(?<!\d)(\d{8,14})(?!\d)", raw or "")
    return vals


@lru_cache(maxsize=8192)
def rami_search(query: str) -> tuple[dict, ...]:
    payload = {"q": query, "aggs": 1, "store": STORE_ID}
    headers = {"accept":"application/json, text/plain, */*","content-type":"application/json;charset=UTF-8","locale":"he","origin":"https://www.rami-levy.co.il","referer":"https://www.rami-levy.co.il/he","user-agent":"Mozilla/5.0 Hamizve/1.0"}
    r = requests.post(RAMI_CATALOG, json=payload, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return tuple(data.get("data") or [])


def candidate_queries(raw: str) -> list[str]:
    q = clean_receipt_line(raw)
    toks = q.split()
    queries = [q]
    # Search progressively shorter phrases because receipt names are truncated.
    if len(toks) >= 4:
        queries.append(" ".join(toks[:4]))
    if len(toks) >= 3:
        queries.append(" ".join(toks[:3]))
    if len(toks) >= 2:
        queries.append(" ".join(toks[:2]))
    # Also search strongest Hebrew text tokens, excluding standalone numbers.
    strong = [t for t in toks if len(t) >= 3 and not t.isdigit()]
    if strong:
        queries.append(" ".join(strong[:3]))
    return list(dict.fromkeys(x for x in queries if len(x) >= 2))


def product_view(p: dict, score: float, reason: str = "text") -> dict:
    gs = p.get("gs") or {}
    image = (p.get("images") or {}).get("small")
    if image and image.startswith("/"):
        image = "https://img.rami-levy.co.il" + image
    return {
        "score": round(float(score), 1), "reason": reason,
        "id": p.get("id"), "barcode": str(p.get("barcode") or ""),
        "name": p.get("name") or gs.get("name") or "",
        "short_name": gs.get("short_name") or "", "brand": gs.get("BrandName") or "",
        "department": (p.get("department") or {}).get("name") or "",
        "group": (p.get("group") or {}).get("name") or "",
        "subgroup": (p.get("subGroup") or {}).get("name") or "",
        "image": image or "", "price": ((p.get("price") or {}).get("price")),
        "source": "rami-levy-catalog",
    }


def matches_for_line(raw: str, limit: int = 3) -> list[dict]:
    q = clean_receipt_line(raw)
    if len(q) < 2:
        return []

    barcodes = extract_barcodes(raw)
    products_by_id: dict[str, dict] = {}

    # Barcode first: exact code wins when the receipt exposes it.
    for code in barcodes:
        try:
            for p in rami_search(code):
                if str(p.get("barcode") or "") == code and is_food(p):
                    return [product_view(p, 100.0, "barcode")]
        except Exception:
            pass

    for query in candidate_queries(raw):
        try:
            results = rami_search(query)
        except Exception:
            continue
        for p in results:
            if not is_food(p):
                continue
            pid = str(p.get("id") or p.get("barcode") or p.get("name"))
            products_by_id[pid] = p
        # enough candidates; stop hitting the upstream API unnecessarily
        if len(products_by_id) >= 20:
            break

    ranked = []
    for p in products_by_id.values():
        scores = [similarity(q, tag) for tag in search_tags(p)]
        score = max(scores, default=0.0)
        # Brand match and matching numeric descriptors improve confidence.
        brand = _norm((p.get("gs") or {}).get("BrandName") or "")
        if brand and any(SequenceMatcher(None, tok, brand).ratio() > .78 for tok in q.split()):
            score += 6
        if _numbers(q) & set().union(*[_numbers(t) for t in search_tags(p)]):
            score += 5
        if score >= 42:
            ranked.append((min(score, 99.0), p))

    ranked.sort(key=lambda x: x[0], reverse=True)
    return [product_view(p, s) for s, p in ranked[:limit]]


@app.get("/health")
def health():
    return jsonify({"ok": True, "store": STORE_ID, "time": int(time.time()), "matcher": 2})


@app.route("/match", methods=["POST", "OPTIONS"])
def match():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    lines = data.get("lines") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    lines = [str(x).strip() for x in lines if str(x).strip()][:100]

    matches = []
    for line in lines:
        candidates = matches_for_line(line, 3)
        if not candidates:
            continue
        best = dict(candidates[0])
        best["receipt_line"] = line
        best["alternatives"] = candidates[1:]
        best["ambiguous"] = len(candidates) > 1 and (candidates[0]["score"] - candidates[1]["score"] < 10)
        matches.append(best)
    return jsonify({"matches": matches, "count": len(matches)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
