"""SQLite persistence: tickets, the dedupe ledger, and the error log.

SQLite is deliberate for a service this size - it gives an atomic
``INSERT OR IGNORE`` on a unique fingerprint, which is what makes duplicate
protection correct under concurrent webhook deliveries rather than merely
likely. Swapping in Postgres means changing the DSN and the placeholder style;
the claim logic is identical.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import TicketIn, TriageResult, utcnow

DB_PATH = os.getenv("DB_PATH", "triage.db")
DEDUPE_TTL_HOURS = float(os.getenv("DEDUPE_TTL_HOURS", "24"))

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS dedupe (
    fingerprint TEXT PRIMARY KEY,
    ticket_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id            TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,
    external_id   TEXT,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    requester_email TEXT,
    channel       TEXT,
    received_at   TEXT NOT NULL,
    status        TEXT NOT NULL,
    category      TEXT,
    urgency       TEXT,
    team          TEXT,
    summary       TEXT,
    confidence    REAL,
    action        TEXT,
    destination   TEXT,
    reason        TEXT,
    sla_minutes   INTEGER,
    flags_json    TEXT NOT NULL DEFAULT '[]',
    overrides_json TEXT NOT NULL DEFAULT '[]',
    provider      TEXT,
    model         TEXT,
    attempts      INTEGER DEFAULT 0,
    latency_ms    INTEGER DEFAULT 0,
    error         TEXT,
    review_state  TEXT,
    reviewer_note TEXT,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_action ON tickets(action);
CREATE INDEX IF NOT EXISTS idx_tickets_received ON tickets(received_at DESC);
CREATE TABLE IF NOT EXISTS errors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    stage     TEXT NOT NULL,
    ticket_id TEXT,
    kind      TEXT,
    message   TEXT,
    attempts  INTEGER,
    payload   TEXT
);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


# --------------------------------------------------------------------------
# Fingerprinting / dedupe
# --------------------------------------------------------------------------
_WS = re.compile(r"\s+")
_QUOTED = re.compile(r"^\s*(>|on .+ wrote:)", re.IGNORECASE | re.MULTILINE)


def fingerprint(ticket: TicketIn) -> str:
    """Stable content hash.

    ``external_id`` wins when the helpdesk supplies one - two different bodies
    under one helpdesk id are an edit, not a new ticket. Otherwise we hash the
    normalised subject + body + requester so that a retried webhook, a
    whitespace-reflowed resend, or the same complaint posted twice collapses to
    one fingerprint.
    """
    if ticket.external_id:
        return "eid:" + hashlib.sha256(ticket.external_id.strip().lower().encode()).hexdigest()[:32]

    body = _QUOTED.sub(" ", ticket.body)          # drop quoted reply chains
    norm = _WS.sub(" ", f"{ticket.subject} {body}").strip().lower()
    norm = re.sub(r"[^a-z0-9\u0600-\u06ff ]+", "", norm)   # keep Arabic ranges too
    key = f"{ticket.requester_email.strip().lower()}|{norm}"
    return "sha:" + hashlib.sha256(key.encode()).hexdigest()[:32]


def claim(fp: str) -> tuple[bool, str]:
    """Atomically claim a fingerprint.

    Returns ``(is_new, ticket_id)``. When ``is_new`` is False the returned id is
    the ticket that already owns this fingerprint. Two concurrent webhook
    deliveries race on the primary key, and exactly one wins.
    """
    conn = connect()
    now = utcnow()
    cutoff = (now - timedelta(hours=DEDUPE_TTL_HOURS)).isoformat()
    new_id = "tkt_" + uuid.uuid4().hex[:12]
    with _lock:
        conn.execute("DELETE FROM dedupe WHERE created_at < ?", (cutoff,))
        cur = conn.execute(
            "INSERT OR IGNORE INTO dedupe (fingerprint, ticket_id, created_at) VALUES (?,?,?)",
            (fp, new_id, now.isoformat()),
        )
        conn.commit()
        if cur.rowcount == 1:
            return True, new_id
        row = conn.execute("SELECT ticket_id FROM dedupe WHERE fingerprint=?", (fp,)).fetchone()
        return False, (row["ticket_id"] if row else new_id)


def release(fp: str) -> None:
    """Give a fingerprint back so a failed ticket can be retried by the sender."""
    conn = connect()
    with _lock:
        conn.execute("DELETE FROM dedupe WHERE fingerprint=?", (fp,))
        conn.commit()


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
def save(ticket: TicketIn, result: TriageResult) -> None:
    conn = connect()
    cls = result.classification
    routing = result.routing
    review_state = None
    if routing and routing.action == "manual_review":
        review_state = "pending"
    with _lock:
        conn.execute(
            """INSERT OR REPLACE INTO tickets
               (id, fingerprint, external_id, subject, body, requester_email, channel,
                received_at, status, category, urgency, team, summary, confidence,
                action, destination, reason, sla_minutes, flags_json, overrides_json,
                provider, model, attempts, latency_ms, error, review_state,
                reviewer_note, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result.ticket_id, result.fingerprint, ticket.external_id, ticket.subject,
                ticket.body, ticket.requester_email, ticket.channel,
                result.received_at.isoformat(), result.status,
                cls.category.value if cls else None,
                cls.urgency.value if cls else None,
                cls.recommended_team.value if cls else None,
                cls.summary if cls else None,
                cls.confidence if cls else None,
                routing.action if routing else None,
                routing.destination if routing else None,
                routing.reason if routing else None,
                routing.sla_minutes if routing else None,
                json.dumps([f.model_dump() for f in result.validation_flags]),
                json.dumps(routing.policy_overrides if routing else []),
                result.provider, result.model, result.attempts, result.latency_ms,
                result.error, review_state, None, None,
            ),
        )
        conn.commit()


