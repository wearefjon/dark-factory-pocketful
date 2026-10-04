"""Pocketful stage 1 — payments and settlements HTTP service.

Single-process FastAPI application. All mutable state lives in memory behind one
re-entrant lock, so every money movement and every idempotency claim is atomic with
respect to every other request. State is intentionally ephemeral: the spec only
requires ``/_test/export`` / ``/_test/import`` to move it, not a container restart.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import re
import secrets
import threading
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from passlib.context import CryptContext
from starlette.exceptions import HTTPException as StarletteHTTPException

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

MAX_AMOUNT = 1_000_000_000
MAX_NOTE = 200
MAX_IDEM_KEY = 255
MAX_TRANSFERS = 32
HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
DIGITS_RE = re.compile(r"^[0-9]+$")
REQUEST_STATUSES = ("pending", "paid", "declined", "cancelled")
VISIBILITIES = ("public", "private")

_pwd = CryptContext(schemes=["bcrypt"], bcrypt__rounds=10, deprecated="auto")

# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "error") -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def fail(status: int, code: str, message: str = "error") -> None:
    raise ApiError(status, code, message)


class Utf8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical(body: Any) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_amount(value: Any) -> int:
    """Exact integer minor units. Booleans and strings are never numbers."""
    if isinstance(value, bool):
        fail(422, "validation_failed", "amount must be an integer")
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            fail(422, "validation_failed", "amount must be an integer")
        n = int(value)
    else:
        fail(422, "validation_failed", "amount must be an integer")
    if n < 1 or n > MAX_AMOUNT:
        fail(422, "validation_failed", "amount out of range")
    return n


def req_str(body: dict, name: str) -> str:
    if name not in body:
        fail(422, "validation_failed", f"{name} is required")
    value = body[name]
    if not isinstance(value, str):
        fail(400, "malformed_request", f"{name} must be a string")
    return value


def parse_note(body: dict) -> str:
    if "note" not in body:
        return ""
    value = body["note"]
    if not isinstance(value, str):
        fail(422, "validation_failed", "note must be a string")
    if len(value) > MAX_NOTE:
        fail(422, "validation_failed", "note too long")
    return value


def parse_visibility(body: dict) -> str:
    if "visibility" not in body:
        return "public"
    value = body["visibility"]
    if value not in VISIBILITIES:
        fail(422, "validation_failed", "visibility must be public or private")
    return value


async def read_json_object(request: Request, *, allow_empty: bool = False) -> dict:
    raw = await request.body()
    if not raw or not raw.strip():
        if allow_empty:
            return {}
        fail(400, "malformed_request", "empty body")
    try:
        data = json.loads(raw)
    except Exception:
        fail(400, "malformed_request", "body is not valid JSON")
    if not isinstance(data, dict):
        fail(400, "malformed_request", "body must be a JSON object")
    return data


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


class State:
    def __init__(self) -> None:
        self.currency = "EUR"
        self.minor_units = 2
        self.users: dict[str, dict] = {}
        self.emails: dict[str, str] = {}
        self.handles: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self.payments: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.splits: dict[str, dict] = {}
        self.settlements: dict[str, dict] = {}
        self.operators: set[str] = set()
        self.idem: dict[tuple, dict] = {}
        self.seq = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


LOCK = threading.RLock()
STATE = State()


def resolve_handle(handle: str) -> dict:
    uid = STATE.handles.get(handle)
    if uid is None:
        fail(404, "not_found", "no user with that handle")
    return STATE.users[uid]


def user_by_token(request: Request) -> dict:
    header = request.headers.get("authorization")
    if not header:
        fail(401, "unauthenticated", "missing bearer token")
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        fail(401, "unauthenticated", "malformed bearer token")
    uid = STATE.tokens.get(parts[1].strip())
    if uid is None:
        fail(401, "unauthenticated", "unknown bearer token")
    return STATE.users[uid]


def idem_key(request: Request) -> str:
    key = request.headers.get("idempotency-key")
    if key is None or key == "":
        fail(400, "missing_idempotency_key", "Idempotency-Key header is required")
    if len(key) > MAX_IDEM_KEY:
        fail(422, "validation_failed", "Idempotency-Key is too long")
    return key


def idem_resolve(uid: str, path: str, key: str, body: dict) -> dict | None:
    record = STATE.idem.get((uid, path, key))
    if record is None:
        return None
    if record["body"] == canonical(body):
        return record["response"]
    fail(409, "idempotency_key_reuse", "key already used with a different body")


def idem_store(uid: str, path: str, key: str, body: dict, response: dict) -> None:
    STATE.idem[(uid, path, key)] = {"body": canonical(body), "response": response}


# --------------------------------------------------------------------------- #
# JSON views
# --------------------------------------------------------------------------- #


def _handle_of(uid: str | None) -> str | None:
    user = STATE.users.get(uid) if uid is not None else None
    return user["handle"] if user else uid


def payment_json(p: dict) -> dict:
    return {
        "payment_id": p["payment_id"],
        "from_user_id": p["from_user_id"],
        "from_handle": _handle_of(p["from_user_id"]),
        "to_user_id": p["to_user_id"],
        "to_handle": _handle_of(p["to_user_id"]),
        "amount": p["amount"],
        "currency": STATE.currency,
        "note": p["note"],
        "visibility": p["visibility"],
        "request_id": p["request_id"],
        "settlement_id": p.get("settlement_id"),
        "created_at": p["created_at"],
    }


def request_json(r: dict) -> dict:
    return {
        "request_id": r["request_id"],
        "requester_id": r["requester_id"],
        "requester_handle": _handle_of(r["requester_id"]),
        "payer_id": r["payer_id"],
        "payer_handle": _handle_of(r["payer_id"]),
        "amount": r["amount"],
        "currency": STATE.currency,
        "note": r["note"],
        "status": r["status"],
        "payment_id": r["payment_id"],
        "created_at": r["created_at"],
    }


def make_payment(frm: dict, to: dict, amount: int, note: str, visibility: str,
                 *, request_id: str | None = None,
                 settlement_id: str | None = None,
                 created_at: str | None = None) -> dict:
    pid = new_id("p")
    record = {
        "payment_id": pid,
        "from_user_id": frm["id"],
        "to_user_id": to["id"],
        "amount": amount,
        "note": note,
        "visibility": visibility,
        "request_id": request_id,
        "settlement_id": settlement_id,
        "created_at": created_at or now_iso(),
        "seq": STATE.next_seq(),
    }
    STATE.payments[pid] = record
    return record


def make_request(requester: dict, payer: dict, amount: int, note: str) -> dict:
    rid = new_id("rq")
    record = {
        "request_id": rid,
        "requester_id": requester["id"],
        "payer_id": payer["id"],
        "amount": amount,
        "note": note,
        "status": "pending",
        "payment_id": None,
        "created_at": now_iso(),
        "seq": STATE.next_seq(),
    }
    STATE.requests[rid] = record
    return record


def page(items: list, limit: int, offset: int) -> tuple[list, bool]:
    total = len(items)
    window = items[offset:offset + limit]
    return window, offset + len(window) < total


# --------------------------------------------------------------------------- #
# fixture / export / import
# --------------------------------------------------------------------------- #


def state_from_fixture(fixture: dict) -> State:
    if not isinstance(fixture, dict):
        fail(422, "validation_failed", "fixture must be an object")
    currency = fixture.get("currency", "EUR")
    minor_units = fixture.get("minor_units", 2)
    if not isinstance(currency, str) or minor_units not in (0, 2, 3):
        fail(422, "validation_failed", "unsupported currency configuration")
    users_in = fixture.get("users")
    if not isinstance(users_in, list):
        fail(422, "validation_failed", "users must be a list")

    # Validate every balance before touching anything (reset changes nothing on error).
    for u in users_in:
        if not isinstance(u, dict) or not isinstance(u.get("balance"), int) \
                or isinstance(u.get("balance"), bool):
            fail(422, "validation_failed", "each user needs an integer balance")
        if u["balance"] < 0:
            fail(422, "validation_failed", "balance must not be negative")

    state = State()
    state.currency = currency
    state.minor_units = minor_units

    seeded_at = now_iso()
    for u in users_in:
        uid = u.get("id")
        handle = u.get("handle")
        email = u.get("email")
        password = u.get("password", "")
        if not isinstance(uid, str) or not isinstance(handle, str) \
                or not isinstance(email, str) or not isinstance(password, str):
            fail(422, "validation_failed", "malformed user")
        state.users[uid] = {
            "id": uid,
            "email": email,
            "handle": handle,
            "display_name": u.get("display_name", handle),
            "password_hash": _pwd.hash(password),
            "balance": int(u["balance"]),
        }
        state.emails[email] = uid
        state.handles[handle] = uid

    for p in fixture.get("payments") or []:
        if not isinstance(p, dict):
            fail(422, "validation_failed", "malformed payment")
        pid = p.get("id")
        if not isinstance(pid, str):
            fail(422, "validation_failed", "malformed payment id")
        state.payments[pid] = {
            "payment_id": pid,
            "from_user_id": p.get("from_user_id"),
            "to_user_id": p.get("to_user_id"),
            "amount": p.get("amount", 0),
            "note": p.get("note", ""),
            "visibility": p.get("visibility", "public"),
            "request_id": p.get("request_id"),
            "settlement_id": p.get("settlement_id"),
            "created_at": p.get("created_at") or seeded_at,
            "seq": state.next_seq(),
        }

    for r in fixture.get("requests") or []:
        if not isinstance(r, dict):
            fail(422, "validation_failed", "malformed request")
        rid = r.get("id")
        if not isinstance(rid, str):
            fail(422, "validation_failed", "malformed request id")
        state.requests[rid] = {
            "request_id": rid,
            "requester_id": r.get("requester_id"),
            "payer_id": r.get("payer_id"),
            "amount": r.get("amount", 0),
            "note": r.get("note", ""),
            "status": r.get("status", "pending"),
            "payment_id": r.get("payment_id"),
            "created_at": r.get("created_at") or seeded_at,
            "seq": state.next_seq(),
        }

    for op in fixture.get("settlement_operator_ids") or []:
        if isinstance(op, str):
            state.operators.add(op)

    return state


def state_to_export(state: State) -> dict:
    return {
        "currency": state.currency,
        "minor_units": state.minor_units,
        "users": list(state.users.values()),
        "tokens": dict(state.tokens),
        "payments": list(state.payments.values()),
        "requests": list(state.requests.values()),
        "splits": list(state.splits.values()),
        "settlements": list(state.settlements.values()),
        "settlement_operator_ids": sorted(state.operators),
        "idem": [
            {"user_id": k[0], "path": k[1], "key": k[2],
             "body": v["body"], "response": v["response"]}
            for k, v in state.idem.items()
        ],
        "seq": state.seq,
    }


def state_from_export(state: dict) -> State:
    if not isinstance(state, dict):
        fail(422, "validation_failed", "state must be an object")
    for required in ("users", "tokens", "payments", "requests", "currency", "minor_units"):
        if required not in state:
            fail(422, "validation_failed", f"state is missing {required}")
    if not isinstance(state["users"], list) or not isinstance(state["tokens"], dict) \
            or not isinstance(state["payments"], list) or not isinstance(state["requests"], list):
        fail(422, "validation_failed", "state is malformed")

    rebuilt = State()
    rebuilt.currency = state["currency"]
    rebuilt.minor_units = state["minor_units"]
    try:
        for u in state["users"]:
            rebuilt.users[u["id"]] = dict(u)
            rebuilt.emails[u["email"]] = u["id"]
            rebuilt.handles[u["handle"]] = u["id"]
        rebuilt.tokens = {str(k): str(v) for k, v in state["tokens"].items()}
        for p in state["payments"]:
            rebuilt.payments[p["payment_id"]] = dict(p)
        for r in state["requests"]:
            rebuilt.requests[r["request_id"]] = dict(r)
        for s in state.get("splits") or []:
            rebuilt.splits[s["split_id"]] = dict(s)
        for s in state.get("settlements") or []:
            rebuilt.settlements[s["settlement_id"]] = dict(s)
        rebuilt.operators = set(state.get("settlement_operator_ids") or [])
        for entry in state.get("idem") or []:
            rebuilt.idem[(entry["user_id"], entry["path"], entry["key"])] = {
                "body": entry["body"], "response": entry["response"]}
        rebuilt.seq = int(state.get("seq", 0))
    except (KeyError, TypeError, ValueError):
        fail(422, "validation_failed", "state is malformed")
    return rebuilt


# --------------------------------------------------------------------------- #
# application
# --------------------------------------------------------------------------- #

app = FastAPI(default_response_class=Utf8JSONResponse)


@app.exception_handler(ApiError)
async def _api_error(_request: Request, exc: ApiError) -> Utf8JSONResponse:
    return Utf8JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, _exc: RequestValidationError) -> Utf8JSONResponse:
    return Utf8JSONResponse(
        {"error": {"code": "malformed_request", "message": "invalid request"}},
        status_code=400)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException) -> Utf8JSONResponse:
    code = "not_found" if exc.status_code == 404 else "malformed_request"
    return Utf8JSONResponse(
        {"error": {"code": code, "message": "http error"}}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def _unhandled(_request: Request, _exc: Exception) -> Utf8JSONResponse:
    return Utf8JSONResponse(
        {"error": {"code": "internal_error", "message": "unexpected error"}},
        status_code=500)


# ---- health and test control -------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/_test/reset")
async def reset(request: Request) -> Response:
    raw = await request.body()
    try:
        fixture = json.loads(raw)
    except Exception:
        fail(400, "malformed_request", "body is not valid JSON")
    with LOCK:
        new_state = state_from_fixture(fixture)
        global STATE
        STATE = new_state
    return Response(status_code=204)


@app.get("/_test/export")
async def export() -> dict:
    with LOCK:
        state = copy.deepcopy(state_to_export(STATE))
    return {"track": "pocketful", "format_version": 1, "state": state}


@app.post("/_test/import")
async def import_state(request: Request) -> Response:
    raw = await request.body()
    try:
        body = json.loads(raw)
    except Exception:
        fail(400, "malformed_request", "body is not valid JSON")
    if not isinstance(body, dict):
        fail(422, "validation_failed", "import body must be an object")
    if body.get("track") != "pocketful":
        fail(422, "validation_failed", "wrong track")
    if body.get("format_version") != 1:
        fail(422, "validation_failed", "wrong format_version")
    if "state" not in body:
        fail(422, "validation_failed", "missing state")
    with LOCK:
        rebuilt = state_from_export(body["state"])
        global STATE
        STATE = rebuilt
    return Response(status_code=204)


# ---- auth --------------------------------------------------------------------


@app.post("/auth/signup", status_code=201)
async def signup(request: Request) -> dict:
    body = await read_json_object(request)
    email = req_str(body, "email")
    password = req_str(body, "password")
    display_name = req_str(body, "display_name")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        fail(422, "validation_failed", "email must be local@domain")
    if len(password) < 8:
        fail(422, "validation_failed", "password too short")
    handle = re.sub(r"[^a-z0-9_]", "_", email.split("@", 1)[0].lower())[:20]
    with LOCK:
        if email in STATE.emails:
            fail(409, "email_taken", "email already registered")
        if handle in STATE.handles:
            fail(409, "handle_taken", "handle already taken")
        uid = new_id("u")
        STATE.users[uid] = {
            "id": uid,
            "email": email,
            "handle": handle,
            "display_name": display_name,
            "password_hash": _pwd.hash(password),
            "balance": 0,
        }
        STATE.emails[email] = uid
        STATE.handles[handle] = uid
        token = secrets.token_urlsafe(32)
        STATE.tokens[token] = uid
    return {"user_id": uid, "display_name": display_name, "token": token}


@app.post("/auth/login")
async def login(request: Request) -> dict:
    body = await read_json_object(request)
    email = req_str(body, "email")
    password = req_str(body, "password")
    with LOCK:
        uid = STATE.emails.get(email)
        if uid is None:
            fail(401, "unauthenticated", "unknown email or wrong password")
        user = STATE.users[uid]
        if not _pwd.verify(password, user["password_hash"]):
            fail(401, "unauthenticated", "unknown email or wrong password")
        token = secrets.token_urlsafe(32)
        STATE.tokens[token] = uid
    return {"user_id": uid, "display_name": user["display_name"], "token": token}


# ---- wallet ------------------------------------------------------------------


@app.get("/me")
async def me(request: Request) -> dict:
    user = user_by_token(request)
    with LOCK:
        return {
            "user_id": user["id"],
            "display_name": user["display_name"],
            "handle": user["handle"],
            "balance": user["balance"],
            "currency": STATE.currency,
            "minor_units": STATE.minor_units,
        }


# ---- payments ----------------------------------------------------------------


@app.post("/payments", status_code=201)
async def post_payment(request: Request) -> Utf8JSONResponse:
    body = await read_json_object(request)
    user = user_by_token(request)
    key = idem_key(request)
    path = request.url.path
    with LOCK:
        replay = idem_resolve(user["id"], path, key, body)
        if replay is not None:
            return Utf8JSONResponse(replay, status_code=200)

        to_handle = req_str(body, "to_handle")
        if "amount" not in body:
            fail(422, "validation_failed", "amount is required")
        amount = parse_amount(body["amount"])
        note = parse_note(body)
        visibility = parse_visibility(body)

        if to_handle == user["handle"]:
            fail(422, "self_payment", "cannot pay yourself")
        recipient = resolve_handle(to_handle)
        if user["balance"] < amount:
            fail(409, "insufficient_funds", "balance is below amount")
        user["balance"] -= amount
        recipient["balance"] += amount
        payment = make_payment(user, recipient, amount, note, visibility)
        result = payment_json(payment)
        idem_store(user["id"], path, key, body, result)
    return Utf8JSONResponse(result, status_code=201)


# ---- requests ----------------------------------------------------------------


@app.post("/requests", status_code=201)
async def post_request(request: Request) -> Utf8JSONResponse:
    body = await read_json_object(request)
    user = user_by_token(request)
    key = idem_key(request)
    path = request.url.path
    with LOCK:
        replay = idem_resolve(user["id"], path, key, body)
        if replay is not None:
            return Utf8JSONResponse(replay, status_code=200)

        payer_handle = req_str(body, "payer_handle")
        if "amount" not in body:
            fail(422, "validation_failed", "amount is required")
        amount = parse_amount(body["amount"])
        note = parse_note(body)

        if payer_handle == user["handle"]:
            fail(422, "self_request", "cannot request from yourself")
        payer = resolve_handle(payer_handle)
        req = make_request(user, payer, amount, note)
        result = request_json(req)
        idem_store(user["id"], path, key, body, result)
    return Utf8JSONResponse(result, status_code=201)


def _get_request_or_404(request_id: str) -> dict:
    req = STATE.requests.get(request_id)
    if req is None:
        fail(404, "not_found", "no such request")
    return req


@app.post("/requests/{request_id}/pay", status_code=201)
async def pay_request(request_id: str, request: Request) -> Utf8JSONResponse:
    body = await read_json_object(request, allow_empty=True)
    user = user_by_token(request)
    key = idem_key(request)
    path = request.url.path
    with LOCK:
        replay = idem_resolve(user["id"], path, key, body)
        if replay is not None:
            return Utf8JSONResponse(replay, status_code=200)

        visibility = parse_visibility(body)
        req = _get_request_or_404(request_id)
        if user["id"] != req["payer_id"]:
            fail(403, "forbidden", "only the payer may pay")
        if req["status"] != "pending":
            fail(409, "request_not_pending", "request is not pending")
        payer = STATE.users[req["payer_id"]]
        requester = STATE.users[req["requester_id"]]
        amount = req["amount"]
        if payer["balance"] < amount:
            fail(409, "insufficient_funds", "balance is below amount")
        payer["balance"] -= amount
        requester["balance"] += amount
        payment = make_payment(payer, requester, amount, req["note"], visibility,
                               request_id=req["request_id"])
        req["status"] = "paid"
        req["payment_id"] = payment["payment_id"]
        result = payment_json(payment)
        idem_store(user["id"], path, key, body, result)
    return Utf8JSONResponse(result, status_code=201)


@app.post("/requests/{request_id}/decline")
async def decline_request(request_id: str, request: Request) -> dict:
    user = user_by_token(request)
    with LOCK:
        req = _get_request_or_404(request_id)
        if user["id"] != req["payer_id"]:
            fail(403, "forbidden", "only the payer may decline")
        if req["status"] == "declined":
            return request_json(req)
        if req["status"] != "pending":
            fail(409, "request_not_pending", "request is not pending")
        req["status"] = "declined"
        return request_json(req)


@app.post("/requests/{request_id}/cancel")
async def cancel_request(request_id: str, request: Request) -> dict:
    user = user_by_token(request)
    with LOCK:
        req = _get_request_or_404(request_id)
        if user["id"] != req["requester_id"]:
            fail(403, "forbidden", "only the requester may cancel")
        if req["status"] == "cancelled":
            return request_json(req)
        if req["status"] != "pending":
            fail(409, "request_not_pending", "request is not pending")
        req["status"] = "cancelled"
        return request_json(req)


def parse_paging(request: Request) -> tuple[int, int]:
    limit, offset = 50, 0
    raw = request.query_params.get("limit")
    if raw is not None:
        if not DIGITS_RE.fullmatch(raw):
            fail(422, "validation_failed", "limit must be a decimal integer")
        limit = int(raw)
        if limit < 1 or limit > 200:
            fail(422, "validation_failed", "limit out of range")
    raw = request.query_params.get("offset")
    if raw is not None:
        if not DIGITS_RE.fullmatch(raw):
            fail(422, "validation_failed", "offset must be a decimal integer")
        offset = int(raw)
    return limit, offset


@app.get("/requests")
async def list_requests(request: Request) -> dict:
    user = user_by_token(request)
    direction = request.query_params.get("direction")
    if direction is not None and direction not in ("incoming", "outgoing"):
        fail(422, "validation_failed", "unknown direction")
    status = request.query_params.get("status")
    if status is not None and status not in REQUEST_STATUSES:
        fail(422, "validation_failed", "unknown status")
    limit, offset = parse_paging(request)
    with LOCK:
        mine = [r for r in STATE.requests.values()
                if r["requester_id"] == user["id"] or r["payer_id"] == user["id"]]
        if direction == "incoming":
            mine = [r for r in mine if r["payer_id"] == user["id"]]
        elif direction == "outgoing":
            mine = [r for r in mine if r["requester_id"] == user["id"]]
        if status is not None:
            mine = [r for r in mine if r["status"] == status]
        mine.sort(key=lambda r: (r["created_at"], r["seq"]), reverse=True)
        window, has_more = page(mine, limit, offset)
        return {"requests": [request_json(r) for r in window], "has_more": has_more}


# ---- splits ------------------------------------------------------------------


def equal_split(amount: int, n: int) -> list[int]:
    base = amount // n
    remainder = amount - base * n
    return [base + (1 if i < remainder else 0) for i in range(n)]


@app.post("/splits", status_code=201)
async def post_split(request: Request) -> Utf8JSONResponse:
    body = await read_json_object(request)
    user = user_by_token(request)
    key = idem_key(request)
    path = request.url.path
    with LOCK:
        replay = idem_resolve(user["id"], path, key, body)
        if replay is not None:
            return Utf8JSONResponse(replay, status_code=200)

        if "amount" not in body:
            fail(422, "validation_failed", "amount is required")
        amount = parse_amount(body["amount"])
        note = parse_note(body)
        handles = body.get("participant_handles")
        if not isinstance(handles, list) or not handles:
            fail(422, "validation_failed", "participant_handles must be a non-empty list")
        if any(not isinstance(h, str) for h in handles):
            fail(422, "validation_failed", "handles must be strings")
        if len(set(handles)) != len(handles):
            fail(422, "validation_failed", "duplicate handle")
        participants = [resolve_handle(h) for h in handles]

        shares = equal_split(amount, len(participants))
        split_id = new_id("sp")
        created_at = now_iso()
        share_list = [{"handle": p["handle"], "amount": s}
                      for p, s in zip(participants, shares)]
        created_requests = []
        for participant, share in zip(participants, shares):
            if participant["id"] == user["id"]:
                continue
            req = make_request(user, participant, share, note)
            created_requests.append(req)

        STATE.splits[split_id] = {
            "split_id": split_id,
            "amount": amount,
            "note": note,
            "created_at": created_at,
        }
        result = {
            "split_id": split_id,
            "amount": amount,
            "currency": STATE.currency,
            "note": note,
            "shares": share_list,
            "requests": [request_json(r) for r in created_requests],
            "created_at": created_at,
        }
        idem_store(user["id"], path, key, body, result)
    return Utf8JSONResponse(result, status_code=201)


# ---- activity ----------------------------------------------------------------


@app.get("/activity")
async def activity(request: Request) -> dict:
    user = user_by_token(request)
    limit, offset = parse_paging(request)
    with LOCK:
        visible = [
            p for p in STATE.payments.values()
            if p["visibility"] == "public"
            or p["from_user_id"] == user["id"]
            or p["to_user_id"] == user["id"]
        ]
        visible.sort(key=lambda p: (p["created_at"], p["seq"]), reverse=True)
        window, has_more = page(visible, limit, offset)
        return {"payments": [payment_json(p) for p in window], "has_more": has_more}


# ---- settlements -------------------------------------------------------------


def settlement_entry(body: dict) -> dict:
    if not isinstance(body, dict):
        fail(422, "validation_failed", "malformed transfer")
    if "amount" not in body:
        fail(422, "validation_failed", "amount is required")
    amount = parse_amount(body["amount"])
    from_handle = body.get("from_handle")
    to_handle = body.get("to_handle")
    if not isinstance(from_handle, str) or not isinstance(to_handle, str):
        fail(422, "validation_failed", "handle is required")
    frm = resolve_handle(from_handle)
    to = resolve_handle(to_handle)
    if frm["id"] == to["id"]:
        fail(422, "self_payment", "cannot transfer to yourself")
    note = parse_note(body)
    visibility = parse_visibility(body)
    return {"from": frm, "to": to, "amount": amount,
            "note": note, "visibility": visibility}


@app.post("/settlements", status_code=201)
async def post_settlement(request: Request) -> Utf8JSONResponse:
    body = await read_json_object(request)
    user = user_by_token(request)
    key = idem_key(request)
    path = request.url.path
    with LOCK:
        replay = idem_resolve(user["id"], path, key, body)
        if replay is not None:
            return Utf8JSONResponse(replay, status_code=200)

        if user["id"] not in STATE.operators:
            fail(403, "forbidden", "not a settlement operator")
        transfers = body.get("transfers")
        if not isinstance(transfers, list) or not (1 <= len(transfers) <= MAX_TRANSFERS):
            fail(422, "validation_failed", "transfers must contain 1 to 32 entries")
        entries = [settlement_entry(t) for t in transfers]

        deltas: dict[str, int] = {}
        for entry in entries:
            deltas[entry["from"]["id"]] = deltas.get(entry["from"]["id"], 0) - entry["amount"]
            deltas[entry["to"]["id"]] = deltas.get(entry["to"]["id"], 0) + entry["amount"]
        for uid, delta in deltas.items():
            if STATE.users[uid]["balance"] + delta < 0:
                fail(409, "insufficient_funds", "collective funds insufficient")

        settlement_id = new_id("st")
        committed_at = now_iso()
        payments = []
        for entry in entries:
            entry["from"]["balance"] -= entry["amount"]
            entry["to"]["balance"] += entry["amount"]
            p = make_payment(entry["from"], entry["to"], entry["amount"],
                             entry["note"], entry["visibility"],
                             settlement_id=settlement_id, created_at=committed_at)
            payments.append(p)
        STATE.settlements[settlement_id] = {
            "settlement_id": settlement_id,
            "committed_at": committed_at,
            "payment_ids": [p["payment_id"] for p in payments],
        }
        result = {
            "settlement_id": settlement_id,
            "committed_at": committed_at,
            "payments": [payment_json(p) for p in payments],
        }
        idem_store(user["id"], path, key, body, result)
    return Utf8JSONResponse(result, status_code=201)
