"""Deterministic, explainable anomaly checks used by reconciliation."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
import re
import unicodedata

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


def auto_approval_anomalies(
    transaction: Transaction,
    invoices: list[Invoice],
    result: MatchResult,
    assigned_invoice_ids: set[str],
    prior_transactions: list[Transaction] | None = None,
    *,
    amount_tolerance: Decimal,
    date_tolerance_days: int,
) -> list[Anomaly]:
    """Recheck every invariant required before a result may enter auto-approval."""
    if result.status != MatchStatus.auto_approved:
        return []

    issues: list[Anomaly] = []

    def block(kind: str, explanation: str, evidence: list[str]) -> None:
        issues.append(
            make_anomaly(
                kind,
                explanation,
                evidence,
                "Keep this transaction in review until the match is verified.",
                AnomalySeverity.high,
            )
        )

    if result.anomalies:
        block(
            "unsafe_auto_approval",
            "The match contains anomaly warnings but was marked auto-approved.",
            [f"Existing anomaly types: {', '.join(item.type for item in result.anomalies)}"],
        )
    if result.transaction_id != transaction.transaction_id:
        block(
            "invalid_match_result",
            "The matcher returned a result for a different transaction.",
            [f"Expected: {transaction.transaction_id}", f"Received: {result.transaction_id}"],
        )
    if result.match_outcome not in {MatchOutcome.exact_match, MatchOutcome.probable_match}:
        block(
            "unsafe_auto_approval",
            "The match outcome is not eligible for automatic approval.",
            [f"Outcome: {result.match_outcome.value if result.match_outcome else 'missing'}"],
        )
    if result.review_status != ReviewStatus.pending_review:
        block(
            "invalid_match_result",
            "A new graph result must be pending review before the decision node acts.",
            [f"Review status: {result.review_status.value}"],
        )
    if not result.invoice_id:
        block("missing_invoice", "An auto-approved result must identify one invoice.", ["Invoice identifier is missing."])
        return issues

    candidates = [item for item in invoices if item.invoice_id == result.invoice_id]
    if len(candidates) != 1:
        block(
            "duplicate_invoice" if candidates else "invalid_match_result",
            "The matched invoice identifier does not identify exactly one source invoice.",
            [f"Invoice: {result.invoice_id}", f"Candidate records: {len(candidates)}"],
        )
        return issues

    invoice = candidates[0]
    if invoice.invoice_id in assigned_invoice_ids:
        block(
            "duplicate_invoice_assignment",
            "The invoice has already been assigned to another transaction in this run.",
            [f"Invoice: {invoice.invoice_id}"],
        )
    if normalize_vendor(transaction.vendor_name) != normalize_vendor(invoice.vendor_name):
        block(
            "vendor_mismatch",
            "The transaction and invoice vendors do not agree after normalization.",
            [f"Transaction vendor: {transaction.vendor_name}", f"Invoice vendor: {invoice.vendor_name}"],
        )
    if transaction.currency != invoice.currency:
        block(
            "currency_mismatch",
            "The transaction and invoice currencies do not agree.",
            [f"Transaction currency: {transaction.currency}", f"Invoice currency: {invoice.currency}"],
        )

    difference = abs(transaction.amount - invoice.amount_due)
    base = abs(invoice.amount_due)
    percentage = Decimal("0") if difference == 0 else Decimal("Infinity") if base == 0 else difference / base
    if result.amount_difference != difference or percentage > amount_tolerance:
        block(
            "amount_discrepancy",
            "The stored amount evidence does not satisfy the automatic approval tolerance.",
            [f"Calculated difference: {difference}", f"Configured tolerance: {amount_tolerance}"],
        )
    days_apart = abs((transaction.date - invoice.invoice_date).days)
    if days_apart > date_tolerance_days:
        block(
            "date_mismatch",
            "The transaction and invoice dates exceed the configured matching window.",
            [f"Days apart: {days_apart}", f"Configured window: {date_tolerance_days} days"],
        )
    if invoice.paid:
        block(
            "invoice_already_paid",
            "A source invoice marked as paid cannot be automatically approved again.",
            [f"Invoice: {invoice.invoice_id}"],
        )

    duplicate_records = [
        group
        for group in duplicate_invoice_record_groups(invoices)
        if any(item.invoice_id == invoice.invoice_id for item in group)
    ]
    if duplicate_records:
        identifiers = sorted({item.invoice_id for item in duplicate_records[0]})
        block(
            "duplicate_invoice_record",
            "The selected invoice has a duplicate source record.",
            ["Invoice identifiers: " + ", ".join(identifiers)],
        )

    duplicate_payments = potential_duplicate_transactions(transaction, prior_transactions or [])
    if duplicate_payments:
        block(
            "potential_duplicate_payment",
            "A prior transaction has the same normalized vendor, amount, and currency within the duplicate-payment window.",
            [
                f"Current transaction: {transaction.transaction_id}",
                "Prior transaction(s): " + ", ".join(item.transaction_id for item in duplicate_payments),
                f"Window: {POTENTIAL_DUPLICATE_WINDOW_DAYS} days",
            ],
        )

    reference_candidates = []
    if transaction.invoice_reference:
        reference = normalize_reference(transaction.invoice_reference)
        reference_candidates = [
            item
            for item in invoices
            if reference in {
                normalize_reference(item.invoice_id),
                normalize_reference(item.invoice_number),
            }
        ]
        if len(reference_candidates) != 1 or reference_candidates[0].invoice_id != result.invoice_id:
            block(
                "invoice_reference_mismatch" if not reference_candidates else "ambiguous_match",
                "The transaction reference does not identify exactly the selected unique invoice.",
                [
                    f"Reference: {transaction.invoice_reference}",
                    "Candidates: " + (", ".join(item.invoice_id for item in reference_candidates) or "none"),
                ],
            )

    if not reference_candidates:
        # Recompute the matcher's deterministic ordering so incomplete or
        # inconsistent matcher output cannot hide a tied or better candidate.
        candidates = [
            item
            for item in invoices
            if normalize_vendor(item.vendor_name) == normalize_vendor(transaction.vendor_name)
        ]
        ranked = []
        for item in candidates:
            difference = abs(transaction.amount - item.amount_due)
            base_amount = abs(item.amount_due)
            percentage = (
                Decimal("0")
                if difference == 0
                else Decimal("Infinity")
                if base_amount == 0
                else difference / base_amount
            )
            days_apart = abs((transaction.date - item.invoice_date).days)
            rank = (
                percentage <= amount_tolerance,
                days_apart <= date_tolerance_days,
                transaction.currency == item.currency,
                -percentage,
                -days_apart,
            )
            ranked.append((rank, item))
        if ranked:
            best_rank = max(rank for rank, _ in ranked)
            best_candidates = [item for rank, item in ranked if rank == best_rank]
            best_ids = sorted({item.invoice_id for item in best_candidates})
            if len(best_candidates) != 1 or best_candidates[0].invoice_id != result.invoice_id:
                block(
                    "ambiguous_match" if len(best_candidates) > 1 else "invalid_match_result",
                    "The selected invoice is not the unique best candidate under deterministic matching rules.",
                    ["Best candidate identifiers: " + ", ".join(best_ids)],
                )
        else:
            block(
                "vendor_mismatch",
                "No source invoice matches the transaction vendor after normalization.",
                [f"Transaction vendor: {transaction.vendor_name}"],
            )
    return issues


def analyze_match_result(
    transaction: Transaction,
    invoices: list[Invoice],
    result: MatchResult,
    assigned_invoice_ids: set[str],
    prior_transactions: list[Transaction] | None = None,
    *,
    amount_tolerance: Decimal,
    date_tolerance_days: int,
) -> MatchResult:
    """Analyze result consistency, enforce one-to-one linking, and block unsafe approvals."""
    anomalies = list(result.anomalies)
    updates: dict = {}

    if result.transaction_id != transaction.transaction_id:
        anomalies.append(make_anomaly(
            "invalid_match_result",
            "The result transaction identifier did not match the graph input.",
            [f"Expected: {transaction.transaction_id}", f"Received: {result.transaction_id}"],
            "Discard the inconsistent result and rerun reconciliation.",
            AnomalySeverity.high,
        ))
        updates["transaction_id"] = transaction.transaction_id

    if result.invoice_id:
        candidates = [item for item in invoices if item.invoice_id == result.invoice_id]
        if len(candidates) != 1:
            anomalies.append(make_anomaly(
                "duplicate_invoice" if candidates else "invalid_match_result",
                "The result invoice identifier does not identify one unique source invoice.",
                [f"Invoice: {result.invoice_id}", f"Candidate records: {len(candidates)}"],
                "Resolve the invoice data or rerun matching before approval.",
                AnomalySeverity.high,
            ))
            updates["invoice_id"] = None
        elif result.invoice_id in assigned_invoice_ids:
            anomalies.append(make_anomaly(
                "duplicate_invoice_assignment",
                "The invoice is already assigned to another transaction in this run.",
                [f"Invoice: {result.invoice_id}"],
                "Review the earlier assignment and select the correct invoice.",
                AnomalySeverity.high,
            ))
            updates["invoice_id"] = None

    anomalies.extend(auto_approval_anomalies(
        transaction,
        invoices,
        result,
        assigned_invoice_ids,
        prior_transactions,
        amount_tolerance=amount_tolerance,
        date_tolerance_days=date_tolerance_days,
    ))

    deduplicated: list[Anomaly] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for anomaly in anomalies:
        key = (anomaly.type, tuple(anomaly.evidence))
        if key not in seen:
            seen.add(key)
            deduplicated.append(anomaly)

    if deduplicated and result.status == MatchStatus.auto_approved:
        updates.update(
            status=MatchStatus.flagged,
            review_status=ReviewStatus.pending_review,
            reviewed_at=None,
            reviewed_by=None,
            reason="Automatic approval was blocked by anomaly analysis; human review is required.",
        )
    if deduplicated != result.anomalies:
        updates["anomalies"] = deduplicated
    return result.model_copy(update=updates) if updates else result
