"""Daily: find TK Maxx UK products from brands END. stocks -> SQLite -> static site -> email digest."""
import argparse
import datetime as dt
import html
import json
import os
import re
import smtplib
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DB_PATH = ROOT / "tkmaxx.db"
SITE_DIR = ROOT / "docs"  # GitHub Pages serves /docs
END_BRANDS_URL = "https://www.endclothing.com/gb/brands"
TK_BASE = "https://www.tkmaxx.com/uk/en"
# TK Maxx's own Bloomreach search API: no key, no Akamai. Undocumented, so failures email ADMIN_EMAIL.
TK_API = ("https://core.dxpapi.com/api/v1/core/?account_id=7256&domain_key=tkmaxx&request_type=search"
          "&search_type=keyword&request_id=1&_br_uid_2=uid%3D1&url=https%3A%2F%2Fwww.tkmaxx.com%2F&ref_url=")
FIELDS = "pid,title,brand,fmt_price,url,thumb_image,sizes"
PAGE_ROWS = 200  # API max
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
NOT_SAME_BRAND = {"osprey"}  # same name, different company (TK Maxx's is Osprey London bags/stationery)
CRAWL_WINDOW =(dt.time(4, 0), dt.time(7, 30))  # TK Maxx robots.txt window (UK time)

SCHEMA = """
CREATE TABLE IF NOT EXISTS brands (name TEXT PRIMARY KEY, norm TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY, brand TEXT, name TEXT, price TEXT, url TEXT, image_url TEXT,
  sizes TEXT, first_seen TEXT, last_seen TEXT, emailed_at TEXT);
"""


def norm(name):
    s = unicodedata.normalize("NFKD", html.unescape(name)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.casefold().replace("&", " and "))


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def tk_query(params):
    data = json.loads(get(TK_API + "&" + urllib.parse.urlencode(params)))
    if "response" not in data:
        raise RuntimeError(f"TK Maxx API error: {data}")
    return data


def end_brands():
    names = {html.unescape(m).strip() for m in re.findall(r'href="/gb/brands/[^"]+"[^>]*>([^<]+)</a>', get(END_BRANDS_URL))}
    if len(names) < 100:  # page layout changed or blocked
        raise RuntimeError(f"Only {len(names)} END. brands found")
    return names


def tk_brands():
    data = tk_query({"q": "*", "rows": 0, "start": 0, "fl": "pid", "facet.limit": 5000})
    return [b["name"].strip() for b in data["facet_counts"]["facet_fields"]["brand"]]


def match_brands(end_names, tk_names):
    """TK Maxx brand spellings whose normalised name is an END. brand (exact, not substring)."""
    end_norms = {norm(n) for n in end_names} - NOT_SAME_BRAND
    return sorted(n for n in set(tk_names) if norm(n) in end_norms)


def parse_size(raw):
    return raw.split(",")[-1].upper()  # "g,0002,0006,universal,s" -> "S"


def to_row(doc):
    return {
        "id": doc["pid"], "brand": doc["brand"].strip(), "name": doc["title"], "price": doc.get("fmt_price", ""),
        "url": TK_BASE + doc["url"], "image_url": doc.get("thumb_image", ""),
        "sizes": json.dumps([parse_size(s) for s in doc.get("sizes", []) if parse_size(s) != "NOSIZE"]),
    }


def tk_products(brands):
    wanted = set(brands)
    fq = "brand:(" + " OR ".join(f'"{b}"' for b in brands) + ")"
    rows, start = [], 0
    while True:
        data = tk_query({"q": "*", "rows": PAGE_ROWS, "start": start, "fl": FIELDS, "fq": fq})
        docs = data["response"]["docs"]
        rows += [to_row(d) for d in docs if d["brand"].strip() in wanted]  # API phrase-matches: "Calvin Klein" hits "Calvin Klein Jeans"
        start += PAGE_ROWS
        if not docs or start >= data["response"]["numFound"] or start >= 10000:  # API caps start at 10000
            return rows
        time.sleep(1)


