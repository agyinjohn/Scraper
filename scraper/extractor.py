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
#   {"selector": "...", "attr": "...", "all": bool, "fields": {name: rule}}
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

    if selector:
        elements = scope.select(selector) if many else [scope.select_one(selector)]
        elements = [e for e in elements if e is not None]
    else:
        elements = [scope]

    def one(el: Tag):
        if fields:
            return {k: apply_rule(el, sub, base_url) for k, sub in fields.items()}
        return _value(el, r.get("attr"), base_url)

    results = [one(el) for el in elements]
    if many:
        return results
    return results[0] if results else None


def extract(
    html: str,
    base_url: str,
    rules: Optional[dict[str, Rule]] = None,
    include_default: bool = True,
) -> dict:
    soup = make_soup(html)
    data: dict[str, Any] = {}
    if include_default:
        data.update(extract_default(soup, base_url))
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
