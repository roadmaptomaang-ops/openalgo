"""
our_20.py — the tradeable arsenal (mirrors trend_rider_v2.ALL_EQUITY_STOCKS)
==============================================================================
Kept as a plain list here (not imported from trend_rider_v2.py) so the
dashboard has zero dependency on the strategy engine — pure display, no
strategy code loaded, no OPENALGO_API_KEY-gated import chain.

If ALL_EQUITY_STOCKS in PIT_codex_fixes/trend_rider_v2.py changes, update this
list to match.
"""

OUR_20 = [
    "BRITANNIA", "BAJFINANCE", "WIPRO", "IOC", "HINDALCO", "ASIANPAINT",
    "INDUSINDBK", "BPCL", "TCS", "SUNPHARMA", "HEROMOTOCO", "HCLTECH",
    "GRASIM", "RELIANCE", "BHARTIARTL", "SBIN", "TATASTEEL", "POWERGRID",
    "NTPC", "COALINDIA",
]

assert len(OUR_20) == 20
