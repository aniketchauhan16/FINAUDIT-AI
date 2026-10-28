import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import database
from app.agents.graph import reconciliation_graph
from app.agents.matcher import AMOUNT_TOLERANCE, match_transaction, score_confidence
from app.main import app
from app.models import (
    Invoice,
    MatchOutcome,
    MatchResult,
    MatchStatus,
    ReviewStatus,
    Transaction,
)


def txn(
    amount: str = "100.00",
    vendor: str = "Acme, Inc.",
    *,
    identifier: str = "TX-1",
    on: date = date(2026, 1, 1),
    currency: str = "USD",
    reference: str | None = None,
) -> Transaction:
    return Transaction(
        transaction_id=identifier,
        vendor_name=vendor,
        date=on,
        amount=Decimal(amount),
        description="Payment for invoice",
        currency=currency,
        invoice_reference=reference,
    )


def inv(
    amount: str = "100.00",
    vendor: str = "Acme Inc",
    *,
    identifier: str = "INV-1",
    on: date = date(2026, 1, 1),
    currency: str = "USD",
    number: str | None = None,
) -> Invoice:
    return Invoice(
        invoice_id=identifier,
        vendor_name=vendor,
        invoice_number=number or identifier,
        invoice_date=on,
        amount_due=Decimal(amount),
        currency=currency,
    )


def run_match(transaction: Transaction, invoices: list[Invoice], **state) -> MatchResult:
    result = match_transaction(
        {"transaction": transaction, "invoices": invoices, "match_result": None, **state}
    )["match_result"]
    return result


class DeterministicMatchingTests(unittest.TestCase):
    def test_exact_reference_match_wins_and_is_explained(self):
        tx = txn("101", reference="  inv-2 ")
        result = run_match(tx, [inv("100", identifier="INV-1"), inv("101", identifier="INV-2")])
        self.assertEqual(result.invoice_id, "INV-2")
        self.assertEqual(result.match_outcome, MatchOutcome.exact_match)
        self.assertEqual(result.status, MatchStatus.auto_approved)
        self.assertEqual(result.evidence["reference"], 1.0)

    def test_vendor_normalization_handles_case_punctuation_and_spaces(self):
        result = run_match(txn(vendor="  ACME... INC "), [inv(vendor="Acme, Inc.")])
        self.assertEqual(result.status, MatchStatus.auto_approved)

    def test_amount_tolerance_boundaries_use_decimal_comparison(self):
        inside = Decimal("100") * (Decimal("1") + AMOUNT_TOLERANCE)
        outside = inside + Decimal("0.01")
        self.assertEqual(run_match(txn(str(inside)), [inv()]).status, MatchStatus.auto_approved)
        beyond = run_match(txn(str(outside)), [inv()])
        self.assertEqual(beyond.match_outcome, MatchOutcome.amount_mismatch)
        self.assertEqual(beyond.status, MatchStatus.flagged)
        self.assertEqual(beyond.amount_difference, abs(outside - Decimal("100")))

    def test_date_window_agreement_and_mismatch(self):
        in_window = run_match(txn(on=date(2026, 1, 8)), [inv()])
        out_window = run_match(txn(on=date(2026, 1, 9)), [inv()])
        self.assertEqual(in_window.status, MatchStatus.auto_approved)
        self.assertEqual(out_window.match_outcome, MatchOutcome.date_mismatch)
        self.assertEqual(out_window.status, MatchStatus.flagged)
        self.assertIn("date_mismatch", [item.type for item in out_window.anomalies])

    def test_currency_mismatch_requires_review(self):
        result = run_match(txn(currency="EUR"), [inv(currency="USD")])
        self.assertEqual(result.match_outcome, MatchOutcome.currency_mismatch)
        self.assertEqual(result.status, MatchStatus.flagged)

    def test_zero_and_negative_amounts_are_valid_financial_values(self):
        self.assertEqual(run_match(txn("0"), [inv("0")]).status, MatchStatus.auto_approved)
        self.assertEqual(run_match(txn("-100"), [inv("-100")]).status, MatchStatus.auto_approved)

    def test_invoice_marked_paid_is_a_potential_duplicate_payment(self):
        paid = inv().model_copy(update={"paid": True})
        result = run_match(txn(), [paid])
        self.assertEqual(result.match_outcome, MatchOutcome.potential_duplicate_payment)
        self.assertEqual(result.status, MatchStatus.flagged)
        self.assertIn("invoice_already_paid", [item.type for item in result.anomalies])

    def test_missing_invoice_is_an_unmatched_anomaly(self):
        result = run_match(txn(), [])
        self.assertEqual(result.match_outcome, MatchOutcome.missing_invoice)
        self.assertEqual(result.status, MatchStatus.unmatched)
        self.assertIn("missing_invoice", [item.type for item in result.anomalies])

    def test_equally_plausible_candidates_are_not_resolved_by_input_order(self):
        result = run_match(txn(), [inv(identifier="INV-A"), inv(identifier="INV-B")])
        self.assertEqual(result.match_outcome, MatchOutcome.ambiguous_candidates)
        self.assertIsNone(result.invoice_id)
        self.assertEqual(result.status, MatchStatus.flagged)

    def test_duplicate_invoice_identifier_is_explicit(self):
        result = run_match(txn(), [inv(), inv(identifier="INV-1", amount="200")])
        self.assertEqual(result.match_outcome, MatchOutcome.duplicate_invoice)
        self.assertIn("duplicate_invoice_identifier", [item.type for item in result.anomalies])

    def test_identical_invoice_records_are_reported_as_anomalies(self):
        result = run_match(
            txn(),
            [inv(identifier="INV-A", number="SAME-NUMBER"), inv(identifier="INV-B", number="SAME-NUMBER")],
        )
        self.assertEqual(result.match_outcome, MatchOutcome.duplicate_invoice)
        self.assertIn("duplicate_invoice_record", [item.type for item in result.anomalies])

    def test_potential_duplicate_payment_and_one_to_one_assignment(self):
        prior = txn(identifier="TX-0", on=date(2025, 12, 31))
        result = run_match(
            txn(), [inv()], assigned_invoice_ids={"INV-1"}, prior_transactions=[prior]
        )
        self.assertEqual(result.match_outcome, MatchOutcome.potential_duplicate_payment)
        self.assertIsNone(result.invoice_id)
        self.assertIn("potential_duplicate_payment", [item.type for item in result.anomalies])

    def test_confidence_is_bounded_reproducible_and_penalizes_anomalies(self):
        evidence = {"vendor": 1, "amount": 0.5, "date": 1, "currency": 1, "uniqueness": 1}
        score = score_confidence(evidence)
        self.assertEqual(score, score_confidence(evidence))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 1)
        self.assertLess(score_confidence(evidence, anomaly_count=1), score)
        self.assertEqual(score_confidence({"vendor": 0}), 0.0)


