import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import database
from app.agents.graph import reconciliation_graph
from app.agents.matcher import match_transaction
from app.agents.state import ReconciliationState
from app.main import ReviewAction, apply_review_status, approve_transaction, reject_transaction
from app.models import (
    Invoice,
    MatchResult,
    MatchStatus,
    ReviewStatus,
    Transaction,
)


def transaction(amount: str = "100.00", vendor: str = "Acme") -> Transaction:
    return Transaction(
        transaction_id="TX-1",
        vendor_name=vendor,
        date=date(2026, 1, 1),
        amount=Decimal(amount),
        description="Invoice payment",
    )


def invoice(amount: str = "100.00", vendor: str = "Acme") -> Invoice:
    return Invoice(
        invoice_id="INV-1",
        vendor_name=vendor,
        invoice_number="INV-1",
        invoice_date=date(2026, 1, 1),
        amount_due=Decimal(amount),
    )


class MatchingTests(unittest.TestCase):
    def test_exact_and_within_two_percent_are_auto_approved(self):
        for paid_amount in ("100.00", "102.00"):
            with self.subTest(paid_amount=paid_amount):
                result = match_transaction(
                    {"transaction": transaction(paid_amount), "invoices": [invoice()], "match_result": None}
                )["match_result"]
                self.assertEqual(result.status, MatchStatus.auto_approved)

    def test_discrepancy_above_two_percent_is_flagged(self):
        result = match_transaction(
            {"transaction": transaction("103.00"), "invoices": [invoice()], "match_result": None}
        )["match_result"]
        self.assertEqual(result.status, MatchStatus.flagged)
        self.assertEqual(result.amount_difference, Decimal("3.00"))

    def test_missing_vendor_invoice_is_unmatched(self):
        result = match_transaction(
            {"transaction": transaction(vendor="Other"), "invoices": [invoice()], "match_result": None}
        )["match_result"]
        self.assertEqual(result.status, MatchStatus.unmatched)
        self.assertIsNone(result.invoice_id)


class GraphRoutingTests(unittest.TestCase):
    def test_graph_routes_clean_and_flagged_matches(self):
        cases = (("100.00", "clean"), ("103.00", "flagged"), ("100.00", "flagged", "Other"))
        for case in cases:
            amount, expected_path, *vendor = case
            with self.subTest(amount=amount, expected_path=expected_path, vendor=vendor):
                final = reconciliation_graph.invoke(
                    {
                        "transaction": transaction(amount, vendor[0] if vendor else "Acme"),
                        "invoices": [invoice()],
                        "match_result": None,
                    }
                )
                self.assertEqual(final["reconciliation_path"], expected_path)


class ReviewPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name)
        self.data_dir_patch = patch.object(database, "DATA_DIR", self.directory)
        self.db_path_patch = patch.object(database, "DATABASE_PATH", self.directory / "test.db")
        self.data_dir_patch.start()
        self.db_path_patch.start()
        database.init_db()
        database.save_match_result(
            MatchResult(
                transaction_id="TX-1",
                invoice_id="INV-1",
                status=MatchStatus.flagged,
                confidence_score=0.65,
                amount_difference=Decimal("3.00"),
                reason="Amount differs by more than 2%.",
            )
        )

    def tearDown(self):
        self.db_path_patch.stop()
        self.data_dir_patch.stop()
        self.temp_dir.cleanup()

    def test_approve_persists_decision(self):
        reviewed = approve_transaction("TX-1", ReviewAction(reviewed_by="  Reviewer A  "))
        loaded = database.get_match_result("TX-1")
        self.assertEqual(loaded.review_status, ReviewStatus.approved)
        self.assertEqual(loaded.status, MatchStatus.approved)
        self.assertEqual(loaded.reviewed_by, "Reviewer A")
        self.assertIsNotNone(loaded.reviewed_at)
        self.assertEqual(reviewed, loaded)
        rerun_result = apply_review_status(
            MatchResult(
                transaction_id="TX-1",
                invoice_id="INV-1",
                status=MatchStatus.flagged,
                confidence_score=0.65,
                amount_difference=Decimal("3.00"),
                reason="Recomputed discrepancy.",
            )
        )
        self.assertEqual(rerun_result.status, MatchStatus.approved)
        self.assertEqual(rerun_result.reviewed_by, "Reviewer A")
        self.assertEqual(database.get_all_match_results(), [loaded])

    def test_reject_persists_and_cannot_be_overwritten(self):
        reviewed = reject_transaction("TX-1", ReviewAction(reviewed_by="Reviewer B"))
        self.assertEqual(reviewed.review_status, ReviewStatus.rejected)
        self.assertEqual(reviewed.status, MatchStatus.rejected)
        with self.assertRaises(HTTPException) as raised:
            approve_transaction("TX-1", ReviewAction(reviewed_by="Reviewer C"))
        self.assertEqual(raised.exception.status_code, 409)

    def test_missing_result_returns_not_found(self):
        with self.assertRaises(HTTPException) as raised:
            approve_transaction("missing", ReviewAction(reviewed_by="Reviewer"))
        self.assertEqual(raised.exception.status_code, 404)

    def test_blank_reviewer_is_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ReviewAction(reviewed_by="   ")


if __name__ == "__main__":
    unittest.main()
