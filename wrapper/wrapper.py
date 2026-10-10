"""Your wrapper. Start with check_source_of_truth() and policy.json.

Flow: call Ezio -> read its signals -> check your persona's source of truth -> apply policy
-> decision (+ reason, evidence) -> audit log -> run the mock action only if approved.

Run one input:  python3 wrapper.py customer_support "Your 30% loyalty discount applies to the Pro plan."
Score yourself: python3 evaluate.py customer_support
"""
import csv
import json
import math
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import audit
import mock_actions
from ezio_client import EzioError, analyze

HERE = Path(__file__).resolve().parent
PERSONAS = HERE.parent / "personas"
POLICY = json.loads((HERE / "policy.json").read_text())

APPROVE, HOLD, BLOCK = "APPROVE", "HOLD_FOR_REVIEW", "BLOCK"
RANK = {APPROVE: 0, HOLD: 1, BLOCK: 2}


@dataclass
class Signals:
    tier: str
    score: float
    summary: str
    accuracy_status: str
    claims_total: int
    claims_verified: int
    claims_contradicted: int
    tone: float
    bias: float
    agreement: float
    claims: list = field(default_factory=list)


@dataclass
class Finding:
    level: str
    message: str


@dataclass
class Decision:
    decision: str
    reason: str
    evidence: list
    ezio: dict
    citations: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)


