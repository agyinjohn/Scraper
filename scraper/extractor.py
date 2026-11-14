"""Turn HTML into structured data.

Two modes:

* **Default** – generic extraction that works on any page: title, meta
  description, headings, main text, links, images, tables and JSON-LD.
* **Rules** – user-supplied CSS selectors, e.g. ``price=.product .price`` or
  ``image=img.hero@src``. Rules can also be nested via a JSON config to pull
  out lists of records (see README).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Union
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

URL_ATTRS = {"href", "src", "action", "data-src", "poster"}

# A rule is either a selector string ("css", "css@attr") or a dict:
#   {"selector": "...", "attr": "...", "all": bool, "regex": "...", "fields": {name: rule}}
# "regex" keeps only the first match (or its first capture group) of the value.
# "count": true returns the number of matching elements instead of their values.
# attr "own" returns only an element's direct text (excluding child elements).
Rule = Union[str, dict]


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ----------------------------------------------------------------- defaults
def extract_default(soup: BeautifulSoup, base_url: str) -> dict:
    def meta(name: str) -> Optional[str]:
        tag = soup.find("meta", attrs={"name": name}) or soup.find(
            "meta", attrs={"property": name}
        )
        return tag.get("content") if tag and tag.get("content") else None

    title = _clean(soup.title.get_text()) if soup.title else None

    headings = []
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        txt = _clean(h.get_text())
        if txt:
            headings.append({"level": int(h.name[1]), "text": txt})

    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if href.startswith(("http://", "https://")) and href not in seen:
            seen.add(href)
            links.append({"url": href, "text": _clean(a.get_text())})

    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src:
            images.append({"src": urljoin(base_url, src), "alt": img.get("alt", "")})

    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [_clean(c.get_text()) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)

    json_ld = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            json_ld.append(json.loads(s.string or ""))
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "title": title,
        "description": meta("description") or meta("og:description"),
        "language": (soup.html.get("lang") if soup.html else None),
        "canonical": _canonical(soup, base_url),
        "og": {
            k: meta(f"og:{k}")
            for k in ("title", "type", "image", "site_name")
            if meta(f"og:{k}")
        },
        "headings": headings,
        "text": main_text(soup),
        "links": links,
        "images": images,
        "tables": tables,
        "json_ld": json_ld,
    }


def _canonical(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    link = soup.find("link", rel="canonical")
    return urljoin(base_url, link["href"]) if link and link.get("href") else None


def main_text(soup: BeautifulSoup) -> str:
    """Readable body text with boilerplate (nav, scripts, footers) stripped."""
    soup = make_soup(str(soup))  # don't mutate caller's tree
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    for tag in root.find_all(["nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    lines = [_clean(line) for line in root.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


# -------------------------------------------------------------------- rules
def parse_rules(specs: list[str]) -> dict[str, Rule]:
    """Parse CLI ``name=selector[@attr]`` specs. Prefix the name with ``+``
    (e.g. ``+tags=.tag``) to collect all matches instead of the first."""
    rules: dict[str, Rule] = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"Invalid rule {spec!r}; expected name=selector")
        name, selector = spec.split("=", 1)
        name = name.strip()
        many = name.startswith("+")
        name = name.lstrip("+")
        sel, attr = _split_attr(selector.strip())
        rules[name] = {"selector": sel, "attr": attr, "all": many}
    return rules


def _split_attr(selector: str) -> tuple[str, Optional[str]]:
    # "a.link@href" -> ("a.link", "href"); "@href" alone means the current element
    m = re.match(r"^(.*?)@([\w:-]+)$", selector)
    if m:
        return m.group(1).strip(), m.group(2)
    return selector, None


def _normalize(rule: Rule) -> dict:
    if isinstance(rule, str):
        sel, attr = _split_attr(rule)
        return {"selector": sel, "attr": attr, "all": False}
    rule = dict(rule)
    if rule.get("selector") and not rule.get("attr"):
        rule["selector"], rule["attr"] = _split_attr(rule["selector"])
    return rule


def _value(el: Tag, attr: Optional[str], base_url: str) -> Optional[str]:
    if attr in (None, "text"):
        return _clean(el.get_text())
    if attr == "html":
        return el.decode_contents()
    if attr == "own":  # only the element's direct text, not its children's
        return _clean(" ".join(el.find_all(string=True, recursive=False)))
    val = el.get(attr)
    if isinstance(val, list):
        val = " ".join(val)
    if val is not None and attr in URL_ATTRS:
        val = urljoin(base_url, val)
    return val


def apply_rule(scope: Union[BeautifulSoup, Tag], rule: Rule, base_url: str) -> Any:
    r = _normalize(rule)
    selector = r.get("selector") or ""
    fields = r.get("fields")
    many = r.get("all", False)

    if r.get("count"):
        return len(scope.select(selector)) if selector else 1

    if selector:
        elements = scope.select(selector) if many else [scope.select_one(selector)]
        elements = [e for e in elements if e is not None]
    else:
        elements = [scope]

    pattern = re.compile(r["regex"]) if r.get("regex") else None

    def one(el: Tag):
        if fields:
            return {k: apply_rule(el, sub, base_url) for k, sub in fields.items()}
        val = _value(el, r.get("attr"), base_url)
        if pattern and val is not None:
            m = pattern.search(val)
            val = (m.group(1) if m.groups() else m.group(0)) if m else None
        return val

    results = [one(el) for el in elements]
    if many:
        return results
    return results[0] if results else None


def extract(
    html: str,
    base_url: str,
    rules: Optional[dict[str, Rule]] = None,
    include_default: bool = True,
    products: bool = False,
) -> dict:
    soup = make_soup(html)
    data: dict[str, Any] = {}
    if include_default:
        data.update(extract_default(soup, base_url))
    if products:
        data["products"] = extract_products(soup, base_url)
    if rules:
        data["data"] = {name: apply_rule(soup, rule, base_url) for name, rule in rules.items()}
    return data


def find_links(html: str, base_url: str) -> list[str]:
    soup = make_soup(html)
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip()).split("#", 1)[0]
        if href.startswith(("http://", "https://")):
            out.append(href)
    return out


# ----------------------------------------------------------------- products
PRICE_RE = re.compile(
    r"(?:[$€£¥₹₦₵]|US\$|GH₵|R\$)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|GHS|NGN)\b"
)
_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def _is_price(text: str) -> bool:
    m = PRICE_RE.search(text)
    return bool(m) and len(text) <= len(m.group(0)) + 25


_GENERIC_ALT = {"", "img", "image", "product", "photo", "picture", "thumb", "thumbnail"}
_CHROME = ["header", "nav", "footer"]


_BADGE_RE = re.compile(r"^(?:-?\d+\s?%(?:\s?off)?|hot|new|sale|trending|featured|sold out|best ?seller)$", re.I)


def _in_badge(el: Tag, card: Tag) -> bool:
    for anc in el.parents:
        if anc is card:
            return False
        if any(re.search(r"badge|label|tag|ribbon|sale|discount", c) for c in anc.get("class", [])):
            return True
    return False


def _photo(card: Tag) -> Optional[Tag]:
    """First image that looks like a photo rather than an icon."""
    for img in card.find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").lower()
        if src and not src.split("?")[0].endswith(".svg") and not re.search(r"icon|rating|star|logo", src.rsplit("/", 1)[-1]):
            return img
    return None


def _rating(card: Tag) -> Optional[float]:
    stars = [i for i in card.find_all("i") if any("star" in c for c in i.get("class", []))]
    if not stars:
        return None
    score = 0.0
    for i in stars:
        cls = i.get("class", [])
        if any(c in ("far", "fa-regular", "fa-star-o") for c in cls):
            continue
        score += 0.5 if any("half" in c for c in cls) else 1
    return score


def extract_products(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Heuristically find product cards: the smallest element around a price
    that also holds a photo and a name. Works across most shop layouts
    without any selectors. Header/nav/footer widgets (mini-carts) are skipped."""
    soup = make_soup(str(soup))
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    cards: list[Tag] = []
    seen_ids: set[int] = set()
    for node in soup.find_all(string=PRICE_RE):
        if node.find_parent(["del", "s", "strike", "option", "select"] + _CHROME):
            continue
        if not _is_price(_clean(node.parent.get_text())):
            continue
        card = None
        for anc in list(node.parents)[:8]:
            if not isinstance(anc, Tag) or anc.name in ("body", "html", "[document]"):
                break
            if _photo(anc) is not None:
                card = anc
                break
        if card is None:
            continue
        prices = {PRICE_RE.search(s).group(0) for s in card.find_all(string=PRICE_RE)
                  if not s.find_parent(["del", "s", "strike"])}
        if len(prices) > 2:  # grabbed a whole grid, not one card
            continue
        if id(card) not in seen_ids:
            seen_ids.add(id(card))
            cards.append(card)

    products = []
    for card in cards:
        name, name_link = None, None
        best = -1
        for h in card.find_all(_HEADINGS):
            t = _clean(h.get_text())
            if not t or _is_price(t) or _BADGE_RE.match(t) or _in_badge(h, card):
                continue
            score = (2 if h.find("a", href=True) else 0) + (1 if len(t) > 3 else 0)
            if score > best:
                best, name, name_link = score, t, h.find("a", href=True)
        if not name:
            for a in card.find_all("a", href=True):
                t = _clean(a.get_text())
                if len(t) > 2 and not _is_price(t):
                    name, name_link = t, a
                    break
        img = _photo(card)
        if not name:
            for txt in card.find_all(string=True):
                t = _clean(txt)
                if len(t) > 2 and not PRICE_RE.search(t) and txt.parent.name not in ("button", "label"):
                    name = t
                    break
        if not name and img is not None and (img.get("alt") or "").lower() not in _GENERIC_ALT:
            name = img["alt"]
        if not name:
            continue

        price = next(PRICE_RE.search(s).group(0) for s in card.find_all(string=PRICE_RE)
                     if not s.find_parent(["del", "s", "strike"]))
        old = card.find(["del", "s", "strike"], string=PRICE_RE)
        old_price = PRICE_RE.search(old.get_text()).group(0) if old else None
        link = name_link if name_link is not None and not name_link["href"].startswith("#") else next(
            (a for a in card.find_all("a", href=True)
             if a["href"].strip() and not a["href"].startswith(("#", "javascript:"))), None)
        src = img.get("src") or img.get("data-src") if img is not None else None

        products.append({
            "name": name,
            "price": price,
            "old_price": old_price if old_price != price else None,
            "url": urljoin(base_url, link["href"]) if link is not None else None,
            "image": urljoin(base_url, src) if src else None,
            "rating": _rating(card),
        })
    return products
