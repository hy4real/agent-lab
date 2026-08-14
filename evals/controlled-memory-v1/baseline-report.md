# Controlled Memory v1 Canonical Report

- Suite: `controlled-memory-v1`
- Version: `1`
- Records: `7`
- Cases: `8`
- Result: `passed`

## Metrics

| Metric | Value |
|---|---:|
| `budget_compliance_rate` | 1.000 |
| `determinism_rate` | 1.000 |
| `expected_recall_rate` | 1.000 |
| `injection_boundary_rate` | 1.000 |
| `scope_isolation_rate` | 1.000 |
| `state_contract_rate` | 1.000 |
| `unconfirmed_exclusion_rate` | 1.000 |

## State Probes

| Probe | Passed |
|---|---:|
| `cas_conflict` | yes |
| `concurrent_write` | yes |
| `corruption_rejected` | yes |
| `feedback_loop_absent` | yes |
| `legacy_migration` | yes |

## Boundaries

This report covers deterministic synthetic storage, recall, isolation, budget, migration,
corruption, CAS, concurrency, and feedback-loop contracts. It does not measure model quality,
real-user usefulness, production concurrency, multi-tenant authorization, or external benchmarks.
