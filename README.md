# FinAudit AI

FinAudit AI is a local reconciliation MVP. It reads transaction and invoice CSVs,
classifies transactions, matches vendor and amount, and stores reconciliation and
human review results in SQLite.

## Requirements and setup

- Python 3.10 or newer
- Install the packages in `requirements.txt`

From the project root, create and activate a virtual environment, then install
the app dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

The app expects `data/transactions.csv` and `data/invoices.csv`. Generate the
reproducible demo CSVs with:

```powershell
python generate_sample_data.py
```

The database is created at `data/finaudit.db` when the app starts. CSV files
and the database are local demo data; keep your own input files backed up.

## Run the API

```powershell
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for interactive API documentation. The local
server provides:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Health check |
| `GET` | `/transactions` | Load transactions from CSV |
| `GET` | `/invoices` | Load invoices from CSV |
| `POST` | `/run-reconciliation` | Run the graph and persist all results |
| `GET` | `/reconciliation-results` | List all saved reconciliation results |
| `GET` | `/transactions/flagged` | List results awaiting human review |
| `POST` | `/transactions/{transaction_id}/approve` | Approve a pending result |
| `POST` | `/transactions/{transaction_id}/reject` | Reject a pending result |

Approve and reject requests take JSON such as:

```json
{"reviewed_by": "Alex Reviewer"}
```

They return `404` when the transaction has no saved reconciliation result and
`409` when it is already reviewed. Invalid request bodies return `422`.
Re-running reconciliation preserves a human decision already stored for that
transaction.

## Run the dashboard

The dashboard calls the FastAPI backend; it does not run matching locally. Open
two PowerShell terminals at the project root after completing setup above.

Terminal 1, start the API:

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload
```

Terminal 2, start Streamlit:

```powershell
.\.venv\Scripts\Activate.ps1
python -m streamlit run dashboard.py
```

Streamlit opens the dashboard at `http://localhost:8501`. Keep the API running
at `http://127.0.0.1:8000`, the default address shown in the dashboard sidebar.
The sidebar also accepts a different API base URL. Enter a reviewer name on the
Review queue page before approving or rejecting a result.

## Demo workflow

1. Generate the sample CSVs.
2. Start the API and dashboard in separate terminals.
3. Select **Run reconciliation** on the Overview page.
4. Review the summary metrics and results table. The matcher auto-approves
   amount differences at or below 2%; larger differences and missing invoices
   enter the review queue.
5. Open **Review queue**, enter a reviewer name, then approve or reject a
   pending result. The dashboard refreshes after each action.
6. The SQLite record keeps the decision and timestamp. The same endpoints are
   also available at `http://127.0.0.1:8000/docs`.

## Tests

Run the focused standard-library test suite from the project root:

```powershell
python -m unittest discover -s tests -v
```

The tests cover the 2% match threshold, unmatched and discrepancy outcomes,
conditional graph routing, review decisions, and SQLite persistence.
