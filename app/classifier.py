"""LLM classification with strict structured output, retries, and a repair pass.

Two providers:

* ``openai``  - used automatically when ``OPENAI_API_KEY`` is set. Calls
  chat/completions with ``response_format = json_schema`` in strict mode,
  temperature 0, and retries transient failures with exponential backoff.
* ``mock``    - deterministic keyword classifier used when no key is present,
  so the demo runs with zero setup and the test-suite runs offline. It can be
  told to emit deliberately broken output (``fault=``) to exercise the
  validation and review paths.

The public surface is one coroutine: :func:`classify`.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, field

import httpx

from .models import CATEGORY_TEAM, Category, TicketIn, Urgency

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

SYSTEM_PROMPT = """You triage inbound customer-support tickets.

Return ONLY the structured object described by the schema. Rules:
- category / urgency / recommended_team must be one of the enum values. Never invent one.
- summary: one sentence, under 300 characters, factual, no greetings, no advice.
- confidence: your honest certainty that BOTH the category and the team are right.
  Use the full range. Below 0.7 means a human should look at it. Do not inflate.
- Ambiguous, multi-issue, or non-English tickets should get a LOW confidence,
  not a confident guess.
- urgency reflects business impact, not the customer's tone. An angry email about
  a $3 discrepancy is not critical; a silent report of a production outage is."""


class ClassifierError(RuntimeError):
    """A call that failed after exhausting retries."""

    def __init__(self, message: str, *, kind: str, attempts: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts


@dataclass
class RawResult:
    data: dict | None
    provider: str
    model: str
    attempts: int
    latency_ms: int
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------
def _user_prompt(ticket: TicketIn, repair_hint: str | None = None) -> str:
    parts = [
        f"channel: {ticket.channel}",
        f"subject: {ticket.subject}",
        "body:",
        ticket.body[:6000],
    ]
    if repair_hint:
        parts += [
            "",
            "Your previous response was rejected by schema validation:",
            repair_hint,
            "Return a corrected object. Use only enum values listed in the schema.",
        ]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# OpenAI provider
# --------------------------------------------------------------------------
async def _classify_openai(ticket: TicketIn, repair_hint: str | None) -> RawResult:
    from .models import CLASSIFICATION_JSON_SCHEMA

    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(ticket, repair_hint)},
        ],
        "response_format": {"type": "json_schema", "json_schema": CLASSIFICATION_JSON_SCHEMA},
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    started = time.perf_counter()
    last_error = "unknown"
    last_kind = "unknown"
    notes: list[str] = []

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await client.post(
                    f"{OPENAI_BASE_URL}/chat/completions", json=payload, headers=headers
                )
                if resp.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    last_kind = "transient_http"
                    await _backoff(attempt, resp.headers.get("retry-after"))
                    notes.append(f"attempt {attempt}: {last_error}")
                    continue
                if resp.status_code >= 400:
                    # 401/403/404 will not fix themselves; fail fast.
                    raise ClassifierError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}",
                        kind="permanent_http",
                        attempts=attempt,
                    )

                body = resp.json()
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length":
                    last_error = "response truncated (finish_reason=length)"
                    last_kind = "truncated"
                    notes.append(f"attempt {attempt}: {last_error}")
                    await _backoff(attempt)
                    continue

                text = choice["message"]["content"] or ""
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    last_error = f"model returned non-JSON: {exc}"
                    last_kind = "unparseable"
                    notes.append(f"attempt {attempt}: {last_error}")
                    await _backoff(attempt)
                    continue

                return RawResult(
                    data=data,
                    provider="openai",
                    model=body.get("model", OPENAI_MODEL),
                    attempts=attempt,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    raw_text=text,
                    notes=notes,
                )

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_kind = "network"
                notes.append(f"attempt {attempt}: {last_error}")
                await _backoff(attempt)

    raise ClassifierError(
        f"OpenAI call failed after {MAX_ATTEMPTS} attempts - {last_error}",
        kind=last_kind,
        attempts=MAX_ATTEMPTS,
    )


async def _backoff(attempt: int, retry_after: str | None = None) -> None:
    if retry_after:
        try:
            await asyncio.sleep(min(float(retry_after), 10.0))
            return
        except ValueError:
            pass
    await asyncio.sleep(min(0.4 * (2 ** (attempt - 1)) + random.random() * 0.2, 8.0))


# --------------------------------------------------------------------------
# Mock provider - deterministic, offline, fault-injectable
# --------------------------------------------------------------------------
KEYWORDS: dict[Category, tuple[str, ...]] = {
    Category.BILLING: (
        "invoice", "charged", "charge", "billing", "payment", "card", "subscription",
        "price", "overcharg", "double bill", "receipt", "vat", "tax",
    ),
    Category.REFUND_CANCELLATION: (
        "refund", "cancel", "money back", "chargeback", "terminate my", "unsubscribe",
    ),
    Category.ACCOUNT_ACCESS: (
        "log in", "login", "sign in", "password", "locked out", "2fa", "mfa",
        "reset link", "can't access my account", "sso", "verification code",
    ),
    Category.TECHNICAL_BUG: (
        "error", "crash", "500", "bug", "broken", "not working", "exception",
        "timeout", "fails", "stack trace", "blank screen", "won't load", "outage",
    ),
    Category.SHIPPING_DELIVERY: (
        "shipment", "delivery", "tracking", "package", "parcel", "courier",
        "arrived", "shipped", "dispatch", "customs",
    ),
    Category.FEATURE_REQUEST: (
        "feature request", "would be great", "suggestion", "please add",
        "add support for", "roadmap", "wishlist", "it would help if",
    ),
    Category.ABUSE_SECURITY: (
        "phishing", "fraud", "hacked", "breach", "unauthorized", "suspicious login",
        "scam", "malware", "data leak", "impersonat",
    ),
}

CRITICAL_MARKERS = ("outage", "production down", "all users", "data loss", "breach",
                    "everyone is affected", "site is down", "cannot process any")
HIGH_MARKERS = ("urgent", "asap", "immediately", "blocked", "deadline", "escalate",
                "third time", "still waiting", "unacceptable")
LOW_MARKERS = ("no rush", "whenever you get a chance", "just curious", "question about",
               "low priority", "nice to have")


def _fnv1a(text: str) -> int:
    """32-bit FNV-1a over UTF-8 bytes. Mirrored byte-for-byte in the browser demo."""
    h = 2166136261
    for byte in text.encode("utf-8"):
        h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
    return h


def _score_categories(text: str) -> dict[Category, int]:
    return {cat: sum(1 for kw in kws if kw in text) for cat, kws in KEYWORDS.items()}


def _mock_classify(ticket: TicketIn, fault: str | None) -> RawResult:
    text = f"{ticket.subject}\n{ticket.body}".lower()
    scores = _score_categories(text)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0

    # Security signals win any tie. A phishing report that also mentions "card
    # details" must not be filed as billing - misrouting that class of ticket is
    # the expensive mistake, so it takes precedence over the raw keyword count.
    if scores[Category.ABUSE_SECURITY] > 0:
        top = Category.ABUSE_SECURITY
        top_score = scores[Category.ABUSE_SECURITY]
        runner_up_score = max(v for k, v in scores.items() if k is not top)

    if top_score == 0:
        category = Category.GENERAL_INQUIRY
        base = 0.42
    else:
        category = top
        base = 0.58 + min(top_score, 4) * 0.09
        if runner_up_score >= top_score:          # genuine ambiguity
            base -= 0.22
        elif runner_up_score > 0:
            base -= 0.07

    if any(m in text for m in CRITICAL_MARKERS) or category is Category.ABUSE_SECURITY:
        urgency = Urgency.CRITICAL
    elif any(m in text for m in HIGH_MARKERS):
        urgency = Urgency.HIGH
    elif any(m in text for m in LOW_MARKERS):
        urgency = Urgency.LOW
    else:
        urgency = Urgency.MEDIUM

    if len(ticket.body) < 60:                     # not much to go on
        base -= 0.12
    if text.count("?") >= 3 or " and also " in text:   # multi-issue ticket
        base -= 0.10

    # Deterministic per-ticket jitter so the demo looks alive but replays
    # identically. FNV-1a (rather than sha256) so the browser-side simulation in
    # static/index.html can reproduce the exact same numbers.
    jitter = (_fnv1a(text) % 1000) / 1000 * 0.08 - 0.04
    confidence = round(max(0.05, min(0.97, base + jitter)), 2)

    sentence = re.split(r"(?<=[.!?])\s", ticket.body.strip())[0]
    summary = (sentence if len(sentence) > 25 else ticket.body.strip())[:240]

    data: dict = {
        "category": category.value,
        "urgency": urgency.value,
        "summary": summary,
        "recommended_team": CATEGORY_TEAM[category].value,
        "confidence": confidence,
    }

    # ---- fault injection: the failure modes this project exists to survive ----
    if fault == "missing_field":
        data.pop("recommended_team", None)
        data.pop("urgency", None)
    elif fault == "bad_confidence":
        data["confidence"] = 1.7
    elif fault == "unknown_enum":
        data["category"] = "cancellation_and_billing_dispute"
        data["recommended_team"] = "the billing guys"
    elif fault == "wrong_team":
        data["recommended_team"] = "product_feedback"
        data["confidence"] = 0.93
    elif fault == "empty_summary":
        data["summary"] = "   "
    elif fault == "low_confidence":
        data["confidence"] = 0.41
    elif fault == "not_json":
        return RawResult(None, "mock", "mock-keyword-v1", 1, 3,
                         raw_text="Sure! Here's the classification: ```json {category: billing,",
                         notes=["model returned prose instead of JSON"])
    elif fault == "api_error":
        raise ClassifierError("simulated upstream 503 after 3 attempts",
                              kind="transient_http", attempts=MAX_ATTEMPTS)

    return RawResult(data, "mock", "mock-keyword-v1", 1, 3, raw_text=json.dumps(data))


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def provider_name() -> str:
    return "openai" if OPENAI_API_KEY else "mock"


async def classify(
    ticket: TicketIn, *, repair_hint: str | None = None, fault: str | None = None
) -> RawResult:
    """Classify one ticket. Raises :class:`ClassifierError` if the call cannot complete."""
    if OPENAI_API_KEY and not fault:
        return await _classify_openai(ticket, repair_hint)
    if fault and repair_hint:
        # A repair pass in mock mode always succeeds - that is the point of the demo:
        # show the pipeline recovering rather than replaying the same fault.
        fault = None
    return _mock_classify(ticket, fault)
