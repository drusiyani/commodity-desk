"""
How Claude learns from its own record. Three parts:

1. Which inputs actually help (every hourly run, no Claude call). When Claude makes a bullish or bearish call on a
   market, the direction each input pointed at that moment is saved with the call: Kronos (chance of a rise above or
   below 50%), the news (Claude's own bullish / bearish comments on that market's headlines in the last 24 hours),
   the long term (price above or below its 200-day average), the other bots' consensus (score beyond +/-10), the lab
   strategies (their signals) and the core holding (always long: a buy-and-hold baseline). Once the call is checked
   24 hours later, each input was right or wrong. The scorecard counts how often each one pointed the right way, and
   how often when Claude leaned on it (gave it at least LEAN of that decision in its own influence split).

2. Trade journal (weekly, Sunday, in the strategy lab run; one Claude call). Claude reviews every trade it closed that
   week and every bullish or bearish call that was checked: what it expected, what actually happened, which inputs
   were right or wrong, and whether a loss came from bad reasoning or just bad luck (and a win from good reasoning or
   good luck).

3. Lessons (the same weekly call). From the journal, Claude keeps a short list of lessons, each backed by the trades
   and calls that support it. Every week it keeps, updates, merges or deletes them in the light of the new reviews.
   A lesson needs at least MIN_SUPPORT supporting trades or calls (counted across weeks) before it goes into the
   trading prompt; until then it is a candidate. At most MAX_LESSONS are kept. The current lessons go into Claude's
   trading prompt on every run.
"""
import json
import math
import os
import time

import requests

INPUTS = {"kronos": "Kronos", "news": "News", "long_term": "Long term and seasonality", "consensus": "Bots' consensus",
          "lab": "Lab strategies", "core": "Core holding (always long)"}
LEAN = 20             # Claude "leaned on" an input when it gave it at least 20% of the decision
MIN_SUPPORT = 3       # a lesson needs at least 3 supporting trades or calls
MAX_LESSONS = 12
MAX_CANDIDATES = 12
WEEK = 7 * 86400
MAX_CALLS = 60        # the journal reviews at most this many checked calls a week (all closed trades always)
JOURNAL_THINKING = 6000
VERDICTS = ("bad_reasoning", "bad_luck", "good_reasoning", "good_luck")


def sign(x):
    return 0 if not x else (1 if x > 0 else -1)


# ---------- 1. which inputs actually help ----------
def input_directions(sym, kronos=None, news=(), history=None, bots=None, sigs=None, now=None):
    """Which way each input pointed for one market at decision time: +1 up, -1 down, 0 no view."""
    out = {}
    f = (kronos or {}).get(sym)
    out["kronos"] = sign(f["prob_up"] - 0.5) if f else 0
    now = now or time.time()
    takes = [n["claude"]["impact"] for n in news if sym in (n.get("symbols") or [n.get("symbol")]) and n.get("claude")
             and n["claude"].get("t", 0) >= now - 86400]
    out["news"] = sign(takes.count("bullish") - takes.count("bearish"))
    st = ((history or {}).get(sym) or {}).get("stats") or {}
    out["long_term"] = sign(st["last"] - st["ma200"]) if st.get("last") and st.get("ma200") else 0
    score = ((bots or {}).get(sym) or {}).get("score")
    out["consensus"] = sign(score) if score is not None and abs(score) >= 10 else 0
    lab = [s.get(sym, {}).get("entry") for s in (sigs or {}).values()]
    out["lab"] = sign(sum(1 if e == "long" else -1 if e == "short" else 0 for e in lab))
    out["core"] = 1
    return out


def scorecard(calls):
    """For each input: how many checked calls it had a view on, how often it was right, and the same when Claude
    leaned on it (influence >= LEAN), plus how often Claude's own call was right then."""
    done = [c for c in calls if c.get("checked") and c.get("inputs") and c.get("chg") is not None]
    rows = []
    for k, name in INPUTS.items():
        pointed = [c for c in done if c["inputs"].get(k)]
        hit = [c for c in pointed if sign(c["chg"]) == c["inputs"][k]]
        leaned = [c for c in pointed if (c.get("influence") or {}).get(k, 0) >= LEAN] if k != "core" else []
        rate = lambda xs, ys: round(len(xs) / len(ys), 3) if ys else None
        rows.append({"input": k, "name": name, "pointed": len(pointed), "right": rate(hit, pointed),
                     "leaned": len(leaned) if k != "core" else None,
                     "right_leaned": rate([c for c in leaned if sign(c["chg"]) == c["inputs"][k]], leaned),
                     "claude_right_leaned": rate([c for c in leaned if c.get("right")], [c for c in leaned if c.get("right") is not None])})
    return {"t": int(time.time()), "calls": len(done), "lean": LEAN, "rows": rows}


