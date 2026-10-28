"""Deterministic invoice candidate selection and explainable match scoring."""

from decimal import Decimal
import math
import os

from app.agents.anomaly_detector import (
    POTENTIAL_DUPLICATE_WINDOW_DAYS,
    duplicate_invoice_identifier_groups,
    duplicate_invoice_record_groups,
    make_anomaly,
    normalize_reference,
    normalize_vendor,
    potential_duplicate_transactions,
)
from app.agents.state import ReconciliationState
from app.models import (
    Anomaly,
    AnomalySeverity,
    Invoice,
    MatchOutcome,
    MatchResult,
    MatchStatus,
    Transaction,
)


def _decimal_env(name: str, default: str, minimum: Decimal, maximum: Decimal) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default))
    except Exception as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not value.is_finite() or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _int_env(name: str, default: str, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


AMOUNT_TOLERANCE = _decimal_env("FINAUDIT_AMOUNT_TOLERANCE", "0.02", Decimal("0"), Decimal("1"))
DATE_TOLERANCE_DAYS = _int_env("FINAUDIT_DATE_TOLERANCE_DAYS", "7", 0, 366)

# These weights are normalized over the evidence available for a given match.
EVIDENCE_WEIGHTS = {
    "reference": 3.0,
    "vendor": 3.0,
    "amount": 3.0,
    "date": 1.0,
    "currency": 1.0,
    "uniqueness": 2.0,
}


def score_confidence(evidence: dict[str, float], anomaly_count: int = 0) -> float:
    """Return a reproducible evidence score, not a calibrated probability.

    Only supplied evidence dimensions contribute. Each anomaly reduces the
    normalized weighted score by 0.08, up to a maximum penalty of 0.40.
    """
    usable = {}
    for key, value in evidence.items():
        if key not in EVIDENCE_WEIGHTS:
            continue
        numeric = float(value)
        usable[key] = min(1.0, max(0.0, numeric)) if math.isfinite(numeric) else 0.0
    weight_total = sum(EVIDENCE_WEIGHTS[key] for key in usable)
    if not weight_total:
        return 0.0
    weighted = sum(EVIDENCE_WEIGHTS[key] * value for key, value in usable.items())
    penalty = min(0.40, max(0, anomaly_count) * 0.08)
    return round(max(0.0, min(1.0, weighted / weight_total - penalty)), 4)


def _percentage_difference(transaction_amount: Decimal, invoice_amount: Decimal) -> Decimal:
    difference = abs(transaction_amount - invoice_amount)
    base = abs(invoice_amount)
    if base == 0:
        return Decimal("0") if difference == 0 else Decimal("Infinity")
    return difference / base


def _amount_evidence(difference_pct: Decimal) -> float:
    if difference_pct > AMOUNT_TOLERANCE:
        return 0.0
    if AMOUNT_TOLERANCE == 0:
        return 1.0
    return float(max(Decimal("0"), Decimal("1") - difference_pct / AMOUNT_TOLERANCE))


def _date_evidence(days_apart: int) -> float:
    if days_apart > DATE_TOLERANCE_DAYS:
        return 0.0
    return (DATE_TOLERANCE_DAYS + 1 - days_apart) / (DATE_TOLERANCE_DAYS + 1)


def _base_anomalies(
    transaction: Transaction,
    prior_transactions: list[Transaction],
) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    duplicates = potential_duplicate_transactions(transaction, prior_transactions)
    if duplicates:
        anomalies.append(
            make_anomaly(
                "potential_duplicate_payment",
                "A prior transaction has the same normalized vendor, amount, and currency within the duplicate-payment window.",
                [
                    f"Current transaction: {transaction.transaction_id}",
                    "Prior transaction(s): " + ", ".join(item.transaction_id for item in duplicates),
                    f"Window: {POTENTIAL_DUPLICATE_WINDOW_DAYS} days",
                ],
                "Compare bank references and supporting documents before approving either payment.",
                AnomalySeverity.high,
            )
        )

    return anomalies


def _invoice_duplicate_anomalies(candidates: list[Invoice]) -> list[Anomaly]:
    by_id = duplicate_invoice_identifier_groups(candidates)
    anomalies = [
        make_anomaly(
            "duplicate_invoice_identifier",
            "More than one invoice record uses the same invoice identifier.",
            [f"Invoice identifier: {invoice_id}", f"Records: {len(records)}"],
            "Correct the duplicate invoice identifier in the source data before matching.",
            AnomalySeverity.high,
        )
        for invoice_id, records in sorted(by_id.items())
    ]
    candidate_ids = {invoice.invoice_id for invoice in candidates}
    for group in duplicate_invoice_record_groups(candidates):
        ids = sorted({invoice.invoice_id for invoice in group})
        if len(ids) > 1 and candidate_ids.intersection(ids):
            anomalies.append(
                make_anomaly(
                    "duplicate_invoice_record",
                    "Multiple invoice records repeat the same normalized invoice number, vendor, amount, date, and currency.",
                    ["Invoice identifiers: " + ", ".join(ids)],
                    "Review the source invoices and identify which record is authoritative.",
                    AnomalySeverity.high,
                )
            )
    return anomalies


def _build_result(
    transaction: Transaction,
    *,
    invoice: Invoice | None,
    status: MatchStatus,
    outcome: MatchOutcome,
    reason: str,
    evidence: dict[str, float],
    anomalies: list[Anomaly],
    amount_difference: Decimal | None = None,
) -> dict:
    return {
        "match_result": MatchResult(
            transaction_id=transaction.transaction_id,
            invoice_id=invoice.invoice_id if invoice else None,
            status=status,
            confidence_score=score_confidence(evidence, len(anomalies)),
            amount_difference=amount_difference,
            reason=reason,
            match_outcome=outcome,
            evidence=evidence,
            anomalies=anomalies,
        )
    }


def _finalize_candidate(
    transaction: Transaction,
    invoice: Invoice,
    *,
    reference_agrees: bool | None,
    unique: bool,
    anomalies: list[Anomaly],
    reference_conflict: bool = False,
) -> dict:
    amount_difference = abs(transaction.amount - invoice.amount_due)
    percentage = _percentage_difference(transaction.amount, invoice.amount_due)
    days_apart = abs((transaction.date - invoice.invoice_date).days)
    vendor_agrees = normalize_vendor(transaction.vendor_name) == normalize_vendor(invoice.vendor_name)
    currency_agrees = transaction.currency == invoice.currency
    evidence = {
        "vendor": 1.0 if vendor_agrees else 0.0,
        "amount": _amount_evidence(percentage),
        "date": _date_evidence(days_apart),
        "currency": 1.0 if currency_agrees else 0.0,
        "uniqueness": 1.0 if unique else 0.0,
    }
    if reference_agrees is not None:
        evidence["reference"] = 1.0 if reference_agrees else 0.0

    outcome = MatchOutcome.probable_match
    reason = "Vendor matched and the amount difference is within the configured tolerance."
    if reference_conflict:
        anomalies.append(
            make_anomaly(
                "invoice_reference_mismatch",
                "The supplied transaction reference did not match the selected invoice reference.",
                [f"Transaction reference: {transaction.invoice_reference}"],
                "Check the transaction reference and invoice number before approval.",
                AnomalySeverity.high,
            )
        )
    if not vendor_agrees:
        outcome = MatchOutcome.vendor_mismatch
        reason = "The invoice reference identified an invoice for a different vendor."
        anomalies.append(make_anomaly(
            "vendor_mismatch", reason,
            [f"Transaction vendor: {transaction.vendor_name}", f"Invoice vendor: {invoice.vendor_name}", f"Invoice: {invoice.invoice_id}"],
            "Confirm the invoice belongs to this transaction.", AnomalySeverity.high,
        ))
    amount_mismatch = percentage > AMOUNT_TOLERANCE
    date_mismatch = days_apart > DATE_TOLERANCE_DAYS
    currency_mismatch = not currency_agrees
    if amount_mismatch:
        if vendor_agrees:
            outcome = MatchOutcome.amount_mismatch
            reason = f"Vendor matched, but the amount difference exceeds {AMOUNT_TOLERANCE:.0%}."
        else:
            reason += f" The amount difference also exceeds {AMOUNT_TOLERANCE:.0%}."
        anomalies.append(make_anomaly(
            "amount_discrepancy", f"The amount difference exceeds {AMOUNT_TOLERANCE:.0%}.",
            [f"Transaction amount: {transaction.amount}", f"Invoice amount: {invoice.amount_due}", f"Absolute difference: {amount_difference}"],
            "Compare the invoice and payment evidence before approval.", AnomalySeverity.medium,
        ))
    if date_mismatch:
        if vendor_agrees and not amount_mismatch:
            outcome = MatchOutcome.date_mismatch
            reason = f"Vendor and amount matched, but the dates differ by {days_apart} days (limit {DATE_TOLERANCE_DAYS})."
        anomalies.append(make_anomaly(
            "date_mismatch", f"Transaction and invoice dates differ by {days_apart} days, beyond the {DATE_TOLERANCE_DAYS}-day window.",
            [f"Transaction date: {transaction.date.isoformat()}", f"Invoice date: {invoice.invoice_date.isoformat()}", f"Days apart: {days_apart}"],
            "Confirm that the transaction settles this invoice.", AnomalySeverity.medium,
        ))
    if currency_mismatch:
        if vendor_agrees and not amount_mismatch and not date_mismatch:
            outcome = MatchOutcome.currency_mismatch
            reason = f"Vendor and amount matched, but currencies differ ({transaction.currency} vs {invoice.currency})."
        anomalies.append(make_anomaly(
            "currency_mismatch", f"Transaction currency {transaction.currency} differs from invoice currency {invoice.currency}.",
            [f"Transaction currency: {transaction.currency}", f"Invoice currency: {invoice.currency}"],
            "Verify the transaction currency and any conversion evidence.", AnomalySeverity.high,
        ))
    if vendor_agrees and not amount_mismatch and not date_mismatch and not currency_mismatch and transaction.amount == invoice.amount_due:
        outcome = MatchOutcome.exact_match
        reason = "Vendor, amount, date, and currency agree exactly."
    if reference_conflict and vendor_agrees and not amount_mismatch and not date_mismatch and not currency_mismatch:
        outcome = MatchOutcome.probable_match
        reason = "Vendor, amount, date, and currency agree, but the supplied invoice reference conflicts."
    if invoice.paid:
        anomalies.append(make_anomaly(
            "invoice_already_paid",
            "The candidate invoice is already marked as paid in the source data.",
            [f"Invoice: {invoice.invoice_id}"],
            "Confirm the payment history before approving another transaction against this invoice.",
            AnomalySeverity.high,
        ))
        outcome = MatchOutcome.potential_duplicate_payment
        reason = "The candidate invoice is already marked as paid; a potential duplicate payment needs review."

    if any(item.type == "potential_duplicate_payment" for item in anomalies):
        outcome = MatchOutcome.potential_duplicate_payment
        reason = "A potential duplicate payment was detected; human review is required."

    clean = outcome in {MatchOutcome.exact_match, MatchOutcome.probable_match} and not anomalies
    status = MatchStatus.auto_approved if clean else MatchStatus.flagged
    selected_invoice = invoice if vendor_agrees else None
    return _build_result(
        transaction,
        invoice=selected_invoice,
        status=status,
        outcome=outcome,
        reason=reason,
        evidence=evidence,
        anomalies=anomalies,
        amount_difference=amount_difference,
    )


def match_transaction(state: ReconciliationState) -> dict:
    """Match a transaction without choosing between equally plausible invoices.

    Optional state keys `assigned_invoice_ids` and `prior_transactions` are
    supplied by the workflow to enforce one-to-one assignment and detect
    potential duplicate payments across the run.
    """
    transaction = state["transaction"]
    invoices = state["invoices"]
    assigned_ids = set(state.get("assigned_invoice_ids", set()))
    prior_transactions = list(state.get("prior_transactions", []))
    anomalies = _base_anomalies(transaction, prior_transactions)

    # A supplied reference has priority. If it fails to resolve, a vendor/amount
    # fallback can be suggested but the reference conflict forces human review.
    reference_candidates: list[Invoice] = []
    reference_conflict = False
    reference_agrees: bool | None = None
    if transaction.invoice_reference:
        reference = normalize_reference(transaction.invoice_reference)
        reference_candidates = [
            invoice for invoice in invoices
            if reference in {
                normalize_reference(invoice.invoice_id),
                normalize_reference(invoice.invoice_number),
            }
        ]
        if reference_candidates:
            if len(reference_candidates) != 1:
                anomalies.extend(_invoice_duplicate_anomalies(reference_candidates))
                duplicate_reference = bool(duplicate_invoice_identifier_groups(reference_candidates)) or any(
                    {item.invoice_id for item in group}.intersection(
                        {candidate.invoice_id for candidate in reference_candidates}
                    )
                    for group in duplicate_invoice_record_groups(reference_candidates)
                )
                if not duplicate_reference:
                    anomalies.append(make_anomaly(
                        "ambiguous_match", "The transaction reference matches multiple invoice records.",
                        [f"Reference: {transaction.invoice_reference}", "Candidates: " + ", ".join(item.invoice_id for item in reference_candidates)],
                        "Resolve the duplicate or ambiguous invoice reference before approval.", AnomalySeverity.high,
                    ))
                return _build_result(
                    transaction, invoice=None, status=MatchStatus.flagged,
                    outcome=MatchOutcome.duplicate_invoice if duplicate_reference else MatchOutcome.ambiguous_candidates,
                    reason="The transaction reference does not identify one unique invoice.",
                    evidence={"reference": 1.0, "vendor": 0.0, "uniqueness": 0.0}, anomalies=anomalies,
                )
            reference_agrees = True
            candidate = reference_candidates[0]
            if candidate.invoice_id in assigned_ids:
                anomalies.append(make_anomaly(
                    "duplicate_invoice_assignment", "This invoice has already been assigned to an earlier transaction in the reconciliation run.",
                    [f"Invoice: {candidate.invoice_id}"], "Review the earlier assignment and determine whether this is a duplicate payment.", AnomalySeverity.high,
                ))
                if any(item.type == "potential_duplicate_payment" for item in anomalies):
                    outcome = MatchOutcome.potential_duplicate_payment
                else:
                    outcome = MatchOutcome.duplicate_invoice
                return _build_result(
                    transaction, invoice=None, status=MatchStatus.flagged, outcome=outcome,
                    reason="The referenced invoice is already assigned to another transaction.",
                    evidence={"reference": 1.0, "vendor": float(normalize_vendor(candidate.vendor_name) == normalize_vendor(transaction.vendor_name)), "uniqueness": 0.0},
                    anomalies=anomalies,
                )
            anomalies.extend(_invoice_duplicate_anomalies(reference_candidates))
            return _finalize_candidate(
                transaction, candidate, reference_agrees=True, unique=True,
                anomalies=anomalies,
            )
        reference_conflict = True
        reference_agrees = False

    vendor_candidates = [
        invoice for invoice in invoices
        if normalize_vendor(invoice.vendor_name) == normalize_vendor(transaction.vendor_name)
    ]
    if not vendor_candidates:
        if not invoices:
            outcome = MatchOutcome.missing_invoice
            reason = "No invoices were available for reconciliation."
            anomaly_type = "missing_invoice"
        else:
            outcome = MatchOutcome.vendor_mismatch
            reason = "No invoice was found for the normalized transaction vendor."
            anomaly_type = "unmatched_transaction"
        anomalies.append(make_anomaly(
            anomaly_type, reason,
            [f"Transaction: {transaction.transaction_id}", f"Vendor: {transaction.vendor_name}"],
            "Check the source invoice file and vendor details.", AnomalySeverity.medium,
        ))
        if any(item.type == "potential_duplicate_payment" for item in anomalies):
            outcome = MatchOutcome.potential_duplicate_payment
        return _build_result(
            transaction, invoice=None, status=MatchStatus.unmatched, outcome=outcome,
            reason=reason, evidence={"vendor": 0.0, "uniqueness": 0.0}, anomalies=anomalies,
        )

    duplicate_ids = duplicate_invoice_identifier_groups(vendor_candidates)
    vendor_invoice_anomalies = _invoice_duplicate_anomalies(vendor_candidates)
    if duplicate_ids:
        anomalies.extend(vendor_invoice_anomalies)
        anomalies.append(make_anomaly(
            "duplicate_invoice_identifier", "A vendor candidate uses an invoice identifier more than once.",
            ["Invoice identifiers: " + ", ".join(sorted(duplicate_ids))],
            "Correct duplicate invoice identifiers before matching.", AnomalySeverity.high,
        ))
        return _build_result(
            transaction, invoice=None, status=MatchStatus.flagged,
            outcome=MatchOutcome.duplicate_invoice,
            reason="Duplicate invoice identifiers prevent a safe match.",
            evidence={"vendor": 1.0, "uniqueness": 0.0}, anomalies=anomalies,
        )

    ranked: list[tuple[tuple, Invoice]] = []
    for invoice in vendor_candidates:
        percentage = _percentage_difference(transaction.amount, invoice.amount_due)
        date_delta = abs((transaction.date - invoice.invoice_date).days)
        rank = (
            percentage <= AMOUNT_TOLERANCE,
            date_delta <= DATE_TOLERANCE_DAYS,
            transaction.currency == invoice.currency,
            -percentage,
            -date_delta,
        )
        ranked.append((rank, invoice))
    ranked.sort(key=lambda item: (item[0], item[1].invoice_id), reverse=True)
    top_rank = ranked[0][0]
    top_candidates = [invoice for rank, invoice in ranked if rank == top_rank]

    duplicate_record_members = {
        invoice.invoice_id
        for group in duplicate_invoice_record_groups(vendor_candidates)
        for invoice in group
    }
    duplicated_top_records = any(item.invoice_id in duplicate_record_members for item in top_candidates)
    if len(top_candidates) > 1 or duplicated_top_records:
        anomalies.extend(vendor_invoice_anomalies)
        duplicate_ids = duplicate_invoice_identifier_groups(top_candidates)
        duplicate_outcome = bool(duplicate_ids) or duplicated_top_records
        if not duplicate_outcome:
            anomalies.append(make_anomaly(
                "ambiguous_match", "Multiple invoice candidates have equally strong available evidence.",
                ["Candidates: " + ", ".join(sorted(item.invoice_id for item in top_candidates))],
                "Review invoice references and source documents to identify the correct invoice.", AnomalySeverity.high,
            ))
        return _build_result(
            transaction, invoice=None, status=MatchStatus.flagged,
            outcome=MatchOutcome.duplicate_invoice if duplicate_outcome else MatchOutcome.ambiguous_candidates,
            reason=("Duplicate invoice records prevent a safe match; no invoice was assigned." if duplicate_outcome else "Multiple invoice candidates are equally plausible; no invoice was assigned."),
            evidence={"vendor": 1.0, "amount": float(top_rank[0]), "date": float(top_rank[1]), "currency": float(top_rank[2]), "uniqueness": 0.0, **({"reference": 0.0} if reference_conflict else {})},
            anomalies=anomalies,
        )

    candidate = top_candidates[0]
    anomalies.extend(vendor_invoice_anomalies)
    if candidate.invoice_id in assigned_ids:
        anomalies.append(make_anomaly(
            "duplicate_invoice_assignment", "The best invoice candidate is already assigned to another transaction in this run.",
            [f"Invoice: {candidate.invoice_id}"], "Review the earlier assignment and confirm whether the transactions are duplicate payments.", AnomalySeverity.high,
        ))
        outcome = MatchOutcome.potential_duplicate_payment if any(item.type == "potential_duplicate_payment" for item in anomalies) else MatchOutcome.duplicate_invoice
        return _build_result(
            transaction, invoice=None, status=MatchStatus.flagged, outcome=outcome,
            reason="The best invoice candidate is already assigned; it was not assigned again.",
            evidence={"vendor": 1.0, "uniqueness": 0.0, **({"reference": 0.0} if reference_conflict else {})},
            anomalies=anomalies,
        )

    return _finalize_candidate(
        transaction, candidate, reference_agrees=reference_agrees, unique=True,
        anomalies=anomalies, reference_conflict=reference_conflict,
    )
