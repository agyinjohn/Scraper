"""Save pages and their media (images, background images, video files) to disk."""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse

from .fetcher import Fetcher

log = logging.getLogger(__name__)

MEDIA_EXT = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|ico|bmp|mp4|webm|mov|mp3|pdf)$", re.I)


def local_path(root: str, url: str, default_name: str = "index.html") -> str:
    p = urlparse(url)
    path = unquote(p.path)
    if not path or path.endswith("/"):
        path += default_name
    parts = [seg for seg in path.split("/") if seg not in ("", ".", "..")]
    safe = [re.sub(r'[<>:"\\|?*]', "_", seg) for seg in parts]
    return os.path.join(root, re.sub(r"[^\w.-]", "_", p.netloc), *safe)


class Downloader:
    def __init__(self, fetcher: Fetcher, root: str, hosts: Optional[set[str]] = None):
        self.fetcher = fetcher
        self.root = root
        self.hosts = hosts  # None = any host
        self.done: set[str] = set()
        self.saved = 0
        self.failed: list[str] = []

    def save_page(self, url: str, html: str) -> str:
        path = local_path(self.root, url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        return path

    def media_urls(self, record: dict) -> list[str]:
        urls = [i["src"] for i in record.get("images", [])]
        urls += record.get("background_images", [])
        urls += [v for v in record.get("videos", []) if MEDIA_EXT.search(urlparse(v).path)]
        urls += [p["image"] for p in record.get("products", []) if p.get("image")]
        if record.get("og", {}).get("image"):
            urls.append(record["og"]["image"])
        return urls

    def download(self, urls: Iterable[str]) -> None:
        for url in urls:
            if url in self.done or not url.startswith(("http://", "https://")):
                continue
            self.done.add(url)
            host = urlparse(url).netloc.lower().removeprefix("www.")
            if self.hosts is not None and host not in self.hosts:
                continue
            path = local_path(self.root, url, "file")
            if os.path.exists(path):
                continue
            try:
                r = self.fetcher.session.get(url, timeout=self.fetcher.timeout, stream=True)
                if r.status_code != 200:
                    self.failed.append(f"{url} (HTTP {r.status_code})")
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    for chunk in r.iter_content(65536):
                        fh.write(chunk)
                self.saved += 1
            except Exception as e:  # network errors shouldn't stop the crawl
                self.failed.append(f"{url} ({e.__class__.__name__})")
                log.debug("download failed %s: %s", url, e)