def scorecard_text(card):
    if not card or not card.get("calls"):
        return "No checked calls with recorded inputs yet."
    p = lambda x: "n/a" if x is None else f"{x:.0%}"
    lines = [f"Over {card['calls']} checked calls (right = pointed the way price went over the next 24 hours; "
             f"'leaned' = you gave it at least {card['lean']}% of the decision):"]
    for r in card["rows"]:
        lines.append(f"- {r['name']}: right {p(r['right'])} of {r['pointed']} times it had a view"
                     + (f"; when you leaned on it ({r['leaned']} calls) it was right {p(r['right_leaned'])} and your call "
                        f"was right {p(r['claude_right_leaned'])}" if r["leaned"] else ""))
    return "\n".join(lines)


def lessons_text(lessons):
    kept = (lessons or {}).get("lessons") or []
    if not kept:
        return "None yet: your weekly trade journal hasn't produced a lesson with at least 3 supporting trades or calls."
    return "\n".join(f"- {x['text']} (supported by {x['support']} trades or calls)" for x in kept)


# ---------- 2 and 3. the weekly journal and lessons ----------
def trade_id(t):
    return f"t{t['t']}-{t['symbol']}"


def call_id(c):
    return f"c{c['t']}-{c['symbol']}"


def week_items(trades, decisions, calls, prices, now):
    """This week's closed trades in Claude's own account (each with the trade that opened it and that decision's
    context) and its checked bullish or bearish calls."""
    since = now - WEEK
    by_t = {d["t"]: d for d in decisions}
    own = [t for t in trades if t.get("source") not in ("core",) and t.get("action") != "ROLL"]
    items = []
    for t in own:
        if t.get("pnl") is None or t["t"] < since:
            continue
        opens = [o for o in own if o["symbol"] == t["symbol"] and o["action"] in ("BUY", "SHORT") and o["t"] <= t["t"]
                 and o.get("source") == "claude"]
        o = opens[-1] if opens else {}
        d = by_t.get(o.get("t"), {})
        note = next((m.get("note") for m in d.get("markets", []) if m.get("symbol") == t["symbol"]), None)
        bars = [b for b in (prices.get(t["symbol"], {}) or {}).get("bars", []) if o.get("t", t["t"]) <= b["time"] <= t["t"]]
        items.append({"id": trade_id(t), "kind": "trade", "market": t["name"], "side": o.get("side") or "long",
                      "opened": o.get("t"), "closed": t["t"], "entry": o.get("price"), "exit": t["price"],
                      "stop": o.get("stop"), "target": o.get("target"), "pnl": t["pnl"], "r": t.get("r"),
                      "how_closed": t.get("exit") or t.get("source"), "reason": o.get("reason"),
                      "thesis": o.get("thesis"), "opposite": o.get("opposite"), "invalidation": o.get("invalidation"),
                      "confidence": o.get("confidence"), "decision_summary": d.get("summary"), "market_note": note,
                      "influence": d.get("influence"),
                      "path": {"high": max(b["high"] for b in bars), "low": min(b["low"] for b in bars)} if bars else None})
    checked = [c for c in calls if c.get("checked") and c.get("right") is not None and since <= c["t"] + 86400 <= now]
    for c in checked[-MAX_CALLS:]:
        d = by_t.get(c["t"], {})
        note = next((m.get("note") for m in d.get("markets", []) if m.get("symbol") == c["symbol"]), None)
        items.append({"id": call_id(c), "kind": "call", "market": c["symbol"], "bias": c["bias"], "score": c.get("score"),
                      "price": c["price"], "change_24h": c.get("chg"), "right": c["right"], "market_note": note,
                      "inputs": c.get("inputs"), "influence": c.get("influence") or d.get("influence")})
    return items


