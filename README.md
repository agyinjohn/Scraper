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

## Capturing everything on a site

Every page record always includes `background_images` (CSS/`data-background`),
`videos` (YouTube/Vimeo links and video files), `embeds` (iframes such as maps),
`emails` and `phones`.

* `--full` adds `full_text` (every visible line, including header, menus,
  footer and popups; `text` stays the main content only) and `forms` (each form's
  action, method and fields with names, labels, placeholders and options).
* `--download DIR` saves every page's HTML plus all of its images, background
  images and video files into `DIR/<host>/<path>`, mirroring the site layout.

```bash
scraper https://example.com -d 10 -n 500 --full --download mirror/ -o site.json
```

## Automatic product detection

`--products` finds product cards on any shop page without writing selectors. It
looks for the smallest block around a price that also has a photo and a name,
and returns name, price, sale/old price, link, image and star rating. Header and
menu widgets (mini-carts), icon feature boxes and badges like "Hot"/"-25%" are
ignored.

```bash
# crawl a whole shop and save one de-duplicated product catalog
scraper https://shop.example.com -d 10 -n 500 --products-file products.csv -o pages.json
```

Each catalog row includes `found_on`, the pages where that product appeared.

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

Add `"regex"` to a rule to keep only part of the value, e.g.
`{"selector": ".price", "regex": "\\$[\\d.,]+"}` (a capture group, if present, is returned).

Other rule options: `"count": true` returns how many elements match (e.g. star
icons → rating), and the attribute `own` (`"h5@own"`) returns only an element's
direct text, ignoring nested tags.

Custom fields appear under the `data` key of each record.

Real-world examples for an e-commerce template:

```bash
# product cards from a listing page (name, price, sale price, image, URL)
scraper https://ex-coders.com/html/ecomas/index.html -c examples/ecomas.json --no-default
# full product page: price, rating, SKU, tags, gallery, specs, reviews
scraper https://ex-coders.com/html/ecomas/product-details.html -c examples/ecomas-details.json --no-default
# start at the listing and follow links to product pages only
scraper https://ex-coders.com/html/ecomas/index.html -d 1 --include 'product-details' \
  -c examples/ecomas-details.json --no-default -f jsonl -o products.jsonl
```

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
| `-p/--products`, `--products-file FILE` | auto-detect products per page / write a site-wide catalog |
| `--full`, `--download DIR` | capture all text and forms / save pages and media |
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
