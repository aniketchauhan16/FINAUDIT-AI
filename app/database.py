import csv
import json
import sqlite3
from dataclasses import dataclass
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Generic, Iterator, TypeVar

from pydantic import ValidationError

from app.models import Anomaly, AnomalySeverity, Invoice, MatchOutcome, MatchResult, MatchStatus, ReviewStatus, Transaction


DATA_DIR = Path("data")
TRANSACTIONS_CSV = DATA_DIR / "transactions.csv"
INVOICES_CSV = DATA_DIR / "invoices.csv"
DATABASE_PATH = DATA_DIR / "finaudit.db"
SCHEMA_VERSION = 1

T = TypeVar("T")


@dataclass(frozen=True)
class CSVRejectedRow:
    row_number: int
    identifier: str | None
    errors: list[str]

    def detail(self) -> dict:
        duplicate = any("Duplicate " in error for error in self.errors)
        anomaly = Anomaly(
            type=(
                "duplicate_invoice_identifier"
                if duplicate and any("invoice_id" in error for error in self.errors)
                else "duplicate_transaction_identifier"
                if duplicate and any("transaction_id" in error for error in self.errors)
                else "duplicate_identifier"
                if duplicate
                else "invalid_input"
            ),
            severity=AnomalySeverity.high if duplicate else AnomalySeverity.medium,
            evidence=[f"CSV row: {self.row_number}", *([f"Identifier: {self.identifier}"] if self.identifier else [])],
            explanation=" ".join(self.errors),
            recommended_action="Correct this CSV row and rerun validation.",
            human_review_required=True,
        )
        return {
            "row_number": self.row_number,
            "identifier": self.identifier,
            "errors": self.errors,
            "match_outcome": MatchOutcome.invalid_input.value,
            "anomalies": [anomaly.model_dump(mode="json")],
        }


@dataclass
class CSVLoadReport(Generic[T]):
    records: list[T]
    rejected_rows: list[CSVRejectedRow]

    def error_details(self) -> list[dict]:
        return [row.detail() for row in self.rejected_rows]


class CSVDataValidationError(ValueError):
    def __init__(self, source: Path, rejected_rows: list[CSVRejectedRow]):
        self.source = str(source)
        self.rejected_rows = rejected_rows
        super().__init__(f"{len(rejected_rows)} invalid row(s) in {source}")

    def detail(self) -> dict:
        return {
            "message": f"CSV validation failed for {self.source}.",
            "rejected_rows": [row.detail() for row in self.rejected_rows],
        }


def _normalized_headers(fieldnames: list[str] | None) -> tuple[dict[str, str], list[str]]:
    if not fieldnames:
        return {}, ["CSV header row is missing or empty."]
    normalized: dict[str, str] = {}
    errors: list[str] = []
    for original in fieldnames:
        key = (original or "").strip().casefold()
        if not key:
            errors.append("CSV contains an empty column name.")
        elif key in normalized:
            errors.append(f"CSV contains duplicate column '{key}'.")
        else:
            normalized[key] = original
    return normalized, errors


def _read_csv(path: Path, required: set[str]) -> tuple[list[tuple[int, dict]], list[CSVRejectedRow], set[str]]:
    rows: list[tuple[int, dict]] = []
    rejected: list[CSVRejectedRow] = []
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        headers, header_errors = _normalized_headers(reader.fieldnames)
        normalized_names = set(headers)
        missing = sorted(required - normalized_names)
        header_errors.extend(f"Required column '{name}' is missing." for name in missing)
        if header_errors:
            rejected.append(CSVRejectedRow(1, None, header_errors))
            return rows, rejected, normalized_names
        try:
            for row in reader:
                row_number = reader.line_num
                normalized_row = {
                    (key or "").strip().casefold(): value
                    for key, value in row.items()
                    if key is not None
                }
                row_errors: list[str] = []
                if None in row:
                    row_errors.append("Row contains more values than the CSV header.")
                if any(value is None for value in row.values()):
                    row_errors.append("Row contains fewer values than the CSV header.")
                if row_errors:
                    rejected.append(CSVRejectedRow(row_number, None, row_errors))
                else:
                    rows.append((row_number, normalized_row))
        except csv.Error as exc:
            rejected.append(CSVRejectedRow(reader.line_num, None, [f"Malformed CSV data: {exc}"]))
    return rows, rejected, normalized_names


def _pydantic_errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]


