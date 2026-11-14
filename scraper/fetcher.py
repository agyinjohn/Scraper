"""Page fetching: plain HTTP via requests, or a headless browser for JS-heavy sites."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 scraper/0.1"
)


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content_type: str
    text: str
    elapsed: float
    headers: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400

    @property
    def is_html(self) -> bool:
        ct = self.content_type.lower()
        return "html" in ct or "xml" in ct or ct == ""


class Fetcher:
    """Fetches pages. Use ``render=True`` to execute JavaScript with Playwright."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        retries: int = 3,
        headers: Optional[dict] = None,
        cookies: Optional[dict] = None,
        proxy: Optional[str] = None,
        render: bool = False,
        wait_for: Optional[str] = None,
        verify_ssl: bool = True,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.render = render
        self.wait_for = wait_for
        self.proxy = proxy

        self.session = requests.Session()
        retry = Retry(
            total=retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        if headers:
            self.session.headers.update(headers)
        if cookies:
            self.session.cookies.update(cookies)
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.verify = verify_ssl

        self._pw = None
        self._browser = None
        self._context = None

    # ------------------------------------------------------------------ http
    def get_text(self, url: str) -> Optional[str]:
        """Plain GET returning body text or None (used for robots.txt etc.)."""
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                return r.text
        except requests.RequestException as e:
            log.debug("get_text failed for %s: %s", url, e)
        return None

    def fetch(self, url: str) -> FetchResult:
        if self.render:
            return self._fetch_browser(url)
        return self._fetch_http(url)

    def _fetch_http(self, url: str) -> FetchResult:
        start = time.monotonic()
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as e:
            return FetchResult(url, 0, "", "", time.monotonic() - start, error=str(e))
        if r.encoding is None or r.encoding.lower() == "iso-8859-1":
            # requests defaults to latin-1 when no charset given; sniff instead
            r.encoding = r.apparent_encoding
        return FetchResult(
            url=r.url,
            status=r.status_code,
            content_type=r.headers.get("Content-Type", ""),
            text=r.text,
            elapsed=time.monotonic() - start,
            headers=dict(r.headers),
        )

    # --------------------------------------------------------------- browser
    def _ensure_browser(self):
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover - depends on env
            raise RuntimeError(
                "JavaScript rendering needs Playwright: pip install 'scraper[browser]' "
                "&& playwright install chromium"
            ) from e
        self._pw = sync_playwright().start()
        launch_kwargs = {"headless": True}
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            extra_http_headers={
                k: v for k, v in self.session.headers.items() if k.lower() != "user-agent"
            },
            ignore_https_errors=not self.session.verify,
        )
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain or "", "path": c.path or "/"}
            for c in self.session.cookies
            if c.domain
        ]
        if cookies:
            self._context.add_cookies(cookies)

    def _fetch_browser(self, url: str) -> FetchResult:
        self._ensure_browser()
        start = time.monotonic()
        page = self._context.new_page()
        try:
            resp = page.goto(url, timeout=self.timeout * 1000, wait_until="networkidle")
            if self.wait_for:
                page.wait_for_selector(self.wait_for, timeout=self.timeout * 1000)
            html = page.content()
            status = resp.status if resp else 200
            headers = resp.headers if resp else {}
            return FetchResult(
                url=page.url,
                status=status,
                content_type=headers.get("content-type", "text/html"),
                text=html,
                elapsed=time.monotonic() - start,
                headers=dict(headers),
            )
        except Exception as e:  # playwright raises its own error types
            return FetchResult(url, 0, "", "", time.monotonic() - start, error=str(e))
        finally:
            page.close()

    def close(self):
        self.session.close()
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()
            self._browser = self._pw = self._context = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
