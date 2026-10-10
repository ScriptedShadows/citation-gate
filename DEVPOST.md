# Citation Gate

**Tagline:** Ezio scores a fake-citation memo GREEN 100. Citation Gate stops it before it reaches the partner, and suggests the correct citation.

**Persona:** Paralegal · **Gated action:** `submit_memo_to_partner(memo)`
**Repo:** https://github.com/ScriptedShadows/citation-gate

## Inspiration
In *Mata v. Avianca* (S.D.N.Y. 2023), lawyers were sanctioned for filing a brief that cited six court cases that didn't exist. When we ran those fabricated citations through Ezio's mock, they came back **GREEN 94–100 with accuracy "not scored"**: Ezio had no public claim to check, so the score said nothing about whether the cases were real. A trust score isn't a decision. We built the decision.

## What it does
Citation Gate sits between the paralegal and the partner. Every memo goes through `POST /decide`, which returns APPROVE, HOLD_FOR_REVIEW, or BLOCK with a plain-language reason and evidence. `submit_memo_to_partner` runs **only** on APPROVE.

- **Finds every authority:** full case names, short names ("Gideon", "Sullivan"), bare reporter cites ("372 U.S. 335"), "vs." spellings, and parallel cites.
- **Checks it against the firm's lists:** a known-fabricated case gives **BLOCK**; a wrong year or reporter page on a trusted case gives **BLOCK**; a case on neither list gives **HOLD** (we can't assume it exists or that it doesn't).
- **Holding fingerprints (our core idea):** when the server starts, we compute each trusted case's distinctive vocabulary from the firm's own one-line holdings. A memo that gives Gideon the exclusionary rule is caught as *"describes Mapp v. Ohio, not Gideon."* We also catch overstated holdings ("all … without exception" when the firm records "unless …"), dropped requirements ("without suspicion" for Terry) and reversals (admissible vs inadmissible). No practice wording is hard-coded.
- **Uses Ezio where it can see:** a contradicted claim gives BLOCK (e.g. "the Fifth Amendment guarantees a speedy trial"); a supported claim needs no second opinion; tone, bias or agreement below 60 gives HOLD, even under a GREEN tier.
- **Corrects, not just rejects:** for a misstated cite, year, or misattributed holding, the gate returns the fix from the trusted list. One click applies it and re-sends the memo through the gate: blocked → fixed → approved, live.
- **Fails safe, keeps checking:** if Ezio is down, slow, rate-limited, or malformed, the citation check still runs, so fabricated cases are still blocked, and nothing is ever approved without Ezio.
- **Human in the loop:** held memos can be released or returned with a required, logged reason. Every decision goes to `audit_log.jsonl`, every action to `actions.log`, every review to `review_log.jsonl`.

## The persona page
A legal-tech review console built for the person about to hit send: the memo is shown with each citation highlighted and numbered (hover for evidence), an inspector with the verdict, the consequence of approving, and a receipt showing whether `submit_memo_to_partner()` ran; a six-step check pipeline with real per-step results; Ezio's signals; one-click fixes; a hold/release review step; a gate simulation where memos flow Drafts → Gate → Partner's inbox and blocked ones visibly stop at the gate; a filterable audit trail; and a ⌘K command palette. Fully offline; it only calls the wrapper's `POST /decide`.

## Results
- Practice set: **8/8 bad caught, 0/8 false blocks, 16/16 exact**
- Failure drills: **15/15** (down, rate-limited, server error, malformed, unavailable, slow)
- 29/29 additional unseen stretch-style memos decided correctly (parallel cites, short names, near-miss cites, misattributed and overstated holdings)
- **Final held-out run: 50.0 / 50 measured**: core 6/6 caught, 0/6 false blocks, 12/12 exact; stretch 6/6; failure drills 15/15 (`results/Citation Gate.json`, `results/Citation Gate.report.md`)

## How we built it
Python 3 standard library only, on the provided Approval-Gate starter. Logic in `wrapper/wrapper.py` (`check_paralegal`), policy in `wrapper/policy.json`, review and audit endpoints in `wrapper/serve.py`, UI in `wrapper/ui/persona.html` (vanilla JS, no build step). One-page decision policy: `DECISION_POLICY.md`.

## Run it
```bash
git clone https://github.com/ScriptedShadows/citation-gate && cd citation-gate
python3 gate.py serve --persona paralegal       # opens the persona page
python3 gate.py practice --persona paralegal    # practice set + failure drills
```

## What's next
Pull authorities from a real citator (CourtListener, Westlaw) instead of a 10-case list; per-paragraph decisions for long briefs; Bluebook format checks; plug into the firm's DMS so the gate runs on "Send to partner".
