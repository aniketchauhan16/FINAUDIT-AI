import csv
from pathlib import Path

from app.models import Invoice, Transaction


DATA_DIR = Path("data")
TRANSACTIONS_CSV = DATA_DIR / "transactions.csv"
INVOICES_CSV = DATA_DIR / "invoices.csv"


def load_transactions() -> list[Transaction]:
    transactions: list[Transaction] = []

    with TRANSACTIONS_CSV.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            transactions.append(
                Transaction(
                    transaction_id=row["transaction_id"],
                    date=row["date"],
                    amount=row["amount"],
                    vendor_name=row["vendor"],
                    description=row["description"],
                    category=row["category"],
                )
            )

    return transactions


def load_invoices() -> list[Invoice]:
    invoices: list[Invoice] = []

    with INVOICES_CSV.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            invoices.append(
                Invoice(
                    invoice_id=row["invoice_id"],
                    vendor_name=row["vendor"],
                    invoice_number=row["invoice_id"],
                    invoice_date=row["date"],
                    amount_due=row["amount"],
                )
            )

    return invoices
