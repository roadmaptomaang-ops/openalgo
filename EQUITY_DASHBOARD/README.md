# Equity Dashboard — live NIFTY 100 heatmap

Pure display, read-only. Never places an order, never imports strategy code.
A Finviz-style treemap: **tile size = market weight, tile color = live %
change from today's open.**

## What it shows

1. **Our Arsenal** — the 20 stocks `trend_rider_v2.py` actually trades, as
   its own treemap up top.
2. **Full Market** — the ~101-symbol NIFTY 50 + Next 50 universe below, same
   treemap style, with the 20 tradeable stocks outlined in indigo so you can
   see how they're behaving relative to the rest of the market.

Color is a gradient (pale near 0%, saturated at ±5%+), not flat red/green —
so a stock up 0.2% barely tints while one up 4% pops immediately.

**Laptop/desktop only, by design.** `body { min-width: 1180px }` +
`html { overflow-x: auto }` means the page never tries to squeeze the
treemap into a phone-sized layout — a narrow window just gets a horizontal
scrollbar instead of unreadable, clipped tiles. There are no mobile
breakpoints and none should be added; this is a laptop webpage on purpose.

## Run

```bash
export OPENALGO_API_KEY=<64-hex key>
cd ~/openalgo/EQUITY_DASHBOARD && ./run_dashboard.sh
```
Then open **http://127.0.0.1:5051** in a browser. Also runs via the VS Code
task `📊 Equity Dashboard — live heatmap`, or `preview_start` name
`equity-dashboard` (`.claude/launch.json`).

The main OpenAlgo app (`uv run app.py`, port 5000) must already be running —
this dashboard has no data of its own, it only polls OpenAlgo's `/quotes`
endpoint for every symbol.

## How the refresh works (no page reload, ever)

Two independent loops:
- **Backend** (`dashboard_server.py`): a background thread fetches all ~101
  quotes every 20s, computes `% change = (ltp - open) / open`, and overwrites
  one in-memory JSON snapshot.
- **Frontend** (`index.html`): polls `GET /api/data` every 12s and replaces
  the treemap's inner HTML in place — the page itself, scroll position, etc.
  never reload. This is what makes it feel "live."

## Files

| File | Role |
|---|---|
| `nifty100_symbols.py` | The universe (NIFTY 50 + Next 50, ~101 symbols) |
| `our_20.py` | The tradeable arsenal — mirrors `trend_rider_v2.ALL_EQUITY_STOCKS` |
| `market_weights.py` | Relative tile-size weights (approximate free-float market cap tiers) |
| `dashboard_server.py` | Flask server, own port 5051, background fetch loop |
| `index.html` | The treemap frontend — vanilla JS squarified-treemap layout, no libraries |
| `run_dashboard.sh` | One-line launcher (key baked in) |

## Honesty notes

- **`nifty100_symbols.py`** is a best-effort snapshot of NIFTY 100
  constituents from training knowledge, not fetched live from NSE. Index
  rebalances happen quarterly — verify against the official factsheet if
  precision matters. Editing the list is a one-line change; nothing else in
  the dashboard depends on the exact membership.
- **`market_weights.py`** weights are approximate relative market-cap tiers,
  used ONLY to size tiles visually. They never affect color, %change, or any
  trading decision — precision doesn't matter here (a tile being 8% vs 9% of
  the canvas is imperceptible).
- If `ALL_EQUITY_STOCKS` in `PIT_codex_fixes/trend_rider_v2.py` ever changes,
  update `our_20.py` to match — it's a deliberate standalone copy (zero
  dependency on strategy code, so the dashboard never needs an API key
  gate beyond its own quotes access, and never risks importing anything
  that could place an order).

## Squarified treemap algorithm

`index.html` implements the standard squarify algorithm (Bruls, Huizing, van
Wijk 1999) in ~40 lines of vanilla JS — no D3, no external libraries. Items
are sorted by weight descending, then packed row-by-row choosing whichever
grouping minimizes the worst aspect ratio, recursing until the container is
tiled. This is the same core algorithm behind Finviz/TradingView-style market
maps.
