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


def show_api_error(error: APIError) -> None:
    """Render API error text literally so untrusted response details stay inert."""
    st.error("The API request failed.")
    st.text(str(error))


def api_request(
    base_url: str,
    path: str,
    method: str = "GET",
    payload=None,
    token: str | None = None,
):
    if not base_url:
        raise APIError("Enter the FastAPI base URL in the sidebar.")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
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

token = st.session_state.get("access_token")
if not token:
    st.subheader("Sign in")
    with st.form("login-form", clear_on_submit=True):
        username_input = st.text_input("Username")
        password_input = st.text_input("Password", type="password")
        login_submitted = st.form_submit_button("Sign in", type="primary")
    if login_submitted:
        try:
            login_result = api_request(
                api_base,
                "/auth/login",
                method="POST",
                payload={"username": username_input, "password": password_input},
            )
            st.session_state["access_token"] = login_result["access_token"]
            st.rerun()
        except APIError as exc:
            show_api_error(exc)
    st.stop()

try:
    current_user = api_request(api_base, "/auth/me", token=token)
except APIError as exc:
    st.session_state.pop("access_token", None)
    st.warning("Your session ended. Sign in again.")
    st.text(str(exc))
    st.rerun()

if current_user["role"] == "admin":
    available_pages = ["User management"]
elif current_user["role"] == "reviewer":
    available_pages = ["Overview", "Review queue", "Review history"]
else:
    available_pages = ["Overview", "Review history"]

with st.sidebar:
    st.caption(f"Signed in as **{current_user['username']}** ({current_user['role']})")
    if st.button("Sign out", width="stretch"):
        try:
            api_request(api_base, "/auth/logout", method="POST", token=token)
        except APIError as exc:
            show_api_error(exc)
        st.session_state.pop("access_token", None)
        st.rerun()
    page = st.radio("Page", tuple(available_pages))
    if st.button("Refresh data", width="stretch"):
        st.rerun()

flash = st.session_state.pop("flash_message", None)
if flash:
    st.success("Action completed.")
    st.text(flash)

if page == "Overview":
    st.subheader("Reconciliation overview")
    if current_user["role"] == "reviewer" and st.button("Run reconciliation", type="primary"):
        try:
            results = api_request(
                api_base, "/run-reconciliation", method="POST", token=token
            )
            st.session_state["flash_message"] = f"Reconciled {len(results)} transactions."
            st.rerun()
        except APIError as exc:
            show_api_error(exc)

    try:
        results = api_request(api_base, "/reconciliation-results", token=token)
    except APIError as exc:
        show_api_error(exc)
        st.info("Start the API with `python -m uvicorn app.main:app --reload`.")
    else:
        if not results:
            st.info("No reconciliation results yet.")
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
    try:
        pending = api_request(api_base, "/transactions/flagged", token=token)
        transactions = api_request(api_base, "/transactions", token=token)
    except APIError as exc:
        show_api_error(exc)
        st.info("Start the API with `python -m uvicorn app.main:app --reload`.")
    else:
        transactions_by_id = {item["transaction_id"]: item for item in transactions}
        if not pending:
            st.info("There are no transactions awaiting review.")
        for index, item in enumerate(pending, start=1):
            transaction = transactions_by_id.get(item["transaction_id"], {})
            with st.expander(f"Review item {index}"):
                st.text(f"Transaction ID: {item['transaction_id']}")
                st.text(f"Vendor: {transaction.get('vendor_name', 'Unknown')}")
                st.text(f"Date: {transaction.get('date', '—')}")
                st.text(f"Transaction amount: {transaction.get('amount', '—')}")
                st.text(f"Invoice: {item.get('invoice_id') or 'No matching invoice'}")
                st.text(f"Amount difference: {item.get('amount_difference') or '—'}")
                st.text(f"Reason: {item['reason']}")
                for anomaly in item.get("anomalies", []):
                    st.warning("Anomaly requires review.")
                    st.text(f"Type: {anomaly['type']} ({anomaly['severity']})")
                    st.text(f"Explanation: {anomaly['explanation']}")
                    if anomaly.get("evidence"):
                        st.text("Evidence: " + "; ".join(anomaly["evidence"]))
                    st.text("Recommended action: " + anomaly["recommended_action"])
                review_reason = st.text_area(
                    "Reason (optional)",
                    key=f"review-reason-{item['transaction_id']}",
                    max_chars=1000,
                )
                approve_col, reject_col = st.columns(2)
                for decision, column in (("approve", approve_col), ("reject", reject_col)):
                    if column.button(
                        decision.title(),
                        key=f"{decision}-{item['transaction_id']}",
                        width="stretch",
                    ):
                        try:
                            api_request(
                                api_base,
                                f"/transactions/{quote(item['transaction_id'], safe='')}/{decision}",
                                method="POST",
                                payload={"reason": review_reason},
                                token=token,
                            )
                            outcome = "approved" if decision == "approve" else "rejected"
                            st.session_state["flash_message"] = (
                                f"{item['transaction_id']} {outcome} by {current_user['username']}."
                            )
                            st.rerun()
                        except APIError as exc:
                            show_api_error(exc)

