from app.agents.state import ReconciliationState
from app.models import TransactionCategory


def classify_transaction(state: ReconciliationState) -> dict:
    transaction = state["transaction"]

    text = f"{transaction.vendor_name} {transaction.description}".lower()

    if "payroll" in text or "salary" in text:
        category = TransactionCategory.payroll
    elif "utility" in text or "electric" in text or "water" in text:
        category = TransactionCategory.utility
    elif "rent" in text or "lease" in text:
        category = TransactionCategory.rent
    elif "payment" in text or "invoice" in text:
        category = TransactionCategory.vendor_payment
    else:
        category = TransactionCategory.misc_expense

    updated_transaction = transaction.model_copy(
        update={"category": category}
    )

    return {"transaction": updated_transaction}