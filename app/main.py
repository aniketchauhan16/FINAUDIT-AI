from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
import sqlite3
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from app import auth as auth_service
from app import database
from app.agents.anomaly_detector import auto_approval_anomalies
from app.agents.graph import AUTO_DECISION, force_human_review, reconciliation_graph
from app.agents.matcher import AMOUNT_TOLERANCE, DATE_TOLERANCE_DAYS
from app.agents.validation import failure_match_result
from app.models import Anomaly, AuthenticatedUser, Invoice, MatchResult, MatchStatus, ReviewEvent, ReviewStatus, Transaction, UserRole
from app.database import (
    get_match_result,
    get_all_match_results,
    get_pending_review_results,
    get_review_history as get_transaction_review_history,
    init_db,
    load_invoices_report,
    load_transactions_report,
    record_review,
    ReviewConflictError,
    ReviewNotFoundError,
    save_match_results,
    UserAlreadyExistsError,
    UserNotFoundError,
    LastActiveAdminError,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    bootstrap_username = os.getenv("FINAUDIT_BOOTSTRAP_ADMIN_USERNAME")
    bootstrap_password = os.getenv("FINAUDIT_BOOTSTRAP_ADMIN_PASSWORD")
    if bool(bootstrap_username) != bool(bootstrap_password):
        raise RuntimeError(
            "Set both application bootstrap admin environment variables or leave both unset."
        )
    if bootstrap_username and bootstrap_password:
        try:
            bootstrap_model = UserCreate(
                username=bootstrap_username,
                password=bootstrap_password,
                role=UserRole.admin,
            )
            database.bootstrap_admin(
                bootstrap_model.username,
                auth_service.hash_password(bootstrap_model.password.get_secret_value()),
                str(uuid4()),
                datetime.now(timezone.utc).isoformat(),
            )
        except (ValueError, ValidationError):
            raise RuntimeError(
                "Configured bootstrap admin credentials are invalid."
            ) from None
    yield


app = FastAPI(
    title="FinAudit AI",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def safe_sensitive_validation_errors(request: Request, exc: RequestValidationError):
    if request.url.path in {"/auth/login", "/admin/users"}:
        errors = [
            {key: value for key, value in error.items() if key not in {"input", "ctx"}}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": errors},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    return await request_validation_exception_handler(request, exc)


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    password: SecretStr = Field(min_length=12, max_length=128)
    role: UserRole

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def require_nonblank_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Password must not contain only whitespace.")
        return value


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: UserRole | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_update(self):
        if self.role is None and self.is_active is None:
            raise ValueError("At least one of role or is_active must be provided.")
        return self


class UserSummary(BaseModel):
    user_id: str
    username: str
    role: UserRole
    is_active: bool
    created_at: datetime


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: AuthenticatedUser


bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = auth_service.authenticate_token(credentials.credentials)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Session is invalid, expired, or revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthenticatedUser.model_validate(user)


def require_roles(*roles: UserRole):
    def check_role(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Your role cannot perform this action.")
        return user

    return check_role


READ_ROLES = (UserRole.reviewer, UserRole.auditor)
RUN_ROLES = (UserRole.reviewer,)


@app.post("/auth/login", response_model=LoginResponse)
def login(credentials: LoginRequest, response: Response) -> LoginResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    account = database.get_user_by_username(credentials.username)
    password = credentials.password.get_secret_value()
    if account is None:
        auth_service.burn_password_check(password)
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password.",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    valid_password = auth_service.verify_password(
        password, account["password_hash"]
    )
    if not valid_password or not account["is_active"]:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password.",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    token, expires_at = auth_service.issue_session(account["user_id"])
    user = AuthenticatedUser(
        user_id=account["user_id"],
        username=account["username"],
        role=account["role"],
    )
    return LoginResponse(access_token=token, expires_at=expires_at, user=user)


@app.get("/auth/me", response_model=AuthenticatedUser)
def auth_me(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    return user


@app.post("/auth/logout")
def logout(
    _: AuthenticatedUser = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict[str, str]:
    auth_service.revoke_token(credentials.credentials)
    return {"status": "signed_out"}


@app.get("/admin/users", response_model=list[UserSummary])
def get_users(
    _: AuthenticatedUser = Depends(require_roles(UserRole.admin)),
) -> list[dict]:
    return database.list_users()


@app.post("/admin/users", response_model=UserSummary, status_code=201)
def add_user(
    request: UserCreate,
    _: AuthenticatedUser = Depends(require_roles(UserRole.admin)),
) -> dict:
    try:
        return database.create_user(
            str(uuid4()),
            request.username,
            auth_service.hash_password(request.password.get_secret_value()),
            request.role,
            datetime.now(timezone.utc).isoformat(),
        )
    except UserAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail="Username is already registered.") from exc


@app.patch("/admin/users/{user_id}", response_model=UserSummary)
def edit_user(
    user_id: str,
    request: UserUpdate,
    _: AuthenticatedUser = Depends(require_roles(UserRole.admin)),
) -> dict:
    try:
        return database.update_user(user_id, role=request.role, is_active=request.is_active)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    except LastActiveAdminError as exc:
        raise HTTPException(
            status_code=409, detail="At least one active administrator must remain."
        ) from exc


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/transactions", response_model=list[Transaction])
def get_transactions(
    _: AuthenticatedUser = Depends(require_roles(*READ_ROLES)),
) -> list[Transaction]:
    report = load_transactions_report()
    if report.rejected_rows:
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV validation failed.", "rejected_rows": report.error_details()},
        )
    return report.records


@app.get("/invoices", response_model=list[Invoice])
def get_invoices(
    _: AuthenticatedUser = Depends(require_roles(*READ_ROLES)),
) -> list[Invoice]:
    report = load_invoices_report()
    if report.rejected_rows:
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV validation failed.", "rejected_rows": report.error_details()},
        )
    return report.records


@app.post("/run-reconciliation", response_model=list[MatchResult])
def run_reconciliation(
    _: AuthenticatedUser = Depends(require_roles(*RUN_ROLES)),
) -> list[MatchResult]:
    transaction_report = load_transactions_report()
    invoice_report = load_invoices_report()
    if transaction_report.rejected_rows or invoice_report.rejected_rows:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "CSV validation failed; no reconciliation was run.",
                "transactions": transaction_report.error_details(),
                "invoices": invoice_report.error_details(),
            },
        )
    invoices = invoice_report.records
    transactions = transaction_report.records

    results: list[MatchResult] = []
    assigned_invoice_ids: set[str] = set()
    prior_transactions: list[Transaction] = []

    for transaction in transactions:
        try:
            final_state = reconciliation_graph.invoke(
                {
                    "transaction": transaction,
                    "invoices": invoices,
                    "match_result": None,
                    "assigned_invoice_ids": assigned_invoice_ids,
                    "prior_transactions": prior_transactions,
                }
            )
        except Exception as exc:
            final_state = {
                "processing_status": "failed",
                "decision": "review_required",
                "reconciliation_path": "flagged",
                "match_result": failure_match_result(
                    transaction,
                    message=f"Graph execution failed ({type(exc).__name__}).",
                    error_type="processing_error",
                ),
            }

        raw_result = final_state.get("match_result") if isinstance(final_state, dict) else None
        try:
            match_result = MatchResult.model_validate(
                raw_result.model_dump(mode="python") if isinstance(raw_result, BaseModel) else raw_result
            )
        except (ValidationError, TypeError, ValueError):
            match_result = failure_match_result(
                transaction,
                message="Graph execution returned an incomplete match result.",
                error_type="processing_error",
            )
        if match_result is None:
            raise HTTPException(status_code=500, detail="Reconciliation could not produce a reviewable result.")

        if match_result.status == MatchStatus.auto_approved:
            safety_check_failed = False
            try:
                unsafe_evidence = auto_approval_anomalies(
                    transaction,
                    invoices,
                    match_result,
                    assigned_invoice_ids,
                    prior_transactions,
                    amount_tolerance=AMOUNT_TOLERANCE,
                    date_tolerance_days=DATE_TOLERANCE_DAYS,
                )
            except Exception:
                unsafe_evidence = []
                safety_check_failed = True
            is_clean_path = (
                isinstance(final_state, dict)
                and final_state.get("processing_status") == "ok"
                and final_state.get("decision") == AUTO_DECISION
                and final_state.get("reconciliation_path") == "clean"
            )
            if not is_clean_path or safety_check_failed or unsafe_evidence or match_result.anomalies:
                match_result = force_human_review(
                    match_result,
                    "The API blocked automatic approval because the graph did not pass every safety check.",
                    [item for item in unsafe_evidence if isinstance(item, Anomaly)],
                )

        match_result = apply_review_status(match_result)
        results.append(match_result)
        if match_result.invoice_id:
            assigned_invoice_ids.add(match_result.invoice_id)
        prior_transactions.append(transaction)

    return save_match_results(results)


