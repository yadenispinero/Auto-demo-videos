"""Opening, closing and thumbnail of a video, with the product's identity (headless Chrome, like record.py).

The identity comes from two CSV (identity.* in config.json; examples in examples/): the products CSV (one row per
product, filled from the identity manual of the entity that makes it) and the entities CSV (logo, web and text
colour of each entity). They can be tabs of a Google Sheet downloaded as CSV (File > Download > CSV).
The spec only says which product it is ("product": its "Module" or "Product" value). Spanish headers (the ones of
the original sheet) are accepted too: see PRODUCT_COLUMNS and ENTITY_COLUMNS.

  opening  <out>/<video>-00-opening.png  1920x1080  product name + the feature shown (spec "feature") + entity
  closing  <out>/<video>-99-closing.png  1920x1080  product logo + store link + entity name and web
  thumb    <final>/<video>.thumb.jpg     1280x720   logo + product name + video title (YouTube thumbnail)

Used by video.py (cards, build). Colours, sizes and layout of the three screens are in CSS below.
"""

import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

from config import SETTINGS, local_path

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(os.path.dirname(HERE))
PRODUCTS_CSV = local_path(SETTINGS["identity"]["products_csv"])
ENTITIES_CSV = local_path(SETTINGS["identity"]["entities_csv"])
FONT = local_path(SETTINGS["identity"]["font"])
CACHE = os.environ.get("VIDEO_CACHE") or os.path.join(WORKSPACE, ".cache", "presentacion")

# Column names of the CSV: English header -> other accepted headers (Spanish, as in the original sheet).
PRODUCT_COLUMNS = {"Product": ["Producto"], "Product name": ["Nombre del producto"],
                   "Entity": ["Entidad que lo produce"], "Module": ["Módulo técnico"],
                   "Store link": ["Link tienda / compra"], "Logo": ["Logo del producto"],
                   "Accent colour": ["Color identificativo"], "Background colour": ["Color de fondo"]}
ENTITY_COLUMNS = {"Entity": ["Entidad"], "Web": [], "Video logo": ["Logo para vídeos"], "Text colour": ["Color de texto"]}
# Columns the screens need (the CSV can have more).
REQUIRED = ["Product name", "Entity", "Store link", "Logo", "Accent colour", "Background colour"]
# The text colour and logo of the entity are only in the entities CSV (not repeated per product).
ENTITY_REQUIRED = ["Web", "Video logo", "Text colour"]
PENDING = ("(to be confirmed)", "(por confirmar)")
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class CardError(Exception):
    """A problem with the product data or the rendering, with a message for the person or agent."""


def english(row, columns):
    """The row with the English header names (the original ones are kept too)."""
    row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
    for name, others in columns.items():
        if not row.get(name):
            row[name] = next((row[o] for o in others if row.get(o)), row.get(name, ""))
    return row


def load_products(path=PRODUCTS_CSV):
    if not os.path.isfile(path):
        raise CardError(f"{path} not found: put the products CSV there (identity.products_csv in config.json; "
                        "example: examples/products.csv)")
    with open(path, encoding="utf-8") as f:
        return [english(r, PRODUCT_COLUMNS) for r in csv.DictReader(f)]


def find_product(key, products=None):
    """The product row whose 'Module' or 'Product' is key, checked for what the screens need."""
    products = load_products() if products is None else products
    row = next((p for p in products if key and key in (p["Module"], p["Product"])), None)
    if not row:
        raise CardError(f"product '{key}' is not in the products CSV (columns 'Module' or 'Product'); "
                        "add it to the products CSV")
    missing = [c for c in REQUIRED if not row.get(c)]
    if missing:
        raise CardError(f"product '{key}': empty {', '.join(missing)} in the products CSV (fill them "
                        "from the entity's identity manual)")
    bad = [c for c in ("Accent colour", "Background colour") if not HEX.match(row[c])]
    if bad:
        raise CardError(f"product '{key}': {', '.join(bad)} must be a #RRGGBB colour")
    if any(p in row["Entity"] for p in PENDING):
        raise CardError(f"product '{key}': the entity is still '{row['Entity']}'; confirm it in the products CSV")
    row["entity"] = find_entity(row["Entity"])
    return row


