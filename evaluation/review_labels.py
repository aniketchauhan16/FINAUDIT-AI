"""Generate a blind-to-predictions label review worksheet."""

from __future__ import annotations

import csv
from pathlib import Path

from evaluation.run_evaluation import DATASET_DIR, LABELS_PATH, load_labels, validate_label_coverage


TRANSACTIONS_PATH = DATASET_DIR / "transactions.csv"
INVOICES_PATH = DATASET_DIR / "invoices.csv"
OUTPUT_PATH = DATASET_DIR / "label-review.csv"
FIELDS = [
    "scenario_id",
    "business_scenario",
    "input_files",
    "expected_invoice_id",
    "expected_match_result",
    "expected_anomaly_class",
    "expected_routing",
    "label_rationale",
    "independently_reviewed",
    "reviewer_status",
    "disagreement_notes",
]
RATIONALES = {
    "exact_match": "A unique invoice agrees with the explicit reference and financial fields, with no review-triggering anomaly.",
    "probable_match": "A unique candidate agrees with normalized vendor and financial evidence; automatic routing still requires all safety checks.",
    "amount_mismatch": "A discrepancy beyond the configured amount tolerance requires human review.",
    "potential_duplicate_payment": "A repeated payment or already-assigned invoice is not eligible for automatic approval.",
    "ambiguous_candidates": "Tied invoice candidates have no unique winner and must remain pending review.",
    "duplicate_invoice": "Duplicate invoice identifiers make candidate identity unreliable and require review.",
    "vendor_mismatch": "No invoice for the transaction vendor is a negative match and remains unmatched.",
    "missing_invoice": "No invoice candidate is available; the transaction cannot be auto-approved.",
    "invalid_input": "Invalid or incomplete processing must fail closed and remain reviewable when an identifier exists.",
}


def generate_review_worksheet(output_path: Path = OUTPUT_PATH) -> int:
    labels = load_labels(LABELS_PATH)
    validate_label_coverage(TRANSACTIONS_PATH, labels)
    with INVOICES_PATH.open(newline="", encoding="utf-8-sig") as source:
        invoice_reader = csv.DictReader(source)
        invoice_ids = [row.get("invoice_id", "").strip() for row in invoice_reader]
    if any(not value for value in invoice_ids) or len(invoice_ids) != len(set(invoice_ids)):
        raise ValueError("Evaluation invoice IDs must be nonblank and unique.")

    rows = []
    for label in labels.values():
        if label.expected_invoice_id and label.expected_invoice_id not in invoice_ids:
            raise ValueError(
                f"Scenario {label.scenario_id} references unknown invoice "
                f"{label.expected_invoice_id}."
            )
        rows.append(
            {
                "scenario_id": label.scenario_id,
                "business_scenario": label.scenario,
                "input_files": "transactions.csv; invoices.csv",
                "expected_invoice_id": label.expected_invoice_id or "",
                "expected_match_result": label.expected_outcome,
                "expected_anomaly_class": ";".join(sorted(label.expected_anomalies)),
                "expected_routing": label.expected_status,
                "label_rationale": RATIONALES.get(
                    label.expected_outcome,
                    "Review against the documented deterministic matching and fail-closed rules.",
                ),
                "independently_reviewed": "no",
                "reviewer_status": "pending",
                "disagreement_notes": "",
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(labels)


if __name__ == "__main__":
    print(f"Wrote {generate_review_worksheet()} label-review rows to {OUTPUT_PATH}.")
