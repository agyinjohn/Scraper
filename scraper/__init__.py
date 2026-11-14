"""A general-purpose web scraper and crawler."""

from .crawler import Crawler, CrawlConfig
from .extractor import extract, parse_rules
from .fetcher import Fetcher, FetchResult

__all__ = ["Crawler", "CrawlConfig", "extract", "parse_rules", "Fetcher", "FetchResult"]
__version__ = "0.1.0"