def find_entity(name, path=ENTITIES_CSV):
    """The row of the entities CSV for the entity that makes the product (logo for the opening, web for the closing)."""
    if not os.path.isfile(path):
        raise CardError(f"{path} not found: put the entities CSV there (identity.entities_csv in config.json; "
                        "example: examples/entities.csv)")
    with open(path, encoding="utf-8") as f:
        rows = [english(r, ENTITY_COLUMNS) for r in csv.DictReader(f)]
    row = next((r for r in rows if r["Entity"].lower() == name.strip().lower()), None)
    if not row:
        raise CardError(f"entity '{name}' is not in the entities CSV: add it there")
    missing = [c for c in ENTITY_REQUIRED if not row.get(c)]
    if missing:
        raise CardError(f"entity '{name}': empty {', '.join(missing)} in the entities CSV")
    if not HEX.match(row["Text colour"]):
        raise CardError(f"entity '{name}': Text colour must be a #RRGGBB colour (entities CSV)")
    return row


LOGOS = local_path(SETTINGS["identity"]["logos_dir"])


def logo_file(url):
    """Local file of a logo. A path (not a URL) is relative to the folder of the scripts. A Drive file is looked up
    first in <logos_dir>/<Drive id>.png (logos shared with people, not by link: this script has no Google access);
    other URLs are downloaded and cached."""
    if not re.match(r"^[a-z]+://", url):
        local = local_path(url)
        if not os.path.isfile(local):
            raise CardError(f"logo not found: {local}")
        return local
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?.*id=)([-\w]+)", url)
    if m:
        local = os.path.join(LOGOS, m.group(1) + ".png")
        if os.path.isfile(local):
            return local
        url = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "logo-" + hashlib.sha1(url.encode()).hexdigest()[:12] + ".png")
    if not os.path.isfile(path):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data, kind = r.read(), r.headers.get("Content-Type", "")
        except Exception as e:
            raise CardError(f"could not download the logo {url}: {e}")
        if not kind.startswith("image/"):
            raise CardError(f"the logo {url} could not be downloaded without signing in ({kind or 'unknown type'}): "
                            + (f"save a copy of it as {os.path.join(LOGOS, m.group(1))}.png" if m else "use a public link"))
        with open(path, "wb") as f:
            f.write(data)
    return path


def trimmed(path):
    """The logo without the white margin around its tile (the 512 px icons come on a white square)."""
    from PIL import Image, ImageChops

    out = os.path.join(CACHE, "trim-" + hashlib.sha1((path + str(os.path.getmtime(path))).encode()).hexdigest()[:12] + ".png")
    if not os.path.isfile(out):
        im = Image.open(path).convert("RGBA")
        white = Image.new("RGBA", im.size, (255, 255, 255, 255))
        box = ImageChops.difference(im, white).convert("L").point(lambda v: 255 if v > 12 else 0).getbbox()
        os.makedirs(CACHE, exist_ok=True)
        (im.crop(box) if box else im).save(out)
    return out


def store_label(url):
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