def save(db, end_names, rows, today):
    first_run = db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
    db.executemany("INSERT INTO brands VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET updated_at = excluded.updated_at",
                   [(n, norm(n), today) for n in end_names])
    db.executemany("""INSERT INTO products (id, brand, name, price, url, image_url, sizes, first_seen, last_seen)
        VALUES (:id, :brand, :name, :price, :url, :image_url, :sizes, :today, :today)
        ON CONFLICT(id) DO UPDATE SET brand=excluded.brand, name=excluded.name, price=excluded.price, url=excluded.url,
          image_url=excluded.image_url, sizes=excluded.sizes, last_seen=excluded.last_seen""",
                   [{**r, "today": today} for r in rows])
    if first_run:  # seed silently: don't email the whole existing catalogue
        db.execute("UPDATE products SET emailed_at = ?", (today,))
    db.commit()


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def in_stock(db, today):
    return [dict(r) for r in db.execute("SELECT * FROM products WHERE last_seen = ? ORDER BY first_seen DESC, brand", (today,))]


def unemailed(db, today):
    return [dict(r) for r in db.execute(
        "SELECT * FROM products WHERE emailed_at IS NULL AND last_seen = ? ORDER BY first_seen DESC, id", (today,))]


def new_brands(db, today):
    """Brands whose first-ever TK Maxx product appeared today and hasn't been emailed."""
    return [r[0] for r in db.execute("""SELECT brand FROM products GROUP BY brand
        HAVING MIN(first_seen) = ? AND SUM(emailed_at IS NULL) > 0 ORDER BY brand""", (today,))]