def log_error(stage: str, message: str, *, ticket_id: str | None = None,
              kind: str = "error", attempts: int = 0, payload: Any = None) -> dict:
    """Append to the error log and emit one structured line to stdout."""
    conn = connect()
    row = {
        "ts": utcnow().isoformat(), "stage": stage, "ticket_id": ticket_id,
        "kind": kind, "message": str(message)[:2000], "attempts": attempts,
        "payload": json.dumps(payload)[:2000] if payload is not None else None,
    }
    with _lock:
        conn.execute(
            "INSERT INTO errors (ts, stage, ticket_id, kind, message, attempts, payload)"
            " VALUES (:ts,:stage,:ticket_id,:kind,:message,:attempts,:payload)", row,
        )
        conn.commit()
    print(json.dumps({"level": "error", **row}), flush=True)
    return row


def resolve_review(ticket_id: str, *, decision: str, team: str | None,
                   note: str = "") -> dict | None:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None:
            return None
        dest = team or row["team"] or "tier1_support"
        conn.execute(
            """UPDATE tickets SET review_state=?, reviewer_note=?, resolved_at=?,
                   destination=?, team=? WHERE id=?""",
            (decision, note, utcnow().isoformat(), dest, dest, ticket_id),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["validation_flags"] = json.loads(d.pop("flags_json") or "[]")
    d["policy_overrides"] = json.loads(d.pop("overrides_json") or "[]")
    d["body"] = (d.get("body") or "")[:2000]
    return d


def get(ticket_id: str) -> dict | None:
    row = connect().execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_tickets(action: str | None = None, review_state: str | None = None,
                 limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM tickets WHERE 1=1"
    params: list[Any] = []
    if action:
        sql += " AND action=?"
        params.append(action)
    if review_state:
        sql += " AND review_state=?"
        params.append(review_state)
    sql += " ORDER BY received_at DESC LIMIT ?"
    params.append(limit)
    return [_row_to_dict(r) for r in connect().execute(sql, params).fetchall()]


def list_errors(limit: int = 100) -> list[dict]:
    return [dict(r) for r in connect().execute(
        "SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def stats() -> dict:
    conn = connect()
    q = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]  # noqa: E731
    # `tickets` holds one row per delivery, including the copies rejected as
    # duplicates, so every bucket below is a disjoint slice of that table.
    received = q("SELECT COUNT(*) FROM tickets")
    processed = q("SELECT COUNT(*) FROM tickets WHERE status='processed'")
    auto = q("SELECT COUNT(*) FROM tickets WHERE action='auto_route'")
    review = q("SELECT COUNT(*) FROM tickets WHERE action='manual_review'")
    pending = q("SELECT COUNT(*) FROM tickets WHERE review_state='pending'")
    failed = q("SELECT COUNT(*) FROM tickets WHERE status='failed'")
    dupes = q("SELECT COUNT(*) FROM tickets WHERE status='duplicate'")
    avg_conf = conn.execute(
        "SELECT AVG(confidence) FROM tickets WHERE confidence IS NOT NULL").fetchone()[0]
    avg_lat = conn.execute(
        "SELECT AVG(latency_ms) FROM tickets WHERE latency_ms > 0").fetchone()[0]
    by_cat = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) n FROM tickets WHERE category IS NOT NULL"
        " GROUP BY category ORDER BY n DESC").fetchall()}
    by_team = {r["team"]: r["n"] for r in conn.execute(
        "SELECT team, COUNT(*) n FROM tickets WHERE team IS NOT NULL"
        " GROUP BY team ORDER BY n DESC").fetchall()}
    by_urg = {r["urgency"]: r["n"] for r in conn.execute(
        "SELECT urgency, COUNT(*) n FROM tickets WHERE urgency IS NOT NULL"
        " GROUP BY urgency").fetchall()}
    classified = auto + review
    return {
        "total_received": received,
        "processed": processed,
        "duplicates_blocked": dupes,
        "auto_routed": auto,
        "manual_review": review,
        "pending_review": pending,
        "failed": failed,
        "automation_rate": round(auto / classified, 3) if classified else 0.0,
        "avg_confidence": round(avg_conf, 3) if avg_conf else 0.0,
        "avg_latency_ms": int(avg_lat) if avg_lat else 0,
        "errors_logged": q("SELECT COUNT(*) FROM errors"),
        "by_category": by_cat,
        "by_team": by_team,
        "by_urgency": by_urg,
    }


def record_duplicate(ticket: TicketIn, fp: str, original_id: str) -> str:
    """Persist the rejected copy so the dashboard can show what was blocked."""
    conn = connect()
    dup_id = "dup_" + uuid.uuid4().hex[:12]
    with _lock:
        conn.execute(
            """INSERT INTO tickets (id, fingerprint, external_id, subject, body,
                   requester_email, channel, received_at, status, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (dup_id, fp, ticket.external_id, ticket.subject, ticket.body,
             ticket.requester_email, ticket.channel, utcnow().isoformat(),
             "duplicate", f"identical fingerprint to {original_id}"),
        )
        conn.commit()
    return dup_id


def reset() -> None:
    conn = connect()
    with _lock:
        conn.executescript("DELETE FROM tickets; DELETE FROM dedupe; DELETE FROM errors;")
        conn.commit()
