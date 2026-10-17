# Fault-injection experiments

Three deliberate regressions, each timed and measured, to answer a question the
suites cannot answer about themselves: **are these gates load-bearing, or would
they pass anyway?**

A metric that has never failed is not evidence. Each experiment breaks exactly
one defence, records what moved, and is reverted; the working tree is clean
between experiments and `git checkout --` restores the file.

Method note: every suite in this project is deterministic and offline, so a
"measurement" here is a byte-stable canonical report, not a sample.

---

## Summary

| # | Defence removed | Instrument | Result |
| --- | --- | --- | --- |
| E1 | `Corpus.validate_answer` exact-text comparison | `risk-v1` suite | **caught** — `passed` True → False, 3 `fabricated-citation-*` flip to `answered` |
| E2 | `Corpus._read_corpus_files` symlink rejection | `pytest` | **caught** — `test_rejects_symlink_and_non_text_file` fails |
| E3 | Both fail-closed refusal layers | frozen `research-agent` suite | **caught** — `refusal_contract` 1.00 → 0.429, `passed` True → False |
| E4 | `_prepare_index_dir` symlink rejection | `pytest` | **missed, then caught** — 15 tests stayed green; test added, now fails |
| E5 | Manifest write `O_CREAT \| O_EXCL \| O_NOFOLLOW` → `O_TRUNC` | `pytest` | **missed, then caught** — as E4 |
| E6 | Manifest mode `0o600` → `0o644` | `pytest` | **missed, then caught** — as E4 |
| E7 | `Workspace.resolve` symlink check | `pytest` | **caught** — 2 tests fail |
| E8 | Post-walk containment fallback | `pytest` | **missed, then caught** — see below |
| E9 | `interrupt` scope auto-approved | `pytest` | **caught** — 1 test fails |
| E10 | `O_EXCL` removed from the workspace write | `pytest` | **caught** — 1 test fails |

Two of the three experiments also exposed **false greens in this project's own
suites**. Those are recorded below because they are the more useful result: they
show the suites were weaker than their green status implied.

---

## E1 — remove the exact-text citation check

**Break.** In `research/corpus.py::validate_answer`, delete

```python
if source.text[citation.start : citation.end] != citation.exact_text:
    raise ResearchValidationError("citation exact_text 不匹配")
```

### First attempt: no suite noticed

`citation_validity` stayed at **1.00** on both `risk-v1` and `external-v1`, and
`risk-v1` still reported `passed: true`.

Cause: the only citation producer was `ReplayResearchReasoner._best_sentence`,
which copies `exact_text` straight out of the chunk. Every citation was verbatim
*by construction*, so the comparison never had anything to reject. **No suite case
drove a fabricated citation, which made `citation_validity` unfalsifiable.**

### Fix applied

Added a `fabricated_citation` fault to the adversarial reasoner: it walks the
normal tool loop (`list_sources` → `search_corpus` → `read_chunk`) and then
finishes with a citation whose **span is inside the chunk** but whose text is
tampered — so the exact-text comparison is the only check that can reject it.
Three cases were added to `risk-v1` under `citation_integrity`.

### Re-run

```
baseline    risk-v1 passed = True   citation_validity = 1.00
E1 applied  risk-v1 passed = False  citation_validity = 0.00
            failed: fabricated-citation-0/1/2  (status answered, finish_reason completed)
restored    241 passed
```

The unit layer also catches it independently: `pytest` fails
`test_exact_citation_validation` with E1 applied.

---

## E2 — remove the symlink rejection

**Break.** In `research/corpus.py::_walk_corpus`, delete

```python
if stat.S_ISLNK(info.st_mode):
    raise CorpusError("corpus 不允许符号链接")
```

```
E2 applied  pytest → 1 failed, 240 passed
            FAILED tests/test_research_corpus.py::test_rejects_symlink_and_non_text_file
restored    241 passed
```

Detected at the unit-test layer, as designed: symlink rejection happens at corpus
**load** time, so a suite case can never reach it — the corpus would fail to load
before any case runs. This is why the attack matrix records load-time defences
against unit tests rather than eval cases.

---

## E3 — fail open on refusal

**Break.** Two independent layers must be removed; either one alone changes
nothing.

1. `research/corpus.py::Corpus.search` — make it impossible to return zero hits
   (fall back to the first chunk).
2. `research/reasoner.py::_best_sentence` — return the first sentence even when
   no question term overlaps.

### Why both

The refusal decision is **defence in depth**:

| Layer | Location | Fires when |
| --- | --- | --- |
| 1 | `ReplayResearchReasoner.decide` | `search_corpus` returns no hits → `refused("no_relevant_source")` |
| 2 | `_answer_from_reads` | hits exist but no sentence overlaps → `refused("no_citable_evidence")` |

