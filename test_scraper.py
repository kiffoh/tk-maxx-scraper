"""Run: python test_scraper.py"""
import json

import scraper as s


def row(pid, brand, sizes=("S", "M")):
    return {"id": pid, "brand": brand, "name": f"{brand} item", "price": "£10", "url": f"https://x/{pid}",
            "image_url": "https://img/x.jpg", "sizes": json.dumps(list(sizes))}


def test_matching():
    assert s.norm("A.P.C.") == s.norm("APC") == "apc"
    assert s.norm("Lyle & Scott") == s.norm("Lyle and Scott")
    assert s.norm("Maison Kitsuné") == s.norm("Maison Kitsune")
    assert s.match_brands({"Gap", "Nike"}, ["Gapstow", "Nike", "NIKE "]) == ["NIKE ", "Nike"]
    assert s.parse_size("g,0002,0006,universal,s") == "S"


def test_db_flow():
    db = s.connect(":memory:")
    s.save(db, {"Nike", "Barbour"}, [row("1", "Nike")], "2026-10-18")
    assert s.unemailed(db, "2026-10-18") == []  # first run seeds silently
    s.save(db, {"Nike", "Barbour"}, [row("1", "Nike"), row("2", "Nike"), row("3", "Barbour")], "2026-10-19")
    assert [p["id"] for p in s.unemailed(db, "2026-10-19")] == ["2", "3"]
    assert s.new_brands(db, "2026-10-19") == ["Barbour"]  # Nike already known
    s.save(db, {"Nike"}, [row("3", "Barbour", ["L"])], "2026-10-20")
    stock = s.in_stock(db, "2026-10-20")
    assert [p["id"] for p in stock] == ["3"] and json.loads(stock[0]["sizes"]) == ["L"]  # 1 and 2 sold out


def test_render():
    p = {**row("9", "Nike", ["XL"]), "first_seen": "2026-10-19", "name": "<b>Tee</b>"}
    page = s.render_site([p], {"nike"}, "2026-10-19")
    assert "New today (1)" in page and "★ Nike" in page and "XL" in page and "&lt;b&gt;Tee" in page
    subject, body = s.render_email([p], ["Nike"], {"nike"}, "https://site")
    assert "1 new" in subject and "favourites:</b> 1" in body and 'href="https://site"' in body


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
