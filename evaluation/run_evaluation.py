"""Run the curated reconciliation benchmark against an isolated SQLite database."""

from __future__ import annotations

import csv
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import database
import app.main as main_module
from app.models import (
    AuthenticatedUser,
    MatchOutcome,
    MatchResult,
    MatchStatus,
    UserRole,
)


DATASET_DIR = Path(__file__).resolve().parent / "dataset"
LABELS_PATH = DATASET_DIR / "labels.csv"
INVALID_LABELS_PATH = DATASET_DIR / "invalid_labels.csv"


@dataclass(frozen=True)
class CaseLabel:
    scenario_id: str
    transaction_id: str
    expected_invoice_id: str | None
    expected_status: str
    expected_outcome: str
    expected_anomalies: frozenset[str]
    scenario: str


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))


def load_labels(path: Path = LABELS_PATH) -> dict[str, CaseLabel]:
    labels: dict[str, CaseLabel] = {}
    required_fields = {
        "scenario_id",
        "transaction_id",
        "expected_invoice_id",
        "expected_status",
        "expected_outcome",
        "expected_anomalies",
        "scenario",
    }
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError("Evaluation labels contain duplicate column names.")
        missing_fields = required_fields - set(fields)
        if missing_fields:
            raise ValueError(
                f"Evaluation labels are missing columns: {sorted(missing_fields)}"
            )
        rows = list(reader)

    scenario_ids: set[str] = set()
    for row in rows:
        if None in row:
            raise ValueError("Evaluation labels contain a row with extra columns.")
        scenario_id = (row.get("scenario_id") or "").strip()
        transaction_id = (row.get("transaction_id") or "").strip()
        if not scenario_id or not transaction_id:
            raise ValueError("Every evaluation label needs a scenario_id and transaction_id.")
        if transaction_id in labels:
            raise ValueError(f"Duplicate evaluation label: {transaction_id}")
        if scenario_id in scenario_ids:
            raise ValueError(f"Duplicate evaluation scenario ID: {scenario_id}")
        scenario_ids.add(scenario_id)
        expected_status = (row.get("expected_status") or "").strip()
        expected_outcome = (row.get("expected_outcome") or "").strip()
        try:
            MatchStatus(expected_status)
            MatchOutcome(expected_outcome)
        except ValueError as exc:
            raise ValueError(
                f"Invalid expected status or outcome for scenario {scenario_id}."
            ) from exc
        anomaly_values = [
            value.strip()
            for value in (row.get("expected_anomalies") or "").split(";")
            if value.strip()
        ]
        if len(anomaly_values) != len(set(anomaly_values)):
            raise ValueError(f"Duplicate expected anomaly in scenario {scenario_id}.")
        scenario = (row.get("scenario") or "").strip()
        if not scenario:
            raise ValueError(f"Missing scenario description for {scenario_id}.")
        labels[transaction_id] = CaseLabel(
            scenario_id=scenario_id,
            transaction_id=transaction_id,
            expected_invoice_id=(row.get("expected_invoice_id") or "").strip() or None,
            expected_status=expected_status,
            expected_outcome=expected_outcome,
            expected_anomalies=frozenset(anomaly_values),
            scenario=scenario,
        )
    if not labels:
        raise ValueError("Evaluation labels contain no scenarios.")
    return labels


def validate_label_coverage(
    transactions_path: Path, labels: dict[str, CaseLabel]
) -> None:
    """Require a one-to-one mapping between transaction inputs and labels."""
    transaction_rows = _read_csv(transactions_path)
    transaction_ids = [(row.get("transaction_id") or "").strip() for row in transaction_rows]
    if any(not transaction_id for transaction_id in transaction_ids):
        raise ValueError("Evaluation transactions contain a blank transaction_id.")
    if len(transaction_ids) != len(set(transaction_ids)):
        raise ValueError("Evaluation transactions contain duplicate transaction IDs.")
    transaction_id_set = set(transaction_ids)
    label_id_set = set(labels)
    if any(label.transaction_id != identifier for identifier, label in labels.items()):
        raise ValueError("Evaluation label keys do not match their transaction_id values.")
    if transaction_id_set != label_id_set:
        raise ValueError(
            "Evaluation labels do not cover inputs exactly; "
            f"missing={sorted(transaction_id_set - label_id_set)}, "
            f"extra={sorted(label_id_set - transaction_id_set)}"
        )


