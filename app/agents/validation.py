"""Graph-boundary validation and safe failure results."""

from typing import Any

from pydantic import BaseModel, ValidationError

from app.models import (
    Anomaly,
    AnomalySeverity,
    Invoice,
    MatchOutcome,
    MatchResult,
    MatchStatus,
    ReviewStatus,
    Transaction,
)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value


def _validation_messages(error: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False, include_context=False)
    ]


def validate_reconciliation_input(state: dict) -> dict:
    """Revalidate graph inputs even when called outside the CSV/API loaders."""
    try:
        transaction = Transaction.model_validate(_plain(state.get("transaction")))
        raw_invoices = state.get("invoices")
        if not isinstance(raw_invoices, list):
            raise TypeError("invoices must be a list")
        invoices = [Invoice.model_validate(_plain(item)) for item in raw_invoices]

        raw_assigned = state.get("assigned_invoice_ids", set())
        if not isinstance(raw_assigned, (set, list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in raw_assigned
        ):
            raise TypeError("assigned_invoice_ids must contain nonblank strings")
        assigned_invoice_ids = {item.strip() for item in raw_assigned}

        raw_prior = state.get("prior_transactions", [])
        if not isinstance(raw_prior, list):
            raise TypeError("prior_transactions must be a list")
        prior_transactions = [
            Transaction.model_validate(_plain(item)) for item in raw_prior
        ]
    except ValidationError as exc:
        return {
            "processing_status": "failed",
            "processing_error": "Input validation failed.",
            "validation_errors": _validation_messages(exc),
            "processing_stage": "validation",
        }
    except (TypeError, ValueError) as exc:
        return {
            "processing_status": "failed",
            "processing_error": "Input validation failed.",
            "validation_errors": [str(exc)],
            "processing_stage": "validation",
        }

    return {
        "transaction": transaction,
        "invoices": invoices,
        "assigned_invoice_ids": assigned_invoice_ids,
        "prior_transactions": prior_transactions,
        "processing_status": "ok",
        "processing_error": None,
        "validation_errors": [],
    }


def failure_match_result(
    transaction: Any,
    *,
    message: str,
    error_type: str = "processing_error",
    evidence: list[str] | None = None,
) -> MatchResult | None:
    """Build a pending-review result when a valid transaction ID is available."""
    if isinstance(transaction, BaseModel):
        identifier = getattr(transaction, "transaction_id", None)
    elif isinstance(transaction, dict):
        identifier = transaction.get("transaction_id")
    else:
        identifier = None
    if not isinstance(identifier, str) or not identifier.strip():
        return None

    anomaly = Anomaly(
        type=error_type,
        severity=AnomalySeverity.high,
        evidence=[f"Transaction: {identifier.strip()}", *(evidence or [])],
        explanation=message,
        recommended_action="Inspect the input and processing error, then rerun or review manually.",
        human_review_required=True,
    )
    return MatchResult(
        transaction_id=identifier.strip(),
        invoice_id=None,
        status=MatchStatus.flagged,
        confidence_score=0.0,
        amount_difference=None,
        reason="Reconciliation did not complete safely; human review is required.",
        review_status=ReviewStatus.pending_review,
        match_outcome=MatchOutcome.invalid_input,
        evidence={},
        anomalies=[anomaly],
    )
