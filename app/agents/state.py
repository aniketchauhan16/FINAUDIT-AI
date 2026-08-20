from typing import TypedDict

from app.models import Invoice, MatchResult, Transaction


class ReconciliationState(TypedDict):
    transaction: Transaction
    invoices: list[Invoice]
    match_result: MatchResult | None