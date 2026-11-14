"""Breadth-first crawler with robots.txt support, politeness delays and scoping."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

from .extractor import Rule, extract, find_links
from .fetcher import Fetcher

log = logging.getLogger(__name__)

SKIP_EXTENSIONS = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|ico|bmp|pdf|zip|gz|tar|rar|7z|exe|dmg|mp[34]|avi|mov|"
    r"wav|ogg|woff2?|ttf|eot|css|js|json|xml|rss)$",
    re.I,
)


@dataclass
class CrawlConfig:
    max_pages: int = 1
    max_depth: int = 0
    same_domain: bool = True
    include: list[str] = field(default_factory=list)  # regexes; URL must match one
    exclude: list[str] = field(default_factory=list)  # regexes; URL must match none
    delay: float = 1.0  # seconds between requests
    respect_robots: bool = True
    rules: Optional[dict[str, Rule]] = None
    include_default: bool = True
    products: bool = False  # auto-detect product cards on every page


def normalize_url(url: str) -> str:
    p = urlparse(url)
    path = p.path or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


class Crawler:
    def __init__(self, fetcher: Fetcher, config: CrawlConfig):
        self.fetcher = fetcher
        self.config = config
        self._robots: dict[str, Optional[RobotFileParser]] = {}
        self._include = [re.compile(p) for p in config.include]
        self._exclude = [re.compile(p) for p in config.exclude]
        self._last_request = 0.0

    # ----------------------------------------------------------- policies
    def allowed_by_robots(self, url: str) -> bool:
        if not self.config.respect_robots:
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            body = self.fetcher.get_text(origin + "/robots.txt")
            if body is None:
                self._robots[origin] = None
            else:
                rp = RobotFileParser()
                rp.parse(body.splitlines())
                self._robots[origin] = rp
                delay = rp.crawl_delay(self.fetcher.user_agent)
                if delay and delay > self.config.delay:
                    log.info("Honouring robots.txt crawl-delay of %ss for %s", delay, origin)
                    self.config.delay = float(delay)
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.fetcher.user_agent, url)

    def in_scope(self, url: str, seeds: set[str]) -> bool:
        if SKIP_EXTENSIONS.search(urlparse(url).path):
            return False
        if self.config.same_domain and _host(url) not in seeds:
            return False
        if self._include and not any(r.search(url) for r in self._include):
            return False
        if any(r.search(url) for r in self._exclude):
            return False
        return True

    def _throttle(self):
        wait = self.config.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    # ---------------------------------------------------------------- run
    def crawl(self, start_urls: list[str]) -> Iterator[dict]:
        """Yield one record per fetched page."""
        seeds = {_host(u) for u in start_urls}
        queue: deque[tuple[str, int]] = deque((normalize_url(u), 0) for u in start_urls)
        seen = {u for u, _ in queue}
        fetched = 0

        while queue and fetched < self.config.max_pages:
            url, depth = queue.popleft()
            if not self.allowed_by_robots(url):
                log.warning("Blocked by robots.txt: %s", url)
                yield {"url": url, "depth": depth, "error": "disallowed by robots.txt"}
                continue

            self._throttle()
            log.info("[%d/%d] depth=%d %s", fetched + 1, self.config.max_pages, depth, url)
            res = self.fetcher.fetch(url)
            fetched += 1

            record: dict = {
                "url": url,
                "final_url": res.url,
                "status": res.status,
                "depth": depth,
                "elapsed": round(res.elapsed, 3),
            }
            if not res.ok:
                record["error"] = res.error or f"HTTP {res.status}"
                yield record
                continue
            if not res.is_html:
                record["error"] = f"skipped non-HTML content ({res.content_type})"
                yield record
                continue

            record.update(
                extract(
                    res.text, res.url, self.config.rules,
                    self.config.include_default, self.config.products,
                )
            )
            yield record

            if depth < self.config.max_depth:
                for link in find_links(res.text, res.url):
                    link = normalize_url(link)
                    if link not in seen and self.in_scope(link, seeds):
                        seen.add(link)
                        queue.append((link, depth + 1))