CSS = """
@font-face { font-family: 'Open Sans'; src: url('file://__FONT__') format('truetype'); font-weight: 300 800; }
* { margin: 0; box-sizing: border-box; }
html, body { width: 100%; height: 100%; }
body { background: var(--bg); color: var(--text); font-family: 'Open Sans', Arial, sans-serif;
       display: flex; align-items: center; justify-content: center; overflow: hidden; }
.bar { position: absolute; left: 0; right: 0; bottom: 0; height: 14px; background: var(--accent); }
.wrap { display: flex; flex-direction: column; align-items: center; text-align: center; padding: 0 80px; }
/* opening */
.product { font-weight: 700; font-size: 104px; line-height: 1.1; color: var(--text); }
.rule { width: 160px; height: 8px; border-radius: 4px; background: var(--accent); margin: 40px 0 34px; }
.entity { font-weight: 600; font-size: 44px; letter-spacing: .02em; color: var(--text); opacity: .85; }
.maker { display: flex; align-items: center; gap: 22px; }
/* opening with the feature: product name above, the feature big, the entity below */
.opening .product { font-size: 46px; font-weight: 600; opacity: .9; }
.opening .rule { margin: 26px 0 30px; width: 120px; height: 7px; }
.feature { font-weight: 700; font-size: 62px; line-height: 1.15; max-width: 1160px; color: var(--text); text-wrap: balance; }
.opening .maker { margin-top: 48px; }
.opening .maker img { width: 56px; height: 56px; }
.opening .entity { font-size: 34px; }
.maker img { width: 76px; height: 76px; object-fit: contain; }
/* closing: entity name and web under the store link */
.by { margin-top: 54px; display: flex; align-items: center; gap: 16px; font-weight: 600; font-size: 28px;
      color: var(--text); opacity: .8; }
.by img { width: 44px; height: 44px; object-fit: contain; }
.by .dot { opacity: .5; }
/* closing */
.logo { width: 250px; height: 250px; }
/* The icons have transparent corners: the shadow follows their rounded shape. */
.logo img { width: 100%; height: 100%; object-fit: contain; filter: drop-shadow(0 8px 22px rgba(0, 0, 0, .14)); }
.link { margin-top: 60px; font-weight: 600; font-size: 36px; color: var(--text);
        border-bottom: 6px solid var(--accent); padding-bottom: 8px; }
/* thumbnail (1280x720) */
.thumb { flex-direction: row; text-align: left; gap: 60px; padding: 0 80px; }
.thumb .logo { width: 260px; height: 260px; flex: none; }
.thumb .product { font-size: 44px; }
.thumb .rule { margin: 18px 0 22px; width: 110px; height: 7px; }
.thumb .title { font-weight: 700; font-size: 56px; line-height: 1.15; }
.thumb .entity { font-size: 28px; margin-top: 26px; }
"""


def page(body, product, scale):
    css = CSS.replace("__FONT__", FONT)
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style>"
            f"<style>:root {{ --bg: {product['Background colour']}; --accent: {product['Accent colour']};"
            f" --text: {product['entity']['Text colour']}; }}</style></head><body>{body}<div class='bar'></div></body></html>")


def capitalized(text):
    text = " ".join(str(text or "").split())
    return text[:1].upper() + text[1:]


def opening_html(p, entity_logo, feature=""):
    if not feature:
        return (f"<div class='wrap'><div class='product'>{html.escape(p['Product name'])}</div>"
                f"<div class='rule'></div><div class='maker'><img src='file://{entity_logo}'>"
                f"<div class='entity'>{html.escape(p['Entity'])}</div></div></div>")
    return (f"<div class='wrap opening'><div class='product'>{html.escape(p['Product name'])}</div>"
            f"<div class='rule'></div><div class='feature'>{html.escape(capitalized(feature))}</div>"
            f"<div class='maker'><img src='file://{entity_logo}'>"
            f"<div class='entity'>{html.escape(p['Entity'])}</div></div></div>")


def closing_html(p, logo, entity_logo):
    e = p["entity"]
    return (f"<div class='wrap'><div class='logo'><img src='file://{logo}'></div>"
            f"<div class='link'>{html.escape(store_label(p['Store link']))}</div>"
            f"<div class='by'><img src='file://{entity_logo}'><span>{html.escape(e['Entity'])}</span>"
            f"<span class='dot'>·</span><span>{html.escape(store_label(e['Web']))}</span></div></div>")


