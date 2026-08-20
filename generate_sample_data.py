"""
generate_sample_data.py
Generates realistic-messy fake transaction + invoice data for FinAudit AI.
Run this once to create data/transactions.csv and data/invoices.csv
"""

import csv
import random
from datetime import datetime, timedelta

random.seed(42)  # reproducible fake data

VENDORS = [
    "Acme Supplies Pvt Ltd", "TechCorp Solutions", "Global Logistics Inc",
    "Sunrise Traders", "Metro Office Supplies", "Prime Consulting Group",
    "BlueSky Manufacturing", "Delta Freight Services", "Nova Enterprises",
    "Horizon Industrial Co"
]

CATEGORIES = ["vendor_payment", "payroll", "utility", "rent", "misc_expense"]


def generate_invoices(n=50):
    invoices = []
    for i in range(1, n + 1):
        vendor = random.choice(VENDORS)
        amount = round(random.uniform(500, 50000), 2)
        date = datetime(2026, 1, 1) + timedelta(days=random.randint(0, 180))
        invoices.append({
            "invoice_id": f"INV-{1000 + i}",
            "vendor": vendor,
            "amount": amount,
            "date": date.strftime("%Y-%m-%d"),
            "description": f"Invoice for services rendered by {vendor}"
        })
    return invoices


def generate_transactions(invoices, n=80):
    """
    Generates transactions - some match invoices exactly, some are close
    (small amount mismatch = should be flagged), some don't match anything
    (should be flagged as suspicious/unmatched).
    """
    transactions = []

    # 60% clean matches to real invoices
    matched_count = int(n * 0.6)
    for i in range(matched_count):
        inv = random.choice(invoices)
        transactions.append({
            "transaction_id": f"TXN-{2000 + i}",
            "date": inv["date"],
            "amount": inv["amount"],
            "vendor": inv["vendor"],
            "description": f"Payment to {inv['vendor']}",
            "category": "vendor_payment"
        })

    # 25% near-matches with small discrepancies (should get flagged)
    near_match_count = int(n * 0.25)
    for i in range(near_match_count):
        inv = random.choice(invoices)
        discrepancy = random.choice([0.95, 1.05, 1.10, 0.90])  # 5-10% off
        transactions.append({
            "transaction_id": f"TXN-{2100 + i}",
            "date": inv["date"],
            "amount": round(inv["amount"] * discrepancy, 2),
            "vendor": inv["vendor"],
            "description": f"Payment to {inv['vendor']} (partial/adjusted)",
            "category": "vendor_payment"
        })

    # 15% no matching invoice at all (should be flagged as suspicious)
    unmatched_count = n - matched_count - near_match_count
    for i in range(unmatched_count):
        date = datetime(2026, 1, 1) + timedelta(days=random.randint(0, 180))
        transactions.append({
            "transaction_id": f"TXN-{2200 + i}",
            "date": date.strftime("%Y-%m-%d"),
            "amount": round(random.uniform(200, 20000), 2),
            "vendor": random.choice(VENDORS + ["Unknown Entity LLC", "Cash Withdrawal"]),
            "description": "Unrecognized transaction - no invoice reference",
            "category": random.choice(CATEGORIES)
        })

    random.shuffle(transactions)
    return transactions


def write_csv(filename, rows, fieldnames):
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {filename}")


if __name__ == "__main__":
    invoices = generate_invoices(50)
    transactions = generate_transactions(invoices, 80)

    write_csv(
        "data/invoices.csv",
        invoices,
        fieldnames=["invoice_id", "vendor", "amount", "date", "description"]
    )
    write_csv(
        "data/transactions.csv",
        transactions,
        fieldnames=["transaction_id", "date", "amount", "vendor", "description", "category"]
    )

    print("\nSample data generated. Roughly 60% should match cleanly, "
          "25% will have amount discrepancies (flag-worthy), "
          "15% have no matching invoice (flag-worthy).")