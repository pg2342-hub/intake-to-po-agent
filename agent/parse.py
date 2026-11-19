"""Read a plain-language purchase request into structured fields.

Two readers share one output shape:
  * rules (default): a small keyword and pattern reader. Fully reproducible, no API key.
  * claude (optional): asks a Claude model for the same JSON. Set ANTHROPIC_API_KEY
    and pass --reader claude. Results in this repo were produced with the rules reader;
    the Claude reader is untested.

The reader only describes the request. It never decides money: channel, checks and
routing live in pipeline.py as plain code, so a wrong read shows up as a wrong field
that a check or a buyer can catch, not as a silent purchase.
"""
import datetime as dt
import json
import os
import re

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Order matters: the first category whose words match wins.
CATEGORY_WORDS = [
    ("IT hardware and software", ["laptop", "monitor", "software", "license", "licence", "printer", "tablet",
                                  "ipad", "keyboard", "headset", "docking station", "barcode scanner", "subscription"]),
    ("Capital equipment", ["new machine", "new lathe", "new press", "new compressor", "new forklift",
                           "buy a forklift", "purchase a forklift", "capital", "new cnc", "new welder"]),
    ("Uniforms and laundry", ["uniform", "coverall", "laundry", "shop towel service", "mat service"]),
    ("Chemicals and lubricants", ["grease", "solvent", "degreaser", "chemical", "thinner", "coolant",
                                  "isopropyl", "lubricant", "hydraulic oil", "gear oil", "wd-40", "epoxy", "primer",
                                  "rust inhibitor", "rust preventive", "aerosol", "sealant", "adhesive"]),
    ("Safety and PPE", ["glove", "safety glasses", "earplug", "ear plug", "respirator", "hard hat", "hi-vis",
                        "vest", "safety boot", "face shield", "fall protection", "harness"]),
    ("Cutting tools and tooling", ["end mill", "drill bit", "insert", "grinding disc", "cutoff wheel", "tap",
                                   "reamer", "abrasive", "flap disc", "saw blade"]),
    ("Packaging and shipping supplies", ["pallet", "stretch wrap", "corrugated", "box", "boxes", "crate",
                                         "packing tape", "strapping", "dunnage"]),
]
SERVICE_WORDS = ["repair", "service call", "come out", "come in", "technician", "inspection", "inspect",
                 "pest control", "cleaning crew", "deep clean", "snow removal", "landscap", "electrician",
                 "plumber", "install", "maintenance visit", "quarterly maintenance", "fix the", "rewire"]
MRO_WORDS = ["bearing", "belt", "hose", "filter", "motor", "fitting", "cable tie", "bulb", "led", "coupling",
             "valve", "sprocket", "chain", "fuse", "breaker", "battery", "caster", "seal", "o-ring", "pump",
             "light fixture", "high bay"]
EQUIPMENT_WORDS = ["compressor", "welder", "lathe", "press", "machine", "forklift", "generator", "crane", "hoist",
                   "oven", "booth", "robot"]
URGENT_WORDS = ["asap", "line down", "line is down", "urgent", "today", "right away", "emergency"]
VAGUE_WORDS = ["some stuff", "the usual", "whatever", "a few things", "stuff for", "something for"]


def _num(s):
    return float(s.replace(",", ""))


def _need_by(text, submitted):
    t = text.lower()
    if any(w in t for w in URGENT_WORDS):
        return submitted + dt.timedelta(days=1), True
    m = re.search(r"\bby (?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,? )?([a-z]{3})[a-z]*\.? (\d{1,2})\b", t)
    if m and m.group(1) in MONTHS:
        d = dt.date(submitted.year, MONTHS[m.group(1)], int(m.group(2)))
        if d < submitted:
            d = dt.date(submitted.year + 1, d.month, d.day)
        return d, False
    m = re.search(r"\bby (?:next )?(" + "|".join(WEEKDAYS) + r")\b", t)
    if m:
        target = WEEKDAYS.index(m.group(1))
        days = (target - submitted.weekday()) % 7 or 7
        if "next " + m.group(1) in t:
            days += 7
        return submitted + dt.timedelta(days=days), False
    m = re.search(r"within (\d+) (?:business )?days", t)
    if m:
        return submitted + dt.timedelta(days=int(m.group(1))), False
    m = re.search(r"next week", t)
    if m:
        return submitted + dt.timedelta(days=7 + (4 - submitted.weekday()) % 7), False
    return None, False


