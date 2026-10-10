# Citation Gate: Decision Policy (Paralegal persona)

**Gated action:** `submit_memo_to_partner(memo)`. It runs only on APPROVE, or when a human reviewer releases a HOLD with a logged reason.
**Stakes:** In *Mata v. Avianca* (S.D.N.Y. 2023), lawyers were sanctioned for citing six cases that didn't exist. Ezio scored our fabricated-case drafts **GREEN 94–100 with accuracy not scored**: a trust score alone would have approved them.

## Signals
| # | Signal | Source | Why it's independent of the Ezio score |
|---|---|---|---|
| 1 | **Case detection**: full names (`X v. Y`), short names ("Gideon", "Sullivan"), bare reporter cites ("372 U.S. 335") | regex + aliases built from `trusted_citations.json` | Ezio doesn't see the firm's lists |
| 2 | **Fabricated list** | `known_fabricated.json` | Hard evidence of a non-existent case |
| 3 | **Cite and year check** against the trusted entry | `trusted_citations.json` | Catches near-miss citations (1813 vs 1803, page 138 vs 137) |
| 4 | **Holding check (doctrine fingerprints)**: each trusted case's distinctive vocabulary is computed from the firm's one-line holdings at startup. A memo describing case A with case B's vocabulary is a misattribution; no overlap at all is a mismatch | trusted holdings | General: no practice wording is hard-coded |
| 5 | **Overstatement / reversal**: absolute words ("all", "without exception") applied to a holding the firm records as qualified ("unless", "only"); antonym flips (admissible vs inadmissible) | trusted holdings | Catches subtle misstatements |
| 5b | **Dropped requirement**: "without suspicion" when the holding turns on suspicion | trusted holdings | Catches holdings stripped of their condition |
| 6 | **Advocacy language**: "slam dunk", "will absolutely", ALL-CAPS hype, "!!" ("clearly established" is excluded as a term of art) | rule 4 | Memos must be neutral |
| 7 | **Ezio claims[]**: contradicted gives BLOCK; supported is accepted with no second opinion | Ezio | Covers constitutional and statutory claims our lists can't |
| 8 | **Ezio dimensions**: tone / bias / agreement | Ezio | A GREEN tier can hide one bad dimension |

## Thresholds and justifications
| Rule | Decision | Why |
|---|---|---|
| Fabricated case | **BLOCK** | Rule 1; the exact Mata v. Avianca harm |
| Trusted case with wrong cite, year, or holding | **BLOCK** | Rule 2; the partner would cite a real case for something it never held |
| Case on neither list | **HOLD** | Rule 3; we can't assume it exists *or* that it doesn't |
| Ezio claim contradicted | **BLOCK** | Rule 6; a concrete, sourced error |
| Tone, bias or agreement < 60 | **HOLD** | 60 is Ezio's own RED cutoff; neutral legal writing scores well above it |
| Advocacy phrase | **HOLD** | Rule 4; a human can rewrite it, so it isn't a hard block |
| Tier YELLOW or RED with no specific evidence | **HOLD** | The tier says something is off but not what; blocks need specific evidence |
| Accuracy `not_scored` | ignored | Rule 5; our lists are the accuracy check for case law; a memo with no citations is fine |
| Accuracy `unavailable` | **HOLD** | Public-law claims would go unchecked; citation BLOCKs still apply |

Decisions take the most severe level of all findings. Every finding is attached as evidence.

## Failure behavior (fail closed, but keep checking)
- Ezio **down / 5xx / malformed / timeout**: the citation check **still runs**. A fabricated or misstated case is BLOCKED with evidence, and everything else is HOLD. We never APPROVE without Ezio.
- **Rate limit / transient 503**: bounded retries honoring `Retry-After` (4 attempts, 45 s budget, under the 60 s harness limit).
- **Slow**: 15 s per-attempt timeout; the UI tells the user it's retrying and never sends while waiting.

## Audit
Every decision goes to `audit_log.jsonl` (input, full Ezio response, signals, per-citation results, decision, reason, evidence, timestamp). Every executed action goes to `actions.log`. Every human release or reject goes to `review_log.jsonl` with the reviewer and their reason.

## Correction, not just rejection
For a misstated cite, year, or misattributed holding, the wrapper returns a `fix` drawn from the firm's trusted list (for example "5 U.S. 137 (1813)" becomes "(1803)", or "Gideon" becomes "Mapp v. Ohio, 367 U.S. 643 (1961)"). The page applies the fix and **re-sends the memo through `POST /decide`**; the gate makes the new decision, never the browser.
