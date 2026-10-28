from typing import TypedDict

from app.models import Invoice, MatchResult, Transaction


class ReconciliationState(TypedDict, total=False):
    transaction: Transaction | dict | None
    invoices: list[Invoice] | list[dict]
    match_result: MatchResult | dict | None
    reconciliation_path: str
    assigned_invoice_ids: set[str]
    prior_transactions: list[Transaction]
    processing_status: str
    processing_error: str | None
    processing_stage: str
    validation_errors: list[str]
    decision: str
