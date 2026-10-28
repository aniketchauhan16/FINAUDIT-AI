from langgraph.graph import END, StateGraph

from app.agents.classifier import classify_transaction
from app.agents.matcher import match_transaction
from app.agents.state import ReconciliationState
from app.models import MatchStatus


def route_match(state: ReconciliationState) -> str:
    """Send clean matches down the auto-approved path; all others need review."""
    match_result = state["match_result"]
    if match_result is not None and match_result.status == MatchStatus.auto_approved:
        return "clean"
    return "flagged"


def mark_clean_path(state: ReconciliationState) -> dict:
    return {"reconciliation_path": "clean"}


def mark_flagged_path(state: ReconciliationState) -> dict:
    return {"reconciliation_path": "flagged"}


def build_reconciliation_graph():
    graph = StateGraph(ReconciliationState)

    graph.add_node("classify", classify_transaction)
    graph.add_node("match", match_transaction)
    graph.add_node("clean", mark_clean_path)
    graph.add_node("flagged", mark_flagged_path)

    graph.set_entry_point("classify")
    graph.add_edge("classify", "match")
    graph.add_conditional_edges(
        "match",
        route_match,
        {"clean": "clean", "flagged": "flagged"},
    )
    graph.add_edge("clean", END)
    graph.add_edge("flagged", END)

    return graph.compile()


reconciliation_graph = build_reconciliation_graph()
