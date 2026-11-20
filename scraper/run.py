"""Weekly run: read every shop that has a reader, compare with last week, write the digest data.

Usage:
  python scraper/run.py                 # live run
  python scraper/run.py --date 2026-11-23 --fixtures tests/fixtures   # offline test
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from readers import READERS, read_detail, is_junk_title  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHOPS_FILE = ROOT / "data" / "shops.json"
STATE_FILE = ROOT / "data" / "state.json"
OUT_FILE = ROOT / "docs" / "data.json"

# Readers that see a shop's whole stock, so a missing item really has gone.
FULL_STOCK_READERS = {"shopify", "woo", "wp_posts", "lightspeed"}
DETAIL_CAP_FIRST_RUN = 250   # product pages fetched per shop on its first run
DETAIL_CAP = 150             # ... and on later runs (new pieces, plus known ones still missing a photo)
SOLD_KEEP_DAYS = 60          # sold items stay on the page this long
USER_AGENT = "Mozilla/5.0 (personal weekly vintage digest; one visit per week)"

# Never show these: clothes, accessories, paper ephemera.
EXCLUDE = re.compile(
    r"sun ?glasses|zonnebril|eyewear|t-?shirt|longsleeve|sweater|hoodie|cardigan|jumper|knitwear|"
    r"\bdress\b|\bjurk\b|trousers|\bjeans\b|\bskirt\b|blouse|\bjacket\b|\bscarf\b|\bsjaal\b|"
    r"handbag|\bpurse\b|sneakers|\bshoes\b|schoenen|jewel(le)?ry(?!\s*(box|cabinet|chest|case|stand|kist|kast|doos))|sieraden(?!kist|kast|doos)|earrings|necklace|bracelet|"
    r"postcards?|ansichtkaart|apparel|clothing|kleding", re.I)

TYPES = [  # checked in this order; first match wins
    ("Lighting", r"lamp|light|lighting|verlichting|pendant|hanglamp|sconce|chandelier|kroonluchter|luminaire|lampen"),
    ("Seating", r"chair|stoel|fauteuil|armchair|sofa|\bbank\b|banken|bench|stool|kruk|lounge|seating|zetel|chaise|daybed|poef|pouf|zitmeubel"),
    ("Tables & desks", r"table|tafel|desk|bureau|trolley|console|bijzettafel|salontafel|eettafel"),
    ("Storage", r"cabinet|kast|sideboard|dressoir|dresser|ladekast|drawer|shel(f|ves|ving)|\brek\b|bookcase|wardrobe|credenza|secretaire|storage|commode|wandmeubel|bar\b"),
    ("Decor & art", r"vase|vaas|ceramic|keramiek|mirror|spiegel|\bart\b|painting|schilderij|print|poster|sculpture|beeld|rug|tapijt|kleed|object|bowl|schaal|glass|glas|silver|zilver|decorati|bookend|clock|klok|textile|candle|kandelaar|tray|dienblad"),
]


OUT_OF_STOCK = re.compile(r"\s*\b(out of stock|sold out|uitverkocht|niet op voorraad)\b\s*", re.I)


def bigger_image(url):
    """Ask the shop's image server for a larger photo where its address allows it."""
    if not url:
        return url
    if "wixstatic.com" in url:  # Wix: small, deliberately blurred thumbnails
        return re.sub(r"/v1/fill/[^/]+/", "/v1/fill/w_600,h_750,al_c,q_85,enc_auto/", url)
    if "/wp-content/uploads/" in url:  # WordPress: drop the -300x300 thumbnail suffix
        return re.sub(r"-\d{2,4}x\d{2,4}(?=\.(jpe?g|png|webp)\b)", "", url, flags=re.I)
    if "cdn.shopify.com" in url and "width=" not in url:
        return url + ("&" if "?" in url else "?") + "width=700"
    if "squarespace-cdn.com" in url and "format=" not in url:
        return url + ("&" if "?" in url else "?") + "format=750w"
    return url


