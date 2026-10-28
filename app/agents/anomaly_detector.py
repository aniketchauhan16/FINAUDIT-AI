"""Deterministic, explainable anomaly checks used by reconciliation."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
import re
import unicodedata

from app.models import Anomaly, AnomalySeverity, Invoice, Transaction


POTENTIAL_DUPLICATE_WINDOW_DAYS = 3


def normalize_vendor(value: str) -> str:
    """Normalize case, Unicode compatibility forms, punctuation, and spacing."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def normalize_reference(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", normalized)


def duplicate_invoice_identifier_groups(invoices: list[Invoice]) -> dict[str, list[Invoice]]:
    grouped: dict[str, list[Invoice]] = defaultdict(list)
    for invoice in invoices:
        grouped[invoice.invoice_id].append(invoice)
    return {key: values for key, values in grouped.items() if len(values) > 1}


def duplicate_invoice_record_groups(invoices: list[Invoice]) -> list[list[Invoice]]:
    """Find repeated invoice number/vendor/amount/date/currency records."""
    grouped: dict[tuple[str, str, Decimal, date, str], list[Invoice]] = defaultdict(list)
    for invoice in invoices:
        key = (
            normalize_vendor(invoice.vendor_name),
            normalize_reference(invoice.invoice_number),
            invoice.amount_due,
            invoice.invoice_date,
            invoice.currency,
        )
        grouped[key].append(invoice)
    return [values for values in grouped.values() if len(values) > 1]


def potential_duplicate_transactions(
    transaction: Transaction,
    prior_transactions: list[Transaction],
    window_days: int = POTENTIAL_DUPLICATE_WINDOW_DAYS,
) -> list[Transaction]:
    """Return earlier payments with the same vendor/amount within a short window."""
    duplicates = []
    for prior in prior_transactions:
        if prior.transaction_id == transaction.transaction_id:
            continue
        if normalize_vendor(prior.vendor_name) != normalize_vendor(transaction.vendor_name):
            continue
        if prior.amount != transaction.amount or prior.currency != transaction.currency:
            continue
        if abs((transaction.date - prior.date).days) <= window_days:
            duplicates.append(prior)
    return sorted(duplicates, key=lambda item: item.transaction_id)


def make_anomaly(
    anomaly_type: str,
    explanation: str,
    evidence: list[str],
    recommended_action: str,
    severity: AnomalySeverity = AnomalySeverity.medium,
) -> Anomaly:
    return Anomaly(
        type=anomaly_type,
        severity=severity,
        evidence=evidence,
        explanation=explanation,
        recommended_action=recommended_action,
        human_review_required=True,
    )