@app.get("/transactions/flagged", response_model=list[MatchResult])
def get_flagged_transactions(
    _: AuthenticatedUser = Depends(require_roles(*READ_ROLES)),
) -> list[MatchResult]:
    return get_pending_review_results()


@app.get("/transactions/{transaction_id}/review-history", response_model=list[ReviewEvent])
def get_review_history(
    transaction_id: str,
    _: AuthenticatedUser = Depends(require_roles(*READ_ROLES)),
) -> list[ReviewEvent]:
    events = get_transaction_review_history(transaction_id)
    if events is None:
        raise HTTPException(status_code=404, detail="Reconciliation result not found.")
    return events


@app.get("/reconciliation-results", response_model=list[MatchResult])
def get_reconciliation_results(
    _: AuthenticatedUser = Depends(require_roles(*READ_ROLES)),
) -> list[MatchResult]:
    return get_all_match_results()


@app.post("/transactions/{transaction_id}/approve", response_model=MatchResult)
def approve_transaction(
    transaction_id: str,
    action: ReviewAction,
    user: AuthenticatedUser = Depends(require_roles(UserRole.reviewer)),
) -> MatchResult:
    return review_transaction(transaction_id, action, ReviewStatus.approved, user)


@app.post("/transactions/{transaction_id}/reject", response_model=MatchResult)
def reject_transaction(
    transaction_id: str,
    action: ReviewAction,
    user: AuthenticatedUser = Depends(require_roles(UserRole.reviewer)),
) -> MatchResult:
    return review_transaction(transaction_id, action, ReviewStatus.rejected, user)


