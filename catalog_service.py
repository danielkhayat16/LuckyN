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

FOOD_HINTS = {
    "חלב", "ביצ", "גבינ", "סלט", "ירקות", "פירות", "בשר", "עוף", "דגים",
    "קפוא", "לחם", "מאפ", "קטניות", "אורז", "פסטה", "דגנים", "שימורים",
    "רטבים", "תבלינים", "ממתקים", "חטיפים", "שתיה", "משקאות", "קפה",
    "תה", "יין", "בירה", "אלכוהול", "מזון", "מכולת", "אפיה", "שמן",
    "קמח", "סוכר", "דבש", "ממרחים", "מרקים", "תינוקות", "תחליף חלב",
}
NON_FOOD_HINTS = {
    "ניקיון", "כביסה", "טואלטיקה", "היגיינה", "שמפו", "סבון", "מגבונים",
    "חיתולים", "חד פעמי", "כלי בית", "מטבח", "נייר", "פארם", "קוסמטיקה",
    "בישום", "חשמל", "צעצוע", "טקסטיל", "אירוח", "נרות", "סוללות",
}


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^0-9a-zא-ת% ]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _category_text(product: dict) -> str:
    parts = []
    for key in ("department", "group", "subGroup"):
        val = product.get(key) or {}
        if isinstance(val, dict):
            parts.append(str(val.get("name") or ""))
    return " ".join(parts)


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
    return tags


def similarity(a: str, b: str) -> float:
    a = _norm(a)
    b = _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        short, long = sorted((len(a), len(b)))
        return 72.0 + 28.0 * (short / max(long, 1))
    aset = set(a.split())
    bset = set(b.split())
    token_score = 100.0 * len(aset & bset) / max(len(aset | bset), 1)
    seq_score = 100.0 * SequenceMatcher(None, a, b).ratio()
    return max(token_score, seq_score)


@lru_cache(maxsize=4096)
def rami_search(query: str) -> tuple[dict, ...]:
    payload = {"q": query, "aggs": 1, "store": STORE_ID}
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "locale": "he",
        "origin": "https://www.rami-levy.co.il",
        "referer": "https://www.rami-levy.co.il/he",
        "user-agent": "Mozilla/5.0 Hamizve/1.0",
    }
    r = requests.post(RAMI_CATALOG, json=payload, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return tuple(data.get("data") or [])


def best_match(line: str) -> dict | None:
    raw = line.strip()
    q = _norm(raw)
    if len(q) < 2:
        return None
    q = re.sub(r"\b\d+[.,]\d{2}\b", " ", q)
    q = re.sub(r"\b\d{1,2}\s*[x×]\s*", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    if len(q) < 2:
        return None

    products = []
    for candidate_query in (q, " ".join(q.split()[:3]), " ".join(q.split()[:2])):
        candidate_query = candidate_query.strip()
        if not candidate_query:
            continue
        try:
            products = list(rami_search(candidate_query))
        except Exception:
            products = []
        if products:
            break

    candidates = []
    for p in products:
        if not is_food(p):
            continue
        score = max((similarity(q, tag) for tag in search_tags(p)), default=0.0)
        if score >= 55:
            candidates.append((score, p))
    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    score, p = candidates[0]
    gs = p.get("gs") or {}
    image = (p.get("images") or {}).get("small")
    if image and image.startswith("/"):
        image = "https://img.rami-levy.co.il" + image
    return {
        "score": round(float(score), 1),
        "id": p.get("id"),
        "barcode": str(p.get("barcode") or ""),
        "name": p.get("name") or gs.get("name") or raw,
        "short_name": gs.get("short_name") or "",
        "brand": gs.get("BrandName") or "",
        "department": (p.get("department") or {}).get("name") or "",
        "group": (p.get("group") or {}).get("name") or "",
        "subgroup": (p.get("subGroup") or {}).get("name") or "",
        "image": image or "",
        "price": ((p.get("price") or {}).get("price")),
        "source": "rami-levy-catalog",
    }


@app.get("/health")
def health():
    return jsonify({"ok": True, "store": STORE_ID, "time": int(time.time())})


@app.route("/match", methods=["POST", "OPTIONS"])
def match():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    lines = data.get("lines") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    lines = [str(x).strip() for x in lines if str(x).strip()][:80]
    matches = []
    for line in lines:
        m = best_match(line)
        if m:
            m["receipt_line"] = line
            matches.append(m)
    return jsonify({"matches": matches, "count": len(matches)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