def thumb_html(p, logo, title):
    return (f"<div class='wrap thumb'><div class='logo'><img src='file://{logo}'></div><div>"
            f"<div class='product'>{html.escape(p['Product name'])}</div><div class='rule'></div>"
            f"<div class='title'>{html.escape(title)}</div>"
            f"<div class='entity'>{html.escape(p['Entity'])}</div></div></div>")


def render(pages, port=9336):
    """pages: [(html, png_path, css_width, css_height, scale)] -> PNG screenshots, one Chrome for all."""
    import record as rec

    if not os.path.isfile(FONT):
        raise CardError(f"font not found: {FONT}")
    profile, work = tempfile.mkdtemp(), tempfile.mkdtemp(dir=CACHE if os.path.isdir(CACHE) else None)
    try:
        chrome = subprocess.Popen(
            [os.environ.get("CHROME", "google-chrome"), "--headless=new", f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}", "--no-first-run", "--hide-scrollbars", "--allow-file-access-from-files",
             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise CardError("Chrome not found: install google-chrome or set CHROME")
    try:
        target = None
        for _ in range(50):
            try:
                target = next(t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
                              if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        if not target:
            raise CardError(f"Chrome did not open its debugging port {port}")
        ws = rec.CDP(target["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        for i, (doc, out, w, h, scale) in enumerate(pages):
            src = os.path.join(work, f"card{i}.html")
            with open(src, "w", encoding="utf-8") as f:
                f.write(doc)
            ws.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=scale, mobile=False)
            ws.call("Page.navigate", url="file://" + src)
            for _ in range(40):
                if ws.js("document.readyState === 'complete' && [...document.images].every(i => i.complete && i.naturalWidth)"):
                    break
                time.sleep(0.1)
            if not ws.js("document.fonts.ready.then(() => document.fonts.check('700 40px \"Open Sans\"'))"):
                raise CardError("Open Sans did not load in Chrome")
            broken = ws.js("[...document.images].filter(i => !i.naturalWidth).length")
            if broken:
                raise CardError(f"{broken} image(s) did not load in {os.path.basename(out)}")
            shot = ws.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            import base64
            with open(out, "wb") as f:
                f.write(base64.b64decode(shot["data"]))
    finally:
        chrome.terminate()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def build(spec_path, spec, out_dir, final_dir, log=print):
    """Renders the three screens of a spec. Returns {"product", "opening", "closing", "thumb_png"}."""
    product = find_product(spec.get("product", ""))
    logo = trimmed(logo_file(product["Logo"]))
    entity_logo = trimmed(logo_file(product["entity"]["Video logo"]))
    video = os.path.splitext(os.path.basename(spec_path))[0]
    lang = spec.get("lang", "es")
    pick = lambda v: (v.get(lang) or next(iter(v.values()), "")) if isinstance(v, dict) else (v or "")
    feature = capitalized(pick(spec.get("feature")))
    title = pick(spec.get("thumbnail_title")) or feature or spec.get("title") or product["Product name"]
    files = {"opening": os.path.join(out_dir, f"{video}-00-opening.png"),
             "closing": os.path.join(out_dir, f"{video}-99-closing.png"),
             "thumb_png": os.path.join(CACHE, f"{video}.thumb.png")}
    os.makedirs(CACHE, exist_ok=True)
    render([(page(opening_html(product, entity_logo, feature), product, 1.5), files["opening"], 1280, 720, 1.5),
            (page(closing_html(product, logo, entity_logo), product, 1.5), files["closing"], 1280, 720, 1.5),
            (page(thumb_html(product, logo, title), product, 1), files["thumb_png"], 1280, 720, 1)])
    log(f"screens of {product['Product name']} ({product['Entity']}): "
        f"{os.path.basename(files['opening'])}, {os.path.basename(files['closing'])}, thumbnail")
    return {"product": product["Product name"], "entity": product["Entity"], **files}