def load_favourites(path, end_names):
    if not path.exists():
        return set()
    favs = {norm(l) for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")}
    unknown = favs - {norm(n) for n in end_names}
    if unknown:
        print(f"warning: favourites not on END.: {sorted(unknown)}", file=sys.stderr)
    return favs


def esc(s):
    return html.escape(str(s))


LETTER_SIZES = ["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]


def size_key(size):
    """XS..XXXL in order, then numbers (shoes, waists), then everything else alphabetically."""
    if size in LETTER_SIZES:
        return (0, LETTER_SIZES.index(size), "")
    num = re.match(r"\D*(\d+(?:\.\d+)?)", size)
    return (1, float(num.group(1)), size) if num else (2, 0, size)


def card(p):
    sizes = json.loads(p["sizes"] or "[]")
    chips = "".join(f'<span class="size">{esc(s)}</span>' for s in sizes)
    return (f'<a class="card" href="{esc(p["url"])}" data-brand="{esc(p["brand"])}" data-sizes="{esc("|".join(sizes))}">'
            f'<img src="{esc(p["image_url"])}" alt="{esc(p["name"])}" loading="lazy">'
            f'<div class="brand">{esc(p["brand"])}</div><div class="name">{esc(p["name"])}</div>'
            f'<div class="price">{esc(p["price"])}</div><div class="sizes">{chips}</div></a>')


def section(title, items):
    return f'<section><h2>{title} (<span class="count">{len(items)}</span>)</h2><div class="grid">{"".join(map(card, items))}</div></section>'


def render_site(products, favourites, today):
    new = [p for p in products if p["first_seen"] == today]
    by_brand = {}
    for p in products:
        by_brand.setdefault(p["brand"], []).append(p)
    order = sorted(by_brand, key=lambda b: (norm(b) not in favourites, b.casefold()))
    sections = [section("New today", new)] if new else []
    sections += [section(("★ " if norm(b) in favourites else "") + esc(b), by_brand[b]) for b in order]
    sizes = sorted({s for p in products for s in json.loads(p["sizes"] or "[]")}, key=size_key)
    brand_opts = "".join(f'<option>{esc(b)}</option>' for b in order)
    size_opts = "".join(f'<option>{esc(s)}</option>' for s in sizes)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TK Maxx × END. brands</title>
<style>
:root{{--bg:#fff;--fg:#111;--muted:#666;--line:#e5e5e5;--chip:#f2f2f2}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--muted:#999;--line:#333;--chip:#222}}}}
body{{margin:0;padding:16px;font:15px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
[hidden]{{display:none!important}}
.filters{{position:sticky;top:0;z-index:1;display:flex;flex-wrap:wrap;gap:12px;padding:8px 0;background:var(--bg);border-bottom:1px solid var(--line)}}
.filters label{{font-size:13px;color:var(--muted)}} select{{font:inherit;margin-left:4px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:16px}}
.card{{color:inherit;text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:8px}}
.card img{{width:100%;aspect-ratio:1;object-fit:cover;border-radius:4px;background:var(--chip)}}
.brand{{font-weight:600}} .name,.price{{font-size:13px}} .price{{color:var(--muted)}}
.size{{display:inline-block;font-size:11px;background:var(--chip);border-radius:4px;padding:1px 5px;margin:4px 4px 0 0}}
</style></head><body><h1>TK Maxx × END. brands</h1>
<p>{len(products)} products in stock across {len(by_brand)} brands · updated {esc(today)}</p>
<div class="filters">
<label>Brand<select id="brand"><option value="">All brands</option>{brand_opts}</select></label>
<label>Size<select id="size"><option value="">All sizes</option>{size_opts}</select></label>
<span id="shown"></span></div>
{"".join(sections)}
<p id="none" hidden>Nothing matches those filters.</p>
<script>
const brand = document.getElementById("brand"), size = document.getElementById("size");
function filter() {{
  for (const c of document.querySelectorAll(".card"))
    c.hidden = (brand.value && c.dataset.brand !== brand.value) || (size.value && !c.dataset.sizes.split("|").includes(size.value));
  let any = false;
  for (const s of document.querySelectorAll("section")) {{
    const n = s.querySelectorAll(".card:not([hidden])").length;
    s.hidden = !n; any ||= n > 0;
    s.querySelector(".count").textContent = n;
  }}
  document.getElementById("none").hidden = any;
}}
brand.onchange = size.onchange = filter;
</script></body></html>"""


def render_email(items, brands, favourites, site_url):
    fav_items = [p for p in items if norm(p["brand"]) in favourites]
    pics = (fav_items + [p for p in items if p not in fav_items])[:3]
    imgs = "".join(f'<a href="{esc(p["url"])}"><img src="{esc(p["image_url"])}" width="160" alt="{esc(p["name"])}" '
                   f'style="margin-right:8px"></a>' for p in pics)
    lines = [f"<p><b>New END. brands at TK Maxx:</b> {esc(', '.join(brands))}</p>"] if brands else []
    if fav_items:
        lines.append(f"<p><b>New in from your favourites:</b> {len(fav_items)} items</p>")
    lines.append(f"<p>{len(items)} new products in total.</p>")
    body = f'{"".join(lines)}<p>{imgs}</p><p><a href="{esc(site_url)}">See everything on the site →</a></p>'
    subject = f"TK Maxx: {len(items)} new from END. brands" + (f" incl. {len(brands)} new brands" if brands else "")
    return subject, body


def send_email(to, subject, body_html):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["GMAIL_USER"], to, subject
    msg.set_content(re.sub(r"<[^>]+>", " ", body_html))
    msg.add_alternative(body_html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def load_env(path):
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def publish():
    subprocess.run(["git", "add", "docs"], cwd=ROOT, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:  # something staged
        subprocess.run(["git", "commit", "-m", "Update site"], cwd=ROOT, check=True)
        subprocess.run(["git", "push"], cwd=ROOT, check=True)


def run(dry_run):
    today = dt.datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    end_names = end_brands()
    brands = match_brands(end_names, tk_brands())
    rows = tk_products(brands)
    if not rows:
        raise RuntimeError(f"No TK Maxx products for {len(brands)} matched brands")
    favourites = load_favourites(ROOT / "favourites.txt", end_names)
    db = connect(DB_PATH)
    save(db, end_names, rows, today)
    products = in_stock(db, today)
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(render_site(products, favourites, today))
    items, nb = unemailed(db, today), new_brands(db, today)
    print(f"{len(brands)} brands, {len(products)} in stock, {len(items)} new, new brands: {nb}")
    if dry_run:
        return
    publish()
    if items:
        subject, body = render_email(items, nb, favourites, os.environ["SITE_URL"])
        send_email(os.environ["ALERT_TO"], subject, body)
        # only after a successful send, so a failed send retries next run
        db.executemany("UPDATE products SET emailed_at = ? WHERE id = ?", [(today, p["id"]) for p in items])
        db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="build docs/index.html only: no push, no email, no time check")
    args = ap.parse_args()
    load_env(ROOT / ".env")
    now = dt.datetime.now(ZoneInfo("Europe/London")).time()
    if not args.dry_run and not CRAWL_WINDOW[0] <= now <= CRAWL_WINDOW[1]:
        sys.exit(f"outside crawl window {CRAWL_WINDOW}, skipping")
    try:
        run(args.dry_run)
    except Exception as e:
        if args.dry_run:
            raise
        send_email(os.environ.get("ADMIN_EMAIL", os.environ["GMAIL_USER"]), "TK Maxx scraper broken", f"<pre>{esc(repr(e))}</pre>")
        raise


if __name__ == "__main__":
    main()
