"""Mock CRM / enrichment / outreach / calendar APIs.

Behaviour per account comes from mock_behavior (deterministic). Each mock keeps a server-side LEDGER of applied effects
keyed by idempotency key, so tests can prove "exactly once" even after retries and uncertain outcomes.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field


class Timeout(Exception):
    pass


@dataclass
class ApiResult:
    status: int
    body: dict = field(default_factory=dict)
    retry_after: int | None = None


def _parity(key: str) -> bool:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % 2 == 0


class MockSystems:
    RETRY_AFTER = {"send": 30, "enrichment": 10, "calendar": 5, "crm": 5}

    def __init__(self, conn: sqlite3.Connection, behaviors: dict | None = None):
        self.conn = conn
        self.override = behaviors or {}          # {account_id: {system: behaviour}}
        self.attempts: dict[tuple, int] = {}
        self.ledger = {"send": {}, "crm": {}, "calendar": {}}
        self.calls: list[tuple] = []             # (system, op, key, status) for assertions and the demo

    # ---- behaviour lookup ---------------------------------------------------------------------------------
    def behaviour(self, account_id: str, system: str) -> str:
        if account_id in self.override and system in self.override[account_id]:
            return self.override[account_id][system]
        r = self.conn.execute("SELECT * FROM mock_behavior WHERE account_id=?", (account_id,)).fetchone()
        return (r[system] if r and r[system] else "ok")

    def _attempt(self, system, key) -> int:
        k = (system, key)
        self.attempts[k] = self.attempts.get(k, 0) + 1
        return self.attempts[k]

    def _end(self, system, op, key, res: ApiResult) -> ApiResult:
        self.calls.append((system, op, key, res.status))
        return res

    def _common_failures(self, system, op, key, b, n):
        """Failure modes shared by every system. Returns an ApiResult, raises Timeout, or None to continue."""
        ra = self.RETRY_AFTER[system]
        if b in ("transient_error_then_ok",) and n == 1:
            return self._end(system, op, key, ApiResult(503))
        if b in ("rate_limit_then_ok", "rate_limit_once") and n == 1:
            return self._end(system, op, key, ApiResult(429, retry_after=ra))
        if b in ("timeout_then_ok", "timeout_once") and n == 1:
            self.calls.append((system, op, key, "timeout"))
            raise Timeout()
        if b == "server_error_persistent":
            return self._end(system, op, key, ApiResult(500))
        return None

    # ---- outreach (single channel: email) ---------------------------------------------------------------------
    def send_email(self, account_id: str, key: str, payload: dict) -> ApiResult:
        b, n = self.behaviour(account_id, "send"), self._attempt("send", key)
        f = self._common_failures("send", "send_email", key, b, n)
        if f:
            return f
        if b == "hard_reject":
            return self._end("send", "send_email", key, ApiResult(422, {"error": "address_rejected"}))
        led = self.ledger["send"]
        if b == "uncertain_outcome" and n == 1:
            if _parity(key):
                led.setdefault(key, payload)       # it may or may not have been sent
            return self._end("send", "send_email", key, ApiResult(200, {"status": "unknown"}))
        if key in led:
            return self._end("send", "send_email", key, ApiResult(200, {"status": "duplicate_ignored"}))
        led[key] = payload
        return self._end("send", "send_email", key, ApiResult(200, {"status": "sent", "message_id": "msg_" + key[:10]}))

    # ---- CRM -------------------------------------------------------------------------------------------------
    def crm_write(self, account_id: str, key: str, payload: dict) -> ApiResult:
        b, n = self.behaviour(account_id, "crm"), self._attempt("crm", key)
        f = self._common_failures("crm", "write", key, b, n)
        if f:
            return f
        led = self.ledger["crm"]
        if b == "stale_version_conflict" and n == 1:
            return self._end("crm", "write", key, ApiResult(409, {"error": "stale_version"}))
        if b == "uncertain_outcome" and n == 1:
            if _parity(key):
                led.setdefault(key, payload)
            return self._end("crm", "write", key, ApiResult(200, {"status": "unknown"}))
        if key in led:
            return self._end("crm", "write", key, ApiResult(200, {"status": "duplicate_ignored"}))
        led[key] = payload
        return self._end("crm", "write", key, ApiResult(200, {"status": "written"}))

    # ---- calendar ---------------------------------------------------------------------------------------------
    def calendar_book(self, account_id: str, key: str, payload: dict) -> ApiResult:
        b, n = self.behaviour(account_id, "calendar"), self._attempt("calendar", key)
        led = self.ledger["calendar"]
        if b == "uncertain_outcome" and n == 1:
            if _parity(key):
                led.setdefault(key, payload)       # the booking may exist even though we time out
            self.calls.append(("calendar", "book", key, "timeout"))
            raise Timeout()
        f = self._common_failures("calendar", "book", key, b, n)
        if f:
            return f
        if b == "slot_conflict":
            return self._end("calendar", "book", key, ApiResult(409, {"error": "slot_taken"}))
        if key in led:
            return self._end("calendar", "book", key, ApiResult(200, {"status": "duplicate_ignored"}))
        led[key] = payload
        return self._end("calendar", "book", key, ApiResult(200, {"status": "booked"}))

    # ---- enrichment -------------------------------------------------------------------------------------------
    def enrich(self, account_id: str, key: str) -> ApiResult:
        b, n = self.behaviour(account_id, "enrichment"), self._attempt("enrichment", key)
        f = self._common_failures("enrichment", "enrich", key, b, n)
        if f:
            return f
        if b == "malformed_response":
            return self._end("enrichment", "enrich", key, ApiResult(200, {"_raw": "<<<not json>>>"}))
        if b == "uncertain_outcome":
            return self._end("enrichment", "enrich", key, ApiResult(202, {"status": "pending", "job_id": "job_" + key[:8]}))
        r = self.conn.execute("SELECT response FROM mock_enrichment WHERE account_id=?", (account_id,)).fetchone()
        body = json.loads(r["response"]) if r else {"status": "ok", "firmographics": {}, "contacts": [], "warnings": []}
        return self._end("enrichment", "enrich", key, ApiResult(200, body))

    def enrich_poll(self, account_id: str, job_id: str) -> ApiResult:
        self.calls.append(("enrichment", "poll", job_id, 202))
        return ApiResult(202, {"status": "pending", "job_id": job_id})      # the uncertain job never resolves

    # ---- reconciliation: ask the provider what it actually did ------------------------------------------------------
    def lookup(self, system: str, key: str) -> ApiResult:
        applied = key in self.ledger[system]
        self.calls.append((system, "lookup", key, 200))
        return ApiResult(200, {"applied": applied})