def validate_invoice_labels(invoices_path: Path, labels: dict[str, CaseLabel]) -> None:
    """Reject duplicate/blank invoice IDs and labels pointing at absent invoices."""
    rows = _read_csv(invoices_path)
    invoice_ids = [(row.get("invoice_id") or "").strip() for row in rows]
    if any(not invoice_id for invoice_id in invoice_ids):
        raise ValueError("Evaluation invoices contain a blank invoice_id.")
    if len(invoice_ids) != len(set(invoice_ids)):
        raise ValueError("Evaluation invoices contain duplicate invoice IDs.")
    known_ids = set(invoice_ids)
    expected_ids = {
        label.expected_invoice_id
        for label in labels.values()
        if label.expected_invoice_id
    }
    unknown = sorted(expected_ids - known_ids)
    if unknown:
        raise ValueError(f"Evaluation labels reference unknown invoice IDs: {unknown}")


def _rate(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "percent": round(100 * numerator / denominator, 2) if denominator else None,
    }


def score_predictions(
    labels: dict[str, CaseLabel], predictions: list[MatchResult]
) -> dict:
    predicted_by_id = {result.transaction_id: result for result in predictions}
    if len(predicted_by_id) != len(predictions):
        raise ValueError("Predictions contain duplicate transaction identifiers.")
    expected_ids, actual_ids = set(labels), set(predicted_by_id)
    if expected_ids != actual_ids:
        raise ValueError(
            f"Prediction IDs differ from labels; missing={sorted(expected_ids - actual_ids)}, "
            f"unexpected={sorted(actual_ids - expected_ids)}"
        )

    match_tp = match_fp = match_fn = match_tn = 0
    status_correct = outcome_correct = 0
    expected_anomalies: set[tuple[str, str]] = set()
    predicted_anomalies: set[tuple[str, str]] = set()
    auto_approved = false_auto_approvals = pending_review = processing_failures = 0
    case_reports = []

    for transaction_id, label in labels.items():
        result = predicted_by_id[transaction_id]
        predicted_invoice_id = result.invoice_id
        if label.expected_invoice_id:
            if predicted_invoice_id == label.expected_invoice_id:
                match_tp += 1
            else:
                match_fn += 1
                if predicted_invoice_id:
                    match_fp += 1
        elif predicted_invoice_id:
            match_fp += 1
        else:
            match_tn += 1

        predicted_types = {item.type for item in result.anomalies}
        expected_anomalies.update((transaction_id, item) for item in label.expected_anomalies)
        predicted_anomalies.update((transaction_id, item) for item in predicted_types)
        status_correct += result.status.value == label.expected_status
        outcome_correct += (result.match_outcome.value if result.match_outcome else "") == label.expected_outcome
        if result.status.value == "auto_approved":
            auto_approved += 1
            if (
                label.expected_status != "auto_approved"
                or predicted_invoice_id != label.expected_invoice_id
            ):
                false_auto_approvals += 1
        if result.review_status.value == "pending_review":
            pending_review += 1
        if (
            result.match_outcome == MatchOutcome.invalid_input
            or predicted_types.intersection({"processing_error", "invalid_input"})
        ):
            processing_failures += 1

        case_reports.append(
            {
                "transaction_id": transaction_id,
                "scenario_id": label.scenario_id,
                "scenario": label.scenario,
                "expected_invoice_id": label.expected_invoice_id,
                "predicted_invoice_id": predicted_invoice_id,
                "expected_status": label.expected_status,
                "predicted_status": result.status.value,
                "expected_outcome": label.expected_outcome,
                "predicted_outcome": result.match_outcome.value if result.match_outcome else None,
                "expected_anomalies": sorted(label.expected_anomalies),
                "predicted_anomalies": sorted(predicted_types),
            }
        )

    anomaly_tp = len(expected_anomalies & predicted_anomalies)
    anomaly_fp = len(predicted_anomalies - expected_anomalies)
    anomaly_fn = len(expected_anomalies - predicted_anomalies)
    case_count = len(labels)
    return {
        "case_count": case_count,
        "match": {
            "true_positive": match_tp,
            "false_positive": match_fp,
            "false_negative": match_fn,
            "true_negative": match_tn,
            "precision": _rate(match_tp, match_tp + match_fp),
            "recall": _rate(match_tp, match_tp + match_fn),
        },
        "false_auto_approval": {
            "count": false_auto_approvals,
            "among_auto_approvals": _rate(false_auto_approvals, auto_approved),
            "auto_approval_count": auto_approved,
        },
        "anomalies": {
            "true_positive": anomaly_tp,
            "false_positive": anomaly_fp,
            "false_negative": anomaly_fn,
            "precision": _rate(anomaly_tp, anomaly_tp + anomaly_fp),
            "recall": _rate(anomaly_tp, anomaly_tp + anomaly_fn),
        },
        "status_accuracy": _rate(status_correct, case_count),
        "outcome_accuracy": _rate(outcome_correct, case_count),
        "manual_review": {
            "pending_count": pending_review,
            "rate": _rate(pending_review, case_count),
        },
        "processing_failures": {
            "count": processing_failures,
            "rate": _rate(processing_failures, case_count),
        },
        "cases": case_reports,
    }


