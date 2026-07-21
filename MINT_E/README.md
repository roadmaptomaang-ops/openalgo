# MINT-E — Trend Rider entries + MINT capture-ratchet exits

**Built 2026-07-10.** Clone of `../PIT_codex_fixes/trend_rider_v2.py` with the
exit machinery replaced by MINT's organs, applied to the NIFTY-50 equity
universe. Logs as strategy **`mint_e`** — fully isolated from `trend_rider`
in `trading.db`, `logs/`, and SAGE. Safe to run alongside Trend Rider V2 as
a live paper A/B (identical entries, different exits — same comparison that
proved MINT vs V5.1 on 2026-07-09).

## Why it exists

NEWPIT showed the whole book's problem is exits (5% capture of what the
market offers). MINT fixed it for index options. Trend Rider — the only
"net-positive" engine — turned out to be net-negative on its EQUITY trades
alone (−₹5,034 over 86 trades; its profit came from an early-June options
era). Its exits show the same disease: 120-second "No Momentum" scratches
that churn ₹60/RT costs, and trailing stops that give back most of the peak.

## What changed vs Trend Rider V2 (exits only — entries identical)

| Organ | Trend Rider V2 | MINT-E |
|---|---|---|
| Dead trade | 120s "No Momentum Exit" (MFE < 1.0pt fixed) | **Zero-MFE bailout**: 90s, MFE < 0.15×ATR |
| Winner protection | BE at ~4pts + ATR trail + HWM trail | **Capture ratchet**: arms at +0.75×ATR, floor = 50% of peak, floor only rises |
| min_hold | delays all exits < 120s | never delays an **armed** ratchet exit |
| Regime gate | reads `trend_rider` history | reads `trend_rider` + `mint_e` (same universe) |

## Replay evidence (86 historical EQUITY trades, ₹60/RT costs)

| Variant | Net | Per trade |
|---|---|---|
| Actual Trend Rider exits | −₹5,034 | −₹59 |
| MINT-E ratchet sim | −₹1,191 | −₹14 |
| + regime gate (skip <2.0 avg MFE, 1 probe/day) | −₹175 | −₹3 |

Walk-forward (fit pre-07-03, test on the decay era): actual −₹47/trade →
sim ≈ −₹33 to −₹20/trade. **The ratchet cuts the bleed 60–75%; it does not
create an edge in chop.** Judge it live by capture % and net/trade vs
trend_rider on the same days, not by expecting green in a dead regime.

Note: floor values above 50% looked even better in replay, but the sim
systematically flatters tight floors (historical MFE was earned under loose
exits), so this uses MINT's proven 50%. Revisit with live data.

## Run

```bash
cd ~/openalgo/MINT_E && ./run_mint_e.sh
```

Logs: `~/openalgo/logs/YYYY-MM-DD/mint_e/`, master CSV
`~/openalgo/logs/mint_e_master.csv`, DB label `mint_e` (SAGE picks it up
automatically on the next `enrich`).