def journal_prompt(items, lessons, card):
    current = (lessons or {}).get("lessons", []) + (lessons or {}).get("candidates", [])
    cur = "\n".join(f"- [{x['id']}] {x['text']} (support so far: {x['support']}{'' if x['support'] >= MIN_SUPPORT else ', a candidate'})"
                    for x in current) or "none yet"
    return f"""You trade a PAPER commodity account (no real money). Once a week you review your own record honestly,
to learn from it. Below is every trade you closed this week and every bullish or bearish call of yours that was checked
24 hours later, with what you knew and thought at the time.

For EACH item, write a short review:
- "expected": what you expected to happen, in one sentence (from your reason, thesis and notes at the time);
- "happened": what actually happened, in one sentence (the result, how price moved, how it closed);
- "inputs_right" / "inputs_wrong": which inputs pointed the right or wrong way. Inputs: kronos, news, long_term,
  consensus, lab, core, risk;
- "verdict": for a loss or a wrong call, "bad_reasoning" (the case was weak, ignored a clear warning or leaned on an
  input with a poor record) or "bad_luck" (the case was sound and something unforeseeable moved price); for a win or
  a right call, "good_reasoning" or "good_luck". Be honest: a loss is not automatically bad reasoning, and a win is
  not automatically good reasoning;
- "why": one or two sentences.

Then update your LESSONS: short, specific, testable habits to keep or avoid (for example "I overweight bullish news
in oil"). Current lessons and candidates:
{cur}
For each lesson, say "keep", "update" (new wording), "delete" (the evidence turned against it), or "new", and list
the ids of THIS WEEK's items that support it in "support_ids" (only items that genuinely support it; none is fine).
Merge overlapping lessons (delete one, update the other). A lesson only enters your trading prompt once at least
{MIN_SUPPORT} trades or calls support it across weeks; at most {MAX_LESSONS} lessons are kept.

How each input has done on checked calls so far:
{scorecard_text(card)}

This week's items:
{json.dumps(items, indent=1)}

Reply with ONLY a JSON object:
{{"reviews": [{{"id": "...", "expected": "...", "happened": "...", "inputs_right": ["..."], "inputs_wrong": ["..."],
              "verdict": "bad_reasoning|bad_luck|good_reasoning|good_luck", "why": "..."}}],
  "lessons": [{{"id": "existing id, or new", "action": "keep|update|delete|new", "text": "the lesson", "support_ids": ["..."]}}],
  "summary": "two or three sentences on the week: what went right, what went wrong, what you'll do differently"}}"""


def ask(prompt, model, thinking=JOURNAL_THINKING):
    key = os.environ["ANTHROPIC_API_KEY"]
    body = {"model": model, "max_tokens": thinking + 12000, "thinking": {"type": "enabled", "budget_tokens": thinking},
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=600)
    if r.status_code == 400 and "thinking" in r.text:
        body.pop("thinking")
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=600)
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def apply_reviews(items, reply, now):
    """Claude's reviews, checked against this week's items (unknown ids and bad verdicts are dropped)."""
    by_id = {x["id"]: x for x in items}
    out = []
    for r in reply.get("reviews") or []:
        if not isinstance(r, dict) or r.get("id") not in by_id:
            continue
        it = by_id[r["id"]]
        clip = lambda v, n=300: str(v or "")[:n]
        lst = lambda v: [str(x)[:20] for x in v][:7] if isinstance(v, list) else []
        out.append({"id": r["id"], "kind": it["kind"], "market": it["market"], "t": now,
                    "result": it.get("pnl") if it["kind"] == "trade" else it.get("right"),
                    "r": it.get("r"), "expected": clip(r.get("expected")), "happened": clip(r.get("happened")),
                    "inputs_right": lst(r.get("inputs_right")), "inputs_wrong": lst(r.get("inputs_wrong")),
                    "verdict": r.get("verdict") if r.get("verdict") in VERDICTS else None, "why": clip(r.get("why"), 400)})
    return out


