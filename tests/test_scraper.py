import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

import pytest

from scraper.cli import main
from scraper.crawler import CrawlConfig, Crawler
from scraper.extractor import apply_rule, extract, make_soup, parse_rules
from scraper.fetcher import Fetcher

PAGES = {
    "index.html": """<html lang="en"><head><title>Home  Page</title>
<meta name="description" content="A test site">
<script type="application/ld+json">{"@type": "WebSite", "name": "Test"}</script></head>
<body><nav><a href="/about.html">About</a></nav>
<main><h1>Welcome</h1><p>Hello <b>world</b>.</p>
<ul class="products">
  <li class="product"><a href="/p1.html">Widget</a><span class="price">$5</span><span class="tag">a</span><span class="tag">b</span></li>
  <li class="product"><a href="/p2.html">Gadget</a><span class="price">$9</span></li>
</ul>
<table><tr><th>k</th><th>v</th></tr><tr><td>x</td><td>1</td></tr></table>
<img src="/logo.png" alt="logo">
<a href="https://other.example.com/">external</a>
<a href="/private/secret.html">secret</a>
</main></body></html>""",
    "about.html": "<html><head><title>About</title></head><body><a href='/index.html'>home</a></body></html>",
    "p1.html": "<html><head><title>P1</title></head><body><a href='/p2.html'>next</a></body></html>",
    "p2.html": "<html><head><title>P2</title></head><body></body></html>",
    "private/secret.html": "<html><head><title>Secret</title></head></html>",
    "robots.txt": "User-agent: *\nDisallow: /private/\n",
}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    for name, body in PAGES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_default_extraction():
    data = extract(PAGES["index.html"], "https://x.test/")
    assert data["title"] == "Home Page"
    assert data["description"] == "A test site"
    assert data["language"] == "en"
    assert {"level": 1, "text": "Welcome"} in data["headings"]
    assert "Hello world ." in data["text"] or "Hello\nworld" in data["text"]
    assert "About" not in data["text"]  # nav stripped
    assert data["images"][0]["src"] == "https://x.test/logo.png"
    assert data["tables"] == [[["k", "v"], ["x", "1"]]]
    assert data["json_ld"][0]["name"] == "Test"
    assert any(l["url"] == "https://x.test/about.html" for l in data["links"])


def test_cli_rules():
    rules = parse_rules(["title=h1", "+prices=.price", "first=.product a@href"])
    out = extract(PAGES["index.html"], "https://x.test/", rules, include_default=False)
    assert out == {"data": {"title": "Welcome", "prices": ["$5", "$9"],
                            "first": "https://x.test/p1.html"}}


def test_nested_rules():
    rule = {"selector": ".product", "all": True, "fields": {
        "name": "a", "url": "a@href", "price": ".price",
        "tags": {"selector": ".tag", "all": True}}}
    res = apply_rule(make_soup(PAGES["index.html"]), rule, "https://x.test/")
    assert res == [
        {"name": "Widget", "url": "https://x.test/p1.html", "price": "$5", "tags": ["a", "b"]},
        {"name": "Gadget", "url": "https://x.test/p2.html", "price": "$9", "tags": []},
    ]


def test_missing_selector_is_none():
    assert apply_rule(make_soup("<p>x</p>"), ".nope", "https://x.test/") is None


def test_crawl_respects_scope_and_robots(site):
    cfg = CrawlConfig(max_pages=20, max_depth=2, delay=0)
    with Fetcher(retries=0) as f:
        records = list(Crawler(f, cfg).crawl([site + "/index.html"]))
    titles = {r.get("title") for r in records}
    assert {"Home Page", "About", "P1", "P2"} <= titles
    assert "Secret" not in titles
    assert any(r.get("error") == "disallowed by robots.txt" for r in records)
    assert not any("other.example.com" in r["url"] for r in records)
    urls = [r["url"] for r in records]
    assert len(urls) == len(set(urls))


def test_max_pages(site):
    cfg = CrawlConfig(max_pages=2, max_depth=5, delay=0)
    with Fetcher(retries=0) as f:
        records = [r for r in Crawler(f, cfg).crawl([site + "/index.html"]) if "error" not in r]
    assert len(records) == 2


def test_404(site):
    with Fetcher(retries=0) as f:
        (rec,) = Crawler(f, CrawlConfig(delay=0)).crawl([site + "/missing.html"])
    assert rec["status"] == 404 and rec["error"] == "HTTP 404"


