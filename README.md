# scraper

A general-purpose web scraper and crawler for Python. Give it any URL and it
returns structured data. You can also tell it exactly what to pull out with
CSS selectors, follow links across a site, and render JavaScript-heavy pages
in a headless browser.

## Install

```bash
pip install -e .                 # core (requests + BeautifulSoup)
pip install -e '.[browser]'      # optional: JavaScript rendering
playwright install chromium
```

## Quick start

```bash
# Generic extraction: title, description, headings, text, links, images, tables, JSON-LD
scraper https://example.com

# Custom fields with CSS selectors (NAME=CSS, NAME=CSS@attr, +NAME = all matches)
scraper https://news.ycombinator.com -s "+titles=.titleline > a" -s "+links=.titleline > a@href" --no-default

# Crawl a site 2 links deep, max 50 pages, streaming JSON Lines
scraper https://example.com -d 2 -n 50 -f jsonl -o site.jsonl

# Only follow blog URLs, skip tag pages
scraper https://example.com/blog -d 3 --include '/blog/' --exclude '/tag/'

# JavaScript-rendered sites (SPAs)
scraper https://spa.example.com --render --wait-for ".item"

# Structured, nested extraction from a rules file, exported to CSV
scraper https://shop.example.com --config rules.example.json -f csv -o products.csv
```

`python -m scraper ...` works too, without installing.

## Extraction rules

On the command line, `-s` takes `name=selector[@attr]`:

| Spec | Result |
|---|---|
| `title=h1` | text of the first `h1` |
| `+prices=.price` | list of text of every `.price` |
| `link=a.more@href` | `href` of the first match, made absolute |
| `body=article@html` | inner HTML |

For nested records, use a JSON config (see `rules.example.json`):

```json
{
  "products": {
    "selector": ".product", "all": true,
    "fields": { "name": "h2", "price": ".price", "url": "a@href" }
  }
}
```

Custom fields appear under the `data` key of each record.

## Options

| Option | Purpose |
|---|---|
| `-d/--depth N` | follow links N levels deep (default 0 = only the given URLs) |
| `-n/--max-pages N` | page limit (default 100 when crawling) |
| `--any-domain` | allow leaving the starting domain |
| `--include/--exclude REGEX` | restrict which links get followed |
| `--delay SEC` | wait between requests (default 1s; robots.txt `Crawl-delay` is honoured) |
| `--ignore-robots` | skip robots.txt checks |
| `--render`, `--wait-for CSS` | use headless Chromium via Playwright |
| `-H 'Name: value'`, `--cookie k=v` | extra headers / cookies (e.g. for logged-in pages) |
| `--proxy URL`, `--user-agent UA`, `--timeout`, `--retries`, `--insecure` | network tuning |
| `-f json\|jsonl\|csv`, `-o FILE` | output format and destination (default JSON to stdout) |
| `-i FILE` | read URLs from a file, one per line |
| `-v` / `-vv` | progress / debug logging |

## Library use

```python
from scraper import Fetcher, Crawler, CrawlConfig, parse_rules

config = CrawlConfig(max_pages=10, max_depth=1, rules=parse_rules(["+h2=h2"]))
with Fetcher() as f:
    for page in Crawler(f, config).crawl(["https://example.com"]):
        print(page["url"], page.get("title"), page.get("data"))
```

## Responsible use

By default the scraper respects robots.txt and rate-limits itself. Check a site's
terms of service before scraping it, and don't overload servers.

## Tests

```bash
pip install -e '.[dev]' && pytest
```
