# FinAudit AI Evaluation Report

- Evaluated date: 2026-10-28
- Evaluated commit: `05a3677`
- Dataset: 7 hand-curated cases; temporary SQLite database
- Scenario IDs: exact-reference, vendor-variation, amount-discrepancy, first-payment, duplicate-payment, tied-candidates, unmatched-vendor
- Labels: hand-curated, not independently reviewed; reviewer status remains pending.
- Scope: synthetic smoke/regression examples, not production performance evidence.

## Metrics

| Metric | Counts | Rate |
| --- | ---: | ---: |
| Invoice match precision | TP 4, FP 0 | 4/4 (100.00%) |
| Invoice match recall | TP 4, FN 0 | 4/4 (100.00%) |
| Invoice match true negatives | TN | 3 cases |
| Anomaly precision | TP 5, FP 0 | 5/5 (100.00%) |
| Anomaly recall | TP 5, FN 0 | 5/5 (100.00%) |
| False auto-approvals | 0 / 3 auto-approvals | 0/3 (0.00%) |
| Status accuracy | exact cases | 7/7 (100.00%) |
| Outcome accuracy | exact cases | 7/7 (100.00%) |
| Manual review | 4 pending | 4/7 (57.14%) |
| Processing failures | 0 | 0/7 (0.00%) |

A zero denominator is shown as `n/a` (JSON uses `null`). Invoice match compares exact invoice IDs; wrong assignments count as FP and FN. Anomaly metrics count `(transaction_id, anomaly_type)` pairs.

Malformed CSV check: HTTP 422, 2 labelled rows rejected; saved results remained unchanged.

## Scenario discrepancies

No discrepancies in the labelled cases.

## Limitations

Small hand-labelled smoke benchmark; percentages describe these cases only and are not a production estimate.
