"""Validation, field repair, and routing policy.

Strict structured outputs make malformed responses rare, not impossible: the
schema can be bypassed by a fallback model, a proxy, a truncated stream, or a
provider that silently downgrades. Everything downstream therefore treats the
LLM response as untrusted input.

Repair order matters. We fix what is mechanically fixable (clamp a number,
truncate a string, snap a near-miss enum), record every fix as a flag, and let
the routing layer decide that a repaired ticket is no longer eligible for
hands-off automation.
"""
from __future__ import annotations

import difflib
import re

from .models import (
    CATEGORY_TEAM,
    URGENCY_SLA_MINUTES,
    Category,
    Classification,
    RoutingDecision,
    Team,
    TicketIn,
    Urgency,
    ValidationFlag,
)

#: Confidence at or above which a ticket may be routed with no human in the loop.
AUTO_ROUTE_THRESHOLD = 0.85
#: Below this, the classification is treated as a hint rather than an answer.
LOW_CONFIDENCE_FLOOR = 0.60


def _snap_enum(value: object, enum_cls, cutoff: float = 0.72):
    """Map a near-miss string onto an enum member, or return None."""
    if isinstance(value, enum_cls):
        return value
    if not isinstance(value, str):
        return None
    raw = value.strip().lower().replace(" ", "_").replace("-", "_")
    for member in enum_cls:
        if raw == member.value:
            return member
    match = difflib.get_close_matches(raw, [m.value for m in enum_cls], n=1, cutoff=cutoff)
    return enum_cls(match[0]) if match else None


def validate_and_repair(
    data: dict | None, ticket: TicketIn
) -> tuple[Classification, list[ValidationFlag]]:
    """Coerce a raw model response into a valid :class:`Classification`.

    Always returns an object - an unusable response degrades to a low-confidence
    ``general_inquiry`` that the router will send to manual review.
    """
    flags: list[ValidationFlag] = []
    data = dict(data or {})
    if not data:
        flags.append(ValidationFlag(field="*", problem="empty or unparseable response",
                                    action="fell back to general_inquiry @ confidence 0",
                                    severity="structural"))

    # --- category -----------------------------------------------------------
    category = _snap_enum(data.get("category"), Category)
    if category is None:
        raw = data.get("category")
        flags.append(ValidationFlag(
            field="category",
            problem=f"missing or unrecognised value {raw!r}",
            action="defaulted to general_inquiry",
            severity="structural",
        ))
        category = Category.GENERAL_INQUIRY
    elif str(data.get("category", "")).strip().lower() != category.value:
        flags.append(ValidationFlag(
            field="category",
            problem=f"non-canonical value {data.get('category')!r}",
            action=f"snapped to {category.value}",
            severity="repaired",
        ))

    # --- urgency ------------------------------------------------------------
    urgency = _snap_enum(data.get("urgency"), Urgency)
    if urgency is None:
        flags.append(ValidationFlag(
            field="urgency",
            problem=f"missing or unrecognised value {data.get('urgency')!r}",
            action="defaulted to medium",
            severity="structural",
        ))
        urgency = Urgency.MEDIUM

    # --- recommended_team ---------------------------------------------------
    expected_team = CATEGORY_TEAM[category]
    team = _snap_enum(data.get("recommended_team"), Team)
    if team is None:
        flags.append(ValidationFlag(
            field="recommended_team",
            problem=f"missing or unrecognised value {data.get('recommended_team')!r}",
            action=f"derived {expected_team.value} from category",
            severity="structural",
        ))
        team = expected_team
    elif team is not expected_team:
        # Deterministically fixable, so no second API call - but a model that
        # picks the wrong owner has told us something about this ticket.
        flags.append(ValidationFlag(
            field="recommended_team",
            problem=f"{team.value} is not the owner of category {category.value}",
            action=f"overridden to {expected_team.value} by the routing table",
            severity="repaired",
        ))
        team = expected_team

    # --- summary ------------------------------------------------------------
    summary = str(data.get("summary") or "").strip()
    summary = re.sub(r"\s+", " ", summary)
    if not summary:
        summary = (ticket.subject or ticket.body)[:240].strip()
        flags.append(ValidationFlag(field="summary", problem="empty",
                                    action="fell back to the ticket subject",
                                    severity="repaired"))
    elif len(summary) > 300:
        summary = summary[:297].rstrip() + "..."
        flags.append(ValidationFlag(field="summary", problem="exceeded 300 characters",
                                    action="truncated", severity="repaired"))

    # --- confidence ---------------------------------------------------------
    raw_conf = data.get("confidence")
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        flags.append(ValidationFlag(field="confidence",
                                    problem=f"missing or non-numeric {raw_conf!r}",
                                    action="set to 0.0 so the ticket goes to review",
                                    severity="structural"))
        confidence = 0.0
    else:
        if 10.0 <= confidence <= 100.0:
            # A model that answered on a 0-100 scale. Rescale, but never trust it:
            # a score we had to reinterpret must not clear the auto-route bar.
            # The >= 10 guard matters: 1.7 is a botched 0-1 answer, not 1.7%.
            flags.append(ValidationFlag(field="confidence",
                                        problem=f"{confidence} out of range 0-1",
                                        action="rescaled from percent and capped at 0.5",
                                        severity="repaired"))
            confidence = min(confidence / 100.0, 0.5)
        elif confidence < 0.0 or confidence > 1.0:
            flags.append(ValidationFlag(field="confidence",
                                        problem=f"{confidence} out of range 0-1",
                                        action="clamped", severity="repaired"))
            confidence = max(0.0, min(1.0, confidence))

    if not data:
        confidence = 0.0

    return (
        Classification(
            category=category,
            urgency=urgency,
            summary=summary,
            recommended_team=team,
            confidence=round(confidence, 3),
        ),
        flags,
    )


