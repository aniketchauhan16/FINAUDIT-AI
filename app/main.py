from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from app.agents.graph import reconciliation_graph
from app.models import Invoice, MatchResult, MatchStatus, ReviewStatus, Transaction
from app.database import (
    get_match_result,
    get_all_match_results,
    get_pending_review_results,
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

    @field_validator("reviewed_by")
    @classmethod
    def validate_reviewer(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reviewed_by must not be blank")
        return value


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
        final_state = reconciliation_graph.invoke(
            {
                "transaction": transaction,
                "invoices": invoices,
                "match_result": None,
                "assigned_invoice_ids": assigned_invoice_ids,
                "prior_transactions": prior_transactions,
            }
        )

        match_result = apply_review_status(final_state["match_result"])
        results.append(match_result)
        if match_result.invoice_id:
            assigned_invoice_ids.add(match_result.invoice_id)
        prior_transactions.append(transaction)

    return save_match_results(results)


@app.get("/transactions/flagged", response_model=list[MatchResult])
def get_flagged_transactions() -> list[MatchResult]:
    return get_pending_review_results()


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
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Reconciliation result not found.") from exc
    except ReviewConflictError as exc:
        raise HTTPException(
            status_code=409, detail="Reconciliation result is not pending review."
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