def _validate_malformed_csv(labels_path: Path) -> dict:
    expected_rows = _read_csv(labels_path)
    try:
        main_module.run_reconciliation(
            AuthenticatedUser(
                user_id="evaluation-reviewer",
                username="evaluation-reviewer",
                role=UserRole.reviewer,
            )
        )
    except HTTPException as exc:
        if exc.status_code != 422 or not isinstance(exc.detail, dict):
            raise AssertionError(f"Expected CSV validation HTTP 422, received {exc.status_code}.") from exc
        rejected = exc.detail.get("transactions", [])
    else:
        raise AssertionError("Malformed evaluation CSV was accepted.")

    by_identifier = {str(item.get("identifier")): item for item in rejected}
    expected_by_id = {row["transaction_id"]: row for row in expected_rows}
    if (
        len(by_identifier) != len(rejected)
        or len(expected_by_id) != len(expected_rows)
        or set(by_identifier) != set(expected_by_id)
    ):
        raise AssertionError(
            f"Rejected CSV identifiers differ from labels: "
            f"expected={sorted(expected_by_id)}, actual={sorted(by_identifier)}"
        )
    for identifier, expected in expected_by_id.items():
        actual = by_identifier[identifier]
        error_text = " ".join(actual.get("errors", []))
        if actual.get("row_number") != int(expected["row_number"]):
            raise AssertionError(f"Unexpected CSV row number for {identifier}.")
        if expected["error_contains"].casefold() not in error_text.casefold():
            raise AssertionError(f"Expected validation evidence missing for {identifier}: {error_text}")
    return {"http_status": 422, "rejected_row_count": len(rejected), "labels_match": True}


def run_evaluation() -> dict:
    labels = load_labels()
    valid_transactions = DATASET_DIR / "transactions.csv"
    validate_label_coverage(valid_transactions, labels)
    invoices = DATASET_DIR / "invoices.csv"
    validate_invoice_labels(invoices, labels)
    malformed_transactions = DATASET_DIR / "invalid_transactions.csv"

    with tempfile.TemporaryDirectory(prefix="finaudit-evaluation-") as temp:
        temp_dir = Path(temp)
        with (
            patch.object(database, "DATA_DIR", temp_dir),
            patch.object(database, "DATABASE_PATH", temp_dir / "evaluation.db"),
            patch.object(database, "TRANSACTIONS_CSV", valid_transactions),
            patch.object(database, "INVOICES_CSV", invoices),
        ):
            database.init_db()
            reviewer = AuthenticatedUser(
                user_id="evaluation-reviewer",
                username="evaluation-reviewer",
                role=UserRole.reviewer,
            )
            predictions = main_module.run_reconciliation(reviewer)
            persisted = database.get_all_match_results()
            persisted_ids = [item.transaction_id for item in persisted]
            if len(persisted_ids) != len(labels) or set(persisted_ids) != set(labels):
                raise AssertionError("Evaluation results were not completely persisted to the temp database.")
            persisted_before_validation = {
                item.transaction_id: item.model_dump(mode="json") for item in persisted
            }
            metrics = score_predictions(labels, predictions)
            with patch.object(database, "TRANSACTIONS_CSV", malformed_transactions):
                validation = _validate_malformed_csv(INVALID_LABELS_PATH)
            persisted_after_validation = {
                item.transaction_id: item.model_dump(mode="json")
                for item in database.get_all_match_results()
            }
            if persisted_after_validation != persisted_before_validation:
                raise AssertionError("Malformed CSV validation changed persisted evaluation results.")

    return {
        "benchmark": "FinAudit AI curated evaluation v1",
        "database": "temporary SQLite database (removed after run)",
        "dataset_limitations": (
            "Small hand-labelled smoke benchmark; percentages describe these cases only and are not a production estimate."
        ),
        "metrics": metrics,
        "csv_validation": validation,
    }


def main() -> None:
    print(json.dumps(run_evaluation(), indent=2))


if __name__ == "__main__":
    main()
