import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database
import app.main as main_module
import app.agents.graph as graph_module
from app.agents.graph import reconciliation_graph
from app.models import (
    Invoice,
    MatchOutcome,
    MatchResult,
    MatchStatus,
    ReviewStatus,
    Transaction,
)


def transaction(*, amount: str = "100.00", on: date = date(2026, 1, 1)) -> Transaction:
    return Transaction(
        transaction_id="TX-P2",
        vendor_name="Acme, Inc.",
        date=on,
        description="Invoice payment",
        amount=Decimal(amount),
    )


def invoice(
    *,
    amount: str = "100.00",
    identifier: str = "INV-P2",
    number: str | None = None,
    on: date = date(2026, 1, 1),
) -> Invoice:
    return Invoice(
        invoice_id=identifier,
        invoice_number=number or identifier,
        vendor_name="Acme Inc",
        invoice_date=on,
        amount_due=Decimal(amount),
    )


def invoke_graph(tx: Transaction | dict, invoices: list[Invoice | dict]):
    return reconciliation_graph.invoke(
        {"transaction": tx, "invoices": invoices, "match_result": None}
    )


class ExplicitGraphOrchestrationTests(unittest.TestCase):
    def test_clean_unique_match_reaches_auto_approval_path(self):
        final = invoke_graph(transaction(), [invoice()])
        self.assertEqual(final["processing_status"], "ok")
        self.assertEqual(final["decision"], graph_module.AUTO_DECISION)
        self.assertEqual(final["reconciliation_path"], "clean")
        self.assertEqual(final["match_result"].status, MatchStatus.auto_approved)
        self.assertEqual(final["match_result"].match_outcome, MatchOutcome.exact_match)
        self.assertFalse(final["match_result"].anomalies)

    def test_ambiguous_and_duplicate_candidates_are_reviewed(self):
        cases = (
            (
                "ambiguous",
                [invoice(identifier="INV-A"), invoice(identifier="INV-B")],
                MatchOutcome.ambiguous_candidates,
            ),
            (
                "duplicate identifier",
                [invoice(), invoice(identifier="INV-P2", amount="200")],
                MatchOutcome.duplicate_invoice,
            ),
        )
        for name, invoices, expected_outcome in cases:
            with self.subTest(name=name):
                final = invoke_graph(transaction(), invoices)
                self.assertEqual(final["decision"], graph_module.REVIEW_DECISION)
                self.assertEqual(final["reconciliation_path"], "flagged")
                self.assertEqual(final["match_result"].match_outcome, expected_outcome)
                self.assertEqual(final["match_result"].review_status, ReviewStatus.pending_review)

    def test_invalid_graph_input_is_returned_for_review_with_validation_evidence(self):
        raw_transaction = {
            "transaction_id": "TX-BAD",
            "vendor_name": "Acme",
            "date": "2026-01-01",
            "description": "bad amount",
            "amount": "NaN",
        }
        final = invoke_graph(raw_transaction, [invoice()])
        self.assertEqual(final["processing_status"], "failed")
        self.assertEqual(final["processing_stage"], "validation")
        self.assertEqual(final["reconciliation_path"], "flagged")
        self.assertEqual(final["match_result"].transaction_id, "TX-BAD")
        self.assertEqual(final["match_result"].match_outcome, MatchOutcome.invalid_input)
        self.assertEqual(final["match_result"].review_status, ReviewStatus.pending_review)
        self.assertTrue(final["match_result"].anomalies[0].evidence)

    def test_classifier_exception_fails_closed(self):
        with patch.object(graph_module, "classify_transaction", side_effect=RuntimeError("details omitted")):
            final = invoke_graph(transaction(), [invoice()])
        self.assertEqual(final["reconciliation_path"], "flagged")
        self.assertEqual(final["decision"], graph_module.REVIEW_DECISION)
        self.assertEqual(final["processing_stage"], "classification")
        self.assertEqual(final["match_result"].status, MatchStatus.flagged)
        self.assertEqual(final["match_result"].review_status, ReviewStatus.pending_review)
        self.assertIn("processing_error", [item.type for item in final["match_result"].anomalies])

    def test_matcher_exception_fails_closed(self):
        with patch.object(graph_module, "match_transaction", side_effect=RuntimeError("matcher failed")):
            final = invoke_graph(transaction(), [invoice()])
        self.assertEqual(final["processing_status"], "failed")
        self.assertEqual(final["processing_stage"], "matching")
        self.assertEqual(final["decision"], graph_module.REVIEW_DECISION)
        self.assertEqual(final["reconciliation_path"], "flagged")
        self.assertEqual(final["match_result"].review_status, ReviewStatus.pending_review)

    def test_anomaly_analysis_exception_fails_closed(self):
        with patch.object(graph_module, "analyze_match_result", side_effect=RuntimeError("analysis failed")):
            final = invoke_graph(transaction(), [invoice()])
        self.assertEqual(final["processing_status"], "failed")
        self.assertEqual(final["processing_stage"], "anomaly_analysis")
        self.assertEqual(final["decision"], graph_module.REVIEW_DECISION)
        self.assertEqual(final["reconciliation_path"], "flagged")
        self.assertEqual(final["match_result"].status, MatchStatus.flagged)
        self.assertEqual(final["match_result"].review_status, ReviewStatus.pending_review)

    def test_incomplete_matcher_output_cannot_reach_clean_path(self):
        with patch.object(graph_module, "match_transaction", return_value={"unexpected": True}):
            final = invoke_graph(transaction(), [invoice()])
        self.assertEqual(final["reconciliation_path"], "flagged")
        self.assertEqual(final["decision"], graph_module.REVIEW_DECISION)
        self.assertNotEqual(final["match_result"].status, MatchStatus.auto_approved)
        self.assertEqual(final["match_result"].review_status, ReviewStatus.pending_review)

    def test_inconsistent_auto_result_is_downgraded_by_anomaly_analysis(self):
        tx = transaction(on=date(2026, 1, 20))
        inv = invoice()
        unsafe = MatchResult(
            transaction_id=tx.transaction_id,
            invoice_id=inv.invoice_id,
            status=MatchStatus.auto_approved,
            confidence_score=1.0,
            amount_difference=Decimal("0"),
            reason="injected incomplete evidence",
            match_outcome=MatchOutcome.exact_match,
        )
        with patch.object(graph_module, "match_transaction", return_value={"match_result": unsafe}):
            final = invoke_graph(tx, [inv])
        self.assertEqual(final["reconciliation_path"], "flagged")
        self.assertEqual(final["decision"], graph_module.REVIEW_DECISION)
        self.assertEqual(final["match_result"].status, MatchStatus.flagged)
        self.assertIn("date_mismatch", [item.type for item in final["match_result"].anomalies])

    def test_auto_result_is_blocked_when_invoice_candidates_are_tied_or_duplicated(self):
        tx = transaction()
        invoices = [
            invoice(identifier="INV-A", number="SHARED"),
            invoice(identifier="INV-B", number="SHARED"),
        ]
        unsafe = MatchResult(
            transaction_id=tx.transaction_id,
            invoice_id="INV-A",
            status=MatchStatus.auto_approved,
            confidence_score=1.0,
            amount_difference=Decimal("0"),
            reason="injected result omitted ambiguity",
            match_outcome=MatchOutcome.exact_match,
        )
        with patch.object(graph_module, "match_transaction", return_value={"match_result": unsafe}):
            final = invoke_graph(tx, invoices)
        self.assertEqual(final["decision"], graph_module.REVIEW_DECISION)
        self.assertEqual(final["reconciliation_path"], "flagged")
        self.assertEqual(final["match_result"].status, MatchStatus.flagged)
        anomaly_types = [item.type for item in final["match_result"].anomalies]
        self.assertIn("duplicate_invoice_record", anomaly_types)
        self.assertIn("ambiguous_match", anomaly_types)

    def test_auto_result_is_blocked_for_duplicate_payment_in_prior_transactions(self):
        tx = transaction(on=date(2026, 1, 2))
        prior = Transaction(
            transaction_id="TX-PRIOR",
            vendor_name="Acme Inc",
            date=date(2026, 1, 1),
            description="Prior invoice payment",
            amount=Decimal("100.00"),
        )
        unsafe = MatchResult(
            transaction_id=tx.transaction_id,
            invoice_id="INV-P2",
            status=MatchStatus.auto_approved,
            confidence_score=1.0,
            amount_difference=Decimal("0"),
            reason="injected result omitted duplicate warning",
            match_outcome=MatchOutcome.exact_match,
        )
        with patch.object(graph_module, "match_transaction", return_value={"match_result": unsafe}):
            final = reconciliation_graph.invoke(
                {
                    "transaction": tx,
                    "invoices": [invoice(on=date(2026, 1, 2))],
                    "match_result": None,
                    "prior_transactions": [prior],
                }
            )
        self.assertEqual(final["decision"], graph_module.REVIEW_DECISION)
        self.assertEqual(final["reconciliation_path"], "flagged")
        self.assertEqual(final["match_result"].status, MatchStatus.flagged)
        self.assertIn(
            "potential_duplicate_payment",
            [item.type for item in final["match_result"].anomalies],
        )

    def test_failed_processing_state_cannot_preserve_an_auto_result(self):
        tx = transaction()
        inv = invoice()
        automatic = MatchResult(
            transaction_id=tx.transaction_id,
            invoice_id=inv.invoice_id,
            status=MatchStatus.auto_approved,
            confidence_score=1.0,
            amount_difference=Decimal("0"),
            reason="candidate result",
            match_outcome=MatchOutcome.exact_match,
        )
        decided = graph_module.decide_node(
            {
                "transaction": tx,
                "invoices": [inv],
                "match_result": automatic,
                "processing_status": "failed",
                "processing_error": "an upstream node failed",
            }
        )
        self.assertEqual(decided["decision"], graph_module.REVIEW_DECISION)
        self.assertEqual(decided["match_result"].status, MatchStatus.flagged)
        self.assertEqual(decided["match_result"].review_status, ReviewStatus.pending_review)

    def test_flagged_result_cannot_follow_auto_approval_decision(self):
        tx = transaction()
        flagged = MatchResult(
            transaction_id=tx.transaction_id,
            invoice_id=None,
            status=MatchStatus.flagged,
            confidence_score=0.5,
            amount_difference=None,
            reason="Multiple candidates require review.",
            review_status=ReviewStatus.pending_review,
            match_outcome=MatchOutcome.ambiguous_candidates,
        )
        state = {
            "transaction": tx,
            "invoices": [invoice(identifier="INV-A"), invoice(identifier="INV-B")],
            "match_result": flagged,
            "processing_status": "ok",
            "decision": graph_module.AUTO_DECISION,
        }
        self.assertEqual(graph_module.route_match(state), "flagged")
        routed = graph_module.mark_flagged_path(state)
        self.assertEqual(routed["decision"], graph_module.REVIEW_DECISION)
        self.assertEqual(routed["reconciliation_path"], "flagged")
        self.assertEqual(flagged.review_status, ReviewStatus.pending_review)