def needs_repair_pass(flags: list[ValidationFlag]) -> str | None:
    """Return a hint for a second LLM attempt, or None if repair is not worth a call.

    Only structural problems justify spending another API call. A clamped
    confidence, a truncated summary, or a team the routing table already
    corrected is fixed locally and costs nothing.
    """
    structural = [f for f in flags if f.severity == "structural"]
    if not structural:
        return None
    return "; ".join(f"{f.field}: {f.problem}" for f in structural)


def decide_route(
    cls: Classification, flags: list[ValidationFlag], *, dedupe_note: str | None = None
) -> RoutingDecision:
    """Apply the automation policy. Confidence alone never decides."""
    overrides: list[str] = []
    sla = URGENCY_SLA_MINUTES[cls.urgency]

    # Policy 1: a response the validator had to correct on a decision-bearing
    # field loses automation, even if the correction was trivial. A truncated
    # summary is cosmetic and does not.
    decision_fields = {"*", "category", "recommended_team", "confidence", "urgency"}
    structural = [
        f for f in flags
        if f.severity == "structural" or f.field in decision_fields
    ]

    # Policy 2: these never go out unattended regardless of how sure the model is.
    if cls.category is Category.ABUSE_SECURITY:
        overrides.append("abuse_security always gets human eyes")
    if cls.urgency is Urgency.CRITICAL:
        overrides.append("critical urgency requires an on-call acknowledgement")

    if structural:
        return RoutingDecision(
            action="manual_review",
            destination="manual_review_queue",
            reason=f"validator had to correct {len(structural)} decision field(s): "
                   + ", ".join(dict.fromkeys(f.field for f in structural)),
            sla_minutes=sla,
            policy_overrides=overrides,
        )

    if overrides:
        return RoutingDecision(
            action="manual_review",
            destination="manual_review_queue",
            reason=f"policy override -> {cls.recommended_team.value} on approval",
            sla_minutes=sla,
            policy_overrides=overrides,
        )

    if cls.confidence >= AUTO_ROUTE_THRESHOLD:
        return RoutingDecision(
            action="auto_route",
            destination=cls.recommended_team.value,
            reason=f"confidence {cls.confidence:.2f} >= {AUTO_ROUTE_THRESHOLD} "
                   "and all fields validated clean",
            sla_minutes=sla,
            policy_overrides=overrides,
        )

    if cls.confidence >= LOW_CONFIDENCE_FLOOR:
        return RoutingDecision(
            action="manual_review",
            destination="manual_review_queue",
            reason=f"confidence {cls.confidence:.2f} is below the {AUTO_ROUTE_THRESHOLD} "
                   f"auto-route bar; suggested team {cls.recommended_team.value}",
            sla_minutes=sla,
            policy_overrides=overrides,
        )

    return RoutingDecision(
        action="manual_review",
        destination="manual_review_queue",
        reason=f"confidence {cls.confidence:.2f} below the {LOW_CONFIDENCE_FLOOR} floor; "
               "treated as unclassified",
        sla_minutes=sla,
        policy_overrides=overrides + ["triage from scratch"],
    )
