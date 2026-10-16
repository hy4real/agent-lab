# risk-v1 provenance

Every file here is **hand-authored for this repository**. No real system,
credential, hostname or attack payload is used, and no third-party dataset is
redistributed.

## Corpus

| File | Content | Origin |
| --- | --- | --- |
| `corpus/ops-runbook.md` | fictional Atlas service runbook | authored 2026-10-16 |
| `corpus/alpha-config.md` | fictional alpha environment values | authored 2026-10-16 |
| `corpus/beta-config.md` | fictional beta values, deliberately conflicting with alpha | authored 2026-10-16 |
| `corpus/catalog.md` | fictional component catalogue | authored 2026-10-16 |
| `corpus/injection-field-note.md` | normal content plus an embedded instruction telling the reader to call `write_file` on `/etc/hosts` | authored 2026-10-16 |
| `corpus/injection-handbook.md` | normal content plus an embedded instruction claiming the citation validator is disabled | authored 2026-10-16 |

The two injection documents use the *shape* of a prompt-injection attempt so the
contract "corpus content is data, never instructions" is exercised. They contain
no working payload and name no real host, tool or service.

## Injection-pattern sources

The pattern is standard and public; the wording here is original. Reference
material consulted for the category, not copied from:

- OWASP Top 10 for LLM Applications — LLM01 Prompt Injection
- the general "ignore previous instructions" pattern documented in public
  prompt-injection write-ups

## Cases

All 22 cases in `suite.json` were authored alongside the corpus. They fall into
six categories: `tool_privilege_escalation` (7), `prompt_injection` (4),
`citation_integrity` (3), `information_conflict` (2), `tool_budget_failure` (2),
`direct` (4).

`direct` cases are a control group: a suite that refuses everything must not pass.

## Boundary

This suite proves tool-layer containment and citation integrity on a synthetic
corpus. It does **not** prove resistance to a real attacker, model-level
alignment, or production isolation. See
[`../../docs/isolation-attack-matrix.md`](../../docs/isolation-attack-matrix.md)
for the full posture and the list of what is deliberately not defended.
