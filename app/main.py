"""FastAPI service: webhook -> dedupe -> classify -> validate -> route.

Run locally:      uvicorn app.main:app --reload
Dashboard:        http://127.0.0.1:8000/
OpenAPI docs:     http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import store
from .classifier import ClassifierError, classify, provider_name
from .models import TicketIn, TriageResult, ValidationFlag, utcnow
from .samples import FAULTS, SAMPLES
from .validation import (
    AUTO_ROUTE_THRESHOLD,
    LOW_CONFIDENCE_FLOOR,
    decide_route,
    needs_repair_pass,
    validate_and_repair,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    store.connect()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Support Ticket Triage",
    version="1.0.0",
    description=(
        "Webhook-driven ticket classification with schema-validated LLM output, "
        "confidence-based routing, duplicate protection, and an error log."
    ),
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------
async def triage(ticket: TicketIn, fault: str | None = None) -> TriageResult:
    started = time.perf_counter()
    fp = store.fingerprint(ticket)
    is_new, ticket_id = store.claim(fp)

    # --- 1. duplicate protection -------------------------------------------
    if not is_new:
        dup_id = store.record_duplicate(ticket, fp, ticket_id)
        return TriageResult(
            ticket_id=dup_id, fingerprint=fp, status="duplicate",
            received_at=utcnow(), duplicate_of=ticket_id,
            provider=provider_name(),
        )

    # --- 2. classify (with retries inside the client) -----------------------
    try:
        raw = await classify(ticket, fault=fault)
    except ClassifierError as exc:
        store.log_error("classify", str(exc), ticket_id=ticket_id,
                        kind=exc.kind, attempts=exc.attempts,
                        payload={"subject": ticket.subject[:120]})
        # Let the sender retry this exact ticket rather than silently swallowing it.
        store.release(fp)
        result = TriageResult(
            ticket_id=ticket_id, fingerprint=fp, status="failed", received_at=utcnow(),
            provider=provider_name(), attempts=exc.attempts, error=str(exc),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        store.save(ticket, result)
        return result

    attempts = raw.attempts
    if raw.data is None:
        store.log_error("parse", "model response was not JSON", ticket_id=ticket_id,
                        kind="unparseable", attempts=attempts,
                        payload={"raw": raw.raw_text[:500]})

    # --- 3. validate and repair --------------------------------------------
    classification, flags = validate_and_repair(raw.data, ticket)

    hint = needs_repair_pass(flags)
    if hint:
        store.log_error("validate", f"structural problems: {hint}", ticket_id=ticket_id,
                        kind="schema_violation", attempts=attempts,
                        payload=raw.data or raw.raw_text[:500])
        try:
            retry = await classify(ticket, repair_hint=hint, fault=fault)
            attempts += retry.attempts
            retry_cls, retry_flags = validate_and_repair(retry.data, ticket)
            if len(retry_flags) < len(flags):
                # Take the repaired classification, but keep the first attempt's
                # flags as the audit trail - the reviewer needs to see what the
                # model got wrong, not just the version that passed.
                classification = retry_cls
                flags = flags + retry_flags + [
                    ValidationFlag(
                        field="*",
                        problem=f"first attempt failed validation ({hint})",
                        action="repair pass succeeded; ticket still sent to review",
                        severity="structural",
                    )
                ]
        except ClassifierError as exc:
            store.log_error("repair", str(exc), ticket_id=ticket_id,
                            kind=exc.kind, attempts=exc.attempts)

    # --- 4. route -----------------------------------------------------------
    routing = decide_route(classification, flags)

    result = TriageResult(
        ticket_id=ticket_id, fingerprint=fp, status="processed", received_at=utcnow(),
        classification=classification, validation_flags=flags, routing=routing,
        provider=raw.provider, model=raw.model, attempts=attempts,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    store.save(ticket, result)
    return result


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "provider": provider_name(),
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini") if provider_name() == "openai"
                 else "mock-keyword-v1",
        "auto_route_threshold": AUTO_ROUTE_THRESHOLD,
        "low_confidence_floor": LOW_CONFIDENCE_FLOOR,
        "dedupe_ttl_hours": store.DEDUPE_TTL_HOURS,
    }


@app.post("/webhook/ticket")
async def webhook(
    request: Request,
    payload: dict = Body(...),
    x_fault: str | None = Header(default=None, alias="X-Demo-Fault"),
) -> JSONResponse:
    """Helpdesk webhook entry point.

    Returns 200 for a duplicate (the sender should stop retrying), 422 for a
    payload that cannot be parsed, 502 when the upstream model call failed.
    """
    try:
        ticket = TicketIn.model_validate(payload)
    except ValidationError as exc:
        store.log_error("ingest", "webhook payload failed schema validation",
                        kind="bad_request", payload=exc.errors()[:5])
        raise HTTPException(status_code=422, detail=exc.errors()[:5])

    fault = x_fault or payload.get("_demo_fault")
    result = await triage(ticket, fault=None if fault in (None, "none") else fault)
    code = 200 if result.status != "failed" else 502
    return JSONResponse(status_code=code, content=json.loads(result.model_dump_json()))


@app.get("/api/tickets")
def api_tickets(action: str | None = None, limit: int = 200) -> dict:
    return {"tickets": store.list_tickets(action=action, limit=limit)}


@app.get("/api/tickets/{ticket_id}")
def api_ticket(ticket_id: str) -> dict:
    row = store.get(ticket_id)
    if not row:
        raise HTTPException(404, "unknown ticket")
    return row


@app.get("/api/review/queue")
def api_queue() -> dict:
    return {"queue": store.list_tickets(review_state="pending")}


@app.post("/api/review/{ticket_id}")
def api_resolve(ticket_id: str, payload: dict = Body(...)) -> dict:
    decision = payload.get("decision", "approved")
    if decision not in {"approved", "reassigned", "rejected"}:
        raise HTTPException(400, "decision must be approved, reassigned or rejected")
    row = store.resolve_review(ticket_id, decision=decision,
                               team=payload.get("team"), note=payload.get("note", ""))
    if row is None:
        raise HTTPException(404, "unknown ticket")
    return {"ok": True, "ticket_id": ticket_id, "decision": decision,
            "destination": row["destination"]}


@app.get("/api/stats")
def api_stats() -> dict:
    return store.stats()


@app.get("/api/errors")
def api_errors(limit: int = 100) -> dict:
    return {"errors": store.list_errors(limit)}


@app.get("/api/samples")
def api_samples() -> dict:
    return {"samples": SAMPLES, "faults": FAULTS}


@app.post("/api/reset")
def api_reset() -> dict:
    store.reset()
    return {"ok": True}


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
