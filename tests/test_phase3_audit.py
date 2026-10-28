import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app import database
import app.main as main_module
from app.models import (
    Anomaly,
    AnomalySeverity,
    MatchOutcome,
    MatchResult,
    MatchStatus,
    ReviewStatus,
)
from tests.auth_support import login_test_user


def pending_result(transaction_id: str) -> MatchResult:
    return MatchResult(
        transaction_id=transaction_id,
        invoice_id=None,
        status=MatchStatus.flagged,
        confidence_score=0.5,
        amount_difference=None,
        reason="Ambiguous candidates require review.",
        review_status=ReviewStatus.pending_review,
        match_outcome=MatchOutcome.ambiguous_candidates,
        evidence={"uniqueness": 0.0},
        anomalies=[
            Anomaly(
                type="ambiguous_match",
                severity=AnomalySeverity.high,
                evidence=["INV-A", "INV-B"],
                explanation="Two invoices are equally plausible.",
                recommended_action="Review the source invoices.",
                human_review_required=True,
            )
        ],
    )


class ReviewAuditApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        data_dir = root / "data"
        data_dir.mkdir()
        self.db_path = root / "audit-test.db"
        self.patchers = [
            patch.object(database, "DATA_DIR", data_dir),
            patch.object(database, "DATABASE_PATH", self.db_path),
            patch.object(database, "TRANSACTIONS_CSV", data_dir / "transactions.csv"),
            patch.object(database, "INVOICES_CSV", data_dir / "invoices.csv"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.client = TestClient(main_module.app)
        self.client.__enter__()
        self.reviewer = login_test_user(
            self.client, username="phase3_reviewer"
        )

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def save_pending(self, transaction_id: str) -> MatchResult:
        result = pending_result(transaction_id)
        return database.save_match_result(result)

    def test_approval_and_rejection_create_events_and_survive_reopen(self):
        self.save_pending("TX-APPROVE")
        self.save_pending("TX-REJECT")

        approved = self.client.post(
            "/transactions/TX-APPROVE/approve",
            json={"reason": " Invoice verified. "},
        )
        rejected = self.client.post(
            "/transactions/TX-REJECT/reject",
            json={"reason": "Invoice could not be verified."},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(approved.json()["evidence"], {"uniqueness": 0.0})
        self.assertEqual(approved.json()["anomalies"][0]["type"], "ambiguous_match")

        # Restart the API lifespan against the same temporary SQLite file.
        authorization = self.client.headers["Authorization"]
        self.client.__exit__(None, None, None)
        self.client = TestClient(main_module.app)
        self.client.__enter__()
        self.client.headers.update({"Authorization": authorization})
        approve_event = self.client.get("/transactions/TX-APPROVE/review-history")
        reject_event = self.client.get("/transactions/TX-REJECT/review-history")
        self.assertEqual(approve_event.status_code, 200)
        self.assertEqual(reject_event.status_code, 200)
        self.assertEqual(len(approve_event.json()), 1)
        self.assertEqual(len(reject_event.json()), 1)
        self.assertEqual(approve_event.json()[0]["action"], "approved")
        self.assertEqual(approve_event.json()[0]["reviewed_by"], self.reviewer)
        self.assertEqual(approve_event.json()[0]["reason"], "Invoice verified.")
        self.assertEqual(approve_event.json()[0]["previous_review_status"], "pending_review")
        self.assertEqual(approve_event.json()[0]["new_review_status"], "approved")
        self.assertEqual(reject_event.json()[0]["action"], "rejected")
        self.assertEqual(reject_event.json()[0]["reason"], "Invoice could not be verified.")

        approved_row = database.get_match_result("TX-APPROVE")
        rejected_row = database.get_match_result("TX-REJECT")
        self.assertEqual(approved_row.review_status, ReviewStatus.approved)
        self.assertEqual(rejected_row.review_status, ReviewStatus.rejected)
        self.assertEqual(approved_row.evidence, {"uniqueness": 0.0})
        self.assertEqual(approved_row.anomalies[0].type, "ambiguous_match")

    def test_repeat_and_conflicting_decisions_return_409_without_extra_events(self):
        self.save_pending("TX-CONFLICT")
        first = self.client.post(
            "/transactions/TX-CONFLICT/approve",
            json={"reason": "Approved once."},
        )
        repeat = self.client.post(
            "/transactions/TX-CONFLICT/approve", json={}
        )
        conflict = self.client.post(
            "/transactions/TX-CONFLICT/reject", json={}
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(repeat.status_code, 409)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(self.client.get("/transactions/TX-CONFLICT/review-history").json()), 1)
        self.assertEqual(database.get_match_result("TX-CONFLICT").review_status, ReviewStatus.approved)

    def test_concurrent_approve_and_reject_persist_only_one_consistent_decision(self):
        self.save_pending("TX-CONCURRENT")
        barrier = Barrier(2)

        def decide(decision: ReviewStatus):
            barrier.wait(timeout=5)
            try:
                result = database.record_review(
                    "TX-CONCURRENT",
                    decision,
                    decision.value.title(),
                    datetime.now(timezone.utc).isoformat(),
                )
                return "saved", result
            except database.ReviewConflictError:
                return "conflict", None

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(decide, (ReviewStatus.approved, ReviewStatus.rejected))
            )

        self.assertCountEqual([outcome for outcome, _ in outcomes], ["saved", "conflict"])
        stored = database.get_match_result("TX-CONCURRENT")
        history = database.get_review_history("TX-CONCURRENT")
        self.assertEqual(len(history), 1)
        self.assertIn(stored.review_status, {ReviewStatus.approved, ReviewStatus.rejected})
        self.assertEqual(history[0].new_review_status, stored.review_status)
        self.assertEqual(history[0].action, stored.review_status.value)

    def test_stale_reconciliation_upsert_cannot_reopen_a_reviewed_result(self):
        self.save_pending("TX-RERUN-RACE")
        stale_reconciliation = pending_result("TX-RERUN-RACE").model_copy(
            update={
                "reason": "Recomputed reconciliation evidence.",
                "evidence": {"refreshed_match_score": 0.8},
            }
        )
        stale_reconciliation = main_module.apply_review_status(stale_reconciliation)
        self.assertEqual(stale_reconciliation.review_status, ReviewStatus.pending_review)

        response = self.client.post(
            "/transactions/TX-RERUN-RACE/approve",
            json={"reason": "Checked invoice."},
        )
        self.assertEqual(response.status_code, 200, response.text)

        persisted = database.save_match_result(stale_reconciliation)
        self.assertEqual(persisted.review_status, ReviewStatus.approved)
        self.assertEqual(persisted.status, MatchStatus.approved)
        self.assertEqual(persisted.reviewed_by, self.reviewer)
        self.assertEqual(persisted.reason, "Recomputed reconciliation evidence.")
        self.assertEqual(persisted.evidence, {"refreshed_match_score": 0.8})
        history = database.get_review_history("TX-RERUN-RACE")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].action, "approved")
        self.assertEqual(history[0].new_review_status, persisted.review_status)

    def test_reason_is_optional_trimmed_and_length_validated(self):
        self.save_pending("TX-OPTIONAL-REASON")
        self.save_pending("TX-INVALID-REASON")

        legacy_payload = self.client.post(
            "/transactions/TX-OPTIONAL-REASON/approve",
            json={},
        )
        invalid_reason = self.client.post(
            "/transactions/TX-INVALID-REASON/reject",
            json={"reason": "x" * 1001},
        )
        self.assertEqual(legacy_payload.status_code, 200, legacy_payload.text)
        self.assertIsNone(self.client.get("/transactions/TX-OPTIONAL-REASON/review-history").json()[0]["reason"])
        self.assertEqual(invalid_reason.status_code, 422)
        self.assertEqual(database.get_match_result("TX-INVALID-REASON").review_status, ReviewStatus.pending_review)
        self.assertEqual(self.client.get("/transactions/TX-INVALID-REASON/review-history").json(), [])

    def test_history_is_chronological_empty_history_and_missing_id_errors(self):
        self.save_pending("TX-HISTORY")
        self.assertEqual(self.client.get("/transactions/TX-HISTORY/review-history").json(), [])
        self.assertEqual(self.client.get("/transactions/DOES-NOT-EXIST/review-history").status_code, 404)
        self.assertEqual(
            self.client.post("/transactions/DOES-NOT-EXIST/approve", json={}).status_code,
            404,
        )

        with database.get_connection() as connection:
            for event_id, occurred_at in (
                ("evt-later", "2026-03-02T00:00:00+00:00"),
                ("evt-earlier", "2026-03-01T00:00:00+00:00"),
            ):
                connection.execute(
                    """
                    INSERT INTO review_events (
                        event_id, transaction_id, action, reviewed_by, occurred_at, reason,
                        previous_review_status, new_review_status,
                        previous_match_status, new_match_status
                    ) VALUES (?, ?, 'approved', 'Reviewer', ?, NULL, 'pending_review', 'approved', 'flagged', 'approved')
                    """,
                    (event_id, "TX-HISTORY", occurred_at),
                )
        history = self.client.get("/transactions/TX-HISTORY/review-history").json()
        self.assertEqual([event["event_id"] for event in history], ["evt-earlier", "evt-later"])

    def test_event_insert_failure_rolls_back_review_status(self):
        self.save_pending("TX-ROLLBACK")
        with database.get_connection() as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_test_event
                BEFORE INSERT ON review_events
                BEGIN
                    SELECT RAISE(ABORT, 'simulated audit write failure');
                END
                """
            )

        response = self.client.post(
            "/transactions/TX-ROLLBACK/approve", json={}
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["detail"],
            "Review could not be recorded; no changes were saved.",
        )
        self.assertEqual(database.get_match_result("TX-ROLLBACK").review_status, ReviewStatus.pending_review)
        self.assertEqual(self.client.get("/transactions/TX-ROLLBACK/review-history").json(), [])

    def test_review_event_rows_cannot_be_updated_or_deleted_by_normal_sql(self):
        self.save_pending("TX-APPEND-ONLY")
        response = self.client.post(
            "/transactions/TX-APPEND-ONLY/reject", json={}
        )
        self.assertEqual(response.status_code, 200)
        with self.assertRaises(sqlite3.IntegrityError):
            with database.get_connection() as connection:
                connection.execute(
                    "UPDATE review_events SET reason = 'rewritten' WHERE transaction_id = ?",
                    ("TX-APPEND-ONLY",),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with database.get_connection() as connection:
                connection.execute(
                    "DELETE FROM review_events WHERE transaction_id = ?",
                    ("TX-APPEND-ONLY",),
                )


class ReviewHistoryMigrationTests(unittest.TestCase):
    def test_legacy_database_migrates_without_fabricating_review_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy_path = root / "legacy.db"
            connection = sqlite3.connect(legacy_path)
            with connection:
                connection.execute(
                    """CREATE TABLE reconciliation_results (
                    transaction_id TEXT PRIMARY KEY, invoice_id TEXT, status TEXT NOT NULL,
                    confidence_score REAL NOT NULL, amount_difference TEXT, reason TEXT NOT NULL,
                    review_status TEXT NOT NULL, reviewed_at TEXT, reviewed_by TEXT)"""
                )
                connection.execute(
                    "INSERT INTO reconciliation_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "TX-LEGACY",
                        "INV-LEGACY",
                        "approved",
                        0.9,
                        "0",
                        "legacy result",
                        "approved",
                        "2026-01-02T00:00:00+00:00",
                        "Earlier Reviewer",
                    ),
                )
            connection.close()
            with (
                patch.object(database, "DATA_DIR", root),
                patch.object(database, "DATABASE_PATH", legacy_path),
            ):
                database.init_db()
                legacy = database.get_match_result("TX-LEGACY")
                history = database.get_review_history("TX-LEGACY")
                with database.get_connection() as connection:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    event_columns = {
                        item["name"]
                        for item in connection.execute("PRAGMA table_info(review_events)")
                    }
            self.assertEqual(version, database.SCHEMA_VERSION)
            self.assertEqual(legacy.review_status, ReviewStatus.approved)
            self.assertEqual(legacy.reviewed_by, "Earlier Reviewer")
            self.assertEqual(history, [])
            self.assertIn("event_id", event_columns)
            self.assertIn("reason", event_columns)


class ReviewHistoryDashboardTests(unittest.TestCase):
    def test_review_history_page_renders_events_and_empty_history(self):
        from streamlit.testing.v1 import AppTest

        dashboard_path = Path(__file__).resolve().parents[1] / "dashboard.py"
        results = [
            {
                "transaction_id": "TX-DASH",
                "invoice_id": "INV-DASH",
                "status": "flagged",
                "review_status": "pending_review",
                "confidence_score": 0.5,
                "reason": "Needs review.",
                "anomalies": [],
                "amount_difference": None,
                "match_outcome": "ambiguous_candidates",
                "reviewed_by": None,
            }
        ]
        history_by_transaction = {
            "TX-DASH": [
                {
                    "event_id": "evt-1",
                    "transaction_id": "TX-DASH",
                    "action": "approved",
                    "reviewed_by": "Dashboard Reviewer",
                    "occurred_at": "2026-04-01T10:00:00+00:00",
                    "reason": "Supporting invoice checked.",
                    "previous_review_status": "pending_review",
                    "new_review_status": "approved",
                    "previous_match_status": "flagged",
                    "new_match_status": "approved",
                }
            ]
        }

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=20):
            path = urlsplit(request.full_url).path
            if path == "/auth/me":
                payload = {
                    "user_id": "user-auditor",
                    "username": "dashboard_auditor",
                    "role": "auditor",
                }
            elif path == "/reconciliation-results":
                payload = results
            elif path == "/transactions/TX-DASH/review-history":
                payload = history_by_transaction.get("TX-DASH", [])
            else:
                raise AssertionError(f"Unexpected dashboard API request: {path}")
            return FakeResponse(payload)

        at = AppTest.from_file(str(dashboard_path), default_timeout=20).run()
        at.session_state["access_token"] = "fake-session"
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            at.run()
            at.radio[0].set_value("Review history").run()
        self.assertFalse(at.exception, [str(item.value) for item in at.exception])
        self.assertTrue(at.dataframe)
        displayed = at.dataframe[0].value
        self.assertEqual(displayed.iloc[0]["Reviewer"], "Dashboard Reviewer")
        self.assertEqual(displayed.iloc[0]["Reason"], "Supporting invoice checked.")

        history_by_transaction["TX-DASH"] = []
        empty_at = AppTest.from_file(str(dashboard_path), default_timeout=20).run()
        empty_at.session_state["access_token"] = "fake-session"
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            empty_at.run()
            empty_at.radio[0].set_value("Review history").run()
        self.assertFalse(empty_at.exception, [str(item.value) for item in empty_at.exception])
        self.assertTrue(any("No review events are recorded" in item.value for item in empty_at.info))


if __name__ == "__main__":
    unittest.main()
