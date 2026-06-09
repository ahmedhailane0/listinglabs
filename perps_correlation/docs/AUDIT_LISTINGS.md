# Listing audit — missing real major-venue listings

_Generated 2026-06-06T14:12:32.852635Z. Source: live exchange candle APIs (OKX/Bybit/Kraken/KuCoin/Bitget/Gate, spot+perp). Read-only._

## 4 token(s) missing listings

| Token | Missing venue | Earliest candle (≈listing) |
|---|---|---|
| BTW | Bybit Perp | 2026-06-05 |
| BTW | KuCoin Futures | 2026-06-05 |
| EDGE | Bybit Perp | 2026-03-20 |
| EDGE | Kraken Spot | 2026-03-01 |
| EDGE | KuCoin Futures | 2026-03-20 |
| EDGE | Gate.io Perp | 2026-03-01 |
| SLX | OKX Perp | 2026-06-01 |
| SLX | Bybit Perp | 2026-06-01 |
| SLX | KuCoin Futures | 2026-06-01 |
| SLX | Bitget Perp | 2026-06-01 |
| ZEST | Bybit Perp | 2026-06-05 |

## ⚠ Suspect hits (pre-floor — likely ticker collision, verify by hand)

| Token | Venue | First candle |
|---|---|---|
| CFG | OKX Spot | 2025-05-03 |

---
To apply the confirmed gaps: `python sweep_venues.py --force` then `python merge_sweep.py` (curated/verified events always win).