def _quantity(text):
    t = text.lower()
    m = re.search(r"\b(\d{1,4}(?:,\d{3})?)\s*(?:x\s+)?(?:boxes|box|packs|pack|rolls|roll|cases|case|pairs|pair|"
                  r"each|ea|units|pcs|pieces|sets|set|gallons|gal|cartridges|tubes|pallets|of|new|more|"
                  r"[a-z\-]+)", t)
    if m:
        return int(_num(m.group(1)))
    if re.search(r"\b(a|an|one)\s+(?!few\b)[a-z]", t):
        return 1
    return None


def _amount(text):
    m = re.search(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", text.lower())
    if not m:
        return None
    v = _num(m.group(1))
    return v * 1000 if m.group(2) else v


def _has(t, words):
    """Whole-word match, plural allowed ("tap" matches "taps", "box" does not match "gearbox")."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w.strip()) + r"(?:s|es)?(?![a-z0-9])", t) for w in words)


def _category(t, estimate=None, capex_threshold=5000):
    if estimate and estimate >= capex_threshold and _has(t, EQUIPMENT_WORDS) and not _has(t, SERVICE_WORDS):
        return "Capital equipment"
    for cat, words in CATEGORY_WORDS:
        if _has(t, words):
            return cat
    if _has(t, SERVICE_WORDS):
        return "Facilities services"
    if _has(t, MRO_WORDS):
        return "MRO supplies"
    return None


def _catalog_match(t, catalog):
    best, best_score = None, 0.0
    for c in catalog:
        if not _has(t, [c["noun"]]):
            continue  # "heat treated wood crates 48x40" must not match "wood pallet 48x40 heat treated"
        words = [w for w in re.findall(r"[a-z0-9\-\.]+", c["item"].lower()) if len(w) > 2 and w not in
                 ("box", "pack", "each", "per", "the", "and", "clear", "employee")]
        hits = sum(1 for w in words if w in t)
        score = hits / max(len(words), 1)
        if hits >= 2 and score > best_score:
            best, best_score = c, score
    return best if best_score >= 0.34 else None


def read_rules(req, catalog):
    text = req["text"]
    t = " " + text.lower() + " "
    submitted = dt.date.fromisoformat(req["submitted"])
    need_by, urgent = _need_by(text, submitted)
    category = _category(t, _amount(text))
    onsite = category == "Facilities services" or _has(t, SERVICE_WORDS)
    match = _catalog_match(t, catalog)
    qty = _quantity(text)
    missing = []
    if any(w in t for w in VAGUE_WORDS) or category is None:
        missing.append("what exactly is needed (item and spec)")
    if qty is None and not onsite:
        missing.append("quantity")
    return {
        "category": category,
        "item": text,
        "qty": qty,
        "estimate": _amount(text),
        "need_by": need_by.isoformat() if need_by else None,
        "urgent": urgent,
        "onsite": onsite,
        "catalog_sku": match["sku"] if match else None,
        "missing": missing,
    }


CLAUDE_PROMPT = """You read one internal purchase request at a manufacturing plant and return JSON only.
Fields: category (one of {cats}), qty (integer or null), estimate (dollars or null, only if the
requester states a price or budget), need_by (YYYY-MM-DD or null; the request was submitted {submitted}),
urgent (true if a line is down or it is needed today), onsite (true if a supplier must work on site),
missing (list of what a buyer would have to ask before ordering). Do not guess missing facts.
Request: {text}"""


def read_claude(req, catalog, categories, model="claude-sonnet-5-5"):
    import anthropic  # optional dependency
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model, max_tokens=400,
        messages=[{"role": "user", "content": CLAUDE_PROMPT.format(
            cats=", ".join(categories), submitted=req["submitted"], text=req["text"])}])
    out = json.loads(msg.content[0].text)
    t = " " + req["text"].lower() + " "
    match = _catalog_match(t, catalog)
    out.update({"item": req["text"], "catalog_sku": match["sku"] if match else None})
    return out
