"""Typed contracts for the support-ticket triage pipeline.

Everything the LLM is allowed to return is described here once, and that same
description is used three ways:

1. compiled into the JSON schema sent to the OpenAI API (strict mode),
2. used to validate/repair whatever actually comes back,
3. used to type the API responses FastAPI serves.

One source of truth is what stops "the model returned a category we have never
heard of" from ever reaching the routing layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Controlled vocabularies
# --------------------------------------------------------------------------
class Category(str, Enum):
    BILLING = "billing"
    REFUND_CANCELLATION = "refund_cancellation"
    ACCOUNT_ACCESS = "account_access"
    TECHNICAL_BUG = "technical_bug"
    SHIPPING_DELIVERY = "shipping_delivery"
    FEATURE_REQUEST = "feature_request"
    ABUSE_SECURITY = "abuse_security"
    GENERAL_INQUIRY = "general_inquiry"


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Team(str, Enum):
    BILLING_OPS = "billing_ops"
    ACCOUNTS_IDENTITY = "accounts_identity"
    TIER1_SUPPORT = "tier1_support"
    TIER2_ENGINEERING = "tier2_engineering"
    LOGISTICS = "logistics"
    PRODUCT_FEEDBACK = "product_feedback"
    TRUST_SAFETY = "trust_safety"


#: The team a category is *allowed* to land in. The model proposes a team; this
#: table is the authority. A mismatch is repaired and flagged, never trusted.
CATEGORY_TEAM: dict[Category, Team] = {
    Category.BILLING: Team.BILLING_OPS,
    Category.REFUND_CANCELLATION: Team.BILLING_OPS,
    Category.ACCOUNT_ACCESS: Team.ACCOUNTS_IDENTITY,
    Category.TECHNICAL_BUG: Team.TIER2_ENGINEERING,
    Category.SHIPPING_DELIVERY: Team.LOGISTICS,
    Category.FEATURE_REQUEST: Team.PRODUCT_FEEDBACK,
    Category.ABUSE_SECURITY: Team.TRUST_SAFETY,
    Category.GENERAL_INQUIRY: Team.TIER1_SUPPORT,
}

#: First-response SLA in minutes, by urgency.
URGENCY_SLA_MINUTES: dict[Urgency, int] = {
    Urgency.CRITICAL: 15,
    Urgency.HIGH: 60,
    Urgency.MEDIUM: 480,
    Urgency.LOW: 1440,
}


# --------------------------------------------------------------------------
# Inbound webhook payload
# --------------------------------------------------------------------------
class TicketIn(BaseModel):
    """What the helpdesk webhook posts to us."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    external_id: str | None = Field(
        default=None,
        max_length=128,
        description="Helpdesk's own ticket id. When present it is the primary dedupe key.",
    )
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20_000)
    requester_email: str = Field(default="", max_length=320)
    channel: Literal["email", "web", "chat", "phone", "api"] = "email"
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# What the LLM must return
# --------------------------------------------------------------------------
class Classification(BaseModel):
    """The structured result the job posting describes."""

    model_config = ConfigDict(extra="ignore")

    category: Category
    urgency: Urgency
    summary: str = Field(min_length=1, max_length=300)
    recommended_team: Team
    confidence: float = Field(ge=0.0, le=1.0)


#: Strict JSON schema handed to the OpenAI API. Written out rather than derived
#: from `model_json_schema()` because strict structured outputs require
#: `additionalProperties: false` plus every property in `required`, and because
#: inlined enums read better in the request log.
CLASSIFICATION_JSON_SCHEMA: dict[str, Any] = {
    "name": "ticket_classification",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "urgency", "summary", "recommended_team", "confidence"],
        "properties": {
            "category": {"type": "string", "enum": [c.value for c in Category]},
            "urgency": {"type": "string", "enum": [u.value for u in Urgency]},
            "summary": {
                "type": "string",
                "description": "One sentence, max 300 characters, no pleasantries.",
            },
            "recommended_team": {"type": "string", "enum": [t.value for t in Team]},
            "confidence": {
                "type": "number",
                "description": "0.0-1.0. Your own certainty in the category and team.",
            },
        },
    },
}


# --------------------------------------------------------------------------
# Pipeline output
# --------------------------------------------------------------------------
class ValidationFlag(BaseModel):
    """One thing the validator had to fix.

    ``severity`` decides what happens next:

    * ``structural`` - the response was unusable as returned (unparseable, or a
      required field missing / outside the vocabulary). Worth spending a second
      API call on a repair pass.
    * ``repaired``   - deterministically fixable here (clamp, truncate, snap a
      near-miss enum, override a team using the routing table). No second call;
      the fix is not in question, only the model's reliability is.
    """

    field: str
    problem: str
    action: str
    severity: Literal["structural", "repaired"] = "repaired"


class RoutingDecision(BaseModel):
    action: Literal["auto_route", "manual_review", "reject"]
    destination: str
    reason: str
    sla_minutes: int
    policy_overrides: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    ticket_id: str
    fingerprint: str
    status: Literal["processed", "duplicate", "failed"]
    received_at: datetime
    classification: Classification | None = None
    validation_flags: list[ValidationFlag] = Field(default_factory=list)
    routing: RoutingDecision | None = None
    duplicate_of: str | None = None
    provider: str = "mock"
    model: str = ""
    attempts: int = 0
    latency_ms: int = 0
    error: str | None = None