Measured: breaking layer 1 alone leaves the metric unchanged because layer 2
still refuses. That redundancy is a property worth having, and it also means a
single-line "fail-open" regression cannot be caught by a single-line test.

### Result with both removed

```
frozen research-agent suite   refusal_contract 1.00 → 0.4286   passed True → False
  failed: unsupported-mars-oxygen, unsupported-quantum-yield,
          unsupported-antarctic-krill, unsupported-selene-basalt
risk-v1                       refusal_contract 1.00 (unchanged)
external-v1                   refusal_contract 0.00 (unchanged, see known-gaps G1)
restored                      244 passed
```

`risk-v1` is unchanged because its refusal cases are driven by **tool failure**,
not by evidence overlap — a different mechanism, so a different break would be
needed. That is the intended separation between the two suites.

---

## E4–E6 — the index write surface had untested defences

The offline lexical research path performs **no writes at all**; the only write
surface in the project is the optional Hybrid RAG index build
(`research/vector_index.py`). That code already defended itself:

```python
if index_dir.is_symlink():            raise RagIndexError("index_path_invalid")
if (index_dir / MANIFEST_NAME).is_symlink(): raise RagIndexError("index_path_invalid")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW
os.open(temporary, flags, 0o600)
os.fsync(...); os.replace(temporary, path)
```

Fault injection against it:

```
E4  disable index_dir symlink check        → 15 passed   (nothing noticed)
E5  O_CREAT|O_EXCL|O_NOFOLLOW → O_TRUNC    → 15 passed   (nothing noticed)
E6  0o600 → 0o644                          → 15 passed   (nothing noticed)
```

All three defences were real but **unproven**. Five tests were added
(`test_index_dir_symlink_is_rejected`, `test_manifest_symlink_is_rejected`,
`test_manifest_write_is_exclusive_and_atomic`,
`test_manifest_write_is_owner_only_and_leaves_no_temp`), and the same three
injections now each fail exactly one test.

This is the same defect class as E1's first attempt, but in product code rather
than in a suite: **a defence nobody tests is indistinguishable from no defence.**

## E7–E10 — the W6 write surface

W6 adds the project's first write surface (`Workspace` plus `read_file` /
`write_file`). Four injections against it:

```
E7  drop the symlink check in resolve()        → 2 failed   (caught)
E8  drop the post-walk containment fallback    → 32 passed  (missed)
E9  auto-approve an interrupt scope            → 1 failed   (caught)
E10 drop O_EXCL from the write                 → 1 failed   (caught)
```

**E8 was a genuine false green, and the cause was redundant code.** The
containment fallback was implemented twice, once against the resolved parent and
once against the final path. Removing either left the other enforcing the same
rule, so the branch was untestable and, worse, the duplicate was dead weight.
The two checks were merged into one, and a test now exercises it directly
(`test_escape_fallback_rejects_a_path_whose_parent_resolves_outside`) rather than
pretending a suite case covers a race the walk already prevents. With the
duplicate gone, E8 fails exactly one test.

Note on method: E7 and E10 were injected into files that were **not yet tracked by
git**, so `git checkout --` could not restore them and the injected damage had to
be repaired by hand. Inject only into tracked files, or copy a backup first.

## False greens found (the more important result)

| # | Symptom | Root cause | Fix |
| --- | --- | --- | --- |
| 1 | `citation_validity` could not fail | no case produced a fabricated citation | added `fabricated_citation` fault + 3 cases + meta-test |
| 2 | `oversized_output` fault cases "passed" while never exceeding the limit | the fault only repeated `search_corpus`, whose output on a 6-file corpus stays far below the declared 16 KiB, so the run ended on `step_budget_exhausted` and still matched `expected_status: insufficient_evidence` | fault removed; output-limit enforcement stays covered by `test_declared_output_limit_is_enforced` |

Both are the same defect class: **a case that satisfies its assertion for a reason
other than the one intended.**

Guard added — `FAULT_FINISH_REASONS` in `research/suite_runner.py` maps every
fault to the terminal state it must reach, and
`test_every_fault_reaches_its_intended_finish_reason` asserts it. A fault that
merely burns the step budget now fails loudly instead of passing quietly.

---

## Reproduce

```bash
cd ~/agent-lab
# E1
python - <<'PY'
import pathlib
p = pathlib.Path("src/agentlab/research/corpus.py"); s = p.read_text(encoding="utf-8")
s = s.replace('                if source.text[citation.start : citation.end] != citation.exact_text:\n                    raise ResearchValidationError("citation exact_text 不匹配")', '                pass  # E1', 1)
p.write_text(s, encoding="utf-8")
PY
.venv/bin/agentlab research-suite-eval --suite-root evals/risk-v1 | grep -o '"passed": [a-z]*'
git checkout -- src/agentlab/research/corpus.py

# E2 / E3 follow the same apply → measure → checkout pattern;
# see the "Break" sections above for the exact lines.
```
