"""Small Streamlit client for the existing FinAudit AI HTTP API."""

import json
import os
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import streamlit as st


class APIError(Exception):
    """A readable error returned while calling the FinAudit API."""


def api_request(base_url: str, path: str, method: str = "GET", payload=None):
    if not base_url:
        raise APIError("Enter the FastAPI base URL in the sidebar.")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        with urlopen(request, timeout=20) as response:
            content = response.read()
            return json.loads(content) if content else None
    except HTTPError as exc:
        try:
            response_body = json.loads(exc.read().decode("utf-8"))
            detail = response_body.get("detail", response_body)
        except (ValueError, AttributeError):
            detail = exc.reason
        raise APIError(f"API returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise APIError(
            f"Could not reach the API at {base_url}. Start the FastAPI server and try again. ({reason})"
        ) from exc


def result_rows(results: list[dict]) -> list[dict]:
    return [
        {
            "Transaction": item["transaction_id"],
            "Invoice": item.get("invoice_id") or "—",
            "Match status": item["status"],
            "Match outcome": item.get("match_outcome") or "legacy / not recorded",
            "Review status": item["review_status"],
            "Difference": item.get("amount_difference") or "—",
            "Confidence": f"{item['confidence_score']:.0%}",
            "Anomalies": ", ".join(anomaly["type"] for anomaly in item.get("anomalies", [])) or "—",
            "Reviewer": item.get("reviewed_by") or "—",
            "Reason": item["reason"],
        }
        for item in results
    ]


st.set_page_config(page_title="FinAudit AI", page_icon="🧾", layout="wide")
st.title("FinAudit AI")
st.caption("Local transaction and invoice reconciliation")

with st.sidebar:
    st.header("Connection")
    api_base = st.text_input(
        "FastAPI base URL",
        value=os.getenv("FINAUDIT_API_URL", "http://127.0.0.1:8000"),
    ).strip()
    page = st.radio("Page", ("Overview", "Review queue"))
    reviewer = st.text_input("Reviewer name", value="")
    reviewer_name = reviewer.strip()
    reviewer_is_valid = 1 <= len(reviewer_name) <= 120
    if reviewer and not reviewer_is_valid:
        st.caption("Reviewer name must contain 1–120 characters.")
    if st.button("Refresh data", width="stretch"):
        st.rerun()

flash = st.session_state.pop("flash_message", None)
if flash:
    st.success(flash)

if page == "Overview":
    st.subheader("Reconciliation overview")
    if st.button("Run reconciliation", type="primary"):
        try:
            results = api_request(api_base, "/run-reconciliation", method="POST")
            st.session_state["flash_message"] = f"Reconciled {len(results)} transactions."
            st.rerun()
        except APIError as exc:
            st.error(str(exc))

    try:
        results = api_request(api_base, "/reconciliation-results")
    except APIError as exc:
        st.error(str(exc))
        st.info("Start the API with `python -m uvicorn app.main:app --reload`.")
    else:
        if not results:
            st.info("No reconciliation results yet. Run reconciliation to get started.")
        else:
            review_counts = Counter(item["review_status"] for item in results)
            status_counts = Counter(item["status"] for item in results)
            human_approved = sum(
                item["review_status"] == "approved" and item.get("reviewed_by") != "system"
                for item in results
            )
            columns = st.columns(6)
            columns[0].metric("Transactions", len(results))
            columns[1].metric("Auto-approved", status_counts["auto_approved"])
            columns[2].metric("Pending review", review_counts["pending_review"])
            columns[3].metric("Human approved", human_approved)
            columns[4].metric("Rejected", review_counts["rejected"])
            columns[5].metric("Unmatched", status_counts["unmatched"])
            st.dataframe(result_rows(results), width="stretch", hide_index=True)

elif page == "Review queue":
    st.subheader("Flagged transactions")
    if not reviewer_name:
        st.info("Enter your reviewer name in the sidebar to enable review actions.")

    try:
        pending = api_request(api_base, "/transactions/flagged")
        transactions = api_request(api_base, "/transactions")
    except APIError as exc:
        st.error(str(exc))
        st.info("Start the API with `python -m uvicorn app.main:app --reload`.")
    else:
        transactions_by_id = {item["transaction_id"]: item for item in transactions}
        if not pending:
            st.info("There are no transactions awaiting review.")
        for item in pending:
            transaction = transactions_by_id.get(item["transaction_id"], {})
            label = f"{item['transaction_id']} · {item['status']} · {transaction.get('vendor_name', 'Unknown vendor')}"
            with st.expander(label):
                st.write(f"**Vendor:** {transaction.get('vendor_name', 'Unknown')}")
                st.write(f"**Date:** {transaction.get('date', '—')}")
                st.write(f"**Transaction amount:** {transaction.get('amount', '—')}")
                st.write(f"**Invoice:** {item.get('invoice_id') or 'No matching invoice'}")
                st.write(f"**Amount difference:** {item.get('amount_difference') or '—'}")
                st.write(f"**Reason:** {item['reason']}")
                for anomaly in item.get("anomalies", []):
                    st.warning(
                        f"{anomaly['type']} ({anomaly['severity']}): {anomaly['explanation']}"
                    )
                    if anomaly.get("evidence"):
                        st.caption("Evidence: " + "; ".join(anomaly["evidence"]))
                    st.caption("Recommended action: " + anomaly["recommended_action"])
                approve_col, reject_col = st.columns(2)
                for decision, column in (("approve", approve_col), ("reject", reject_col)):
                    if column.button(
                        decision.title(),
                        key=f"{decision}-{item['transaction_id']}",
                        disabled=not reviewer_is_valid,
                        width="stretch",
                    ):
                        try:
                            api_request(
                                api_base,
                                f"/transactions/{quote(item['transaction_id'], safe='')}/{decision}",
                                method="POST",
                                payload={"reviewed_by": reviewer_name},
                            )
                            outcome = "approved" if decision == "approve" else "rejected"
                            st.session_state["flash_message"] = (
                                f"{item['transaction_id']} {outcome} by {reviewer_name}."
                            )
                            st.rerun()
                        except APIError as exc:
                            st.error(str(exc))