elif page == "Review history":
    st.subheader("Review history")
    try:
        results = api_request(api_base, "/reconciliation-results", token=token)
    except APIError as exc:
        show_api_error(exc)
        st.info("Start the API with `python -m uvicorn app.main:app --reload`.")
    else:
        transaction_ids = [item["transaction_id"] for item in results]
        if not transaction_ids:
            st.info("No reconciliation results are available yet.")
        else:
            selected_id = st.selectbox("Transaction", transaction_ids)
            try:
                events = api_request(
                    api_base,
                    f"/transactions/{quote(selected_id, safe='')}/review-history",
                    token=token,
                )
            except APIError as exc:
                show_api_error(exc)
            else:
                if not events:
                    st.info(
                        "No review events are recorded for this result. Decisions made before "
                        "review history was introduced are not backfilled."
                    )
                else:
                    st.dataframe(
                        [
                            {
                                "Action": event["action"],
                                "Reviewer": event["reviewed_by"],
                                "User ID": event.get("reviewer_user_id") or "legacy / unavailable",
                                "Timestamp (UTC)": event["occurred_at"],
                                "Reason": event.get("reason") or "—",
                                "Previous review status": event["previous_review_status"],
                                "New review status": event["new_review_status"],
                            }
                            for event in events
                        ],
                        width="stretch",
                        hide_index=True,
                    )

elif page == "User management":
    st.subheader("User management")
    with st.form("create-user-form", clear_on_submit=True):
        new_username = st.text_input("Username")
        new_password = st.text_input("Temporary password (12+ characters)", type="password")
        new_role = st.selectbox("Role", ("reviewer", "auditor", "admin"))
        create_submitted = st.form_submit_button("Create user", type="primary")
    if create_submitted:
        try:
            created = api_request(
                api_base,
                "/admin/users",
                method="POST",
                payload={"username": new_username, "password": new_password, "role": new_role},
                token=token,
            )
            st.success(f"Created {created['role']} account {created['username']}.")
        except APIError as exc:
            show_api_error(exc)
    try:
        users = api_request(api_base, "/admin/users", token=token)
    except APIError as exc:
        show_api_error(exc)
    else:
        if users:
            st.dataframe(
                [
                    {
                        "Username": user["username"],
                        "Role": user["role"],
                        "Active": user["is_active"],
                        "Created": user["created_at"],
                    }
                    for user in users
                ],
                width="stretch",
                hide_index=True,
            )
            selected_user = st.selectbox(
                "Account to update",
                users,
                format_func=lambda user: f"{user['username']} ({user['role']})",
            )
            update_col, active_col = st.columns(2)
            with update_col:
                updated_role = st.selectbox(
                    "Assign role",
                    ("admin", "reviewer", "auditor"),
                    index=("admin", "reviewer", "auditor").index(selected_user["role"]),
                    key="update-user-role",
                )
            with active_col:
                updated_active = st.checkbox(
                    "Account active", value=selected_user["is_active"]
                )
            if st.button("Save account", width="stretch"):
                try:
                    api_request(
                        api_base,
                        f"/admin/users/{quote(selected_user['user_id'], safe='')}",
                        method="PATCH",
                        payload={"role": updated_role, "is_active": updated_active},
                        token=token,
                    )
                    st.success("User account updated.")
                    st.rerun()
                except APIError as exc:
                    show_api_error(exc)
