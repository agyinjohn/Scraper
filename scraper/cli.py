"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__
from .crawler import CrawlConfig, Crawler
from .extractor import parse_rules
from .fetcher import DEFAULT_USER_AGENT, Fetcher
from .output import write


def _kv_pairs(items: list[str], sep: str) -> dict:
    out = {}
    for item in items:
        if sep not in item:
            raise SystemExit(f"Expected KEY{sep}VALUE, got {item!r}")
        k, v = item.split(sep, 1)
        out[k.strip()] = v.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scraper",
        description="Scrape any website: fetch pages, extract data, optionally crawl links.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  scraper https://example.com
  scraper https://news.ycombinator.com -s "+titles=.titleline > a" --no-default
  scraper https://example.com --depth 2 --max-pages 50 -f jsonl -o site.jsonl
  scraper https://spa.example.com --render --wait-for "#app .item"
  scraper https://shop.example.com --config rules.json -f csv -o products.csv
""",
    )
    p.add_argument("urls", nargs="*", help="URL(s) to scrape")
    p.add_argument("-i", "--input", help="file with one URL per line")

    ex = p.add_argument_group("extraction")
    ex.add_argument(
        "-s", "--select", action="append", default=[], metavar="NAME=CSS[@ATTR]",
        help="custom field; prefix NAME with + to get all matches (repeatable)",
    )
    ex.add_argument("-c", "--config", help="JSON file of extraction rules (supports nesting)")
    ex.add_argument("--no-default", action="store_true",
                    help="only output custom fields, skip generic page extraction")

    cr = p.add_argument_group("crawling")
    cr.add_argument("-d", "--depth", type=int, default=0, help="link depth to follow (default 0)")
    cr.add_argument("-n", "--max-pages", type=int, help="max pages to fetch (default: 1 per URL, 100 when crawling)")
    cr.add_argument("--any-domain", action="store_true", help="follow links to other domains")
    cr.add_argument("--include", action="append", default=[], metavar="REGEX",
                    help="only follow URLs matching this regex (repeatable)")
    cr.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                    help="never follow URLs matching this regex (repeatable)")
    cr.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    cr.add_argument("--ignore-robots", action="store_true", help="don't check robots.txt")

    fe = p.add_argument_group("fetching")
    fe.add_argument("--render", action="store_true", help="render JavaScript with a headless browser (needs playwright)")
    fe.add_argument("--wait-for", metavar="CSS", help="with --render, wait for this selector")
    fe.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    fe.add_argument("-H", "--header", action="append", default=[], metavar="'Name: value'")
    fe.add_argument("--cookie", action="append", default=[], metavar="name=value")
    fe.add_argument("--proxy", help="proxy URL, e.g. http://user:pass@host:port")
    fe.add_argument("--timeout", type=float, default=20.0)
    fe.add_argument("--retries", type=int, default=3)
    fe.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")

    out = p.add_argument_group("output")
    out.add_argument("-f", "--format", choices=["json", "jsonl", "csv"], default="json")
    out.add_argument("-o", "--output", help="output file (default stdout)")
    out.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING - 10 * min(args.verbose, 2),
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if args.verbose < 2:
        logging.getLogger("urllib3").setLevel(logging.ERROR)

    urls = list(args.urls)
    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            urls += [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    if not urls:
        build_parser().print_usage(sys.stderr)
        print("error: no URLs given", file=sys.stderr)
        return 2
    urls = [u if "://" in u else "https://" + u for u in urls]

    rules = {}
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            rules.update(json.load(fh))
    try:
        rules.update(parse_rules(args.select))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    max_pages = args.max_pages or (100 if args.depth > 0 else len(urls))
    config = CrawlConfig(
        max_pages=max_pages,
        max_depth=args.depth,
        same_domain=not args.any_domain,
        include=args.include,
        exclude=args.exclude,
        delay=args.delay,
        respect_robots=not args.ignore_robots,
        rules=rules or None,
        include_default=not args.no_default,
    )

    headers = _kv_pairs(args.header, ":")
    cookies = _kv_pairs(args.cookie, "=")
    with Fetcher(
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
        headers=headers,
        cookies=cookies,
        proxy=args.proxy,
        render=args.render,
        wait_for=args.wait_for,
        verify_ssl=not args.insecure,
    ) as fetcher:
        crawler = Crawler(fetcher, config)
        try:
            n = write(crawler.crawl(urls), args.format, args.output)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    if args.output:
        print(f"wrote {n} record(s) to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
