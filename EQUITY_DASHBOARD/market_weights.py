"""
market_weights.py — relative size weight per symbol, for the treemap tile sizes
=================================================================================
HONESTY NOTE: these are APPROXIMATE relative weights (roughly free-float market
cap tiers), not fetched live. They only control how BIG a tile is on the
heatmap — they never affect color, %change, or any trading decision. Precision
doesn't matter much for a visual (a tile being 8% vs 9% of the canvas is
imperceptible) but if you want it exact, swap this for a live market-cap feed.

Unlisted symbols fall back to DEFAULT_WEIGHT (a small/mid-cap-sized tile).
"""

# Relative weight, roughly proportional to free-float market cap (arbitrary units).
WEIGHTS: dict[str, float] = {
    # mega caps
    "RELIANCE": 100, "TCS": 70, "HDFCBANK": 68, "ICICIBANK": 55, "INFY": 45,
    "BHARTIARTL": 42, "HINDUNILVR": 35, "SBIN": 34, "ITC": 33, "LT": 30,
    # large caps
    "BAJFINANCE": 28, "KOTAKBANK": 26, "HCLTECH": 24, "MARUTI": 22,
    "AXISBANK": 22, "SUNPHARMA": 20, "ASIANPAINT": 18, "TITAN": 18,
    "ULTRACEMCO": 16, "ADANIENT": 16, "BAJAJFINSV": 16, "NTPC": 15,
    "ONGC": 15, "WIPRO": 14, "POWERGRID": 14, "NESTLEIND": 14,
    "COALINDIA": 13, "M&M": 13, "TATAMOTORS": 13, "JSWSTEEL": 12,
    "ADANIPORTS": 12, "HDFCLIFE": 11, "SBILIFE": 11, "GRASIM": 10,
    "TATASTEEL": 10, "INDUSINDBK": 10, "DRREDDY": 9, "CIPLA": 9,
    "EICHERMOT": 9, "BPCL": 8, "BRITANNIA": 8, "HINDALCO": 8,
    "TECHM": 8, "APOLLOHOSP": 7, "DIVISLAB": 7, "TATACONSUM": 7,
    "HEROMOTOCO": 7, "SHRIRAMFIN": 6, "LTIM": 6, "BAJAJ-AUTO": 6, "UPL": 5,
    # next-50 mid caps
    "ADANIGREEN": 9, "ADANIPOWER": 7, "VBL": 7, "ZOMATO": 7, "DLF": 6,
    "GAIL": 6, "IOC": 6, "JIOFIN": 6, "VEDL": 6, "PNB": 5, "BANKBARODA": 5,
    "CANBK": 5, "SIEMENS": 5, "HAVELLS": 5, "ICICIGI": 5, "ICICIPRULI": 5,
    "INDIGO": 5, "TATAPOWER": 5, "AMBUJACEM": 4, "TVSMOTOR": 4, "SRF": 4,
    "MOTHERSON": 4, "TORNTPHARM": 4, "CHOLAFIN": 4, "LODHA": 4,
    "GODREJCP": 4, "PIDILITIND": 4, "MUTHOOTFIN": 4, "AUROPHARMA": 3,
    "NAUKRI": 3, "BOSCHLTD": 3, "COLPAL": 3, "MARICO": 3, "DABUR": 3,
    "LUPIN": 3, "PAGEIND": 3, "JINDALSTEL": 3, "INDUSTOWER": 3, "RECLTD": 3,
    "IRCTC": 3, "BERGEPAINT": 3, "PIIND": 3, "ABB": 3, "POLYCAB": 3,
    "GMRAIRPORT": 3, "BANDHANBNK": 2, "BIOCON": 2, "YESBANK": 2,
    "ZYDUSLIFE": 3,
}

DEFAULT_WEIGHT = 3.0


def weight_for(symbol: str) -> float:
    return WEIGHTS.get(symbol, DEFAULT_WEIGHT)
