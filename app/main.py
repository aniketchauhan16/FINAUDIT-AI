from contextlib import asynccontextmanager
from datetime import datetime, timezone
import sqlite3

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator
from app.agents.anomaly_detector import auto_approval_anomalies
from app.agents.graph import AUTO_DECISION, force_human_review, reconciliation_graph
from app.agents.matcher import AMOUNT_TOLERANCE, DATE_TOLERANCE_DAYS
from app.agents.validation import failure_match_result
from app.models import Anomaly, Invoice, MatchResult, MatchStatus, ReviewEvent, ReviewStatus, Transaction
from app.database import (
    get_match_result,
    get_all_match_results,
    get_pending_review_results,
    get_review_history as get_transaction_review_history,
    init_db,
    load_invoices_report,
    load_transactions_report,
    record_review,
    ReviewConflictError,
    ReviewNotFoundError,
    save_match_results,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="FinAudit AI",
    version="0.1.0",
    lifespan=lifespan,
)


class ReviewAction(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reviewed_by")
    @classmethod
    def validate_reviewer(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reviewed_by must not be blank")
        return value

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/transactions", response_model=list[Transaction])
def get_transactions() -> list[Transaction]:
    report = load_transactions_report()
    if report.rejected_rows:
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV validation failed.", "rejected_rows": report.error_details()},
        )
    return report.records


@app.get("/invoices", response_model=list[Invoice])
def get_invoices() -> list[Invoice]:
    report = load_invoices_report()
    if report.rejected_rows:
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV validation failed.", "rejected_rows": report.error_details()},
        )
    return report.records


@app.post("/run-reconciliation", response_model=list[MatchResult])
def run_reconciliation() -> list[MatchResult]:
    transaction_report = load_transactions_report()
    invoice_report = load_invoices_report()
    if transaction_report.rejected_rows or invoice_report.rejected_rows:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "CSV validation failed; no reconciliation was run.",
                "transactions": transaction_report.error_details(),
                "invoices": invoice_report.error_details(),
            },
        )
    invoices = invoice_report.records
    transactions = transaction_report.records

    results: list[MatchResult] = []
    assigned_invoice_ids: set[str] = set()
    prior_transactions: list[Transaction] = []

    for transaction in transactions:
        try:
            final_state = reconciliation_graph.invoke(
                {
                    "transaction": transaction,
                    "invoices": invoices,
                    "match_result": None,
                    "assigned_invoice_ids": assigned_invoice_ids,
                    "prior_transactions": prior_transactions,
                }
            )
        except Exception as exc:
            final_state = {
                "processing_status": "failed",
                "decision": "review_required",
                "reconciliation_path": "flagged",
                "match_result": failure_match_result(
                    transaction,
                    message=f"Graph execution failed ({type(exc).__name__}).",
                    error_type="processing_error",
                ),
            }

        raw_result = final_state.get("match_result") if isinstance(final_state, dict) else None
        try:
            match_result = MatchResult.model_validate(
                raw_result.model_dump(mode="python") if isinstance(raw_result, BaseModel) else raw_result
            )
        except (ValidationError, TypeError, ValueError):
            match_result = failure_match_result(
                transaction,
                message="Graph execution returned an incomplete match result.",
                error_type="processing_error",
            )
        if match_result is None:
            raise HTTPException(status_code=500, detail="Reconciliation could not produce a reviewable result.")

        if match_result.status == MatchStatus.auto_approved:
            safety_check_failed = False
            try:
                unsafe_evidence = auto_approval_anomalies(
                    transaction,
                    invoices,
                    match_result,
                    assigned_invoice_ids,
                    prior_transactions,
                    amount_tolerance=AMOUNT_TOLERANCE,
                    date_tolerance_days=DATE_TOLERANCE_DAYS,
                )
            except Exception:
                unsafe_evidence = []
                safety_check_failed = True
            is_clean_path = (
                isinstance(final_state, dict)
                and final_state.get("processing_status") == "ok"
                and final_state.get("decision") == AUTO_DECISION
                and final_state.get("reconciliation_path") == "clean"
            )
            if not is_clean_path or safety_check_failed or unsafe_evidence or match_result.anomalies:
                match_result = force_human_review(
                    match_result,
                    "The API blocked automatic approval because the graph did not pass every safety check.",
                    [item for item in unsafe_evidence if isinstance(item, Anomaly)],
                )

        match_result = apply_review_status(match_result)
        results.append(match_result)
        if match_result.invoice_id:
            assigned_invoice_ids.add(match_result.invoice_id)
        prior_transactions.append(transaction)

    return save_match_results(results)


@app.get("/transactions/flagged", response_model=list[MatchResult])
def get_flagged_transactions() -> list[MatchResult]:
    return get_pending_review_results()


@app.get("/transactions/{transaction_id}/review-history", response_model=list[ReviewEvent])
def get_review_history(transaction_id: str) -> list[ReviewEvent]:
    events = get_transaction_review_history(transaction_id)
    if events is None:
        raise HTTPException(status_code=404, detail="Reconciliation result not found.")
    return events


@app.get("/reconciliation-results", response_model=list[MatchResult])
def get_reconciliation_results() -> list[MatchResult]:
    return get_all_match_results()


@app.post("/transactions/{transaction_id}/approve", response_model=MatchResult)
def approve_transaction(transaction_id: str, action: ReviewAction) -> MatchResult:
    return review_transaction(transaction_id, action, ReviewStatus.approved)


@app.post("/transactions/{transaction_id}/reject", response_model=MatchResult)
def reject_transaction(transaction_id: str, action: ReviewAction) -> MatchResult:
    return review_transaction(transaction_id, action, ReviewStatus.rejected)


def review_transaction(
    transaction_id: str, action: ReviewAction, decision: ReviewStatus
) -> MatchResult:
    reviewer = action.reviewed_by.strip()
    if not reviewer:
        raise HTTPException(status_code=422, detail="reviewed_by must not be blank.")
    try:
        return record_review(
            transaction_id,
            decision,
            reviewer,
            datetime.now(timezone.utc).isoformat(),
            action.reason,
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Reconciliation result not found.") from exc
    except ReviewConflictError as exc:
        raise HTTPException(
            status_code=409, detail="Reconciliation result is not pending review."
        ) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=500,
            detail="Review could not be recorded; no changes were saved.",
        ) from exc


def apply_review_status(match_result: MatchResult) -> MatchResult:
    existing_result = get_match_result(match_result.transaction_id)

    if (
        existing_result is not None
        and existing_result.review_status in {ReviewStatus.approved, ReviewStatus.rejected}
        and existing_result.reviewed_by != "system"
    ):
        return match_result.model_copy(
            update={
                "status": existing_result.status,
                "review_status": existing_result.review_status,
                "reviewed_at": existing_result.reviewed_at,
                "reviewed_by": existing_result.reviewed_by,
            }
        )

    if match_result.status in {MatchStatus.flagged, MatchStatus.unmatched}:
        return match_result.model_copy(
            update={
                "review_status": ReviewStatus.pending_review,
                "reviewed_at": None,
                "reviewed_by": None,
            }
        )

    return match_result.model_copy(
        update={
            "review_status": ReviewStatus.approved,
            "reviewed_at": datetime.now(timezone.utc),
            "reviewed_by": "system",
        }
    )
