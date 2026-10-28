import csv
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from app.models import Invoice, MatchResult, MatchStatus, ReviewStatus, Transaction


DATA_DIR = Path("data")
TRANSACTIONS_CSV = DATA_DIR / "transactions.csv"
INVOICES_CSV = DATA_DIR / "invoices.csv"
DATABASE_PATH = DATA_DIR / "finaudit.db"


def load_transactions() -> list[Transaction]:
    transactions: list[Transaction] = []

    with TRANSACTIONS_CSV.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            transactions.append(
                Transaction(
                    transaction_id=row["transaction_id"],
                    date=row["date"],
                    amount=row["amount"],
                    vendor_name=row["vendor"],
                    description=row["description"],
                    category=row["category"],
                )
            )

    return transactions


def load_invoices() -> list[Invoice]:
    invoices: list[Invoice] = []

    with INVOICES_CSV.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            invoices.append(
                Invoice(
                    invoice_id=row["invoice_id"],
                    vendor_name=row["vendor"],
                    invoice_number=row["invoice_id"],
                    invoice_date=row["date"],
                    amount_due=row["amount"],
                )
            )

    return invoices


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reconciliation_results (
                transaction_id TEXT PRIMARY KEY,
                invoice_id TEXT,
                status TEXT NOT NULL,
                confidence_score REAL NOT NULL,
                amount_difference TEXT,
                reason TEXT NOT NULL,
                review_status TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by TEXT
            )
            """
        )


def save_match_result(match_result: MatchResult) -> MatchResult:
    init_db()

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO reconciliation_results (
                transaction_id,
                invoice_id,
                status,
                confidence_score,
                amount_difference,
                reason,
                review_status,
                reviewed_at,
                reviewed_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transaction_id) DO UPDATE SET
                invoice_id = excluded.invoice_id,
                status = excluded.status,
                confidence_score = excluded.confidence_score,
                amount_difference = excluded.amount_difference,
                reason = excluded.reason,
                review_status = excluded.review_status,
                reviewed_at = excluded.reviewed_at,
                reviewed_by = excluded.reviewed_by
            """,
            (
                match_result.transaction_id,
                match_result.invoice_id,
                match_result.status.value,
                match_result.confidence_score,
                str(match_result.amount_difference)
                if match_result.amount_difference is not None
                else None,
                match_result.reason,
                match_result.review_status.value,
                match_result.reviewed_at.isoformat()
                if match_result.reviewed_at is not None
                else None,
                match_result.reviewed_by,
            ),
        )

    return match_result


def save_match_results(match_results: list[MatchResult]) -> list[MatchResult]:
    for match_result in match_results:
        save_match_result(match_result)

    return match_results


def get_match_result(transaction_id: str) -> MatchResult | None:
    init_db()

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM reconciliation_results
            WHERE transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()

    if row is None:
        return None

    return row_to_match_result(row)


def get_pending_review_results() -> list[MatchResult]:
    init_db()

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM reconciliation_results
            WHERE review_status = ?
            ORDER BY transaction_id
            """,
            (ReviewStatus.pending_review.value,),
        ).fetchall()

    return [row_to_match_result(row) for row in rows]


def get_all_match_results() -> list[MatchResult]:
    """Return saved reconciliation results for dashboard and reporting views."""
    init_db()

    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM reconciliation_results ORDER BY transaction_id"
        ).fetchall()

    return [row_to_match_result(row) for row in rows]


class ReviewNotFoundError(Exception):
    """Raised when a transaction has no saved reconciliation result."""


class ReviewConflictError(Exception):
    """Raised when a reconciliation result is not awaiting review."""


def record_review(
    transaction_id: str, decision: ReviewStatus, reviewed_by: str, reviewed_at: str
) -> MatchResult:
    """Persist a human decision only while the result is pending review."""
    if decision not in {ReviewStatus.approved, ReviewStatus.rejected}:
        raise ValueError("A review decision must be approved or rejected.")

    match_status = (
        MatchStatus.approved if decision == ReviewStatus.approved else MatchStatus.rejected
    )
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE reconciliation_results
            SET review_status = ?, reviewed_at = ?, reviewed_by = ?, status = ?
            WHERE transaction_id = ? AND review_status = ?
            """,
            (
                decision.value,
                reviewed_at,
                reviewed_by,
                match_status.value,
                transaction_id,
                ReviewStatus.pending_review.value,
            ),
        )
        if cursor.rowcount == 0:
            exists = connection.execute(
                "SELECT 1 FROM reconciliation_results WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if exists is None:
                raise ReviewNotFoundError(transaction_id)
            raise ReviewConflictError(transaction_id)

        row = connection.execute(
            "SELECT * FROM reconciliation_results WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()

    return row_to_match_result(row)


def row_to_match_result(row: sqlite3.Row) -> MatchResult:
    return MatchResult(
        transaction_id=row["transaction_id"],
        invoice_id=row["invoice_id"],
        status=row["status"],
        confidence_score=row["confidence_score"],
        amount_difference=Decimal(row["amount_difference"])
        if row["amount_difference"] is not None
        else None,
        reason=row["reason"],
        review_status=row["review_status"],
        reviewed_at=row["reviewed_at"],
        reviewed_by=row["reviewed_by"],
    )
