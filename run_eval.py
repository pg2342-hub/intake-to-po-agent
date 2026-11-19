"""Grade the agent against a labeled request set.

  python run_eval.py                       # 25-request eval set, phase "after"
  python run_eval.py --set dev             # 12-request set used while writing the rules
  python run_eval.py --phase month1        # pilot's first month: nothing auto-orders
  python run_eval.py --show R07            # full trace and RFQ emails for one request

A route is "unsafe" when the agent would raise a PO on its own but the label says a person
had to look first. Unsafe autos are the number that decides whether autonomy expands.
"""
import argparse, json, os, sys
from agent.pipeline import decide, HERE

p = argparse.ArgumentParser()
p.add_argument("--set", default="eval", choices=["eval", "dev", "holdout"])
p.add_argument("--phase", default="after", choices=["after", "month1"])
p.add_argument("--reader", default="rules", choices=["rules", "claude"])
p.add_argument("--show")
p.add_argument("--json")
a = p.parse_args()

fn = {"eval": "eval_requests.jsonl", "dev": "dev_requests.jsonl", "holdout": "holdout_requests.jsonl"}[a.set]
reqs = [json.loads(l) for l in open(os.path.join(HERE, "data", fn)) if l.strip()]

if a.show:
    r = next(x for x in reqs if x["id"] == a.show)
    d = decide(r, a.phase, a.reader)
    print(r["text"]); print(json.dumps({k: v for k, v in d.items() if k != "rfqs"}, indent=2))
    for e in d["rfqs"]: print("\n" + e)
    sys.exit()

rows, route_ok, cat_ok, flag_ok, unsafe, extra_touch = [], 0, 0, 0, 0, 0
for r in reqs:
    d = decide(r, a.phase, a.reader)
    exp = dict(r["expected"])
    if a.phase == "month1" and exp["route"] == "AUTO_PO":
        exp["route"] = "BUYER_APPROVE"
    ok_r = d["route"] == exp["route"]
    ok_c = d["category"] == exp["category"]
    ok_f = set(exp["flags"]) <= set(d["flags"])
    route_ok += ok_r; cat_ok += ok_c; flag_ok += ok_f
    if d["route"] == "AUTO_PO" and exp["route"] != "AUTO_PO": unsafe += 1
    if exp["route"] == "AUTO_PO" and d["route"] != "AUTO_PO": extra_touch += 1
    rows.append((r["id"], d["route"], exp["route"], ok_r and ok_c and ok_f, d["why"]))

for i, got, want, ok, why in rows:
    print(f"{i}  {'PASS' if ok else 'FAIL'}  got {got:<15} want {want:<15} {why[:90]}")
n = len(reqs)
auto = sum(1 for r in rows if r[1] == "AUTO_PO")
print(f"\n{a.set} set, phase {a.phase}, reader {a.reader}: {n} requests")
print(f"  all-correct cases     {sum(r[3] for r in rows)} of {n}")
print(f"  route correct         {route_ok} of {n}")
print(f"  category correct      {cat_ok} of {n}")
print(f"  expected flags raised {flag_ok} of {n}")
print(f"  auto-PO                {auto} of {n}")
print(f"  unsafe auto-PO         {unsafe}   (agent ordered alone where a person had to look)")
print(f"  extra buyer touches    {extra_touch}   (agent asked a person where it could have ordered)")
if a.json:
    json.dump({"set": a.set, "phase": a.phase, "n": n, "all_correct": sum(r[3] for r in rows), "route_correct": route_ok,
               "category_correct": cat_ok, "flags_correct": flag_ok, "auto": auto, "unsafe_auto": unsafe,
               "extra_touches": extra_touch, "rows": rows}, open(a.json, "w"), indent=1)
