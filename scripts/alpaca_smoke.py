#!/usr/bin/env python3
"""Read-only smoke test for the Alpaca paper account + market data.

Usage:  .venv/bin/python scripts/alpaca_smoke.py

Verifies, without placing any orders:
  1. Trading API auth   -> GET /v2/account   (cash, equity, status)
  2. Market clock        -> GET /v2/clock    (is the market open?)
  3. Real-time data      -> latest IEX quote for SPY + two universe names
  4. Historical data     -> last 5 daily bars for SPY (Basic plan check)

Keys come from .env (repo root):
  ALPACA_API_KEY / ALPACA_SECRET_KEY  (paper keys — dashboard shows the
  secret only once at generation time). Quote values in .env if they
  contain $ or # (same dotenv gotcha as the hwsinvest password).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

KEY = os.environ.get("ALPACA_API_KEY", "")
SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
if not KEY or not SECRET:
    sys.exit("ALPACA_API_KEY / ALPACA_SECRET_KEY missing from .env — add them first.")

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# paper=True routes to https://paper-api.alpaca.markets — never touches live.
trading = TradingClient(KEY, SECRET, paper=True)

acct = trading.get_account()
print(f"[1/4] account OK   status={acct.status}  cash=${float(acct.cash):,.2f}  "
      f"equity=${float(acct.equity):,.2f}  buying_power=${float(acct.buying_power):,.2f}")

clock = trading.get_clock()
state = "OPEN" if clock.is_open else "closed"
print(f"[2/4] clock OK     market {state}  next_open={clock.next_open}  next_close={clock.next_close}")

data = StockHistoricalDataClient(KEY, SECRET)
quotes = data.get_stock_latest_quote(
    StockLatestQuoteRequest(symbol_or_symbols=["SPY", "DELL", "MU"]))
for sym, q in sorted(quotes.items()):
    print(f"[3/4] quote OK     {sym}: bid {q.bid_price} x {q.bid_size}  "
          f"ask {q.ask_price} x {q.ask_size}  ({q.timestamp})")

bars = data.get_stock_bars(
    StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day, limit=5))
closes = [f"{b.timestamp.date()} {b.close}" for b in bars["SPY"]]
print(f"[4/4] bars OK      SPY last daily closes: {', '.join(closes)}")

print("\nAll four checks passed — paper account and market data are live.")
