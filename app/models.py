from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from enum import Enum

from pydantic import BaseModel, Field, field_validator


SUPPORTED_CURRENCIES = frozenset({"USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY", "CHF"})

class TransactionCategory(str, Enum):
    vendor_payment = "vendor_payment"
    payroll = "payroll"
    utility = "utility"
    rent = "rent"
    misc_expense = "misc_expense"
    unknown = "unknown"

class MatchStatus(str, Enum):
    auto_approved = "auto_approved"
    flagged = "flagged"
    approved = "approved"
    rejected = "rejected"
    unmatched = "unmatched"

class ReviewStatus(str, Enum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class MatchOutcome(str, Enum):
    exact_match = "exact_match"
    probable_match = "probable_match"
    amount_mismatch = "amount_mismatch"
    date_mismatch = "date_mismatch"
    currency_mismatch = "currency_mismatch"
    vendor_mismatch = "vendor_mismatch"
    missing_invoice = "missing_invoice"
    ambiguous_candidates = "ambiguous_candidates"
    duplicate_invoice = "duplicate_invoice"
    potential_duplicate_payment = "potential_duplicate_payment"
    invalid_input = "invalid_input"


class AnomalySeverity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Anomaly(BaseModel):
    type: str
    severity: AnomalySeverity
    evidence: list[str] = Field(default_factory=list)
    explanation: str
    recommended_action: str
    human_review_required: bool = True

class Transaction(BaseModel):
    transaction_id: str
    vendor_name: str
    date:date
    description: str
    amount: Decimal
    currency: str = "USD"
    category: TransactionCategory = TransactionCategory.unknown
    invoice_reference: Optional[str] = None

    @field_validator("transaction_id", "vendor_name", "description", "currency")
    @classmethod
    def strip_text(cls,value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("invoice_reference")
    @classmethod
    def normalize_reference(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls,value: str) -> str:
        value = value.upper()
        if value not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency '{value}'")
        return value

    @field_validator("amount")
    @classmethod
    def finite_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount must be a finite decimal")
        return value

class Invoice(BaseModel):
    invoice_id: str
    vendor_name: str
    invoice_number: str
    invoice_date: date
    amount_due : Decimal 
    currency: str = "USD"
    paid: bool = False

    @field_validator("invoice_id","vendor_name","invoice_number","currency")
    @classmethod
    def strip_text(cls,value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls,value: str) -> str:
        value = value.upper()
        if value not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency '{value}'")
        return value

    @field_validator("amount_due")
    @classmethod
    def finite_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount_due must be a finite decimal")
        return value

class MatchResult(BaseModel):
    transaction_id: str
    invoice_id: Optional[str] = None
    status: MatchStatus
    confidence_score: float = Field(ge=0.0, le=1.0)
    amount_difference: Optional[Decimal] = None
    reason: str
    review_status: ReviewStatus = ReviewStatus.pending_review
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    match_outcome: Optional[MatchOutcome] = None
    evidence: dict[str, float] = Field(default_factory=dict)
    anomalies: list[Anomaly] = Field(default_factory=list)
