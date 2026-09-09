"""End-to-end checks. No network, no API key required.

    python -m pytest tests -q      (or: python tests/test_pipeline.py)
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

import asyncio  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import store  # noqa: E402
from app.main import app, triage  # noqa: E402
from app.models import Category, Team, TicketIn  # noqa: E402
from app.samples import SAMPLES  # noqa: E402
from app.validation import validate_and_repair  # noqa: E402

client = TestClient(app)


def run(coro):
    return asyncio.run(coro)


def ticket(**kw) -> TicketIn:
    base = {"subject": "Charged twice for invoice INV-1", "body": "I was billed twice "
            "for the same monthly subscription payment on my card.", "requester_email": "a@b.c"}
    return TicketIn(**{**base, **kw})


def test_clean_ticket_auto_routes():
    store.reset()
    r = run(triage(ticket()))
    assert r.status == "processed"
    assert r.classification.category is Category.BILLING
    assert r.classification.recommended_team is Team.BILLING_OPS
    assert r.routing.action == "auto_route", r.routing.reason
    assert r.validation_flags == []


def test_duplicate_is_blocked_not_reprocessed():
    store.reset()
    first = run(triage(ticket()))
    second = run(triage(ticket(subject="Charged   TWICE for invoice inv-1  ")))
    assert second.status == "duplicate"
    assert second.duplicate_of == first.ticket_id
    assert store.stats()["duplicates_blocked"] == 1


def test_external_id_is_the_primary_dedupe_key():
    store.reset()
    a = run(triage(ticket(external_id="HD-77", body="totally different text here about login")))
    b = run(triage(ticket(external_id="HD-77", body="another different body entirely")))
    assert b.status == "duplicate" and b.duplicate_of == a.ticket_id


def test_missing_fields_are_repaired_and_sent_to_review():
    store.reset()
    r = run(triage(ticket(), fault="missing_field"))
    assert r.status == "processed"
    assert r.classification.recommended_team is Team.BILLING_OPS      # derived
    assert r.routing.action == "manual_review"
    assert any(f.field == "recommended_team" for f in r.validation_flags)


def test_invented_enum_never_reaches_routing():
    store.reset()
    r = run(triage(ticket(), fault="unknown_enum"))
    assert r.classification.category in set(Category)
    assert r.classification.recommended_team in set(Team)
    assert r.routing.action == "manual_review"


def test_out_of_range_confidence_is_clamped():
    cls, flags = validate_and_repair(
        {"category": "billing", "urgency": "high", "summary": "x",
         "recommended_team": "billing_ops", "confidence": 1.7}, ticket())
    # 1.7 is a botched 0-1 answer, not 1.7 percent - it must clamp, not rescale.
    assert cls.confidence == 1.0
    assert any(f.field == "confidence" and f.action == "clamped" for f in flags)


def test_percent_scale_confidence_is_rescaled_and_capped():
    cls, flags = validate_and_repair(
        {"category": "billing", "urgency": "high", "summary": "x",
         "recommended_team": "billing_ops", "confidence": 92}, ticket())
    assert cls.confidence <= 0.5, "a rescaled score must not clear the auto-route bar"


def test_wrong_team_is_overridden_by_the_routing_table():
    store.reset()
    r = run(triage(ticket(), fault="wrong_team"))
    assert r.classification.recommended_team is Team.BILLING_OPS
    assert r.routing.action == "manual_review"
    assert any("routing table" in f.action for f in r.validation_flags)


def test_unparseable_response_degrades_safely():
    store.reset()
    r = run(triage(ticket(), fault="not_json"))
    assert r.status == "processed"
    assert r.routing.action == "manual_review"
    assert store.list_errors(), "an unparseable response must be logged"


def test_api_failure_is_logged_and_fingerprint_released():
    store.reset()
    r = run(triage(ticket(), fault="api_error"))
    assert r.status == "failed" and r.error
    errs = store.list_errors()
    assert errs and errs[0]["stage"] == "classify"
    # the sender must be able to retry the same ticket after a failure
    retry = run(triage(ticket()))
    assert retry.status == "processed"


def test_abuse_and_critical_never_auto_route():
    store.reset()
    for s in SAMPLES:
        if s["id"] in {"s3", "s4"}:
            r = run(triage(TicketIn(**{k: v for k, v in s.items()
                                       if k in {"subject", "body", "requester_email", "channel"}})))
            assert r.routing.action == "manual_review", s["id"]
            assert r.routing.policy_overrides


def test_webhook_rejects_a_bad_payload():
    store.reset()
    resp = client.post("/webhook/ticket", json={"body": "no subject field"})
    assert resp.status_code == 422


def test_webhook_roundtrip_and_review_resolution():
    store.reset()
    resp = client.post("/webhook/ticket", json={
        "subject": "it doesn't work", "body": "hi it stopped working can you fix",
        "requester_email": "x@y.z"})
    assert resp.status_code == 200
    tid = resp.json()["ticket_id"]
    assert resp.json()["routing"]["action"] == "manual_review"

    queue = client.get("/api/review/queue").json()["queue"]
    assert any(t["id"] == tid for t in queue)

    out = client.post(f"/api/review/{tid}",
                      json={"decision": "reassigned", "team": "tier2_engineering",
                            "note": "backend export bug"}).json()
    assert out["destination"] == "tier2_engineering"
    assert not any(t["id"] == tid for t in client.get("/api/review/queue").json()["queue"])


def test_stats_are_consistent():
    store.reset()
    for s in SAMPLES:
        client.post("/webhook/ticket", json={k: v for k, v in s.items()
                                             if k in {"subject", "body", "requester_email",
                                                      "channel"}})
    st = client.get("/api/stats").json()
    assert st["processed"] + st["duplicates_blocked"] + st["failed"] == st["total_received"]
    assert st["duplicates_blocked"] == 1, "sample s11 is a duplicate of s1"
    assert st["auto_routed"] + st["manual_review"] == st["processed"]
    assert 0.0 <= st["automation_rate"] <= 1.0
    assert st["auto_routed"] > 0 and st["manual_review"] > 0, "demo must show both paths"


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