def classify(*texts):
    text = " ".join(t or "" for t in texts).lower()
    for name, pattern in TYPES:
        if re.search(pattern, text):
            return name
    return "Other"


def make_fetch(fixtures=None, delay=1.0):
    if fixtures:
        fixtures = pathlib.Path(fixtures)
        index = json.loads((fixtures / "index.json").read_text())

        def fetch(url):
            if url not in index:
                raise RuntimeError(f"no fixture for {url}")
            return (fixtures / index[url]).read_text(encoding="utf8")
        return fetch

    import requests
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    def fetch(url):
        time.sleep(delay)
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return r.text
    return fetch


def load(path, default):
    return json.loads(path.read_text(encoding="utf8")) if path.exists() else default


def run(today, fetch):
    shops = load(SHOPS_FILE, {})["shops"]
    state = load(STATE_FILE, {"runs": [], "shops": {}, "items": {}})
    items = state["items"]

    for shop in shops:
        reader = shop.get("reader")
        if not reader:
            continue
        sid = shop["id"]
        sstate = state["shops"].setdefault(sid, {"baseline_done": False})
        first_run = not sstate["baseline_done"]
        try:
            if reader["type"] == "auto" and sstate.get("detected"):
                try:
                    found = READERS[sstate["detected"]["type"]](dict(shop, reader=sstate["detected"]), fetch)
                except Exception:
                    found = READERS["auto"](shop, fetch)
            else:
                found = READERS[reader["type"]](shop, fetch)
            if shop.get("_detected"):
                sstate["detected"] = shop.pop("_detected")
            reader = sstate.get("detected", reader)
        except Exception as e:  # one broken shop must not stop the others
            sstate.update(status="error", error=f"{type(e).__name__}: {e}"[:300], last_run=today)
            print(f"[{sid}] ERROR {e}")
            traceback.print_exc()
            continue

        # Fetch product pages only for items we have not seen before.
        cap = reader.get("detail_cap") or (DETAIL_CAP_FIRST_RUN if first_run else DETAIL_CAP)
        fetched = 0
        for it in found:
            iid = f"{sid}:{it['key']}"
            known = items.get(iid)
            bad_title = known is not None and is_junk_title(known.get("title"))
            needs = it.pop("_needs_detail", False) or not it.get("image") or bad_title
            if not needs:
                continue
            if known:  # known item: reuse what we stored
                for k in ("title", "image", "price"):
                    if k == "title" and bad_title:
                        continue
                    it[k] = it.get(k) or known.get(k)
                if it.get("image") and not bad_title:
                    continue  # otherwise fetch its page again for a photo or a proper title
            if fetched >= cap:
                continue
            try:
                it.update(read_detail(it["url"], fetch))
            except Exception as e:
                print(f"[{sid}] detail failed {it['url']}: {e}")
            fetched += 1

        for it in found:
            if it.get("title"):
                it["title"] = re.sub(r"\s*\(stock:\s*\d+\)", "", it["title"]).strip()
                if OUT_OF_STOCK.search(it["title"]):  # e.g. Deja Vintage: "... Out of stock"
                    it["available"] = False
                    it["title"] = OUT_OF_STOCK.sub("", it["title"]).strip(" -|·")
        found = [it for it in found if not EXCLUDE.search(f"{it.get('title', '')} {it.get('category', '')}")]
        live_ids = set()
        for it in found:
            iid = f"{sid}:{it['key']}"
            prev = items.get(iid)
            if prev is None:
                if not it.get("available", True):
                    continue  # already sold when we first saw it: not news
                items[iid] = {
                    "shop": sid, "title": it.get("title") or "Untitled", "url": it["url"],
                    "image": it.get("image"), "price": it.get("price"),
                    "type": classify(it.get("category"), it.get("title"), it.get("url")),
                    "first_seen": today, "last_seen": today, "status": "live",
                    "baseline": first_run, "price_history": [[today, it.get("price")]],
                }
                live_ids.add(iid)
                continue
            if not it.get("available", True):
                if prev["status"] == "live":
                    prev.update(status="sold", sold_on=today)
                continue
            live_ids.add(iid)
            if prev["status"] != "live":  # came back
                prev["status"] = "live"
                prev.pop("sold_on", None)
            if it.get("price") and prev.get("price") and it["price"] != prev["price"]:
                prev["price_history"].append([today, it["price"]])
            keep_title = reader["type"] in ("html", "instagram_embed") and not is_junk_title(prev.get("title")) and prev.get("title") != "Untitled"
            prev.update(last_seen=today, url=it["url"],
                        title=prev["title"] if keep_title else (it.get("title") or prev["title"]),
                        image=it.get("image") or prev.get("image"),
                        price=it.get("price") or prev.get("price"))

        # Items that have disappeared.
        last_count = sstate.get("last_count") or 0
        looks_broken = last_count and len(live_ids) < 0.6 * last_count
        warning = None
        if looks_broken:
            warning = f"Found {len(live_ids)} items, last time {last_count}. Disappearances ignored this week."
        for iid, prev in items.items():
            if prev["shop"] != sid or prev["status"] != "live" or iid in live_ids or looks_broken:
                continue
            if reader["type"] in FULL_STOCK_READERS:
                prev.update(status="sold", sold_on=today)
            else:
                prev.update(status="gone")  # only partial listing: can't tell sold from moved

        sstate.update(baseline_done=True, status="ok", error=None, warning=warning,
                      last_run=today, last_count=len(live_ids))
        print(f"[{sid}] {len(live_ids)} live, detail pages fetched: {fetched}")

    if today not in state["runs"]:
        state["runs"].append(today)

    # Drop long-sold items from the state so it doesn't grow for ever.
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=SOLD_KEEP_DAYS)).isoformat()
    state["items"] = {k: v for k, v in items.items()
                      if not (v["status"] in ("sold", "gone") and (v.get("sold_on") or v["last_seen"]) < cutoff)}

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=0), encoding="utf8")
    write_page_data(shops, state, today)


