from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional
from enum import Enum

from pydantic import BaseModel, Field, field_validator 

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

class Transaction(BaseModel):
    transaction_id: str
    vendor_name: str
    date:date
    description: str
    amount: Decimal
    currency: str = "USD"
    category: TransactionCategory = TransactionCategory.unknown

    @field_validator("transaction_id", "vendor_name", "description", "currency")
    @classmethod
    def strip_text(cls,value: str) -> str:
        return value.strip() 

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls,value: str) -> str:
        return value.upper()

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
        return value.strip()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls,value: str) -> str:
        return value.upper()

class MatchResult(BaseModel):
    transaction_id: str
    invoice_id: Optional[str] = None
    status: MatchStatus
    confidence_score: float = Field(ge=0.0, le=1.0)
    amount_difference: Optional[Decimal] = None
    reason: str