def test_cli_end_to_end(site, tmp_path, capsys):
    out = tmp_path / "out.json"
    rc = main([site + "/index.html", "-s", "+names=.product a", "--no-default",
               "--delay", "0", "-o", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data[0]["data"]["names"] == ["Widget", "Gadget"]


def test_cli_csv(site, tmp_path):
    out = tmp_path / "out.csv"
    assert main([site + "/index.html", "-s", "h=h1", "-f", "csv", "-o", str(out), "--delay", "0"]) == 0
    text = out.read_text()
    assert "data.h" in text.splitlines()[0] and "Welcome" in text


def test_regex_rule():
    soup = make_soup("<li>$19.00 <del>$27.00</del></li><b>SKU: 42</b>")
    assert apply_rule(soup, {"selector": "li", "regex": r"\$[\d.]+"}, "") == "$19.00"
    assert apply_rule(soup, {"selector": "b", "regex": r"SKU: (\d+)"}, "") == "42"
    assert apply_rule(soup, {"selector": "b", "regex": r"zzz"}, "") is None


def test_count_and_own_text():
    soup = make_soup('<div class="star"><i></i><i></i><i></i></div><h5>Ann <span>June 1</span></h5>')
    assert apply_rule(soup, {"selector": ".star i", "count": True}, "") == 3
    assert apply_rule(soup, "h5@own", "") == "Ann"
    assert apply_rule(soup, "h5 span", "") == "June 1"


SHOP = """<html><body>
<header><div class="menu-cart"><div class="cart-product"><img src="/c.jpg"><a href="/x">Cart thing</a><span>$1.00</span></div></div></header>
<div class="feature"><img src="/icons/truck.svg"><h5>Free shipping</h5><p>Orders over $140.00</p></div>
<div class="grid">
  <div class="card"><div class="badge"><h6>Hot</h6></div><img src="/a.jpg">
    <span>Speaker</span><h5><a href="/p/a">Mini Speaker</a></h5>
    <div class="star"><i class="fas fa-star"></i><i class="fas fa-star"></i><i class="far fa-star"></i></div>
    <h6><del>$120.00</del> $97.00</h6></div>
  <div class="card"><img src="/b.jpg"><a href="/p/b">Desk Lamp</a><span class="price">€45</span></div>
</div></body></html>"""


def test_auto_product_detection():
    from scraper.extractor import extract_products
    ps = extract_products(make_soup(SHOP), "https://shop.test/")
    assert ps == [
        {"name": "Mini Speaker", "price": "$97.00", "old_price": "$120.00",
         "url": "https://shop.test/p/a", "image": "https://shop.test/a.jpg", "rating": 2.0},
        {"name": "Desk Lamp", "price": "€45", "old_price": None,
         "url": "https://shop.test/p/b", "image": "https://shop.test/b.jpg", "rating": None},
    ]


def test_media_contacts_forms():
    html = """<html><body><header><a href="tel:+1 555 010 9999">call</a></header>
    <section data-background="img/bg.jpg" style="background-image:url('img/hero.png')"></section>
    <a href="https://www.youtube.com/watch?v=abc">play</a><iframe src="https://maps.example/embed"></iframe>
    <footer>Mail us: hello@shop.test</footer>
    <form action="/subscribe" method="post"><label for="e">Email</label>
      <input id="e" name="email" type="email" required><select name="c"><option>A</option><option>B</option></select>
      <button>Go</button></form></body></html>"""
    d = extract(html, "https://shop.test/", full=True)
    assert d["background_images"] == ["https://shop.test/img/bg.jpg", "https://shop.test/img/hero.png"]
    assert d["videos"] == ["https://www.youtube.com/watch?v=abc"]
    assert d["embeds"] == ["https://maps.example/embed"]
    assert d["emails"] == ["hello@shop.test"] and d["phones"] == ["+1 555 010 9999"]
    assert "Mail us: hello@shop.test" in d["full_text"] and "Mail us" not in d["text"]
    (form,) = d["forms"]
    assert form["action"] == "https://shop.test/subscribe" and form["method"] == "POST"
    assert form["fields"][0] == {"tag": "input", "type": "email", "name": "email", "label": "Email", "required": True}
    assert form["fields"][1]["options"] == ["A", "B"]


def test_download(site, tmp_path):
    rc = main([site + "/index.html", "--download", str(tmp_path), "--delay", "0", "-o", str(tmp_path / "o.json")])
    assert rc == 0
    files = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert "index.html" in files


def test_phone_noise_rejected():
    html = "<p>+670 413 90 762</p><p>7398 Main St</p><p>(22) (15) (03)</p><p>Order 2024-2025</p><p>Call (020) 555-0199</p>"
    assert extract(html, "https://x.test/")["phones"] == ["+670 413 90 762", "(020) 555-0199"]
