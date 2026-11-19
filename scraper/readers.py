"""One reader per kind of shop website.

Each reader takes (shop, fetch) and returns a list of item dicts:
  key, title, url, image, price (float euros or None), category, available (bool)
`fetch(url)` returns the response text, or raises.
"""
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

PRICE_RE = re.compile(r"(?:€|EUR)\s?(\d[\d.,\s]*)")


def parse_price_text(text):
    """Euro amount -> float. Handles '€ 1.250,00', '€1,250.00', '€ 2.500,-', '€ 950'."""
    if not text:
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    raw = re.sub(r"\s", "", m.group(1)).rstrip(".,")
    dec = re.search(r"[.,](\d{1,2})$", raw)  # 1 or 2 digits after the last separator = cents
    whole = raw[:dec.start()] if dec else raw
    whole = re.sub(r"[.,]", "", whole)
    try:
        value = float(whole + ("." + dec.group(1) if dec else ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _sold_text(text):
    return bool(re.search(r"\b(sold|verkocht|uitverkocht)\b", text or "", re.I))


# ---------------------------------------------------------------- Shopify
def read_shopify(shop, fetch):
    base = shop["reader"]["base"].rstrip("/")
    # Optional: read only some collections (e.g. a shop's vintage section)
    sources = [f"{base}/collections/{c}/products.json" for c in shop["reader"].get("collections", [])] \
        or [f"{base}/products.json"]
    items, seen = [], set()
    done = set()
    for source, page in ((src, pg) for src in sources for pg in range(1, 41)):
        if source in done:
            continue
        data = json.loads(fetch(f"{source}?limit=250&page={page}"))
        if len(data.get("products", [])) < 250:
            done.add(source)
        products = [p for p in data.get("products", []) if p["id"] not in seen]
        seen.update(p["id"] for p in products)
        if not data.get("products"):
            continue
        for p in products:
            variants = p.get("variants") or []
            prices = [float(v["price"]) for v in variants if v.get("price") not in (None, "")]
            images = p.get("images") or []
            tags = p.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            items.append({
                "key": str(p["id"]),
                "title": p.get("title", "").strip(),
                "url": f"{base}/products/{p.get('handle')}",
                "image": images[0]["src"] if images else None,
                "price": min(prices) if prices and min(prices) > 0 else None,
                "category": " ".join([p.get("product_type") or ""] + tags),
                "available": any(v.get("available", True) for v in variants) if variants else True,
                "source_date": p.get("published_at"),
            })
    return items


# ---------------------------------------------------------------- WooCommerce
def read_woo(shop, fetch):
    base = shop["reader"]["base"].rstrip("/")
    items = []
    for page in range(1, 51):
        products = json.loads(fetch(f"{base}/wp-json/wc/store/v1/products?per_page=100&page={page}&orderby=date&order=desc"))
        if not products:
            break
        for p in products:
            prices = p.get("prices") or {}
            price = None
            if prices.get("price") not in (None, "", "0"):
                price = int(prices["price"]) / (10 ** int(prices.get("currency_minor_unit", 2)))
            images = p.get("images") or []
            items.append({
                "key": str(p["id"]),
                "title": BeautifulSoup(p.get("name", ""), "html.parser").get_text().strip(),
                "url": p.get("permalink"),
                "image": (images[0].get("thumbnail") or images[0].get("src")) if images else None,
                "price": price if price else None,
                "category": " ".join(c.get("name", "") for c in p.get("categories") or []),
                "available": bool(p.get("is_in_stock", True)),
            })
        if len(products) < 100:
            break
    return items


# ---------------------------------------------------------------- WordPress posts used as product pages
def read_wp_posts(shop, fetch):
    base = shop["reader"]["base"].rstrip("/")
    items = []
    for page in range(1, 31):
        try:
            posts = json.loads(fetch(f"{base}/wp-json/wp/v2/posts?per_page=100&page={page}&_embed=1"))
        except Exception:
            if page > 1:  # WordPress answers past the last page with an error
                break
            raise
        if not posts or not isinstance(posts, list):
            break
        for p in posts:
            emb = p.get("_embedded") or {}
            media = (emb.get("wp:featuredmedia") or [{}])[0] or {}
            terms = [t.get("name", "") for group in emb.get("wp:term") or [] for t in group or []]
            content_text = BeautifulSoup((p.get("content") or {}).get("rendered", ""), "html.parser").get_text(" ")
            title = BeautifulSoup((p.get("title") or {}).get("rendered", ""), "html.parser").get_text().strip()
            items.append({
                "key": str(p["id"]),
                "title": title,
                "url": p.get("link"),
                "image": media.get("source_url"),
                "price": parse_price_text(content_text),
                "category": " ".join(terms),
                "available": not _sold_text(title + " " + " ".join(terms)),
                "source_date": p.get("date"),
            })
        if len(posts) < 100:
            break
    return items


# ---------------------------------------------------------------- Lightspeed (append ?format=json to any page)
def read_lightspeed(shop, fetch):
    cfg = shop["reader"]
    base = cfg["base"].rstrip("/")
    items, seen = [], set()
    for page in range(1, 41):
        path = f"{cfg['collection']}/" if page == 1 else f"{cfg['collection']}/page{page}.html"
        data = json.loads(fetch(f"{base}/{path}?format=json"))
        shop_id = (data.get("shop") or {}).get("id")
        products = (data.get("collection") or {}).get("products") or {}
        if isinstance(products, dict):
            products = list(products.values())
        new_here = 0
        for p in products:
            pid = str(p.get("id"))
            if pid in seen:
                continue
            seen.add(pid)
            new_here += 1
            price = p.get("price") or {}
            if isinstance(price, dict):
                price = price.get("price_incl") or price.get("price")
            image = p.get("image")
            if image and str(image).isdigit() and shop_id:
                image = f"https://cdn.webshopapp.com/shops/{shop_id}/files/{image}/600x600x2/image.jpg"
            elif image and not str(image).startswith("http"):
                image = None
            items.append({
                "key": pid,
                "title": (p.get("fulltitle") or p.get("title") or "").strip(),
                "url": urljoin(base + "/", p.get("url") or ""),
                "image": image,
                "price": float(price) if price else None,
                "category": cfg["collection"],
                "available": p.get("available", True) is not False,
            })
        if not new_here:
            break
    return items


# ---------------------------------------------------------------- Instagram feed embedded on the shop's site
def read_instagram_embed(shop, fetch):
    items, seen = [], set()
    for page_url in shop["reader"]["pages"]:
        soup = BeautifulSoup(fetch(page_url), "html.parser")
        for a in soup.select('a[href*="instagram.com/p/"]'):
            m = re.search(r"instagram\.com/p/([^/?#]+)", a["href"])
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            box = a.find_parent(class_=re.compile(r"sbi_item|item|post")) or a.parent
            img = a.find("img") or (box.find("img") if box else None)
            caption = (a.get("data-caption") or (img.get("alt") if img else "") or "").strip()
            if not caption or re.search(r"open post by|instagram post", caption, re.I):
                caption = box.get_text(" ", strip=True) if box else ""
            caption = re.sub(r"open (post|reel|carousel) by \S+ with id \d+", "", caption, flags=re.I)
            title = re.sub(r"^[\d\s]+", "", re.split(r"€", caption)[0].replace("`", "")).strip(" -|") or "Instagram post"
            image = a.get("data-full-res") or (img and (img.get("data-src") or img.get("src")))
            items.append({
                "key": m.group(1),
                "title": title[:140],
                "url": f"https://www.instagram.com/p/{m.group(1)}/",
                "image": image if image and image.startswith("http") else None,
                "price": parse_price_text(caption),
                "category": caption,
                "available": not _sold_text(caption),
            })
    return items


# ---------------------------------------------------------------- Plain HTML: collect product links from listing pages
def read_html(shop, fetch):
    cfg = shop["reader"]
    pattern = re.compile(cfg["link_pattern"])
    key_re = re.compile(cfg["key_pattern"]) if cfg.get("key_pattern") else None
    found = {}
    for listing in cfg["listings"]:
        pages = range(1, listing.get("max_pages", 1) + 1) if "{page}" in listing["url"] else [None]
        for page in pages:
            url = listing["url"].format(page=page) if page else listing["url"]
            try:
                html = fetch(url)
            except Exception:
                if page and page > 1:
                    break
                continue
            soup = BeautifulSoup(html, "html.parser")
            before = len(found)
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"]).split("#")[0].replace("&amp;", "&")
                if not pattern.search(href.rstrip("/")):
                    continue
                if key_re and key_re.search(href):
                    key = key_re.search(href).group(1)
                    href = _clean_url(href, cfg)
                else:
                    key = urlparse(href).path.rstrip("/")
                if key in found:
                    text = _clean_title(a.get_text(" ", strip=True))
                    if text and not found[key]["title"] and not re.fullmatch(r"(view|bekijk|more|meer).*", text, re.I):
                        found[key]["title"] = text  # first link to it was an image without text
                    continue
                img = a.find("img") or _card_image(a, pattern)
                text = _clean_title(a.get_text(" ", strip=True))
                found[key] = {
                    "key": key,
                    "title": text or _clean_title(img.get("alt") if img else "") or "",
                    "url": href,
                    "image": _img_src(img, url),
                    "price": None,
                    "category": urlparse(href).path,
                    "available": True,
                    "_needs_detail": True,
                }
            if page and len(found) == before:
                break  # this page added nothing new: past the last page
    if not found:
        raise RuntimeError("no product links matched on the listing pages")
    return list(found.values())


JUNK_TITLE = re.compile(r"^(quick ?view|view( product)?|bekijk|meer info|more info|nieuw|new|sold|verkocht|img[_ -]?\d+.*|dsc[_ -]?\d+.*|\d+)( (sold|verkocht|nieuw|new))?$", re.I)


def is_junk_title(text):
    return not _clean_title(text)


def _clean_title(text):
    text = re.sub(r"[\ue000-\uf8ff]", "", text or "").strip()  # drop icon-font characters
    return "" if JUNK_TITLE.match(text) else text


def _card_image(a, pattern):
    """Photo sitting next to the link rather than inside it: walk up to the
    smallest block that holds an image and links only to this product."""
    href = a.get("href")
    node = a
    for _ in range(5):
        node = node.parent
        if node is None:
            return None
        links = {x.get("href") for x in node.find_all("a", href=True) if pattern.search(x["href"].rstrip("/"))}
        if len(links) > 1:
            return None  # reached a block holding other products
        img = node.find("img")
        if img is not None and links <= {href}:
            return img
    return None


def _clean_url(href, cfg):
    if "treasuredepot" in href:
        pid = re.search(r"product_id=(\d+)", href).group(1)
        return f"https://www.treasuredepot.nl/index.php?route=product/product&product_id={pid}"
    return href


def _img_src(img, page_url):
    if not img:
        return None
    for attr in ("data-src", "data-lazy-src", "data-original", "src", "data-srcset", "srcset"):
        v = img.get(attr)
        if v and not v.startswith("data:"):
            v = v.split(",")[-1].strip().split(" ")[0] if "srcset" in attr else v
            return urljoin(page_url, v)
    return None


def read_detail(url, fetch):
    """Fill title, image, price and sold status from a product page."""
    soup = BeautifulSoup(fetch(url), "html.parser")
    out = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else data.get("@graph", [data]):
            if isinstance(node, dict) and "Product" in str(node.get("@type")):
                out["title"] = node.get("name")
                img = node.get("image")
                out["image"] = img[0] if isinstance(img, list) else (img.get("url") if isinstance(img, dict) else img)
                offers = node.get("offers") or {}
                offers = offers[0] if isinstance(offers, list) and offers else offers
                if isinstance(offers, dict):
                    if offers.get("price") not in (None, "", "0", 0):
                        try:
                            out["price"] = float(str(offers["price"]).replace(",", "."))
                        except ValueError:
                            pass
                    if "OutOfStock" in str(offers.get("availability", "")) or "SoldOut" in str(offers.get("availability", "")):
                        out["available"] = False

    def meta(prop):
        t = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return t.get("content") if t else None

    h1 = soup.find("h1")
    out["title"] = (out.get("title") or meta("og:title") or (h1.get_text(" ", strip=True) if h1 else None)
                    or (soup.title.get_text(strip=True) if soup.title else None))
    out["image"] = out.get("image") or meta("og:image") or meta("twitter:image")
    if not out.get("image"):
        main = soup.find("main") or soup.body or soup
        for img in main.find_all("img"):
            src = _img_src(img, url)
            if src and not re.search(r"logo|icon|sprite|placeholder|\.svg", src, re.I):
                out["image"] = src
                break
    if not out.get("price"):
        amount = meta("product:price:amount")
        if amount:
            try:
                out["price"] = float(amount.replace(",", "."))
            except ValueError:
                pass
    if not out.get("price"):
        main = soup.find("main") or soup.body or soup
        out["price"] = parse_price_text(main.get_text(" "))
    if "available" not in out:
        out["available"] = not _sold_text((h1.get_text() if h1 else "") + " " + (out.get("title") or ""))
    return {k: v for k, v in out.items() if v not in (None, "")}


READERS = {
    "shopify": read_shopify,
    "woo": read_woo,
    "wp_posts": read_wp_posts,
    "lightspeed": read_lightspeed,
    "instagram_embed": read_instagram_embed,
    "html": read_html,
}


# ---------------------------------------------------------------- Auto: work out which kind of site this is
# Product-link shapes, most specific first. Each is matched against the path of links on the shop's own host.
LINK_FAMILIES = [
    r"/products?/[^/?#]+/?$",
    r"/product-page/[^/?#]+/?$",
    r"/(item|items|artikel|artikelen|objects?)/[^/?#]+/?$",
    r"-detail/?$",
    r"/\d{3,}-[^/?#]+\.html$",
    r"/(shop|collectie|collection|aanbod|webshop|winkel|catalog|meubels|stock|vintage)/[^/?#]+/[^/?#]{8,}/?$",
    r"/(shop|collectie|collection|aanbod|webshop|winkel|meubels|stock|vintage)/[^/?#]{12,}/?$",
]
LISTING_PATHS = ["/", "/shop", "/shop/", "/collection", "/collectie", "/aanbod", "/webshop", "/products",
                 "/winkel", "/catalog", "/meubels", "/new", "/nieuw", "/vintage", "/collection/all", "/stock"]


def _json(fetch, url):
    try:
        return json.loads(fetch(url))
    except Exception:
        return None


def detect(shop, fetch):
    """Return a concrete reader config for the shop's website, or raise."""
    base = (shop["reader"].get("base") or shop["website"]).rstrip("/")
    d = _json(fetch, f"{base}/products.json?limit=1")
    if isinstance(d, dict) and "products" in d:
        return {"type": "shopify", "base": base}
    d = _json(fetch, f"{base}/wp-json/wc/store/v1/products?per_page=1")
    if isinstance(d, list) and d and "prices" in d[0]:
        return {"type": "woo", "base": base}
    host = re.escape(urlparse(base).netloc.replace("www.", ""))
    hits = {}  # family -> {listing urls, links}
    for path in LISTING_PATHS:
        url = base + path
        try:
            soup = BeautifulSoup(fetch(url), "html.parser")
        except Exception:
            continue
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"]).split("#")[0]
            if not re.search(rf"//(www\.)?{host}/", href):
                continue
            path_only = urlparse(href).path
            for fam in LINK_FAMILIES:
                if re.search(fam, path_only):
                    h = hits.setdefault(fam, {"listings": set(), "links": set()})
                    h["listings"].add(url)
                    h["links"].add(path_only.rstrip("/"))
                    break
    for fam in LINK_FAMILIES:  # first family (most specific) with enough products wins
        if fam in hits and len(hits[fam]["links"]) >= 4:
            return {"type": "html", "listings": [{"url": u} for u in sorted(hits[fam]["listings"])],
                    "link_pattern": rf"{host}" + r"[^?#]*" + fam}
    raise RuntimeError("couldn't recognise this website's shop pages")


def read_auto(shop, fetch):
    cfg = detect(shop, fetch)
    shop["_detected"] = cfg
    return READERS[cfg["type"]](dict(shop, reader=cfg), fetch)


READERS["auto"] = read_auto