def load_transactions_report(path: Path | None = None) -> CSVLoadReport[Transaction]:
    source = Path(path) if path is not None else TRANSACTIONS_CSV
    required = {"transaction_id", "date", "amount", "vendor", "description", "category"}
    rows, rejected, headers = _read_csv(source, required)
    records: list[Transaction] = []
    seen_ids: set[str] = set()
    reference_header = next((name for name in ("invoice_reference", "invoice_number", "reference", "invoice_id") if name in headers), None)
    for row_number, row in rows:
        identifier = (row.get("transaction_id") or "").strip() or None
        errors: list[str] = []
        if identifier and identifier in seen_ids:
            errors.append(f"Duplicate transaction_id '{identifier}'.")
        elif identifier:
            seen_ids.add(identifier)
        values = {
            "transaction_id": row.get("transaction_id"),
            "date": row.get("date"),
            "amount": row.get("amount"),
            "vendor_name": row.get("vendor"),
            "description": row.get("description"),
            "category": row.get("category"),
        }
        if "currency" in headers and (row.get("currency") or "").strip():
            values["currency"] = row["currency"]
        if reference_header and (row.get(reference_header) or "").strip():
            values["invoice_reference"] = row[reference_header]
        try:
            record = Transaction(**values)
        except ValidationError as exc:
            errors.extend(_pydantic_errors(exc))
        if errors:
            rejected.append(CSVRejectedRow(row_number, identifier, errors))
        elif record is not None:
            records.append(record)
    return CSVLoadReport(records, rejected)


def load_invoices_report(path: Path | None = None) -> CSVLoadReport[Invoice]:
    source = Path(path) if path is not None else INVOICES_CSV
    required = {"invoice_id", "vendor", "amount", "date"}
    rows, rejected, headers = _read_csv(source, required)
    records: list[Invoice] = []
    seen_ids: set[str] = set()
    for row_number, row in rows:
        identifier = (row.get("invoice_id") or "").strip() or None
        errors: list[str] = []
        if identifier and identifier in seen_ids:
            errors.append(f"Duplicate invoice_id '{identifier}'.")
        elif identifier:
            seen_ids.add(identifier)
        values = {
            "invoice_id": row.get("invoice_id"),
            "vendor_name": row.get("vendor"),
            "invoice_number": row.get("invoice_number") or row.get("invoice_id"),
            "invoice_date": row.get("date"),
            "amount_due": row.get("amount"),
        }
        if "currency" in headers and (row.get("currency") or "").strip():
            values["currency"] = row["currency"]
        if "paid" in headers and (row.get("paid") or "").strip():
            paid_value = row["paid"].strip().casefold()
            if paid_value in {"true", "1", "yes"}:
                values["paid"] = True
            elif paid_value in {"false", "0", "no"}:
                values["paid"] = False
            else:
                errors.append("paid: expected true/false, yes/no, or 1/0")
        try:
            record = Invoice(**values)
        except ValidationError as exc:
            errors.extend(_pydantic_errors(exc))
        if errors:
            rejected.append(CSVRejectedRow(row_number, identifier, errors))
        elif record is not None:
            records.append(record)
    return CSVLoadReport(records, rejected)


def load_transactions() -> list[Transaction]:
    report = load_transactions_report()
    if report.rejected_rows:
        raise CSVDataValidationError(TRANSACTIONS_CSV, report.rejected_rows)
    return report.records


def load_invoices() -> list[Invoice]:
    report = load_invoices_report()
    if report.rejected_rows:
        raise CSVDataValidationError(INVOICES_CSV, report.rejected_rows)
    return report.records


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
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(reconciliation_results)")
        }
        additive_columns = {
            "match_outcome": "TEXT",
            "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            "anomalies_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, declaration in additive_columns.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE reconciliation_results ADD COLUMN {name} {declaration}"
                )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


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
                reviewed_by,
                match_outcome,
                evidence_json,
                anomalies_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transaction_id) DO UPDATE SET
                invoice_id = excluded.invoice_id,
                status = excluded.status,
                confidence_score = excluded.confidence_score,
                amount_difference = excluded.amount_difference,
                reason = excluded.reason,
                review_status = excluded.review_status,
                reviewed_at = excluded.reviewed_at,
                reviewed_by = excluded.reviewed_by,
                match_outcome = excluded.match_outcome,
                evidence_json = excluded.evidence_json,
                anomalies_json = excluded.anomalies_json
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
                match_result.match_outcome.value if match_result.match_outcome else None,
                json.dumps(match_result.evidence, sort_keys=True),
                json.dumps([item.model_dump(mode="json") for item in match_result.anomalies], sort_keys=True),
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
    init_db()
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
    keys = set(row.keys())
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
        match_outcome=row["match_outcome"] if "match_outcome" in keys else None,
        evidence=json.loads(row["evidence_json"] or "{}") if "evidence_json" in keys else {},
        anomalies=[Anomaly.model_validate(item) for item in json.loads(row["anomalies_json"] or "[]")]
        if "anomalies_json" in keys else [],
    )
