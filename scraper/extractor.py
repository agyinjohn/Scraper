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
def extract_default(soup: BeautifulSoup, base_url: str, full: bool = False) -> dict:
    """Generic page data. ``full=True`` adds all visible text (header, menus,
    footer, popups included) and every form with its fields."""
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

    data = {
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
        **extract_media(soup, base_url),
        **extract_contacts(soup),
        "assets": extract_assets(soup, base_url),
    }
    if full:
        data["full_text"] = all_text(soup)
        data["forms"] = extract_forms(soup, base_url)
    return data


_BG_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")
_VIDEO_HOSTS = re.compile(r"youtube\.com|youtu\.be|vimeo\.com|wistia|dailymotion", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\w])(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,4}\)[ .-]?)?\d[\d .-]{5,}\d(?![\w])")


def extract_media(soup: BeautifulSoup, base_url: str) -> dict:
    """Background images (inline styles / data-background), videos, audio and embeds."""
    bgs: list[str] = []
    for el in soup.find_all(True):
        for attr in ("data-background", "data-bg", "data-bg-src", "data-background-image"):
            if el.get(attr):
                bgs.append(el[attr])
        style = el.get("style")
        if style and "url(" in style:
            bgs.extend(_BG_RE.findall(style))
    for st in soup.find_all("style"):
        bgs.extend(_BG_RE.findall(st.get_text()))
    bgs = [u for u in bgs if not u.startswith("data:")]

    videos: list[str] = []
    for v in soup.find_all(["video", "audio", "source"]):
        if v.get("src"):
            videos.append(v["src"])
    for a in soup.find_all("a", href=True):
        if _VIDEO_HOSTS.search(a["href"]) or re.search(r"\.(mp4|webm|mov|mp3)$", a["href"], re.I):
            videos.append(a["href"])
    embeds = [i.get("src") for i in soup.find_all(["iframe", "embed"]) if i.get("src")]

    def uniq(urls):
        out = []
        for u in urls:
            u = urljoin(base_url, u.strip())
            if u not in out:
                out.append(u)
        return out

    return {"background_images": uniq(bgs), "videos": uniq(videos), "embeds": uniq(embeds)}


def extract_assets(soup: BeautifulSoup, base_url: str) -> dict:
    """Files the page needs to render: stylesheets, scripts, icons/manifest/preloads."""
    css, js, other = [], [], []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel", [])).lower()
        url = urljoin(base_url, link["href"].strip())
        if "stylesheet" in rel or (rel == "preload" and link.get("as") == "style"):
            css.append(url)
        elif any(k in rel for k in ("icon", "manifest", "preload", "mask-icon")):
            other.append(url)
    for sc in soup.find_all("script", src=True):
        js.append(urljoin(base_url, sc["src"].strip()))
    dedupe = lambda xs: list(dict.fromkeys(x for x in xs if x.startswith(("http://", "https://"))))
    return {"stylesheets": dedupe(css), "scripts": dedupe(js), "other": dedupe(other)}


def extract_contacts(soup: BeautifulSoup) -> dict:
    emails, phones = [], []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.lower().startswith("mailto:"):
            emails.append(h[7:].split("?")[0])
        elif h.lower().startswith("tel:"):
            phones.append(h[4:])
    text = all_text(soup)
    emails += EMAIL_RE.findall(text)
    for m in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", m)
        if (m.startswith("+") or "(" in m) and 8 <= len(digits) <= 15 and not re.fullmatch(r"(19|20)\d{2}\D*(19|20)\d{2}", m.strip()):
            phones.append(_clean(m))
    norm = lambda p: re.sub(r"\D", "", p)
    seen_p, phones_u = set(), []
    for p in phones:
        if norm(p) not in seen_p:
            seen_p.add(norm(p))
            phones_u.append(p.strip())
    return {"emails": list(dict.fromkeys(e.strip().lower() for e in emails)), "phones": phones_u}


def extract_forms(soup: BeautifulSoup, base_url: str) -> list[dict]:
    forms = []
    for f in soup.find_all("form"):
        fields = []
        for el in f.find_all(["input", "select", "textarea", "button"]):
            typ = el.get("type", el.name) if el.name == "input" else el.name
            if typ in ("hidden",) and not el.get("name"):
                continue
            label = None
            if el.get("id"):
                lab = soup.find("label", attrs={"for": el["id"]})
                label = _clean(lab.get_text()) if lab else None
            if not label and el.find_parent("label"):
                label = _clean(el.find_parent("label").get_text())
            field = {
                "tag": el.name, "type": typ, "name": el.get("name"),
                "label": label, "placeholder": el.get("placeholder"),
                "value": el.get("value") if el.name != "textarea" else _clean(el.get_text()) or None,
                "required": el.has_attr("required"),
            }
            if el.name == "select":
                field["options"] = [_clean(o.get_text()) for o in el.find_all("option")]
            if el.name == "button":
                field["text"] = _clean(el.get_text())
            fields.append({k: v for k, v in field.items() if v not in (None, False, "")})
        forms.append({
            "id": f.get("id"), "action": urljoin(base_url, f.get("action") or ""),
            "method": (f.get("method") or "GET").upper(), "fields": fields,
        })
    return forms


def all_text(soup: BeautifulSoup) -> str:
    """Every visible line of text on the page, chrome and popups included."""
    soup = make_soup(str(soup))
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe", "head"]):
        tag.decompose()
    lines = [_clean(line) for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


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
    full: bool = False,
) -> dict:
    soup = make_soup(html)
    data: dict[str, Any] = {}
    if include_default:
        data.update(extract_default(soup, base_url, full))
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