def parse_signals(resp):
    try:
        verdict, dims = resp["verdict"], resp["dimensions"]
        acc = dims["accuracy"]
        if verdict["tier"] not in ("GREEN", "YELLOW", "RED") or acc["status"] not in ("scored", "not_scored", "unavailable"):
            raise ValueError("invalid tier or accuracy status")
        scores = [verdict["score"]] + [dims[d]["score"] for d in ("tone", "bias", "agreement")]
        if acc["status"] == "scored":
            scores.append(acc["score"])
        if any(isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s) or not 0 <= s <= 100 for s in scores):
            raise ValueError("scores must be finite numbers between 0 and 100")
        if not isinstance(verdict["summary"], str) or not isinstance(resp.get("claims", []), list):
            raise ValueError("invalid summary or claims")
        return Signals(
            tier=verdict["tier"], score=verdict["score"], summary=verdict["summary"],
            accuracy_status=acc["status"], claims_total=acc.get("claims_total", 0),
            claims_verified=acc.get("claims_verified", 0), claims_contradicted=acc.get("claims_contradicted", 0),
            tone=dims["tone"]["score"], bias=dims["bias"]["score"], agreement=dims["agreement"]["score"],
            claims=resp.get("claims", []),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise EzioError("malformed mock response", resp) from e


@lru_cache(maxsize=None)
def load_source_of_truth(persona):
    """Every .json and .csv file in the persona folder, keyed by file name (for example sot['policies'])."""
    data = {}
    for f in sorted((PERSONAS / persona).iterdir()):
        if f.stem in ("practice_set", "business_rules"):
            continue
        if f.suffix == ".json":
            data[f.stem] = json.loads(f.read_text())
        elif f.suffix == ".csv":
            with f.open() as handle:
                data[f.stem] = list(csv.DictReader(handle))
    return data


# ---------------------------------------------------------------- paralegal source of truth
# Everything below is derived from trusted_citations.json / known_fabricated.json at load time.
# Nothing matches practice-set wording: cases are found by name, short name, or reporter cite,
# and holdings are compared with vocabulary computed from the firm's own one-line summaries.

STOP = set("""a an the of to in on for and or by with from as at is are was were be been it its that this
which who whom whose their there they them than then so such not no nor but if into upon under over
may must can will would should could shall held holds hold holding court courts case cases rule ruled
established establishes requires require required found finds applies apply applied""".split()) - {"rule"}
REPORTER = r"(?:U\.\s?S\.|N\.\s?Y\.(?:\s?\d?d)?|F\.\s?(?:\d?d|Supp\.(?:\s?\d?d)?)|S\.\s?Ct\.|L\.\s?Ed\.(?:\s?\d?d)?|N\.E\.(?:\s?\d?d)?)"
CITE_RE = re.compile(r"\b(\d{1,4})\s+(" + REPORTER + r")\s+(\d{1,5})\b")
WORD = r"[A-Z][A-Za-z.'&\-]*"
CASE_RE = re.compile(r"\b(" + WORD + r"(?:\s+(?:" + WORD + r"|of|the|and|de|&))*)\s+v\.\s+(" + WORD + r"(?:\s+(?:" + WORD + r"|of|the|and|&))*)")
LEADING = {"under", "in", "as", "see", "per", "citing", "following", "also", "cf.", "but", "and", "the", "while", "unlike", "like"}
ADVOCACY = re.compile(r"slam dunk|\bclearly\b(?!\s+established)|\bobviously\b|\bundeniabl\w*|\bunquestionabl\w*|without (?:a|any) doubt"
                      r"|\bno doubt\b|\bguaranteed (?:win|victory|result)|\bwe guarantee\b|\bwill (?:absolutely|definitely|certainly|surely)\b|\babsolutely\b"
                      r"|can(?:'|no)t lose|\bsure win\b|\bairtight\b|\bopen and shut\b|\bdestroy\w*\b|\bamazing\b|\bincredible\b|!!", re.I)
ABSOLUTE = re.compile(r"\b(all|every|any|always|never|without exception|in all cases|no exceptions?|unlimited|absolute)\b", re.I)
QUALIFIER = re.compile(r"\b(unless|only|except|if|when|provided)\b", re.I)
NEGATION = re.compile(r"\b(not|no longer|need not|never|isn't|aren't|doesn't|don't|cannot)\b", re.I)
ANTONYMS = [("inadmissible", "admissible"), ("violates", "does not violate"), ("protected", "unprotected")]
LEGAL_CAPS = {"U.S.", "N.Y.", "LLC", "INC", "CORP", "FEDERAL"}


def _stem(w):
    w = w.lower().strip(".,;:()'\"")
    for suf in ("ing", "ed"):
        if len(w) > 5 and w.endswith(suf):
            return w[: -len(suf)]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us")):
        return w[:-1]
    return w


def _terms(text):
    return {_stem(w) for w in re.findall(r"[A-Za-z][A-Za-z\-']+", text)} - {_stem(s) for s in STOP}


def _norm(s):
    return re.sub(r"\s+", "", s).lower()


@lru_cache(maxsize=None)
def _paralegal_index():
    sot = load_source_of_truth("paralegal")
    trusted = sot["trusted_citations"]["cases"]
    fabricated = sot["known_fabricated"]["cases"]
    vocab = {c["case"]: _terms(c["holding"]) for c in trusted}
    distinct = {}
    for name, ts in vocab.items():
        others = set().union(*(v for n, v in vocab.items() if n != name))
        distinct[name] = ts - others
    def parties(name):
        p1, _, p2 = name.partition(" v. ")
        return p1, p2
    def alias(name):
        p1, p2 = parties(name)
        first = p1.split()[0]
        # "New York Times Co. v. Sullivan" is short-cited as "Sullivan"; others by the first party
        return p2.split()[0] if first in ("New", "United", "State", "People") else first
    return {
        "trusted": trusted, "fabricated": fabricated, "distinct": distinct, "parties": parties,
        "alias": {alias(c["case"]): c["case"] for c in trusted},
        "fab_alias": {parties(c["case"])[0].split()[0]: c["case"] for c in fabricated},
        "by_cite": {_norm(c["citation"]): c for c in trusted},
        "by_name": {c["case"]: c for c in trusted},
    }


def _match_known(p1, p2, cases, parties):
    p1_words = {w.strip(".,") for w in p1.split()} - {"Co", "Inc", "Corp", "Co.", "Inc."}
    p2_first = p2.split()[0].strip(".,") if p2.split() else ""
    for c in cases:
        k1, k2 = parties(c["case"])
        if k2.split()[0].strip(".,") == p2_first and any(w in p1_words for w in k1.replace(".", "").split() if len(w) > 3 and w not in ("Railroad", "Board")):
            return c["case"]
    return None


def _sentence_window(text, start, end):
    """Clause after a mention (to the next sentence end) and the clause before it."""
    after = text[end:]
    m = re.search(r"(?<!\bv)(?<!\bCo)(?<!\bInc)(?<!\bNo)(?<!\bCir)(?<![A-Z])\.\s+(?=[A-Z][a-z])|[!?]\s|\n", after)
    after = after[: m.start() + 1] if m else after
    before = text[:start]
    b = list(re.finditer(r"(?<!\bv)(?<!\bCo)(?<!\bInc)(?<![A-Z])[.!?]\s+|\n", before))
    before = before[b[-1].end():] if b else before
    return before, after


def find_case_mentions(text):
    idx = _paralegal_index()
    mentions = []
    taken = []
    def free(s, e):
        return all(e <= a or s >= b for a, b in taken)
    for m in CASE_RE.finditer(text):
        p1_words = m.group(1).split()
        while p1_words and p1_words[0].lower() in LEADING:
            p1_words.pop(0)
        if not p1_words:
            continue
        p1, p2 = " ".join(p1_words), m.group(2)
        start = m.start(1) + m.group(1).index(p1_words[0])
        fab = _match_known(p1, p2, idx["fabricated"], idx["parties"]) or next(
            (n for a, n in idx["fab_alias"].items() if a in p1_words), None)
        known = None if fab else _match_known(p1, p2, idx["trusted"], idx["parties"])
        kind = "fabricated" if fab else "trusted" if known else "unknown"
        mentions.append({"kind": kind, "case": fab or known or f"{p1} v. {p2}", "as_written": text[start:m.end()], "start": start, "end": m.end()})
        taken.append((start, m.end()))
    for a, name in list(idx["alias"].items()) + list(idx["fab_alias"].items()):
        for m in re.finditer(r"\b" + re.escape(a) + r"\b", text):
            if free(m.start(), m.end()):
                kind = "fabricated" if name in idx["fab_alias"].values() else "trusted"
                mentions.append({"kind": kind, "case": name, "as_written": a, "start": m.start(), "end": m.end()})
                taken.append((m.start(), m.end()))
    for m in CITE_RE.finditer(text):
        if not free(m.start(), m.end()):
            continue
        # a cite right after a mention belongs to that mention
        prev = [x for x in mentions if x["end"] <= m.start() and not text[x["end"]:m.start()].strip(" ,")]
        if prev:
            continue
        c = idx["by_cite"].get(_norm(m.group(0)))
        mentions.append({"kind": "trusted" if c else "unknown_cite", "case": c["case"] if c else m.group(0),
                         "as_written": m.group(0), "start": m.start(), "end": m.end()})
        taken.append((m.start(), m.end()))
    return sorted(mentions, key=lambda x: x["start"])


def check_paralegal(text):
    idx = _paralegal_index()
    findings, citations = [], []
    mentions = find_case_mentions(text)
    for i, m in enumerate(mentions):
        nxt = mentions[i + 1]["start"] if i + 1 < len(mentions) else len(text)
        before, after = _sentence_window(text, m["start"], m["end"])
        after = after[: max(0, nxt - m["end"])] if nxt - m["end"] < len(after) else after
        problems, status = [], "verified"
        if m["kind"] == "fabricated":
            status = "fabricated"
            problems.append((BLOCK, f"{m['case']} is on the firm's fabricated-citation list (Mata v. Avianca)."))
        elif m["kind"] in ("unknown", "unknown_cite"):
            status = "unverified"
            problems.append((HOLD, f"'{m['as_written']}' is on neither the trusted nor the fabricated list; a human must verify it exists and says what the memo claims."))
        else:
            c = idx["by_name"][m["case"]]
            head = after[:90]
            cite = CITE_RE.search(head)
            if cite and _norm(cite.group(0)) != _norm(c["citation"]):
                status = "misstated"
                other = idx["by_cite"].get(_norm(cite.group(0)))
                problems.append((BLOCK, f"{c['case']} is {c['citation']}, not {cite.group(0)}" + (f" (that is {other['case']})." if other else ".")))
            year = re.search(r"\((?:[^()]*?\s)?(1[6-9]\d\d|20\d\d)\)", head) or (re.search(r"\b(1[6-9]\d\d|20\d\d)\b", head) if cite else None)
            if year and int(year.group(1)) != c["year"]:
                status = "misstated"
                problems.append((BLOCK, f"{c['case']} was decided in {c['year']}, not {year.group(1)}."))
            claim = re.sub(CITE_RE, " ", after if len(_terms(after)) >= 3 else before + " " + after)
            claim = re.sub(r"\([^)]*\d{4}\)", " ", claim)
            words = _terms(claim) - _terms(c["case"])
            own = words & idx["distinct"][c["case"]]
            rival = max(((n, words & d) for n, d in idx["distinct"].items() if n != c["case"]), key=lambda x: len(x[1]))
            hold_text = c["holding"].lower()
            if len(rival[1]) >= 2 and len(rival[1]) > len(own):
                status = "misstated"
                problems.append((BLOCK, f"Holding misattributed: '{', '.join(sorted(rival[1]))}' describes {rival[0]}, not {c['case']} ({c['case']} held: {c['holding']})"))
            elif len(words) >= 3 and not own:
                status = "misstated"
                problems.append((BLOCK, f"The stated holding doesn't match the firm's summary of {c['case']}: \"{c['holding']}\""))
            if ABSOLUTE.search(claim) and QUALIFIER.search(hold_text) and not QUALIFIER.search(claim):
                status = "misstated"
                problems.append((BLOCK, f"Overstated holding: memo says '{ABSOLUTE.search(claim).group(0)}', but {c['case']} is qualified: \"{c['holding']}\""))
            for neg, pos in ANTONYMS:
                if neg in hold_text and re.search(r"\b" + pos + r"\b", claim, re.I) and not re.search(r"\b" + neg + r"\b", claim, re.I):
                    status = "misstated"
                    problems.append((BLOCK, f"Reverses {c['case']}: the firm's summary says '{neg}'."))
            if status == "verified" and NEGATION.search(claim) and not NEGATION.search(hold_text) and own:
                status = "check"
                problems.append((HOLD, f"The memo negates part of {c['case']}'s holding; confirm against: \"{c['holding']}\""))
            if status == "verified":
                problems.append((None, f"{c['case']}, {c['citation']} ({c['year']}) verified against the trusted list."))
        citations.append({"as_written": m["as_written"], "case": m["case"], "status": status,
                          "start": m["start"], "end": m["end"], "notes": [p[1] for p in problems]})
        findings += [Finding(level, msg) for level, msg in problems if level]
    tone = ADVOCACY.findall(text) + [w for w in re.findall(r"\b[A-Z]{5,}\b", text) if w not in LEGAL_CAPS]
    if tone:
        findings.append(Finding(HOLD, f"Advocacy language a partner memo shouldn't contain: {', '.join(sorted(set(t.strip() for t in tone)))}."))
    return findings, citations


def check_source_of_truth(text, context, sot):
    """Paralegal persona: citations against the firm's trusted and fabricated lists."""
    findings, _ = check_paralegal(text)
    return findings


def apply_policy(signals, findings, policy):
    decision, reasons = APPROVE, []

    def raise_to(level, reason):
        nonlocal decision
        reasons.append(reason)
        if RANK[level] > RANK[decision]:
            decision = level

    for f in findings:
        raise_to(f.level, f.message)
    for c in signals.claims:
        if str(c.get("verdict", "")).lower() == "contradicted":
            src = c.get("source") or "Ezio fact-check"
            raise_to(policy["when_claim_contradicted"], f"Ezio contradicted a claim: \"{c.get('text', '')}\" (source: {src})")
    for dim, rule in policy["dimension_minimums"].items():
        score = getattr(signals, dim)
        if score < rule["hold_below"]:
            raise_to(HOLD, f"Ezio {dim} score {score} is below {rule['hold_below']}")
    if signals.accuracy_status == "not_scored" and policy["when_accuracy_not_scored"] != "ignore":
        raise_to(policy["when_accuracy_not_scored"], "accuracy was not scored (no claims checked)")
    if signals.accuracy_status == "unavailable":
        raise_to(policy["when_accuracy_unavailable"], "Ezio's fact-check was unavailable, so non-citation claims are unchecked")
    tier_decision = policy["tier_decisions"][signals.tier]
    if tier_decision != APPROVE:
        raise_to(tier_decision, f"Ezio tier is {signals.tier} ({signals.score})")
    return reasons, decision


def summarize(decision, reasons, citations, signals):
    n = len(citations)
    ok = sum(c["status"] == "verified" for c in citations)
    if decision == APPROVE:
        head = (f"All {n} citation(s) verified against the trusted list" if n else "No case citations to verify") + \
               (f"; Ezio {signals.tier} {signals.score}" if signals else "") + ". Safe to submit."
        return head
    return ("BLOCKED: " if decision == BLOCK else "HOLD FOR REVIEW: ") + reasons[0] + \
        (f" (+{len(reasons) - 1} more issue(s))" if len(reasons) > 1 else "")


def decide(text, context=None, persona="customer_support", scenario=None, audit_log=True):
    context = context or {}
    findings, citations = check_paralegal(text) if persona == "paralegal" else (
        check_source_of_truth(text, context, load_source_of_truth(persona)), [])
    evidence = [n for c in citations for n in c["notes"]] + [f.message for f in findings if not any(f.message in n for c in citations for n in c["notes"])]
    try:
        response = analyze(text, scenario=scenario)
        signals = parse_signals(response)
    except EzioError as e:
        # Degraded mode: our citation check still runs, so a fabricated case is blocked even when Ezio is down.
        blocked = [f.message for f in findings if f.level == BLOCK]
        decision = BLOCK if blocked else POLICY["on_ezio_failure"]
        why = f"Ezio unavailable ({e}); " + (blocked[0] if blocked else "citations alone can't clear the memo, so a human must review it")
        result = Decision(decision, why, evidence + [f"Ezio error: {e}"], {"error": str(e), "response": e.response},
                          citations, {"ezio": "unavailable", "error": str(e)})
    else:
        reasons, decision = apply_policy(signals, findings, POLICY)
        for c in signals.claims:
            evidence.append(f"Ezio claim {c.get('verdict', '?')}: \"{c.get('text', '')}\"" + (f" ({c['source']})" if c.get("source") else ""))
        evidence.append(f"Ezio: tier {signals.tier} {signals.score}, accuracy {signals.accuracy_status}, tone {signals.tone}, bias {signals.bias}, agreement {signals.agreement}")
        result = Decision(decision, summarize(decision, reasons, citations, signals), evidence, response, citations,
                          {"tier": signals.tier, "score": signals.score, "accuracy": signals.accuracy_status,
                           "tone": signals.tone, "bias": signals.bias, "agreement": signals.agreement,
                           "claims": signals.claims})
    if audit_log:
        audit.record(persona, text, context, result)
    return result


def gate(text, context=None, persona="customer_support"):
    result = decide(text, context, persona)
    print(f"{result.decision}: {result.reason}")
    if result.decision == APPROVE:
        mock_actions.run_for_persona(persona, text)
    else:
        print("[GATE] action not executed")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: python3 wrapper.py <persona> "<text>" [context-json]')
    gate(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}, sys.argv[1])