def apply_lessons(lessons, reply, valid_ids, now):
    """Merge Claude's lesson changes into the stored list. Support accumulates across weeks and only counts ids from
    this week's items. Lessons with at least MIN_SUPPORT go into the prompt (at most MAX_LESSONS, best supported
    first); the rest stay candidates. Returns the new state with this week's changes listed."""
    old = {x["id"]: x for x in (lessons or {}).get("lessons", []) + (lessons or {}).get("candidates", [])}
    seq = (lessons or {}).get("seq", 0)
    changes, seen = [], set()
    for x in reply.get("lessons") or []:
        if not isinstance(x, dict):
            continue
        action = x.get("action")
        support = [i for i in (x.get("support_ids") or []) if i in valid_ids]
        text = str(x.get("text") or "").strip()[:240]
        if x.get("id") in old and action in ("keep", "update", "delete"):
            cur = old[x["id"]]
            seen.add(cur["id"])
            if action == "delete":
                changes.append({"id": cur["id"], "action": "deleted", "text": cur["text"]})
                old.pop(cur["id"])
                continue
            new_support = [i for i in support if i not in cur.get("ids", [])]
            if action == "update" and text and text != cur["text"]:
                changes.append({"id": cur["id"], "action": "updated", "text": text, "was": cur["text"]})
                cur["text"] = text
            elif new_support:
                changes.append({"id": cur["id"], "action": "more support", "text": cur["text"], "added": len(new_support)})
            cur["ids"] = (cur.get("ids", []) + new_support)[-200:]
            cur["support"] = cur.get("support", 0) + len(new_support)
            cur["updated"] = now
        elif action == "new" and text:
            seq += 1
            lid = f"L{seq}"
            old[lid] = {"id": lid, "text": text, "support": len(support), "ids": support, "created": now, "updated": now}
            seen.add(lid)
            changes.append({"id": lid, "action": "new", "text": text})
    ranked = sorted(old.values(), key=lambda x: (-x["support"], -x.get("updated", 0)))
    kept = [x for x in ranked if x["support"] >= MIN_SUPPORT][:MAX_LESSONS]
    dropped = [x for x in ranked if x["support"] >= MIN_SUPPORT][MAX_LESSONS:]
    for x in dropped:
        changes.append({"id": x["id"], "action": "dropped (more than 12)", "text": x["text"]})
    cands = [x for x in ranked if x["support"] < MIN_SUPPORT][:MAX_CANDIDATES]
    before = {x["id"] for x in (lessons or {}).get("lessons", [])}
    for x in kept:
        if x["id"] not in before and not any(c["id"] == x["id"] and c["action"] == "new" for c in changes):
            changes.append({"id": x["id"], "action": "now in the prompt", "text": x["text"]})
    hist = ((lessons or {}).get("history") or []) + [{"t": now, "changes": changes}]
    return {"t": now, "seq": seq, "lessons": kept, "candidates": cands, "changes": changes, "history": hist[-30:]}


def run_week(E, now=None, model=None):
    """The weekly journal and lesson update: one Claude call. E is the engine module (storage and the model)."""
    now = now or int(time.time())
    trades, decisions = E.load("trades.json", []), E.load("decisions.json", [])
    calls, lessons = E.load("calls.json", []), E.load("lessons.json", {})
    journal = E.load("journal.json", {"weeks": []})
    items = week_items(trades, decisions, calls, E.load("prices.json", {}), now)
    if not items:
        print("Journal: nothing closed or checked this week, no Claude call")
        journal["weeks"].append({"t": now, "items": 0, "summary": "Nothing closed or checked this week.", "reviews": []})
        E.save("journal.json", {"weeks": journal["weeks"][-26:]})
        return journal
    reply = ask(journal_prompt(items, lessons, scorecard(calls)), model or E.MODEL)
    reviews = apply_reviews(items, reply, now)
    lessons = apply_lessons(lessons, reply, {x["id"] for x in items}, now)
    week = {"t": now, "items": len(items), "summary": str(reply.get("summary") or "")[:800], "reviews": reviews,
            "model": model or E.MODEL, "prompt_v": E.PROMPT_VERSION}
    journal["weeks"] = (journal["weeks"] + [week])[-26:]
    E.save("journal.json", journal)
    E.save("lessons.json", lessons)
    print(f"Journal: {len(reviews)} reviews, {len(lessons['lessons'])} lessons in the prompt, "
          f"{len(lessons['candidates'])} candidates, {len(lessons['changes'])} changes")
    return journal


if __name__ == "__main__":
    import engine
    run_week(engine)
