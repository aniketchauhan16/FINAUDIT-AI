"""Fail-closed LangGraph orchestration for reconciliation."""

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from app.agents.anomaly_detector import (
    analyze_match_result,
    auto_approval_anomalies,
    make_anomaly,
)
from app.agents.classifier import classify_transaction
from app.agents.matcher import AMOUNT_TOLERANCE, DATE_TOLERANCE_DAYS, match_transaction
from app.agents.state import ReconciliationState
from app.agents.validation import failure_match_result, validate_reconciliation_input
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


AUTO_DECISION = "auto_approve"
REVIEW_DECISION = "review_required"


def _plain(value):
    return value.model_dump(mode="python") if isinstance(value, BaseModel) else value


def validate_node(state: ReconciliationState) -> dict:
    try:
        return validate_reconciliation_input(state)
    except Exception as exc:
        return {
            "processing_status": "failed",
            "processing_error": f"Input validation failed ({type(exc).__name__}).",
            "validation_errors": [],
            "processing_stage": "validation",
        }


def classify_node(state: ReconciliationState) -> dict:
    if state.get("processing_status") != "ok":
        return {}
    try:
        result = classify_transaction(state)
        transaction = Transaction.model_validate(_plain(result.get("transaction")))
        return {"transaction": transaction}
    except Exception as exc:
        return {
            "processing_status": "failed",
            "processing_error": f"Classification failed ({type(exc).__name__}).",
            "processing_stage": "classification",
        }


def match_node(state: ReconciliationState) -> dict:
    if state.get("processing_status") != "ok":
        return {}
    try:
        result = match_transaction(state)
        match_result = result.get("match_result") if isinstance(result, dict) else None
        if match_result is None:
            raise ValueError("Matcher did not return a result")
        return {"match_result": MatchResult.model_validate(_plain(match_result))}
    except Exception as exc:
        return {
            "processing_status": "failed",
            "processing_error": f"Matching failed ({type(exc).__name__}).",
            "processing_stage": "matching",
        }


def analyze_node(state: ReconciliationState) -> dict:
    transaction = state.get("transaction")
    if state.get("processing_status") != "ok" or not isinstance(transaction, Transaction):
        failure_stage = state.get("processing_stage")
        result = failure_match_result(
            transaction,
            message=state.get("processing_error") or "Input validation did not complete.",
            error_type="invalid_input" if failure_stage == "validation" else "processing_error",
            evidence=state.get("validation_errors", []),
        )
        return {"match_result": result, "processing_status": "failed"}

    raw_result = state.get("match_result")
    try:
        if raw_result is None:
            raise ValueError("No match result was produced")
        match_result = MatchResult.model_validate(_plain(raw_result))
        invoices = state.get("invoices")
        if not isinstance(invoices, list) or not all(isinstance(item, Invoice) for item in invoices):
            raise ValueError("Validated invoice list is incomplete")
        analyzed = analyze_match_result(
            transaction,
            invoices,
            match_result,
            state.get("assigned_invoice_ids", set()),
            state.get("prior_transactions", []),
            amount_tolerance=AMOUNT_TOLERANCE,
            date_tolerance_days=DATE_TOLERANCE_DAYS,
        )
        return {"match_result": analyzed}
    except Exception as exc:
        result = failure_match_result(
            transaction,
            message=f"Anomaly analysis failed ({type(exc).__name__}).",
        )
        return {
            "match_result": result,
            "processing_status": "failed",
            "processing_error": f"Anomaly analysis failed ({type(exc).__name__}).",
            "processing_stage": "anomaly_analysis",
        }


def force_human_review(
    result: MatchResult,
    explanation: str,
    extra_anomalies: list[Anomaly] | None = None,
) -> MatchResult:
    anomaly = make_anomaly(
        "unsafe_auto_approval",
        explanation,
        [f"Transaction: {result.transaction_id}"],
        "Keep the record pending until a reviewer verifies the match.",
        AnomalySeverity.high,
    )
    anomalies = list(result.anomalies)
    for item in extra_anomalies or []:
        if not any(existing.type == item.type and existing.evidence == item.evidence for existing in anomalies):
            anomalies.append(item)
    if not any(item.type == anomaly.type and item.explanation == anomaly.explanation for item in anomalies):
        anomalies.append(anomaly)
    return result.model_copy(
        update={
            "status": MatchStatus.flagged,
            "review_status": ReviewStatus.pending_review,
            "reviewed_at": None,
            "reviewed_by": None,
            "reason": explanation,
            "anomalies": anomalies,
        }
    )


