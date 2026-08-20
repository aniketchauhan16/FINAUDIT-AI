from fastapi import FastAPI
from app.agents.graph import reconciliation_graph
from app.models import Invoice, MatchResult, Transaction
from app.database import load_invoices, load_transactions


app = FastAPI(
    title="FinAudit AI",
    version="0.1.0",
)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/transactions", response_model=list[Transaction])
def get_transactions() -> list[Transaction]:
    return load_transactions()


@app.get("/invoices", response_model=list[Invoice])
def get_invoices() -> list[Invoice]:
    return load_invoices()

@app.post("/run-reconciliation", response_model=list[MatchResult])
def run_reconciliation() -> list[MatchResult]:
    invoices = load_invoices()
    transactions = load_transactions()

    results: list[MatchResult] = []

    for transaction in transactions:
        final_state = reconciliation_graph.invoke(
            {
                "transaction": transaction,
                "invoices": invoices,
                "match_result": None,
            }
        )

        results.append(final_state["match_result"])

    return results