def write_page_data(shops, state, today):
    shop_rows = []
    for s in shops:
        st = state["shops"].get(s["id"], {})
        shop_rows.append({
            "id": s["id"], "name": s["name"], "town": s["town"], "zone": s["zone"],
            "km": s["km_from_dam"], "website": s["website"], "covered": bool(s.get("reader")),
            "status": st.get("status"), "error": st.get("error"), "warning": st.get("warning"),
            "count": st.get("last_count"), "note": s.get("note"), "design_icons": bool(s.get("design_icons")),
        })
    out_items = []
    for iid, it in state["items"].items():
        if it["status"] == "gone":
            continue
        hist = [p for _, p in it.get("price_history", []) if p]
        out_items.append({
            "id": iid, "shop": it["shop"], "title": it["title"], "url": it["url"], "image": bigger_image(it.get("image")), "thumb": it.get("image") if bigger_image(it.get("image")) != it.get("image") else None,
            "price": it.get("price"), "type": it["type"], "first_seen": it["first_seen"],
            "baseline": it.get("baseline", False), "status": it["status"], "sold_on": it.get("sold_on"),
            "was": max(hist) if len(hist) > 1 and it.get("price") and max(hist) > it["price"] else None,
            "price_changed": it["price_history"][-1][0] if len(it.get("price_history", [])) > 1 else None,
        })
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps({"generated": today, "runs": state["runs"], "shops": shop_rows,
                                    "items": out_items}, ensure_ascii=False), encoding="utf8")
    print(f"wrote {len(out_items)} items to {OUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--fixtures")
    args = ap.parse_args()
    run(args.date, make_fetch(args.fixtures, delay=0 if args.fixtures else 1.0))
