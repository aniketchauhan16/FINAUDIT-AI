# FinAudit AI

**AI-Powered Financial Audit & Transaction Analysis**

FinAudit AI is an academic project focused on automating part of the financial audit workflow. The current system works with transaction and invoice data to identify exact matches, amount discrepancies, and unmatched transactions that may require further review.

## Research Background

The project is inspired by the growing use of AI and workflow automation in finance.

### Zomma

Zomma is a Y Combinator company building an AI workforce for financial services using computer-use agents. Its agents work on tasks such as KYC reviews, transaction alerts, and disputes inside the software used by financial operations teams.

**Learning for FinAudit AI:** repetitive financial review tasks can be structured into workflows where AI assists with investigation, evidence gathering, and exception handling.

Reference: https://www.ycombinator.com/companies/zomma

### Billow AI Labs

Billow AI Labs is a Y Combinator company building an AI-native accounting firm. It uses AI agents for accounting and FP&A work and works with existing financial systems and spreadsheets to keep teams compliant and audit-ready.

**Learning for FinAudit AI:** accounting workflows contain repetitive reconciliation, information gathering, and approval tasks that can be automated while keeping humans involved for important decisions.

Reference: https://www.ycombinator.com/companies/billow-ai-labs

## Project Scope

FinAudit AI focuses on a smaller and more specific problem than these commercial systems:

- Loading financial transactions and invoices
- Validating and normalizing financial data
- Matching transactions with invoices
- Comparing vendors, dates, and amounts
- Identifying amount discrepancies
- Detecting unmatched transactions
- Assigning a match status and confidence score
- Providing a reason for the result
- Separating automatically acceptable cases from cases requiring review

## Current Workflow

```text
Transactions + Invoices
          |
          v
   Data Validation
          |
          v
 Transaction/Invoice Matching
          |
          v
 Vendor + Date + Amount Comparison
          |
          v
 Confidence / Difference Calculation
          |
          v
 +-------------------------------+
 | Match Result                  |
 |-------------------------------|
 | Auto-approved                 |
 | Flagged                       |
 | Unmatched                     |
 | Approved / Rejected           |
 +-------------------------------+
          |
          v
     Human Review
```

## Project Structure

```text
FinAudit-AI/
│
├── app/
│   ├── models.py
│   ├── database.py
│   └── ...
│
├── data/
│   ├── transactions.csv
│   └── invoices.csv
│
├── generate_sample_data.py
│
├── requirements.txt
│
└── README.md
```

> File names may vary slightly depending on the current project version.

## Data Models

The project uses Pydantic models to keep financial records structured and validated.

### Transaction

A transaction contains fields such as:

- `transaction_id`
- `vendor_name`
- `date`
- `description`
- `amount`
- `currency`
- `category`

### Invoice

An invoice contains:

- `invoice_id`
- `vendor_name`
- `invoice_number`
- `invoice_date`
- `amount_due`
- `currency`
- `paid`

### MatchResult

The matching output contains:

- `transaction_id`
- `invoice_id`
- `status`
- `confidence_score`
- `amount_difference`
- `reason`

## Sample Data

The project includes a sample-data generator for testing the workflow.

The generated dataset contains approximately:

- **60% exact transaction/invoice matches**
- **25% near matches with amount discrepancies**
- **15% unmatched transactions**

This allows the system to be tested on both normal and suspicious cases without requiring real financial data.

Run:

```bash
python generate_sample_data.py
```

This creates:

```text
data/invoices.csv
data/transactions.csv
```

## Technology Stack

- **Python** – core development
- **Pydantic** – data validation and structured models
- **CSV** – initial sample-data storage
- **Decimal** – accurate financial amount representation
- **Enum** – controlled transaction/match categories
- **AI/ML concepts** – planned/ongoing intelligent matching and anomaly analysis

## AI Engineering Reference

For learning about building AI applications, we are referring to:

**Chip Huyen – AI Engineering: Building Applications with Foundation Models, O'Reilly Media**

Relevant concepts include:

- AI application architecture
- Data preparation
- Evaluation
- Agents and workflows
- Reliable AI application design
- Human-in-the-loop systems

Reference: https://www.oreilly.com/library/view/ai-engineering/9781098166298/

## Research Direction

The project is being developed by studying existing AI-finance products and practical AI engineering concepts rather than attempting to reproduce a commercial product.

The research currently focuses on:

1. Financial workflow automation
2. Transaction and invoice matching
3. Financial anomaly detection
4. AI-assisted auditing
5. Confidence-based decision making
6. Human review of exceptional cases
7. Evaluation of AI-assisted financial workflows

## Future Scope

Possible future improvements include:

- Fuzzy matching for vendor names
- NLP-based invoice/transaction description matching
- Machine-learning-based anomaly detection
- LLM-assisted explanations
- PDF invoice extraction
- Database integration
- Real-time financial data ingestion
- Audit trails and evidence tracking
- Human-in-the-loop approval workflows
- Dashboard for auditors

## Disclaimer

FinAudit AI is an academic project and uses generated/sample financial data. It is intended for learning and demonstration purposes and should not be treated as professional accounting, auditing, compliance, or financial advice.

## References

1. Y Combinator – Zomma: https://www.ycombinator.com/companies/zomma
2. Y Combinator – Billow AI Labs: https://www.ycombinator.com/companies/billow-ai-labs
3. Chip Huyen – *AI Engineering*, O'Reilly Media: https://www.oreilly.com/library/view/ai-engineering/9781098166298/
