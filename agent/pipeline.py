"""Intake-to-PO pipeline for low-value indirect purchases at one plant.

read request -> pick buying channel (catalog, quotes) -> run policy checks -> route.

Routes
  AUTO_PO         the agent would raise the PO itself (only after the pilot's first month)
  BUYER_APPROVE   a buyer approves the agent's recommended award and PO
  CLARIFY         the agent asks the requester a question before doing anything
  REDIRECT        the request belongs to another team (IT, capex, EHS)
  HOLD_DUPLICATE  an open PO already covers it; the agent asks before ordering again
  ESCALATE        looks like one need split into small orders; a buyer must look

Phase "month1": every AUTO_PO is shown to a buyer as BUYER_APPROVE (the pilot's first month).
Phase "after":  AUTO_PO goes through for preferred or contract suppliers under the limit.
"""
import datetime as dt
import json
import os
import re

from .parse import read_rules, read_claude

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    with open(os.path.join(HERE, "data", name)) as f:
        return json.load(f)


POLICY = load("policy.json")
SUP = load("suppliers.json")
HISTORY = load("po_history.json")["pos"]
SUPPLIERS = {s["id"]: s for s in SUP["suppliers"]}
CATALOG = {c["sku"]: c for c in SUP["catalog"]}


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


def _similar(a, b):
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / max(min(len(ta), len(tb)), 1)


