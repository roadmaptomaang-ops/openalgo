"""
nifty100_symbols.py — the tradeable universe for the Equity Dashboard
======================================================================
NIFTY 50 + NIFTY Next 50 (~101 symbols, "NIFTY 100" universe), NSE
cash-market symbols in OpenAlgo format (base symbol, exchange="NSE").

HONESTY NOTE: this list is a best-effort snapshot, not fetched live from NSE.
Index constituents change periodically (quarterly rebalances, corporate
actions). If a symbol below no longer trades or a new one should be added,
just edit the two lists — nothing else in the dashboard needs to change.
Verify against the official NSE NIFTY 100 factsheet if precision matters.
"""

NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC",
    "SBIN", "BHARTIARTL", "BAJFINANCE", "KOTAKBANK", "LT", "HCLTECH",
    "ASIANPAINT", "AXISBANK", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO",
    "WIPRO", "NESTLEIND", "ONGC", "NTPC", "POWERGRID", "M&M", "TATAMOTORS",
    "TATASTEEL", "ADANIENT", "ADANIPORTS", "COALINDIA", "BAJAJFINSV",
    "HDFCLIFE", "SBILIFE", "DRREDDY", "GRASIM", "BRITANNIA", "EICHERMOT",
    "CIPLA", "DIVISLAB", "HEROMOTOCO", "BPCL", "INDUSINDBK", "TECHM",
    "APOLLOHOSP", "BAJAJ-AUTO", "SHRIRAMFIN", "JSWSTEEL", "HINDALCO",
    "LTIM", "TATACONSUM", "UPL",
]

NIFTY_NEXT_50 = [
    "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "BANKBARODA", "BERGEPAINT",
    "BOSCHLTD", "CANBK", "CHOLAFIN", "COLPAL", "DABUR", "DLF", "GAIL",
    "GODREJCP", "HAVELLS", "ICICIGI", "ICICIPRULI", "INDIGO", "INDUSTOWER",
    "IOC", "IRCTC", "JINDALSTEL", "JIOFIN", "LODHA", "LUPIN", "MARICO",
    "MOTHERSON", "MUTHOOTFIN", "NAUKRI", "PAGEIND", "PIDILITIND", "PIIND",
    "PNB", "RECLTD", "SIEMENS", "SRF", "TATAPOWER", "TORNTPHARM",
    "TVSMOTOR", "UNITDSPR", "VBL", "VEDL", "YESBANK", "ZOMATO", "ZYDUSLIFE",
    "ABB", "AUROPHARMA", "BANDHANBNK", "BIOCON", "GMRAIRPORT", "POLYCAB",
]

NIFTY_100 = NIFTY_50 + NIFTY_NEXT_50
EXCHANGE = "NSE"

assert len(NIFTY_100) == len(set(NIFTY_100)), "duplicate symbol in universe"
