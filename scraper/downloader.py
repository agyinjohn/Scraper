"""Mirror pages to disk with everything they need to display offline:
images, background images, video files, stylesheets, scripts, fonts and any
files referenced from inside CSS (``url()`` / ``@import``). Saved pages have
their absolute links rewritten to the local copies."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Iterable, Optional
from urllib.parse import unquote, urljoin, urlparse

from .extractor import make_soup
from .fetcher import Fetcher

log = logging.getLogger(__name__)

MEDIA_EXT = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|ico|bmp|mp4|webm|mov|mp3|pdf)$", re.I)
# @import "x.css" | @import url("x.css") | @import url(x.css) | url("x") | url(x)
CSS_URL_RE = re.compile(
    r"""@import\s+(?:url\(\s*(?:(?P<iq>['"])(?P<imp_q>.+?)(?P=iq)|(?P<imp_u>[^'")\s]+))\s*\)"""
    r"""|(?P<iq2>['"])(?P<imp_s>.+?)(?P=iq2))"""
    r"""|url\(\s*(?:(?P<uq>['"])(?P<url_q>.+?)(?P=uq)|(?P<url_u>[^'")\s]+))\s*\)""",
    re.I,
)


def _css_ref(m: "re.Match") -> tuple[str, bool]:
    imp = m.group("imp_q") or m.group("imp_u") or m.group("imp_s")
    if imp is not None:
        return imp.strip(), True
    return (m.group("url_q") or m.group("url_u") or "").strip(), False


CONTENT_EXT = {
    "text/css": ".css", "javascript": ".js", "font/woff2": ".woff2", "font/woff": ".woff",
    "font/ttf": ".ttf", "font/otf": ".otf", "image/png": ".png", "image/jpeg": ".jpg",
    "image/webp": ".webp", "image/gif": ".gif", "image/svg+xml": ".svg", "text/html": ".html",
}
REWRITE_ATTRS = ("href", "src", "data-src", "data-background", "data-bg", "poster")


def local_path(root: str, url: str, default_name: str = "index.html",
               content_type: str = "") -> str:
    """Map a URL to a file under ``root``. URLs that differ only by query string
    (``css2?family=A`` vs ``css2?family=B``, ``style.css?ver=2``) get distinct
    names, and a missing extension is added from the content type."""
    p = urlparse(url)
    path = unquote(p.path)
    if not path or path.endswith("/"):
        path += default_name
    parts = [seg for seg in path.split("/") if seg not in ("", ".", "..")]
    safe = [re.sub(r'[<>:"\\|?*]', "_", seg) for seg in parts]
    name = safe[-1] if safe else default_name
    stem, ext = os.path.splitext(name)
    if p.query:
        stem += "_" + hashlib.sha1(p.query.encode()).hexdigest()[:8]
    if not ext and content_type:
        ext = next((e for k, e in CONTENT_EXT.items() if k in content_type.lower()), "")
    safe[-1:] = [stem + ext]
    return os.path.join(root, re.sub(r"[^\w.-]", "_", p.netloc), *safe)


def _relative(from_file: str, to_file: str) -> str:
    rel = os.path.relpath(to_file, os.path.dirname(from_file))
    return rel.replace(os.sep, "/")


class Downloader:
    """``hosts`` limits which hosts *images/media* may come from (None = any).
    Stylesheets, scripts and fonts are always fetched, wherever they're hosted,
    because pages can't render without them."""

    def __init__(self, fetcher: Fetcher, root: str, hosts: Optional[set[str]] = None):
        self.fetcher = fetcher
        self.root = root
        self.hosts = hosts
        self.files: dict[str, str] = {}  # url -> local path (downloaded or already present)
        self.tried: set[str] = set()
        self.saved = 0
        self.by_kind: dict[str, int] = {}
        self.failed: list[str] = []

    # ------------------------------------------------------------ pages
    def save_page(self, url: str, html: str) -> str:
        path = local_path(self.root, url)
        self.files[url] = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self._rewrite_html(html, url, path))
        return path

    def _rewrite_html(self, html: str, url: str, path: str) -> str:
        """Point absolute URLs that we saved locally at the local file. Relative
        URLs already work because the mirror keeps the site's folder layout."""
        soup = make_soup(html)
        changed = False
        for el in soup.find_all(True):
            for attr in REWRITE_ATTRS:
                val = el.get(attr)
                if not isinstance(val, str) or val.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
                    continue
                target = self.files.get(urljoin(url, val.strip()).split("#")[0])
                if target and target != path:
                    rel = _relative(path, target)
                    if rel != val:
                        el[attr] = rel
                        changed = True
        return str(soup) if changed else html

    # ------------------------------------------------------------ assets
    def resource_urls(self, record: dict) -> list[tuple[str, str]]:
        """(url, kind) pairs a page record references."""
        out = [(u, "css") for u in record.get("assets", {}).get("stylesheets", [])]
        out += [(u, "js") for u in record.get("assets", {}).get("scripts", [])]
        out += [(u, "other") for u in record.get("assets", {}).get("other", [])]
        out += [(i["src"], "image") for i in record.get("images", [])]
        out += [(u, "image") for u in record.get("background_images", [])]
        out += [(v, "media") for v in record.get("videos", []) if MEDIA_EXT.search(urlparse(v).path)]
        out += [(p["image"], "image") for p in record.get("products", []) if p.get("image")]
        if record.get("og", {}).get("image"):
            out.append((record["og"]["image"], "image"))
        return out

    # kept for backwards compatibility
    def media_urls(self, record: dict) -> list[str]:
        return [u for u, _ in self.resource_urls(record)]

    def download_page_resources(self, record: dict) -> None:
        self.download(self.resource_urls(record))

    def download(self, items: Iterable) -> None:
        for item in items:
            url, kind = item if isinstance(item, tuple) else (item, "image")
            self._get(url.split("#")[0], kind)

    def _get(self, url: str, kind: str) -> None:
        if url in self.tried or not url.startswith(("http://", "https://")):
            return
        self.tried.add(url)
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if kind in ("image", "media") and self.hosts is not None and host not in self.hosts:
            return
        try:
            r = self.fetcher.session.get(url, timeout=self.fetcher.timeout, stream=True)
            if r.status_code != 200:
                self.failed.append(f"{url} (HTTP {r.status_code})")
                return
            ctype = r.headers.get("Content-Type", "")
            path = local_path(self.root, url, "file", ctype)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                for chunk in r.iter_content(65536):
                    fh.write(chunk)
        except Exception as e:  # network errors shouldn't stop the crawl
            self.failed.append(f"{url} ({e.__class__.__name__})")
            log.debug("download failed %s: %s", url, e)
            return
        self.saved += 1
        k = "font" if re.search(r"\.(woff2?|ttf|otf|eot)$", path, re.I) else (
            "css" if path.lower().endswith(".css") else kind)
        self.by_kind[k] = self.by_kind.get(k, 0) + 1
        self.files[url] = path
        if path.lower().endswith(".css") or "text/css" in ctype:
            self._process_css(url, path)

    def _process_css(self, url: str, path: str) -> None:
        """Download fonts/images/@imports a stylesheet references, then rewrite
        references whose local file name differs (absolute URLs, query strings)."""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                css = fh.read()
        except OSError:
            return
        for m in CSS_URL_RE.finditer(css):
            ref, is_import = _css_ref(m)
            if not ref or ref.startswith(("data:", "#", "about:")):
                continue
            self._get(urljoin(url, ref).split("#")[0], "css" if is_import else "asset")

        def repl(m):
            ref, is_import = _css_ref(m)
            if not ref or ref.startswith(("data:", "#", "about:")):
                return m.group(0)
            target = self.files.get(urljoin(url, ref).split("#")[0])
            if not target:
                return m.group(0)
            rel = _relative(path, target)
            if rel == ref:
                return m.group(0)
            return f'@import url("{rel}")' if is_import else f'url("{rel}")'

        new = CSS_URL_RE.sub(repl, css)
        if new != css:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
