# Intake-to-PO agent for small indirect purchases

A small, runnable version of the agent I would pilot at one manufacturing plant: an employee types a purchase request in plain language, and the agent reads it, picks a buying channel (contract catalog or quotes), runs the plant's buying policy on the result, and routes it.

Python 3.10+, standard library only. No API key needed.

```
python run_eval.py                  # 25-request eval set
python run_eval.py --set holdout    # 10 requests written after the last code change, run once
python run_eval.py --phase month1   # pilot month 1: a buyer approves every award
python run_eval.py --show R09       # full trace and RFQ emails for one request
```

## What it decides

| Route | When |
|---|---|
| AUTO_PO | Contract or preferred supplier, under the $2,500 auto limit, every check passed (only after month 1) |
| BUYER_APPROVE | An urgent (line down) request, sent with the fastest on-time quote, or a check fired (over the limit, price up more than 10% on the last PO, fewer than 3 quotes on a $1,000+ buy, expired insurance certificate for on-site work, need-by date no quote can meet, non-preferred supplier) |
| CLARIFY | The request is missing the item, quantity, or cost center; the agent asks the requester |
| REDIRECT | It belongs to another team: IT hardware and software, capital equipment, or a chemical EHS has not reviewed |
| HOLD_DUPLICATE | An open PO for the same item from the same requester already exists |
| ESCALATE | Looks like one need split into small orders to stay under the limit |

## How it is built

The reader (`agent/parse.py`) only describes the request: category, quantity, need-by date, whether a supplier works on site. Everything that touches money (channel, quotes, checks, route) is plain code in `agent/pipeline.py`. A wrong read shows up as a wrong field that a check or a buyer can catch, not as a silent purchase.

The reader here is a keyword reader so results are reproducible. `--reader claude` swaps in a Claude call with the same output shape (needs `ANTHROPIC_API_KEY` and the `anthropic` package). It is untested, and none of the results below use it.

## Results

The number that matters is **unsafe auto-PO**: the agent ordering on its own where a person had to look first.

| Set | Routes correct | Unsafe auto-PO | Notes |
|---|---|---|---|
| Eval, first run (labels written before the agent saw them) | 21 of 25 | 1 | "heat treated wood crates 48x40" matched the catalog's "wood pallet 48x40 heat treated" and would have auto-ordered pallets |
| Eval, after fixes | 25 of 25 | 0 | Fixes: catalog match needs the item's noun, whole-word keywords ("gearbox" had matched "box"), equipment over the capex line goes to capex, last supplier always gets the RFQ |
| Held-out, run once after the fixes | 9 of 10 (8 of 10 fully correct) | 0 | "welding helmets" is not in the reader's vocabulary, so the agent asked the requester instead of sending it to the buyer; "packing supplies" got the right route (ask the requester) but no category |
| Eval, month-1 phase | 25 of 25 | 0 | Nothing auto-orders; every award goes to a buyer with the agent's recommendation |

After the held-out run, one change was made from review: urgent (line down) requests no longer wait for three quotes and go to the buyer with the fastest on-time quote. Every set's routes are unchanged by it.

Raw outputs: `results_eval_first_run.json`, `results_eval.json`, `results_holdout.json`.

What this shows and does not show: the policy checks and routing behave as designed on cases written to test them, and the reader's misses fail toward asking a person. It does not show accuracy on a real plant's requests. That is what a pilot measures, on the customer's own request history, before any autonomy is turned on.

## Data

Everything in `data/` is synthetic: suppliers (invented names), catalog prices, PO history, policy limits, and supplier quote replies. The policy numbers are examples to be replaced by the customer's own policy in week 1. `dev_requests.jsonl` (12) was used while writing the rules; `eval_requests.jsonl` (25) and `holdout_requests.jsonl` (10) carry hand-written labels.