class InputValidationTests(unittest.TestCase):
    def test_required_text_finite_amount_and_currency_validation(self):
        for changes in (
            {"transaction_id": "   "},
            {"vendor_name": "  "},
            {"amount": Decimal("NaN")},
            {"amount": Decimal("Infinity")},
            {"currency": "ZZZ"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                Transaction(**{**txn().model_dump(), **changes})

    def test_csv_reports_invalid_rows_and_duplicate_identifiers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "transactions.csv"
            path.write_text(
                "transaction_id,date,amount,vendor,description,category\n"
                "TX-1,2026-01-01,10,Acme,Valid,vendor_payment\n"
                "TX-1,2026-01-01,11,Acme,Duplicate id,vendor_payment\n"
                "TX-3,not-a-date,NaN,  ,Bad row,vendor_payment\n"
                "TX-4,2026-01-01,12,Acme,,vendor_payment\n",
                encoding="utf-8",
            )
            report = database.load_transactions_report(path)
        self.assertEqual([item.transaction_id for item in report.records], ["TX-1"])
        self.assertEqual([row.row_number for row in report.rejected_rows], [3, 4, 5])
        self.assertTrue(any("Duplicate transaction_id" in item for item in report.rejected_rows[0].errors))
        self.assertTrue(any("amount" in item for item in report.rejected_rows[1].errors))
        self.assertTrue(any("must not be blank" in item for item in report.rejected_rows[2].errors))
        rejected_detail = report.error_details()[0]
        self.assertEqual(rejected_detail["match_outcome"], MatchOutcome.invalid_input.value)
        self.assertEqual(rejected_detail["anomalies"][0]["type"], "duplicate_transaction_identifier")

    def test_invoice_csv_supports_paid_and_rejects_malformed_boolean(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "invoices.csv"
            path.write_text(
                "invoice_id,vendor,amount,date,description,paid\n"
                "INV-1,Acme,10,2026-01-01,Invoice,yes\n"
                "INV-2,Acme,10,2026-01-01,Invoice,sometimes\n",
                encoding="utf-8",
            )
            report = database.load_invoices_report(path)
        self.assertEqual([item.invoice_id for item in report.records], ["INV-1"])
        self.assertTrue(report.records[0].paid)
        self.assertIn("paid", report.rejected_rows[0].errors[0])


class PersistenceAndIntegrationTests(unittest.TestCase):
    def test_additive_migration_preserves_legacy_review_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            db_path = directory / "legacy.db"
            connection = sqlite3.connect(db_path)
            with connection:
                connection.execute(
                    """CREATE TABLE reconciliation_results (
                    transaction_id TEXT PRIMARY KEY, invoice_id TEXT, status TEXT NOT NULL,
                    confidence_score REAL NOT NULL, amount_difference TEXT, reason TEXT NOT NULL,
                    review_status TEXT NOT NULL, reviewed_at TEXT, reviewed_by TEXT)"""
                )
                connection.execute(
                    "INSERT INTO reconciliation_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("TX-OLD", "INV-OLD", "approved", 0.9, "0", "legacy", "approved", "2026-01-02T00:00:00+00:00", "Reviewer"),
                )
            connection.close()
            with patch.object(database, "DATA_DIR", directory), patch.object(database, "DATABASE_PATH", db_path):
                database.init_db()
                loaded = database.get_match_result("TX-OLD")
                with database.get_connection() as connection:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, database.SCHEMA_VERSION)
            self.assertEqual(loaded.review_status, ReviewStatus.approved)
            self.assertEqual(loaded.reviewed_by, "Reviewer")
            self.assertEqual(loaded.evidence, {})
            self.assertEqual(loaded.anomalies, [])

    def test_ambiguous_result_uses_existing_flagged_graph_path(self):
        final = reconciliation_graph.invoke(
            {
                "transaction": txn(),
                "invoices": [inv(identifier="INV-A"), inv(identifier="INV-B")],
                "match_result": None,
            }
        )
        self.assertEqual(final["match_result"].match_outcome, MatchOutcome.ambiguous_candidates)
        self.assertEqual(final["reconciliation_path"], "flagged")

    def test_api_run_enforces_one_to_one_and_preserves_review_reruns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            data_dir.mkdir()
            transactions = data_dir / "transactions.csv"
            invoices = data_dir / "invoices.csv"
            transactions.write_text(
                "transaction_id,date,amount,vendor,description,category\n"
                "TX-1,2026-01-01,100,Acme,First,vendor_payment\n"
                "TX-2,2026-01-02,100,Acme,Second,vendor_payment\n",
                encoding="utf-8",
            )
            invoices.write_text(
                "invoice_id,vendor,amount,date,description\n"
                "INV-1,Acme,100,2026-01-01,Invoice\n",
                encoding="utf-8",
            )
            with (
                patch.object(database, "DATA_DIR", data_dir),
                patch.object(database, "DATABASE_PATH", root / "test.db"),
                patch.object(database, "TRANSACTIONS_CSV", transactions),
                patch.object(database, "INVOICES_CSV", invoices),
            ):
                client = TestClient(app)
                first_response = client.post("/run-reconciliation")
                self.assertEqual(first_response.status_code, 200, first_response.text)
                first_run = first_response.json()
                self.assertEqual(first_run[0]["invoice_id"], "INV-1")
                self.assertIsNone(first_run[1]["invoice_id"])
                self.assertEqual(first_run[1]["match_outcome"], MatchOutcome.potential_duplicate_payment.value)
                review_response = client.post(
                    "/transactions/TX-2/approve", json={"reviewed_by": "Phase One Reviewer"}
                )
                self.assertEqual(review_response.status_code, 200, review_response.text)
                self.assertEqual(review_response.json()["review_status"], ReviewStatus.approved.value)
                second_response = client.post("/run-reconciliation")
                self.assertEqual(second_response.status_code, 200, second_response.text)
                second_run = second_response.json()
                self.assertEqual(second_run[1]["review_status"], ReviewStatus.approved.value)
                self.assertEqual(second_run[1]["reviewed_by"], "Phase One Reviewer")
                results_response = client.get("/reconciliation-results")
                self.assertEqual(results_response.status_code, 200)
                persisted = database.get_match_result("TX-2")
                self.assertEqual(persisted.reviewed_by, "Phase One Reviewer")
                self.assertEqual(persisted.match_outcome, MatchOutcome.potential_duplicate_payment)
                self.assertTrue(persisted.evidence)
                self.assertIn("potential_duplicate_payment", [item.type for item in persisted.anomalies])

    def test_api_reports_invalid_csv_rows_without_running_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            data_dir.mkdir()
            transactions = data_dir / "transactions.csv"
            invoices = data_dir / "invoices.csv"
            transactions.write_text(
                "transaction_id,date,amount,vendor,description,category\n"
                "TX-1,2026-01-01,not-money,Acme,Invalid,vendor_payment\n",
                encoding="utf-8",
            )
            invoices.write_text(
                "invoice_id,vendor,amount,date,description\nINV-1,Acme,10,2026-01-01,Invoice\n",
                encoding="utf-8",
            )
            with (
                patch.object(database, "DATA_DIR", data_dir),
                patch.object(database, "DATABASE_PATH", root / "test.db"),
                patch.object(database, "TRANSACTIONS_CSV", transactions),
                patch.object(database, "INVOICES_CSV", invoices),
            ):
                response = TestClient(app).post("/run-reconciliation")
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"]["transactions"][0]["row_number"], 2)


if __name__ == "__main__":
    unittest.main()
