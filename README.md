# Catalog Pulse

A small scheduled catalog monitor. It checks the German men's sale feed, applies
the local filters in `settings.json`, and sends relevant changes to a Discord
webhook stored as a GitHub Actions secret.

## Current filters

- Sizes: S, M, L
- Tops: up to €20
- Pants: up to €30, excluding slim fit
- Jackets, overshirts and coats: up to €70
- Excludes socks, underwear, bags, shorts and graphic/collaboration T-shirts

## Setup

1. Upload all files and folders to your repository.
2. Confirm the repository secret is named `DISCORD_WEBHOOK_URL`.
3. Open **Actions → Catalog Pulse → Run workflow**.
4. The first run sends one confirmation message and creates a baseline.
5. Later runs notify only for new matching products, price drops, restocks or
   newly available S/M/L sizes.

## Changing filters

Edit `settings.json`. The main price limits are:

```json
"price_limits_eur": {
  "tops": 20,
  "pants": 30,
  "jackets": 70
}
```

Add future franchises or collaboration names to `excluded_tshirts`.

## State

`data/state.json` stores the baseline and previous prices/sizes. Do not delete it
unless you deliberately want to reset the monitor.

## Upstream dependency

Product and stock retrieval uses the MIT-licensed
`kequach/uniqlo-sales-alerter` project, pinned to a specific commit.
See `THIRD_PARTY_NOTICES.md`.
