# NEWPIT — The Capture Engine

**A brand-new analytical paradigm. Built 2026-07-02.**

Every other tool in this system (PIT, SAGE) measures what you **made**. NEWPIT
measures what the market **offered** you — and how little of it you kept. It's a
lens no standard trading analytics uses, and it completely reframes the problem.

## The idea — the Regret Surface

Every trade's **MFE** (max favourable excursion) is the profit the market handed
you at the peak. Your **P&L** is what you actually kept. The gap is **regret**.

```
  OFFERED   = Σ MFE            the market's generosity (the ceiling)
  CAPTURED  = Σ P&L            what you kept
  REGRET    = OFFERED-CAPTURED what you gave back
  CAPTURE % = CAPTURED/OFFERED the ONE number that matters
```

## What it found (the whole book, 275 trades)

| | |
|---|---|
| The market OFFERED | **₹117,351** |
| You CAPTURED | **₹5,881** |
| **CAPTURE EFFICIENCY** | **5.0%** |
| Gifts turned into losses (MFE>0, P&L<0) | **49% of trades** |

**The problem was never entries.** The market offered ₹117k of favourable
movement — there's no shortage of opportunity. The entire loss is a **capture
problem, living in the exits.** You keep 5 of every 100 rupees handed to you.

### Where the leak is (by exit reason)
| Exit | Offered | Captured | Capture% |
|---|---|---|---|
| Target Hit | ₹14,839 | +₹9,182 | **62%** ✅ |
| Hard Exit | ₹8,300 | +₹4,500 | 54% |
| Trailing SL | ₹50,354 | +₹23,070 | 46% |
| High Water Exit | ₹13,551 | +₹1,711 | 13% |
| No Momentum Exit | ₹10,939 | +₹1,112 | 10% |
| **SL Hit** | ₹18,441 | **−₹32,757** | **−178%** ⛔ |

**`SL Hit` is the killer:** trades that reached +₹18k of MFE round-tripped all
the way through the stop to lose −₹32k. Winners turned into losers. That single
exit path is the whole book's grave.

### The ceiling (the punchline)
If you captured K% of every trade's MFE:
| Capture K% | Net P&L |
|---|---|
| 5% (now) | −₹9,612 |
| **13% (breakeven)** | ~₹0 |
| 20% | +₹7,990 |
| 30% | +₹19,725 |
| 50% | +₹43,195 |

**You need capture 5% → 13% to flip the book positive.** trend_rider already
captures **46%** — so the target is not just possible, it's proven by your own
equity engine. Every strategy improvement from here should be judged by ONE
question: *does it raise capture %?*

## Run it

```bash
cd ~/openalgo/NEWPIT
uv run python3 newpit.py                      # full capture report
uv run python3 newpit.py --strategy trend_rider   # one engine
uv run python3 newpit.py --ceiling            # the capture-vs-breakeven curve
```

Or use `run_newpit.sh`, or the VS Code task "🎯 NEWPIT — capture report".

## Why this changes everything

Old diagnosis (from P&L): "the scalpers lose money, maybe the entries are bad."
NEWPIT diagnosis: "the market offered ₹117k, you have a 5% capture rate, and the
`SL Hit` exit alone bleeds −178% — fix the exits, not the entries." It turns a
vague losing system into a precise, solvable engineering problem: **raise capture
from 5% to 13%.** That's the new north-star metric.
