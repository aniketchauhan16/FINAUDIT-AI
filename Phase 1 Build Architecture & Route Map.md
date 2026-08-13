# FinAudit AI — Phase 1 Build Architecture & Route Map

## Phase 1 Goal

A working, deployed MVP: messy transaction/invoice data in → classified & matched →
flagged items wait for human approval → deployed on GitHub with a clean README.

---

## Architecture Diagram (text form)

```
┌─────────────────┐
│  Data Ingestion  │  CSV transactions + invoices (already generated via script)
└────────┬─────────┘
         ↓
┌─────────────────────────┐
│   LangGraph State Graph  │
│                          │
│  ┌────────────────────┐  │
│  │  Node 1: Classifier │  │  ← LLM call: reads transaction, assigns category
│  └─────────┬──────────┘  │     (vendor_payment, payroll, utility, etc.)
│            ↓             │
│  ┌────────────────────┐  │
│  │  Node 2: Matcher    │  │  ← Looks up matching invoice, checks amount/vendor
│  └─────────┬──────────┘  │
│            ↓             │
│  ┌────────────────────┐  │
│  │ Conditional Edge:   │  │  ← Decision point: clean match vs needs review
│  │ Clean or Flagged?   │  │
│  └──┬──────────────┬───┘  │
│     ↓              ↓      │
│  Auto-Approve   Flag for  │
│  (green)        Human     │
│                 (yellow)  │
└─────────────────────────┘
         ↓
┌─────────────────┐
│  FastAPI Layer   │  Exposes endpoints: /transactions, /flagged, /approve/{id}
└────────┬─────────┘
         ↓
┌─────────────────┐
│  Streamlit UI    │  Friend's part: table view + approve/reject buttons
└─────────────────┘
```

---

## Build Order — do these in sequence, not in parallel

### Step 1: Data layer (you can start this NOW, doesn't need LangGraph)

- [x] Already done: `generate_sample_data.py` creates `transactions.csv` + `invoices.csv`
- [ ] Write simple Python functions to load these into memory/SQLite as a starting DB
- [ ] Define Pydantic models: `Transaction`, `Invoice`, `MatchResult`

### Step 2: The two LangGraph nodes (build once LangGraph playlist is done)

**Node 1 — Classifier**

- Input: one `Transaction` object
- Prompt: "Given this transaction description, classify it as one of: vendor_payment,
  payroll, utility, rent, misc_expense"
- Output: category label added to transaction state

**Node 2 — Matcher**

- Input: classified transaction
- Logic: search invoices.csv for matching vendor + similar amount (this is where
  the `get_complexity_signals()` function from the Model Router doc plugs in later)
- Output: `matched_invoice_id` (or `None`) + `amount_diff_pct`

### Step 3: Conditional edge — the decision point

- If `amount_diff_pct < 2%` AND invoice found → route to **auto-approve**
- Else → route to **flagged, needs human review**
- This conditional routing is exactly what you're learning in LangGraph's
  "Conditional Workflows" video — build this node right after watching it

### Step 4: Human-in-the-loop node (the Trust Layer)

- Flagged transactions get written to a `pending_review` table/list
- Graph pauses here — doesn't proceed until a human calls the approve/reject endpoint
- This is LangGraph's "Human-in-the-Loop" pattern from your playlist — build this
  right after that specific video

### Step 5: Wrap it in FastAPI

Endpoints needed for v1:

```
GET  /transactions          → list all transactions with status
GET  /transactions/flagged  → list only flagged ones
POST /transactions/{id}/approve  → human approves, updates status
POST /transactions/{id}/reject   → human rejects, updates status
POST /run-reconciliation    → triggers the LangGraph pipeline on new data
```

### Step 6: Deploy

- Same pattern as insurance-premium-api: Dockerfile → Render
- Push clean repo, README, present_files-style demo screenshots

---

## What to build yourself vs. hand to your friend

**You build (backend, all of the above):**

- Data models, LangGraph graph, FastAPI endpoints, deployment

**Friend builds (frontend, parallel, using mock data until your API is ready):**

- Table showing transactions with color-coded status
- Detail view + Approve/Reject buttons calling your `/approve` and `/reject` endpoints
- (Already scoped in the frontend document shared with him earlier)

---

## Order of operations — what to do this week vs. later

**Do now (doesn't block on LangGraph completion):**

1. Set up repo structure (already done)
2. Data models (Pydantic classes for Transaction/Invoice)
3. Basic FastAPI skeleton with mock/hardcoded responses — lets your friend start
   building frontend against fake data immediately

**Do once LangGraph playlist is finished:** 4. Build Node 1 (Classifier) — test standalone before wiring into graph 5. Build Node 2 (Matcher) — test standalone 6. Wire conditional edge (clean vs flagged) 7. Add human-in-the-loop pause/resume 8. Replace mock FastAPI responses with real graph execution 9. Deploy, write README, done — Phase 1 complete

---

## Definition of "Phase 1 done"

- [ ] Runs end-to-end: CSV in → classified → matched → flagged/approved
- [ ] At least one flagged transaction can be approved/rejected via API
- [ ] Deployed publicly (Render)
- [ ] Frontend shows real data (not mock) from your live backend
- [ ] README explains the Trust Layer concept clearly
- [ ] Pushed to GitHub as a clean, standalone repo

Once all boxes are checked, Phase 1 is genuinely finished — move to Phase 2
(fraud agent, RBAC, LangSmith, PostgreSQL, Redis) from the main roadmap file.
