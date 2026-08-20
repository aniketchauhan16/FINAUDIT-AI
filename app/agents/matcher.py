from decimal import Decimal

from app.agents.state import ReconciliationState
from app.models import MatchResult, MatchStatus


AUTO_APPROVE_THRESHOLD = Decimal("0.02")


def match_transaction(state: ReconciliationState) -> dict:
    transaction = state["transaction"]
    invoices = state["invoices"]

    vendor_matches = [
        invoice
        for invoice in invoices
        if invoice.vendor_name.lower() == transaction.vendor_name.lower()
    ]

    if not vendor_matches:
        return {
            "match_result": MatchResult(
                transaction_id=transaction.transaction_id,
                invoice_id=None,
                status=MatchStatus.unmatched,
                confidence_score=0.0,
                amount_difference=None,
                reason="No invoice found for this vendor.",
            )
        }

    best_invoice = min(
        vendor_matches,
        key=lambda invoice: abs(invoice.amount_due - transaction.amount),
    )

    amount_difference = abs(best_invoice.amount_due - transaction.amount)
    amount_diff_pct = amount_difference / best_invoice.amount_due

    if amount_diff_pct <= AUTO_APPROVE_THRESHOLD:
        status = MatchStatus.auto_approved
        confidence_score = 0.95
        reason = "Vendor matched and amount difference is within 2%."
    else:
        status = MatchStatus.flagged
        confidence_score = 0.65
        reason = "Vendor matched but amount difference is above 2%."

    return {
        "match_result": MatchResult(
            transaction_id=transaction.transaction_id,
            invoice_id=best_invoice.invoice_id,
            status=status,
            confidence_score=confidence_score,
            amount_difference=amount_difference,
            reason=reason,
        )
    }