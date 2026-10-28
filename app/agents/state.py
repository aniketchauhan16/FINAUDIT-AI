from typing import TypedDict
from typing_extensions import NotRequired

from app.models import Invoice, MatchResult, Transaction


class ReconciliationState(TypedDict):
    transaction: Transaction
    invoices: list[Invoice]
    match_result: MatchResult | None
    reconciliation_path: str
    assigned_invoice_ids: NotRequired[set[str]]
    prior_transactions: NotRequired[list[Transaction]]
