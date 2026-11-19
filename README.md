# Vintage this week

A personal weekly digest of new stock from vintage design shops listed on vntg.com, starting with the 16 within 20 km of Amsterdam.

Every Monday morning GitHub runs `scraper/run.py`. It reads each shop's website, compares the stock with last week's, and updates the page in `docs/`. The page shows what's new, what dropped in price and what sold, filtered by area, furniture type, price and shop.

## Files

- `data/shops.json`: all 105 Dutch VNTG shops with address, distance from Dam Square and area. A shop with `"reader": null` is listed but not checked yet.
- `data/state.json`: everything seen so far (written by the weekly run, don't edit).
- `scraper/readers.py`: how each kind of shop website is read.
- `scraper/run.py`: the weekly run: read shops, compare with last week, write `docs/data.json`.
- `docs/index.html`: the digest page.
- `.github/workflows/weekly.yml`: the Monday schedule.

## Adding a shop

Give the shop a `reader` in `data/shops.json`. Shops on Shopify or WooCommerce need one line, e.g. `{"type": "woo", "base": "https://www.example.nl"}`. Other shops need the address of their listing page and a pattern for their product links.

## How "new" works

Most shops don't date their stock, so an item counts as new the first week it appears. A shop's first check only records what's there, so new items show from its second check.
