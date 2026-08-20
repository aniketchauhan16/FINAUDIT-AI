from langgraph.graph import END, StateGraph

from app.agents.classifier import classify_transaction
from app.agents.matcher import match_transaction
from app.agents.state import ReconciliationState


def build_reconciliation_graph():
    graph = StateGraph(ReconciliationState)

    graph.add_node("classify", classify_transaction)
    graph.add_node("match", match_transaction)

    graph.set_entry_point("classify")
    graph.add_edge("classify", "match")
    graph.add_edge("match", END)

    return graph.compile()


reconciliation_graph = build_reconciliation_graph()