"""Redis multi-symbol market bus (MT5 / external) — contract only, no Wine/MT5 on VPS.

Bridges publish into Redis. Esses can keep reading the legacy cTrader cache via
optional EURUSD mirror until PAPER flips to RedisMarketFeed.

Keys
----
market:bus:v1:{SYMBOL}:tick
market:bus:v1:{SYMBOL}:bars:{TF}   TF in M1,M5,M15,H1,H4,D1
market:bus:v1:{SYMBOL}:instrument
market:bus:v1:{SYMBOL}:status
market:bus:v1:{SYMBOL}:source      mt5 | external | ctrader
market:bus:v1:symbols              JSON list

Legacy mirror (EURUSD only, optional on ingest)
-----------------------------------------------
ctrader:stream:v1:{env}:{account}:tick|bars:*|instrument|status

HTTP (admin write / research read)
---------------------------------
POST /research/market-bus/ingest   X-API-Key admin
GET  /research/market-bus/status?symbol=EURUSD
GET  /research/market-bus/symbols

Example ingest body
-------------------
{
  "source": "mt5",
  "symbol": "EURUSD",
  "status": "connected",
  "mirror_legacy": true,
  "tick": {"bid": "1.08500", "ask": "1.08512", "as_of": "2026-10-06T13:30:01+00:00"},
  "instrument": {"digits": 5, "pip_position": 4},
  "bars": {
    "M1": [{
      "open": "1.08490", "high": "1.08520", "low": "1.08480", "close": "1.08500",
      "volume": "12", "closed_at": "2026-10-06T13:29:00+00:00"
    }]
  }
}

Rollout note
------------
No MT5 terminal on this Ubuntu VPS. A Windows/VPS bridge (or external feed) POSTs
here. Keep cTrader Open API for FTMO execution / prop-sim, not as the 24/5 price feed.
"""
