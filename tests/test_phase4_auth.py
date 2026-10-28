import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app import auth, database
import app.main as main_module
from app.models import MatchResult, MatchStatus, ReviewStatus, UserRole
from tests.auth_support import TEST_PASSWORD, create_test_session


class AuthenticationAndRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_dir = root / "data"
        self.data_dir.mkdir()
        transactions = self.data_dir / "transactions.csv"
        invoices = self.data_dir / "invoices.csv"
        transactions.write_text(
            "transaction_id,date,amount,vendor,description,category\n"
            "TX-AUTH,2026-01-01,100,Acme,Test payment,vendor_payment\n",
            encoding="utf-8",
        )
        invoices.write_text(
            "invoice_id,vendor,amount,date,description\n"
            "INV-AUTH,Acme,100,2026-01-01,Test invoice\n",
            encoding="utf-8",
        )
        self.patchers = [
            patch.object(database, "DATA_DIR", self.data_dir),
            patch.object(database, "DATABASE_PATH", root / "auth-test.db"),
            patch.object(database, "TRANSACTIONS_CSV", transactions),
            patch.object(database, "INVOICES_CSV", invoices),
            patch.dict(
                os.environ,
                {
                    "FINAUDIT_BOOTSTRAP_ADMIN_USERNAME": "",
                    "FINAUDIT_BOOTSTRAP_ADMIN_PASSWORD": "",
                },
            ),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.client = TestClient(main_module.app)
        self.client.__enter__()
        self.anon = TestClient(main_module.app)
        self.anon.__enter__()
        self.admin_name, self.admin_token = create_test_session(
            self.client, UserRole.admin, "phase4_admin"
        )
        self.admin_headers = {"Authorization": f"Bearer {self.admin_token}"}

    def tearDown(self):
        self.anon.__exit__(None, None, None)
        self.client.__exit__(None, None, None)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def create_session(self, username: str, role: UserRole) -> tuple[str, dict[str, str]]:
        name, token = create_test_session(self.client, role, username)
        return name, {"Authorization": f"Bearer {token}"}

    @staticmethod
    def pending_result(transaction_id: str) -> MatchResult:
        return MatchResult(
            transaction_id=transaction_id,
            invoice_id=None,
            status=MatchStatus.flagged,
            confidence_score=0.4,
            amount_difference=None,
            reason="Needs a human review.",
            review_status=ReviewStatus.pending_review,
        )

    def test_public_health_and_unauthenticated_requests_are_restricted(self):
        self.assertEqual(self.anon.get("/health").status_code, 200)
        for method, path, kwargs in (
            ("get", "/auth/me", {}),
            ("get", "/transactions", {}),
            ("get", "/invoices", {}),
            ("post", "/run-reconciliation", {}),
            ("get", "/transactions/flagged", {}),
            ("get", "/reconciliation-results", {}),
            ("get", "/transactions/TX-MISSING/review-history", {}),
            ("post", "/transactions/TX-MISSING/approve", {"json": {}}),
            ("post", "/transactions/TX-MISSING/reject", {"json": {}}),
            ("get", "/admin/users", {}),
            ("post", "/auth/logout", {}),
        ):
            with self.subTest(method=method, path=path):
                response = getattr(self.anon, method)(path, **kwargs)
                self.assertEqual(response.status_code, 401, response.text)

    def test_unknown_and_malformed_bearer_tokens_are_rejected(self):
        for token in ("unknown-token", "not a valid opaque token !!!"):
            with self.subTest(token=token):
                response = self.client.get(
                    "/auth/me", headers={"Authorization": f"Bearer {token}"}
                )
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(token, response.text)

    def test_login_uses_case_insensitive_username_and_stores_only_token_hash(self):
        response = self.anon.post(
            "/auth/login",
            json={"username": self.admin_name.upper(), "password": TEST_PASSWORD},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        token = response.json()["access_token"]
        me = self.anon.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], self.admin_name)
        self.assertEqual(me.json()["role"], "admin")
        with database.get_connection() as connection:
            stored_count = connection.execute(
                "SELECT COUNT(*) FROM auth_sessions WHERE token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()[0]
            stored_password = connection.execute(
                "SELECT password_hash FROM users WHERE username = ?", (self.admin_name,)
            ).fetchone()[0]
        self.assertEqual(stored_count, 1)
        self.assertNotEqual(stored_password, TEST_PASSWORD)

    def test_password_hashes_use_the_configured_work_factor_and_distinct_salts(self):
        first_hash = auth.hash_password(TEST_PASSWORD)
        second_hash = auth.hash_password(TEST_PASSWORD)
        first_algorithm, first_rounds, first_salt, first_digest = first_hash.split("$")
        second_algorithm, second_rounds, second_salt, second_digest = second_hash.split("$")

        self.assertEqual(first_algorithm, "pbkdf2_sha256")
        self.assertEqual(second_algorithm, "pbkdf2_sha256")
        self.assertEqual(int(first_rounds), auth.PASSWORD_ITERATIONS)
        self.assertEqual(int(second_rounds), auth.PASSWORD_ITERATIONS)
        self.assertNotEqual(first_salt, second_salt)
        self.assertEqual(len(bytes.fromhex(first_salt)), 16)
        self.assertEqual(len(bytes.fromhex(second_salt)), 16)
        self.assertNotEqual(first_digest, second_digest)
        self.assertTrue(auth.verify_password(TEST_PASSWORD, first_hash))
        self.assertTrue(auth.verify_password(TEST_PASSWORD, second_hash))

    def test_invalid_expired_and_revoked_sessions_are_denied(self):
        invalid = self.anon.post(
            "/auth/login", json={"username": self.admin_name, "password": "wrong password"}
        )
        self.assertEqual(invalid.status_code, 401)
        unknown_user = self.anon.post(
            "/auth/login", json={"username": "unknown_account", "password": "wrong password"}
        )
        self.assertEqual(unknown_user.status_code, 401)
        self.assertEqual(unknown_user.json(), invalid.json())
        self.assertEqual(invalid.headers.get("cache-control"), "no-store")
        self.assertEqual(unknown_user.headers.get("cache-control"), "no-store")
        self.assertNotIn("wrong password", invalid.text)
        self.assertNotIn(TEST_PASSWORD, invalid.text)

        token = self.admin_token
        self.assertEqual(
            self.client.post("/auth/logout", headers=self.admin_headers).status_code, 200
        )
        self.assertEqual(
            self.client.get("/auth/me", headers=self.admin_headers).status_code, 401
        )

        _, fresh_headers = self.create_session("fresh_admin", UserRole.admin)
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?",
                (
                    (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                    auth.token_digest(fresh_headers["Authorization"].removeprefix("Bearer ")),
                ),
            )
        expired = self.client.get("/auth/me", headers=fresh_headers)
        self.assertEqual(expired.status_code, 401)

    def test_passwords_are_not_echoed_in_validation_errors(self):
        sensitive_password = "Sensitive-Password-" + ("x" * 140)
        invalid_login = self.anon.post(
            "/auth/login",
            json={"username": self.admin_name, "password": sensitive_password},
        )
        self.assertEqual(invalid_login.status_code, 422, invalid_login.text)
        self.assertNotIn(sensitive_password, invalid_login.text)
        self.assertEqual(invalid_login.headers.get("cache-control"), "no-store")

        invalid_user = self.client.post(
            "/admin/users",
            headers=self.admin_headers,
            json={
                "username": "password_echo_check",
                "password": sensitive_password,
                "role": "reviewer",
            },
        )
        self.assertEqual(invalid_user.status_code, 422, invalid_user.text)
        self.assertNotIn(sensitive_password, invalid_user.text)
        self.assertEqual(invalid_user.headers.get("cache-control"), "no-store")

    def test_valid_server_session_survives_api_restart(self):
        headers = self.admin_headers.copy()
        self.client.__exit__(None, None, None)
        self.client = TestClient(main_module.app)
        self.client.__enter__()
        response = self.client.get("/auth/me", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["username"], self.admin_name)

    def test_role_permissions_include_documented_reviewer_run_permission(self):
        _, reviewer_headers = self.create_session("phase4_reviewer", UserRole.reviewer)
        _, auditor_headers = self.create_session("phase4_auditor", UserRole.auditor)
        database.save_match_result(self.pending_result("TX-ROLE-CHECK"))

        self.assertEqual(self.client.get("/admin/users", headers=self.admin_headers).status_code, 200)
        self.assertEqual(self.client.get("/admin/users", headers=reviewer_headers).status_code, 403)
        self.assertEqual(self.client.get("/admin/users", headers=auditor_headers).status_code, 403)
        for headers in (reviewer_headers, auditor_headers):
            self.assertEqual(
                self.client.post(
                    "/admin/users",
                    headers=headers,
                    json={
                        "username": f"blocked_{headers['Authorization'][-8:]}",
                        "password": TEST_PASSWORD,
                        "role": "auditor",
                    },
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.patch(
                    "/admin/users/some-user",
                    headers=headers,
                    json={"role": "admin"},
                ).status_code,
                403,
            )
        self.assertEqual(self.client.get("/transactions", headers=self.admin_headers).status_code, 403)
        self.assertEqual(
            self.client.get("/reconciliation-results", headers=self.admin_headers).status_code,
            403,
        )
        self.assertEqual(self.client.get("/transactions", headers=auditor_headers).status_code, 200)
        self.assertEqual(self.client.get("/invoices", headers=auditor_headers).status_code, 200)
        self.assertEqual(self.client.get("/reconciliation-results", headers=auditor_headers).status_code, 200)
        self.assertEqual(self.client.get("/transactions/flagged", headers=auditor_headers).status_code, 200)
        self.assertEqual(self.client.post("/run-reconciliation", headers=auditor_headers).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/transactions/TX-ROLE-CHECK/approve",
                headers=auditor_headers,
                json={},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/transactions/TX-ROLE-CHECK/approve",
                headers=self.admin_headers,
                json={},
            ).status_code,
            403,
        )
        approved = self.client.post(
            "/transactions/TX-ROLE-CHECK/approve",
            headers=reviewer_headers,
            json={"reason": "Verified invoice."},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["reviewed_by"], "phase4_reviewer")
        history = self.client.get(
            "/transactions/TX-ROLE-CHECK/review-history", headers=auditor_headers
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()[0]["reviewed_by"], "phase4_reviewer")
        self.assertEqual(
            self.client.post("/run-reconciliation", headers=reviewer_headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/run-reconciliation", headers=self.admin_headers).status_code,
            403,
        )

    def test_audit_identity_comes_from_session_not_request_body(self):
        _, reviewer_headers = self.create_session("attributed_reviewer", UserRole.reviewer)
        database.save_match_result(self.pending_result("TX-AUTHOR"))
        spoofed = self.client.post(
            "/transactions/TX-AUTHOR/approve",
            headers=reviewer_headers,
            json={"reviewed_by": "admin", "reason": "Spoof attempt."},
        )
        self.assertEqual(spoofed.status_code, 422)
        self.assertEqual(database.get_review_history("TX-AUTHOR"), [])

        accepted = self.client.post(
            "/transactions/TX-AUTHOR/approve",
            headers=reviewer_headers,
            json={"reason": "Reviewed by authenticated user."},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        event = database.get_review_history("TX-AUTHOR")[0]
        principal = self.client.get("/auth/me", headers=reviewer_headers).json()
        self.assertEqual(event.reviewed_by, "attributed_reviewer")
        self.assertEqual(event.reviewer_user_id, principal["user_id"])

    def test_admin_user_management_validates_passwords_and_protects_last_admin(self):
        created = self.client.post(
            "/admin/users",
            headers=self.admin_headers,
            json={
                "username": "new_auditor",
                "password": "A-Long-Password-13",
                "role": "auditor",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn("password", created.json())
        self.assertNotIn("password_hash", created.json())
        duplicate = self.client.post(
            "/admin/users",
            headers=self.admin_headers,
            json={
                "username": "NEW_AUDITOR",
                "password": "A-Long-Password-13",
                "role": "auditor",
            },
        )
        self.assertEqual(duplicate.status_code, 409)
        weak = self.client.post(
            "/admin/users",
            headers=self.admin_headers,
            json={"username": "weak_user", "password": "short", "role": "reviewer"},
        )
        self.assertEqual(weak.status_code, 422)
        whitespace = self.client.post(
            "/admin/users",
            headers=self.admin_headers,
            json={"username": "blank_pass", "password": " " * 12, "role": "reviewer"},
        )
        self.assertEqual(whitespace.status_code, 422)
        users = self.client.get("/admin/users", headers=self.admin_headers).json()
        admin_id = next(user["user_id"] for user in users if user["username"] == self.admin_name)
        last_admin = self.client.patch(
            f"/admin/users/{admin_id}",
            headers=self.admin_headers,
            json={"is_active": False},
        )
        self.assertEqual(last_admin.status_code, 409)

    def test_disabling_user_invalidates_existing_session_and_role_changes_apply_immediately(self):
        _, reviewer_headers = self.create_session("change_role", UserRole.reviewer)
        users = self.client.get("/admin/users", headers=self.admin_headers).json()
        reviewer = next(user for user in users if user["username"] == "change_role")
        demoted = self.client.patch(
            f"/admin/users/{reviewer['user_id']}",
            headers=self.admin_headers,
            json={"role": "auditor"},
        )
        self.assertEqual(demoted.status_code, 200)
        self.assertEqual(
            self.client.get("/auth/me", headers=reviewer_headers).status_code, 401
        )
        auditor_login = self.client.post(
            "/auth/login",
            json={"username": "change_role", "password": TEST_PASSWORD},
        )
        self.assertEqual(auditor_login.status_code, 200, auditor_login.text)
        auditor_headers = {
            "Authorization": f"Bearer {auditor_login.json()['access_token']}"
        }
        self.assertEqual(
            self.client.post("/run-reconciliation", headers=auditor_headers).status_code,
            403,
        )
        disabled = self.client.patch(
            f"/admin/users/{reviewer['user_id']}",
            headers=self.admin_headers,
            json={"is_active": False},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers=auditor_headers).status_code, 401)
        reenabled = self.client.patch(
            f"/admin/users/{reviewer['user_id']}",
            headers=self.admin_headers,
            json={"is_active": True},
        )
        self.assertEqual(reenabled.status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers=reviewer_headers).status_code, 401)
        self.assertEqual(self.client.get("/auth/me", headers=auditor_headers).status_code, 401)
        reauthenticated = self.client.post(
            "/auth/login",
            json={"username": "change_role", "password": TEST_PASSWORD},
        )
        self.assertEqual(reauthenticated.status_code, 200, reauthenticated.text)
        reauthenticated_headers = {
            "Authorization": f"Bearer {reauthenticated.json()['access_token']}"
        }
        self.assertEqual(self.client.get("/auth/me", headers=reauthenticated_headers).status_code, 200)

    def test_phase3_database_migration_adds_identity_column_without_fabricating_events(self):
        legacy_path = self.data_dir / "phase3-legacy.db"
        connection = sqlite3.connect(legacy_path)
        with connection:
            connection.execute(
                """CREATE TABLE reconciliation_results (
                   transaction_id TEXT PRIMARY KEY, invoice_id TEXT, status TEXT NOT NULL,
                   confidence_score REAL NOT NULL, amount_difference TEXT, reason TEXT NOT NULL,
                   review_status TEXT NOT NULL, reviewed_at TEXT, reviewed_by TEXT)"""
            )
            connection.execute(
                "INSERT INTO reconciliation_results VALUES ('TX-OLD', NULL, 'approved', 1, NULL, 'legacy', 'approved', NULL, 'Old Reviewer')"
            )
            connection.execute(
                """CREATE TABLE review_events (
                   event_id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL,
                   action TEXT NOT NULL, reviewed_by TEXT NOT NULL, occurred_at TEXT NOT NULL,
                   reason TEXT, previous_review_status TEXT NOT NULL, new_review_status TEXT NOT NULL,
                   previous_match_status TEXT NOT NULL, new_match_status TEXT NOT NULL)"""
            )
            connection.execute(
                "INSERT INTO review_events VALUES ('evt-old', 'TX-OLD', 'approved', 'Old Reviewer', '2026-01-01T00:00:00+00:00', NULL, 'pending_review', 'approved', 'flagged', 'approved')"
            )
            connection.execute("PRAGMA user_version = 2")
        connection.close()
        with patch.object(database, "DATABASE_PATH", legacy_path):
            database.init_db()
            history = database.get_review_history("TX-OLD")
            with database.get_connection() as migrated:
                columns = {
                    item["name"] for item in migrated.execute("PRAGMA table_info(review_events)")
                }
                version = migrated.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertIn("reviewer_user_id", columns)
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0].reviewer_user_id)


class BootstrapAdminTests(unittest.TestCase):
    def test_invalid_bootstrap_password_is_not_in_startup_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            data.mkdir()
            password = "Nope"
            with (
                patch.object(database, "DATA_DIR", data),
                patch.object(database, "DATABASE_PATH", data / "bootstrap-invalid.db"),
                patch.dict(
                    os.environ,
                    {
                        "FINAUDIT_BOOTSTRAP_ADMIN_USERNAME": "bootstrap_admin",
                        "FINAUDIT_BOOTSTRAP_ADMIN_PASSWORD": password,
                    },
                ),
            ):
                async def start_application():
                    async with main_module.lifespan(main_module.app):
                        pass

                with self.assertRaises(RuntimeError) as raised:
                    asyncio.run(start_application())

            rendered = "".join(traceback.format_exception(raised.exception))
            self.assertNotIn(password, rendered)

    def test_environment_bootstrap_creates_admin_once_and_never_resets_password(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            password = "Bootstrap-Admin-Password-42!"
            env = {
                "FINAUDIT_BOOTSTRAP_ADMIN_USERNAME": "bootstrap_admin",
                "FINAUDIT_BOOTSTRAP_ADMIN_PASSWORD": password,
            }
            patches = [
                patch.object(database, "DATA_DIR", data),
                patch.object(database, "DATABASE_PATH", data / "bootstrap.db"),
                patch.object(database, "TRANSACTIONS_CSV", data / "transactions.csv"),
                patch.object(database, "INVOICES_CSV", data / "invoices.csv"),
                patch.dict(os.environ, env),
            ]
            for item in patches:
                item.start()
            try:
                with TestClient(main_module.app) as client:
                    first = client.post(
                        "/auth/login", json={"username": "bootstrap_admin", "password": password}
                    )
                    self.assertEqual(first.status_code, 200, first.text)
                with patch.dict(
                    os.environ,
                    {
                        "FINAUDIT_BOOTSTRAP_ADMIN_USERNAME": "attacker_admin",
                        "FINAUDIT_BOOTSTRAP_ADMIN_PASSWORD": "Changed-Password-After-Setup!",
                    },
                ):
                    with TestClient(main_module.app):
                        pass
                self.assertEqual(len(database.list_users()), 1)
                with TestClient(main_module.app) as client:
                    old_password = client.post(
                        "/auth/login", json={"username": "bootstrap_admin", "password": password}
                    )
                    new_password = client.post(
                        "/auth/login",
                        json={"username": "bootstrap_admin", "password": "Changed-Password-After-Setup!"},
                    )
                self.assertEqual(old_password.status_code, 200)
                self.assertEqual(new_password.status_code, 401)
            finally:
                for item in reversed(patches):
                    item.stop()


class DashboardLoginTests(unittest.TestCase):
    def test_login_sends_password_once_and_uses_bearer_session_for_api_calls(self):
        from streamlit.testing.v1 import AppTest

        dashboard = Path(__file__).resolve().parents[1] / "dashboard.py"
        requests = []

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
            requests.append((path, request.headers, request.data))
            if path == "/auth/login":
                payload = {
                    "access_token": "test-session-token",
                    "token_type": "bearer",
                    "expires_at": "2026-10-28T16:00:00+00:00",
                    "user": {
                        "user_id": "user-reviewer",
                        "username": "dashboard_reviewer",
                        "role": "reviewer",
                    },
                }
            elif path == "/auth/me":
                payload = {
                    "user_id": "user-reviewer",
                    "username": "dashboard_reviewer",
                    "role": "reviewer",
                }
            elif path == "/reconciliation-results":
                payload = []
            else:
                raise AssertionError(f"Unexpected dashboard API request: {path}")
            return FakeResponse(payload)

        at = AppTest.from_file(str(dashboard), default_timeout=20).run()
        at.text_input[0].set_value("dashboard_reviewer")
        at.text_input[1].set_value("not-a-real-password")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            at.button[0].click().run()

        self.assertFalse(at.exception, [str(item.value) for item in at.exception])
        self.assertIn("Review queue", at.radio[0].options)
        self.assertEqual(at.session_state["access_token"], "test-session-token")
        self.assertEqual(requests[0][0], "/auth/login")
        self.assertEqual(requests[1][0], "/auth/me")
        self.assertEqual(requests[2][0], "/reconciliation-results")
        self.assertEqual(
            requests[2][1].get("Authorization"), "Bearer test-session-token"
        )
        self.assertNotIn("reviewed_by", json.loads(requests[0][2]))

    def test_admin_sees_user_management_and_dashboard_handles_user_list(self):
        from streamlit.testing.v1 import AppTest

        dashboard = Path(__file__).resolve().parents[1] / "dashboard.py"
        users = [
            {
                "user_id": "user-admin",
                "username": "dashboard_admin",
                "role": "admin",
                "is_active": True,
                "created_at": "2026-10-28T12:00:00+00:00",
            },
            {
                "user_id": "user-auditor",
                "username": "dashboard_auditor",
                "role": "auditor",
                "is_active": True,
                "created_at": "2026-10-28T12:05:00+00:00",
            },
        ]

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
                payload = {"user_id": "user-admin", "username": "dashboard_admin", "role": "admin"}
            elif path == "/reconciliation-results":
                payload = []
            elif path == "/admin/users":
                payload = users
            else:
                raise AssertionError(f"Unexpected dashboard API request: {path}")
            return FakeResponse(payload)

        at = AppTest.from_file(str(dashboard), default_timeout=20).run()
        at.session_state["access_token"] = "admin-session"
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            at.run()
            self.assertIn("User management", at.radio[0].options)
            at.radio[0].set_value("User management").run()
        self.assertFalse(at.exception, [str(item.value) for item in at.exception])
        self.assertTrue(at.dataframe)
        self.assertTrue(any(item.label == "Account to update" for item in at.selectbox))

    def test_review_queue_renders_untrusted_csv_text_literally(self):
        from streamlit.testing.v1 import AppTest

        dashboard = Path(__file__).resolve().parents[1] / "dashboard.py"
        injected_markdown = "![remote pixel](https://example.invalid/pixel.png)"
        pending = [
            {
                "transaction_id": "TX-MARKDOWN",
                "invoice_id": None,
                "status": "flagged",
                "review_status": "pending_review",
                "amount_difference": None,
                "confidence_score": 0.2,
                "reason": injected_markdown,
                "anomalies": [
                    {
                        "type": "vendor_mismatch",
                        "severity": "high",
                        "explanation": injected_markdown,
                        "evidence": [injected_markdown],
                        "recommended_action": injected_markdown,
                    }
                ],
            }
        ]

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
                    "user_id": "user-reviewer",
                    "username": "dashboard_reviewer",
                    "role": "reviewer",
                }
            elif path == "/reconciliation-results":
                payload = []
            elif path == "/transactions/flagged":
                payload = pending
            elif path == "/transactions":
                payload = [
                    {
                        "transaction_id": "TX-MARKDOWN",
                        "vendor_name": injected_markdown,
                        "date": "2026-01-01",
                        "amount": "100.00",
                    }
                ]
            else:
                raise AssertionError(f"Unexpected dashboard API request: {path}")
            return FakeResponse(payload)

        at = AppTest.from_file(str(dashboard), default_timeout=20).run()
        at.session_state["access_token"] = "review-session"
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            at.run()
            at.radio[0].set_value("Review queue").run()

        self.assertFalse(at.exception, [str(item.value) for item in at.exception])
        literal_text = "\n".join(str(item.value) for item in at.text)
        rendered_markdown = "\n".join(str(item.value) for item in at.markdown)
        self.assertIn(injected_markdown, literal_text)
        self.assertNotIn(injected_markdown, rendered_markdown)

if __name__ == "__main__":
    unittest.main()