def review_transaction(
    transaction_id: str,
    action: ReviewAction,
    decision: ReviewStatus,
    user: AuthenticatedUser,
) -> MatchResult:
    try:
        return record_review(
            transaction_id,
            decision,
            user.username,
            datetime.now(timezone.utc).isoformat(),
            action.reason,
            user.user_id,
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Reconciliation result not found.") from exc
    except ReviewConflictError as exc:
        raise HTTPException(
            status_code=409, detail="Reconciliation result is not pending review."
        ) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=500,
            detail="Review could not be recorded; no changes were saved.",
        ) from exc


def apply_review_status(match_result: MatchResult) -> MatchResult:
    existing_result = get_match_result(match_result.transaction_id)

    if (
        existing_result is not None
        and existing_result.review_status in {ReviewStatus.approved, ReviewStatus.rejected}
        and existing_result.reviewed_by != "system"
    ):
        return match_result.model_copy(
            update={
                "status": existing_result.status,
                "review_status": existing_result.review_status,
                "reviewed_at": existing_result.reviewed_at,
                "reviewed_by": existing_result.reviewed_by,
            }
        )

    if match_result.status in {MatchStatus.flagged, MatchStatus.unmatched}:
        return match_result.model_copy(
            update={
                "review_status": ReviewStatus.pending_review,
                "reviewed_at": None,
                "reviewed_by": None,
            }
        )

    return match_result.model_copy(
        update={
            "review_status": ReviewStatus.approved,
            "reviewed_at": datetime.now(timezone.utc),
            "reviewed_by": "system",
        }
    )
