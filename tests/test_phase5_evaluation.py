import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from evaluation.run_evaluation import (
    CaseLabel,
    INVALID_LABELS_PATH,
    _validate_malformed_csv,
    load_labels,
    run_evaluation,
    score_predictions,
    validate_label_coverage,
    validate_invoice_labels,
)
from evaluation.review_labels import FIELDS as REVIEW_FIELDS, generate_review_worksheet
from evaluation.write_report import render_markdown
from app.agents.validation import failure_match_result
from app.models import MatchOutcome, MatchResult, MatchStatus, ReviewStatus


class Phase5EvaluationTests(unittest.TestCase):
    def test_curated_dataset_reconciles_against_separate_expected_labels(self):
        report = run_evaluation()
        metrics = report["metrics"]

        self.assertEqual(report["database"], "temporary SQLite database (removed after run)")
        self.assertEqual(metrics["case_count"], 7)
        self.assertEqual(metrics["status_accuracy"]["percent"], 100.0)
        self.assertEqual(metrics["outcome_accuracy"]["percent"], 100.0)
        self.assertEqual(metrics["match"]["true_positive"], 4)
        self.assertEqual(metrics["match"]["true_negative"], 3)
        self.assertEqual(metrics["match"]["precision"]["percent"], 100.0)
        self.assertEqual(metrics["match"]["recall"]["percent"], 100.0)
        self.assertEqual(metrics["false_auto_approval"]["count"], 0)
        self.assertEqual(metrics["false_auto_approval"]["auto_approval_count"], 3)
        self.assertEqual(metrics["anomalies"]["true_positive"], 5)
        self.assertEqual(metrics["anomalies"]["false_positive"], 0)
        self.assertEqual(metrics["anomalies"]["false_negative"], 0)
        self.assertEqual(metrics["manual_review"]["pending_count"], 4)
        self.assertEqual(metrics["processing_failures"]["count"], 0)
        self.assertEqual(report["csv_validation"]["http_status"], 422)
        self.assertEqual(report["csv_validation"]["rejected_row_count"], 2)
        self.assertTrue(report["csv_validation"]["labels_match"])

    def test_wrong_auto_approval_counts_as_match_error_and_false_approval(self):
        labels = {
            "TX-WRONG": CaseLabel(
                scenario_id="wrong-auto-approval",
                transaction_id="TX-WRONG",
                expected_invoice_id="INV-EXPECTED",
                expected_status="flagged",
                expected_outcome="amount_mismatch",
                expected_anomalies=frozenset({"amount_discrepancy"}),
                scenario="An incorrect auto-approval must be counted as a failure",
            )
        }
        predictions = [
            MatchResult(
                transaction_id="TX-WRONG",
                invoice_id="INV-WRONG",
                status=MatchStatus.auto_approved,
                confidence_score=0.95,
                reason="Synthetic prediction for metric definition testing.",
                review_status=ReviewStatus.approved,
                match_outcome=MatchOutcome.exact_match,
            )
        ]

        metrics = score_predictions(labels, predictions)

        self.assertEqual(metrics["match"]["false_positive"], 1)
        self.assertEqual(metrics["match"]["false_negative"], 1)
        self.assertEqual(metrics["false_auto_approval"]["count"], 1)
        self.assertEqual(metrics["false_auto_approval"]["among_auto_approvals"]["percent"], 100.0)

    def test_error_result_counts_as_processing_failure_and_zero_denominators_are_explicit(self):
        labels = {
            "TX-ERROR": CaseLabel(
                scenario_id="graph-error",
                transaction_id="TX-ERROR",
                expected_invoice_id=None,
                expected_status="flagged",
                expected_outcome="invalid_input",
                expected_anomalies=frozenset({"processing_error"}),
                scenario="Graph processing error must remain pending review",
            )
        }
        failed = failure_match_result(
            {"transaction_id": "TX-ERROR"},
            message="Graph execution failed (RuntimeError).",
            error_type="processing_error",
        )
        metrics = score_predictions(labels, [failed])

        self.assertEqual(metrics["processing_failures"]["count"], 1)
        self.assertEqual(metrics["processing_failures"]["rate"]["denominator"], 1)
        self.assertIsNone(metrics["match"]["precision"]["percent"])
        self.assertIsNone(metrics["false_auto_approval"]["among_auto_approvals"]["percent"])

    def test_label_loader_rejects_duplicate_scenario_and_transaction_ids(self):
        label_header = (
            "scenario_id,transaction_id,expected_invoice_id,expected_status,"
            "expected_outcome,expected_anomalies,scenario\n"
        )
        row = "same-case,TX-1,,flagged,invalid_input,processing_error,Failure\n"
        for duplicate_rows, message in (
            (row + "same-case,TX-2,,flagged,invalid_input,processing_error,Failure\n", "scenario ID"),
            (row + "second-case,TX-1,,flagged,invalid_input,processing_error,Failure\n", "label"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                labels_path = Path(temp) / "labels.csv"
                labels_path.write_text(label_header + duplicate_rows, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    load_labels(labels_path)

    def test_label_coverage_rejects_missing_extra_and_duplicate_input_ids(self):
        label = CaseLabel(
            scenario_id="case-one",
            transaction_id="TX-1",
            expected_invoice_id=None,
            expected_status="unmatched",
            expected_outcome="vendor_mismatch",
            expected_anomalies=frozenset(),
            scenario="No invoice exists",
        )
        labels = {"TX-1": label}
        with tempfile.TemporaryDirectory() as temp:
            transactions_path = Path(temp) / "transactions.csv"
            transactions_path.write_text(
                "transaction_id\nTX-1\nTX-2\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "missing=.*TX-2"):
                validate_label_coverage(transactions_path, labels)

            labels["TX-2"] = CaseLabel(
                scenario_id="case-two",
                transaction_id="TX-2",
                expected_invoice_id=None,
                expected_status="unmatched",
                expected_outcome="vendor_mismatch",
                expected_anomalies=frozenset(),
                scenario="Another unmatched transaction",
            )
            with self.assertRaisesRegex(ValueError, "extra=.*TX-2"):
                transactions_path.write_text(
                    "transaction_id\nTX-1\n", encoding="utf-8"
                )
                validate_label_coverage(transactions_path, labels)

            transactions_path.write_text(
                "transaction_id\nTX-1\nTX-1\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "duplicate transaction IDs"):
                validate_label_coverage(transactions_path, {"TX-1": label})

    def test_invoice_ids_must_be_unique_and_ground_truth_must_reference_existing_ids(self):
        label = CaseLabel(
            scenario_id="invoice-case",
            transaction_id="TX-1",
            expected_invoice_id="INV-1",
            expected_status="auto_approved",
            expected_outcome="exact_match",
            expected_anomalies=frozenset(),
            scenario="Exact invoice reference",
        )
        with tempfile.TemporaryDirectory() as temp:
            invoices_path = Path(temp) / "invoices.csv"
            invoices_path.write_text("invoice_id\nINV-1\nINV-1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate invoice IDs"):
                validate_invoice_labels(invoices_path, {"TX-1": label})
            invoices_path.write_text("invoice_id\nINV-2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown invoice IDs"):
                validate_invoice_labels(invoices_path, {"TX-1": label})

    def test_malformed_csv_duplicate_rejections_cannot_be_collapsed_by_identifier(self):
        duplicate_rejections = [
            {
                "identifier": "TX-EVAL-INVALID-MISSING",
                "row_number": 2,
                "errors": ["vendor_name: must not be blank"],
            },
            {
                "identifier": "TX-EVAL-INVALID-AMOUNT",
                "row_number": 3,
                "errors": ["amount: invalid decimal"],
            },
            {
                "identifier": "TX-EVAL-INVALID-MISSING",
                "row_number": 2,
                "errors": ["vendor_name: must not be blank"],
            },
        ]
        failure = HTTPException(422, detail={"transactions": duplicate_rejections})
        with patch(
            "evaluation.run_evaluation.main_module.run_reconciliation",
            side_effect=failure,
        ):
            with self.assertRaisesRegex(AssertionError, "identifiers differ"):
                _validate_malformed_csv(INVALID_LABELS_PATH)

    def test_review_worksheet_is_pending_and_excludes_predictions(self):
        with tempfile.TemporaryDirectory() as temp:
            worksheet = Path(temp) / "review.csv"
            self.assertEqual(generate_review_worksheet(worksheet), 7)
            with worksheet.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 7)
            self.assertEqual(set(rows[0]), set(REVIEW_FIELDS))
            self.assertNotIn("predicted_outcome", REVIEW_FIELDS)
            self.assertTrue(all(row["reviewer_status"] == "pending" for row in rows))
            self.assertTrue(all(row["independently_reviewed"] == "no" for row in rows))

    def test_markdown_report_uses_computed_metrics_and_identifies_smoke_cases(self):
        report = run_evaluation()
        markdown = render_markdown(report, "test-commit", "2026-10-28")
        self.assertIn("Invoice match precision | TP 4, FP 0 | 4/4 (100.00%)", markdown)
        self.assertIn("False auto-approvals | 0 / 3 auto-approvals", markdown)
        self.assertIn("exact-reference", markdown)
        self.assertIn("not independently reviewed", markdown)
        self.assertIn("test-commit", markdown)


if __name__ == "__main__":
    unittest.main()