def _decide_node(state: ReconciliationState) -> dict:
    transaction = state.get("transaction")
    result = state.get("match_result")
    if state.get("processing_status") != "ok" or not isinstance(transaction, Transaction):
        if not isinstance(result, MatchResult):
            result = failure_match_result(
                transaction,
                message=state.get("processing_error") or "Reconciliation processing did not complete.",
                error_type="processing_error",
            )
        elif result.status not in {MatchStatus.flagged, MatchStatus.unmatched}:
            result = force_human_review(
                result,
                state.get("processing_error") or "Processing did not complete; automatic approval is blocked.",
            )
        else:
            result = result.model_copy(
                update={"review_status": ReviewStatus.pending_review, "reviewed_at": None, "reviewed_by": None}
            )
        return {
            "match_result": result,
            "decision": REVIEW_DECISION,
            "processing_status": "failed",
        }

    if not isinstance(result, MatchResult):
        result = failure_match_result(
            transaction,
            message="The graph returned an incomplete match result.",
            error_type="processing_error",
        )
        return {
            "match_result": result,
            "decision": REVIEW_DECISION,
            "processing_status": "failed",
        }

    invoices = state.get("invoices", [])
    try:
        unsafe = auto_approval_anomalies(
            transaction,
            invoices,
            result,
            state.get("assigned_invoice_ids", set()),
            state.get("prior_transactions", []),
            amount_tolerance=AMOUNT_TOLERANCE,
            date_tolerance_days=DATE_TOLERANCE_DAYS,
        )
    except Exception:
        unsafe = [make_anomaly(
            "unsafe_auto_approval",
            "The automatic approval safety check could not complete.",
            [f"Transaction: {transaction.transaction_id}"],
            "Keep the record pending until a reviewer verifies the match.",
            AnomalySeverity.high,
        )]

    if result.status == MatchStatus.auto_approved and not unsafe and not result.anomalies:
        return {"decision": AUTO_DECISION}

    if result.status == MatchStatus.auto_approved:
        explanation = "Automatic approval was blocked because the safety checks did not pass."
        result = analyze_match_result(
            transaction,
            invoices,
            result,
            state.get("assigned_invoice_ids", set()),
            state.get("prior_transactions", []),
            amount_tolerance=AMOUNT_TOLERANCE,
            date_tolerance_days=DATE_TOLERANCE_DAYS,
        )
        if result.status == MatchStatus.auto_approved:
            result = force_human_review(result, explanation)

    if result.status not in {MatchStatus.flagged, MatchStatus.unmatched}:
        result = force_human_review(
            result,
            "The graph produced a non-review status that is not valid for a new reconciliation result.",
        )
    elif result.review_status != ReviewStatus.pending_review:
        result = result.model_copy(
            update={"review_status": ReviewStatus.pending_review, "reviewed_at": None, "reviewed_by": None}
        )
    return {"match_result": result, "decision": REVIEW_DECISION}


def decide_node(state: ReconciliationState) -> dict:
    try:
        return _decide_node(state)
    except Exception as exc:
        transaction = state.get("transaction")
        result = state.get("match_result")
        message = f"Decision processing failed ({type(exc).__name__}); human review is required."
        if not isinstance(result, MatchResult):
            result = failure_match_result(
                transaction,
                message=message,
                error_type="processing_error",
            )
        elif result.status not in {MatchStatus.flagged, MatchStatus.unmatched}:
            result = force_human_review(result, message)
        else:
            result = result.model_copy(
                update={"review_status": ReviewStatus.pending_review, "reviewed_at": None, "reviewed_by": None}
            )
        return {
            "match_result": result,
            "decision": REVIEW_DECISION,
            "processing_status": "failed",
            "processing_error": message,
            "processing_stage": "decision",
        }


def route_match(state: ReconciliationState) -> str:
    """Fail-closed route: only a fully rechecked clean result reaches auto-approval."""
    try:
        if (
            state.get("processing_status") != "ok"
            or state.get("decision") != AUTO_DECISION
            or not isinstance(state.get("transaction"), Transaction)
            or not isinstance(state.get("match_result"), MatchResult)
        ):
            return "flagged"
        result = state["match_result"]
        if result.status != MatchStatus.auto_approved or result.anomalies:
            return "flagged"
        if result.match_outcome not in {MatchOutcome.exact_match, MatchOutcome.probable_match}:
            return "flagged"
        invoices = state.get("invoices", [])
        if not isinstance(invoices, list) or not all(isinstance(item, Invoice) for item in invoices):
            return "flagged"
        if auto_approval_anomalies(
            state["transaction"],
            invoices,
            result,
            state.get("assigned_invoice_ids", set()),
            state.get("prior_transactions", []),
            amount_tolerance=AMOUNT_TOLERANCE,
            date_tolerance_days=DATE_TOLERANCE_DAYS,
        ):
            return "flagged"
        return "clean"
    except Exception:
        return "flagged"


def mark_clean_path(state: ReconciliationState) -> dict:
    if route_match(state) != "clean":
        return mark_flagged_path(state)
    return {"reconciliation_path": "clean"}


def mark_flagged_path(state: ReconciliationState) -> dict:
    result = state.get("match_result")
    if isinstance(result, MatchResult) and result.status == MatchStatus.auto_approved:
        result = force_human_review(
            result,
            "The final route rejected this result because its clean-path conditions were not met.",
        )
        return {"match_result": result, "decision": REVIEW_DECISION, "reconciliation_path": "flagged"}
    return {"decision": REVIEW_DECISION, "reconciliation_path": "flagged"}


def build_reconciliation_graph():
    graph = StateGraph(ReconciliationState)

    graph.add_node("validate", validate_node)
    graph.add_node("classify", classify_node)
    graph.add_node("match", match_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("decide", decide_node)
    graph.add_node("clean", mark_clean_path)
    graph.add_node("flagged", mark_flagged_path)

    graph.set_entry_point("validate")
    graph.add_edge("validate", "classify")
    graph.add_edge("classify", "match")
    graph.add_edge("match", "analyze")
    graph.add_edge("analyze", "decide")
    graph.add_conditional_edges(
        "decide",
        route_match,
        {"clean": "clean", "flagged": "flagged"},
    )
    graph.add_edge("clean", END)
    graph.add_edge("flagged", END)

    return graph.compile()


reconciliation_graph = build_reconciliation_graph()
