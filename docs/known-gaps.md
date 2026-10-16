# Known gaps

Measured boundaries of this project. Each entry states what was measured, how,
and what the project therefore may **not** claim. Entries are added when an
external evaluation changes a conclusion that a self-authored evaluation had
supported.

---

## G1. Refusal correctness collapses on externally authored unanswerable questions

**Status:** open. Offline reasoner limitation, not a retrieval or validation defect.

### What was measured

`evals/research-agent/` (self-authored) reports `refusal_contract = 1.0`.
`evals/external-v1/` — 30 crowd-labelled unanswerable questions from SQuAD 2.0 —
reports `refusal_contract = **0.0**`. All 30 were answered instead of refused.

Full external-suite result (`evals/external-v1/canonical-report.json`, 90 cases):

| metric | value | threshold | verdict |
| --- | ---: | ---: | --- |
| `citation_validity` | 1.00 | 1.00 | pass |
| `unsupported_factual_claims` | 0 | 0 | pass |
| `retrieval_recall_at_5` | 0.983 | 0.90 | pass |
| `forbidden_tool_calls` | 0 | 0 | pass |
| `refusal_contract` | **0.00** | 1.00 | **fail** |
| `case_pass_rate` | 0.656 | — | 59/90 |

### Root cause

`ReplayResearchReasoner` refuses only when lexical search returns **zero** hits
(`research/reasoner.py`). Otherwise it selects the highest-overlap sentence and
emits a claim. `Corpus.validate_answer` then confirms the citation is verbatim —
so the answer is *well-cited but off-question*.

Four examples, each answered with a verbatim citation that fails the question:

| asked | cited evidence | mismatch |
| --- | --- | --- |
| 15 **smallest** cities in Germany, **miles** | "15 **largest** cities … 2000 **kilometres**" | entity + unit substituted |
| beginning of **sociology** studies | "beginning of systematic studies in **computational complexity**" | domain substituted |
| year **Dick Sullivan** published on rudimentary sets | "**Raymond Smullyan**'s study of rudimentary sets (1961)" | author substituted |
| complexity classes **not** defined by | "Many complexity classes **are defined using** …" | polarity inverted |

Self-authored unanswerable cases were lexically **disjoint** from the corpus
(they mention topics such as `mars`, `oxygen`, `colony` that never occur), so the
zero-hit rule caught them. Crowd-labelled SQuAD 2.0 unanswerable questions are
deliberately built to be lexically **near-identical** to answerable content — that
is what makes them hard. The rule cannot catch them.

### Two lexical fixes were attempted and rejected by measurement

**Attempt 1 — question-term coverage gate.** Require the selected evidence to
cover a minimum fraction of the question's content terms.

```
external suite   answerable      (n=60)  coverage min 0.286  p50 0.571
external suite   unanswerable    (n=30)  coverage min 0.375  p50 0.800   ← higher
```

Unanswerable questions cover question terms *better* than answerable ones, so no
threshold separates them. Rejected.

**Attempt 2 — absent-term gate.** Refuse when the question contains a content
term that appears nowhere in the corpus.

```
external suite   detection on unanswerable   0.70
external suite   false positive on answerable 0.88   ← worse than chance
```

Questions paraphrase (`combine` vs the corpus's own wording) and the term
extractor keeps low-value tokens, so "absent from corpus" is normal for
answerable questions too. Rejected.

### Consequence

Answerability here is a **semantic** judgement (entity identity, comparatives,
negation). The offline replay reasoner is a deterministic tool-loop exerciser
with no model; it cannot make that judgement, and no lexical proxy substitutes
for it. Therefore:

- The project may claim: verbatim citation validation and fail-closed refusal on
  zero-evidence inputs, verified on 90 externally authored cases.
- The project may **not** claim: that it refuses questions which are topically
  related but unanswerable. On externally authored near-miss questions it answers
  with well-cited, off-question text.
- `evals/external-v1` is therefore a **diagnostic report**, not a regression
  gate. `passed: false` is the recorded state, and
  `tests/test_research_suite_runner.py` pins it so the number cannot drift
  silently. Improving refusal correctness requires a model-backed reasoner and is
  out of scope for the offline path.

### Reproduce

```bash
.venv/bin/agentlab research-suite-eval --suite-root evals/external-v1
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from pathlib import Path
from agentlab.research.corpus import Corpus, research_terms
from agentlab.research.agent import ResearchAgent
c = Corpus.load(Path('evals/external-v1/corpus'))
r = ResearchAgent(c, clock=lambda: 0.0).run(
    'How many miles does the traveling salesman problem seek to classify a route between the 15 smallest cities in Germany?')
print(r.answer.status, r.answer.answer[:120])
"
```