def _quotes(req_id):
    path = os.path.join(HERE, "data", "quotes", f"{req_id}.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)["replies"]


def rfq_email(req, read, supplier):
    need = read["need_by"] or "your standard lead time"
    return (f"To: {supplier['name']}\nSubject: Request for quote, {req['id']}\n\n"
            f"Please quote the following for delivery to the plant by {need}: {req['text']} "
            f"Include unit price, freight, lead time, and how long the quote is valid.")


def decide(req, phase="after", reader="rules"):
    trace, flags = [], []
    if reader == "claude":
        read = read_claude(req, SUP["catalog"], POLICY["categories"])
    else:
        read = read_rules(req, SUP["catalog"])
    trace.append(f"read: category={read['category']}, qty={read['qty']}, need_by={read['need_by']}, "
                 f"onsite={read['onsite']}, catalog={read['catalog_sku']}")
    submitted = dt.date.fromisoformat(req["submitted"])

    def out(route, why, award=None, rfqs=None):
        if route == "AUTO_PO" and phase == "month1":
            route, why = "BUYER_APPROVE", "first month: buyer approves every award. Agent recommends: " + why
        return {"id": req["id"], "route": route, "why": why, "category": read["category"],
                "flags": flags, "award": award, "rfqs": rfqs or [], "trace": trace}

    # 1. Can the request be acted on at all?
    if not req.get("cost_center"):
        read["missing"].append("cost center")
    if read["missing"]:
        flags.append("missing_info")
        return out("CLARIFY", "ask the requester for " + ", ".join(read["missing"]))

    # 2. Does it belong to procurement?
    cat = read["category"]
    if cat in POLICY["redirect_categories"]:
        flags.append("redirect")
        if read["estimate"] and read["estimate"] >= POLICY["capex_threshold"]:
            flags.append("capex_size")
        return out("REDIRECT", POLICY["redirect_categories"][cat])
    if read["estimate"] and read["estimate"] >= POLICY["capex_threshold"] and not read["onsite"]:
        flags.append("capex_size")
        return out("REDIRECT", "over the capex threshold; " + POLICY["redirect_categories"]["Capital equipment"])
    if cat == "Chemicals and lubricants":
        known = any(c.lower() in req["text"].lower() for c in SUP["approved_chemicals_with_sds"])
        if not known:
            flags.append("new_chemical")
            return out("REDIRECT", POLICY["hazmat_review"])

    # 3. Is it already on order?
    for po in HISTORY:
        age = (submitted - dt.date.fromisoformat(po["date"])).days
        if po["status"] == "open" and 0 <= age <= 14 and po["requester"] == req["requester"] \
                and _similar(po["item"], req["text"]) >= 0.6:
            flags.append("duplicate")
            return out("HOLD_DUPLICATE", f"{po['po']} ({po['item']}) is still open; confirm a second order is needed")

    # 4. Buying channel
    award, rfqs = None, []
    if read["catalog_sku"]:
        c = CATALOG[read["catalog_sku"]]
        s = SUPPLIERS[c["supplier"]]
        award = {"supplier": s["id"], "supplier_name": s["name"], "channel": "contract catalog",
                 "amount": round(c["unit_price"] * read["qty"], 2), "lead_days": s["lead_days"], "preferred": True}
        trace.append(f"channel: catalog {c['sku']} at ${c['unit_price']} x {read['qty']}")
    else:
        active = [s for s in SUP["suppliers"] if cat in s["categories"] and s["status"] == "active"]
        last = [SUPPLIERS[po["supplier"]] for po in HISTORY if _similar(po["item"], req["text"]) >= 0.6]
        invited = (last + [s for s in active if s not in last])[:4]  # last supplier for this item always gets the RFQ
        rfqs = [rfq_email(req, read, s) for s in invited]
        replies = [r for r in _quotes(req["id"]) if r["supplier"] in {s["id"] for s in invited} and r.get("valid", True)]
        trace.append(f"channel: RFQ to {[s['id'] for s in invited]}, {len(replies)} valid replies")
        if not replies:
            flags.append("no_quotes")
            return out("BUYER_APPROVE", "no valid quotes came back; buyer sources it", rfqs=rfqs)
        need_by = dt.date.fromisoformat(read["need_by"]) if read["need_by"] else None
        on_time = [r for r in replies if not need_by or submitted + dt.timedelta(days=r["lead_days"]) <= need_by]
        if not on_time:
            flags.append("need_by_unmet")
            return out("BUYER_APPROVE", "no quote meets the need-by date; buyer expedites or resets the date", rfqs=rfqs)
        if read["urgent"]:  # line down: take the fastest on-time quote, no wait for three
            best = min(on_time, key=lambda r: (r["lead_days"], r["total"]))
        else:
            best = min(on_time, key=lambda r: r["total"])
        s = SUPPLIERS[best["supplier"]]
        award = {"supplier": s["id"], "supplier_name": s["name"], "channel": "quotes", "amount": best["total"],
                 "lead_days": best["lead_days"], "preferred": cat in s["preferred_for"],
                 "quotes_received": len(replies), "unit_price": best.get("unit_price")}
        if best["total"] >= POLICY["three_quote_threshold"] and len(replies) < POLICY["min_quotes"] \
                and not read["urgent"]:
            flags.append("too_few_quotes")
    trace.append(f"award: {award['supplier_name']} ${award['amount']:,.2f}")

    # 5. Policy checks on the award
    s = SUPPLIERS[award["supplier"]]
    if s["status"] != "active":
        flags.append("supplier_inactive")
    if read["onsite"]:
        work_day = dt.date.fromisoformat(read["need_by"]) if read["need_by"] else submitted
        if not s["coi_expires"] or dt.date.fromisoformat(s["coi_expires"]) < work_day:
            flags.append("coi_expired")
    if award.get("unit_price"):
        for po in HISTORY:
            if po["supplier"] == s["id"] and _similar(po["item"], req["text"]) >= 0.6:
                if award["unit_price"] > po["unit_price"] * (1 + POLICY["max_price_increase_vs_last_po"]):
                    flags.append("price_up_vs_last_po")
                    trace.append(f"last price {po['po']}: ${po['unit_price']} vs quoted ${award['unit_price']}")
                break
    if award["amount"] > POLICY["auto_po_limit"]:
        flags.append("over_auto_limit")
    recent = [po for po in HISTORY if po["requester"] == req["requester"] and po["category"] == cat
              and po["supplier"] == s["id"] and _similar(po["item"], req["text"]) >= 0.5
              and 0 <= (submitted - dt.date.fromisoformat(po["date"])).days <= POLICY["split_window_days"]]
    if recent and award["amount"] <= POLICY["auto_po_limit"] and \
            sum(p["amount"] for p in recent) + award["amount"] > POLICY["auto_po_limit"]:
        flags.append("possible_split")
        return out("ESCALATE", f"with {', '.join(p['po'] for p in recent)} this is over the "
                               f"${POLICY['auto_po_limit']:,} limit within {POLICY['split_window_days']} days", award, rfqs)

    # 6. Route
    if read["urgent"]:
        flags.append("urgent")  # urgent buys always go to the buyer, with any other check results attached
    if flags:
        return out("BUYER_APPROVE", "checks raised: " + ", ".join(flags), award, rfqs)
    if not award["preferred"]:
        return out("BUYER_APPROVE", "best quote is from a non-preferred supplier", award, rfqs)
    return out("AUTO_PO", f"{award['channel']} award to {award['supplier_name']} for ${award['amount']:,.2f}, "
                          f"all checks passed", award, rfqs)