class APIFailClosedTests(unittest.TestCase):
    def run_api_with_graph_result(self, transaction_date: str, graph_result: dict):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        data_dir = root / "data"
        data_dir.mkdir()
        transactions = data_dir / "transactions.csv"
        invoices = data_dir / "invoices.csv"
        transactions.write_text(
            "transaction_id,date,amount,vendor,description,category\n"
            f"TX-API,{transaction_date},100,Acme,Invoice payment,vendor_payment\n",
            encoding="utf-8",
        )
        invoices.write_text(
            "invoice_id,vendor,amount,date,description\n"
            "INV-API,Acme,100,2026-01-01,Invoice\n",
            encoding="utf-8",
        )
        with (
            patch.object(database, "DATA_DIR", data_dir),
            patch.object(database, "DATABASE_PATH", root / "test.db"),
            patch.object(database, "TRANSACTIONS_CSV", transactions),
            patch.object(database, "INVOICES_CSV", invoices),
            patch.object(main_module, "reconciliation_graph") as graph,
        ):
            graph.invoke.return_value = graph_result
            with TestClient(main_module.app) as client:
                response = client.post("/run-reconciliation")
            saved = database.get_match_result("TX-API")
        return temp, response, saved

    @staticmethod
    def auto_result() -> MatchResult:
        return MatchResult(
            transaction_id="TX-API",
            invoice_id="INV-API",
            status=MatchStatus.auto_approved,
            confidence_score=1.0,
            amount_difference=Decimal("0"),
            reason="candidate result",
            match_outcome=MatchOutcome.exact_match,
        )

    def test_api_blocks_auto_result_when_clean_route_metadata_is_missing(self):
        temp, response, saved = self.run_api_with_graph_result(
            "2026-01-01",
            {
                "processing_status": "ok",
                "match_result": self.auto_result(),
            },
        )
        try:
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()[0]["status"], MatchStatus.flagged.value)
            self.assertEqual(response.json()[0]["review_status"], ReviewStatus.pending_review.value)
            self.assertEqual(saved.status, MatchStatus.flagged)
        finally:
            temp.cleanup()

    def test_api_rechecks_match_evidence_even_when_graph_claims_clean_route(self):
        temp, response, saved = self.run_api_with_graph_result(
            "2026-01-20",
            {
                "processing_status": "ok",
                "decision": graph_module.AUTO_DECISION,
                "reconciliation_path": "clean",
                "match_result": self.auto_result(),
            },
        )
        try:
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()[0]["status"], MatchStatus.flagged.value)
            self.assertEqual(response.json()[0]["review_status"], ReviewStatus.pending_review.value)
            self.assertEqual(saved.status, MatchStatus.flagged)
            self.assertIn("unsafe_auto_approval", [item.type for item in saved.anomalies])
        finally:
            temp.cleanup()

    def test_api_rechecks_duplicate_payment_against_prior_transactions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            data_dir.mkdir()
            transactions = data_dir / "transactions.csv"
            invoices = data_dir / "invoices.csv"
            transactions.write_text(
                "transaction_id,date,amount,vendor,description,category\n"
                "TX-API-1,2026-01-01,100,Acme,Invoice payment,vendor_payment\n"
                "TX-API-2,2026-01-02,100,Acme,Invoice payment,vendor_payment\n",
                encoding="utf-8",
            )
            invoices.write_text(
                "invoice_id,vendor,amount,date,description\n"
                "INV-API,Acme,100,2026-01-01,Invoice\n",
                encoding="utf-8",
            )

            def claim_clean_match(state):
                tx = state["transaction"]
                result = self.auto_result().model_copy(update={"transaction_id": tx.transaction_id})
                return {
                    "processing_status": "ok",
                    "decision": graph_module.AUTO_DECISION,
                    "reconciliation_path": "clean",
                    "match_result": result,
                }

            with (
                patch.object(database, "DATA_DIR", data_dir),
                patch.object(database, "DATABASE_PATH", root / "test.db"),
                patch.object(database, "TRANSACTIONS_CSV", transactions),
                patch.object(database, "INVOICES_CSV", invoices),
                patch.object(main_module, "reconciliation_graph") as graph,
            ):
                graph.invoke.side_effect = claim_clean_match
                with TestClient(main_module.app) as client:
                    response = client.post("/run-reconciliation")
                saved = database.get_all_match_results()

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0].status, MatchStatus.auto_approved)
            self.assertEqual(saved[1].status, MatchStatus.flagged)
            self.assertIn("potential_duplicate_payment", [item.type for item in saved[1].anomalies])

    def test_unhandled_graph_exception_is_saved_pending_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            data_dir.mkdir()
            transactions = data_dir / "transactions.csv"
            invoices = data_dir / "invoices.csv"
            transactions.write_text(
                "transaction_id,date,amount,vendor,description,category\n"
                "TX-API,2026-01-01,100,Acme,Invoice payment,vendor_payment\n",
                encoding="utf-8",
            )
            invoices.write_text(
                "invoice_id,vendor,amount,date,description\n"
                "INV-API,Acme,100,2026-01-01,Invoice\n",
                encoding="utf-8",
            )
            with (
                patch.object(database, "DATA_DIR", data_dir),
                patch.object(database, "DATABASE_PATH", root / "test.db"),
                patch.object(database, "TRANSACTIONS_CSV", transactions),
                patch.object(database, "INVOICES_CSV", invoices),
                patch.object(main_module, "reconciliation_graph") as graph,
            ):
                graph.invoke.side_effect = RuntimeError("unexpected graph failure")
                response = TestClient(main_module.app).post("/run-reconciliation")
                pending = database.get_pending_review_results()
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()[0]
            self.assertEqual(result["status"], MatchStatus.flagged.value)
            self.assertEqual(result["review_status"], ReviewStatus.pending_review.value)
            self.assertEqual(pending[0].transaction_id, "TX-API")
            self.assertEqual(pending[0].match_outcome, MatchOutcome.invalid_input)


if __name__ == "__main__":
    unittest.main()
