"""A demo corpus chosen to exercise every branch of the pipeline.

Each entry notes which path it is meant to prove, so a reviewer can check the
behaviour rather than take the dashboard's word for it.
"""
from __future__ import annotations

SAMPLES: list[dict] = [
    {
        "id": "s1",
        "proves": "clean auto-route",
        "subject": "Charged twice for the October invoice",
        "body": "Hi, invoice INV-40921 was charged to my card on Oct 2 and then again "
                "on Oct 3 for the same amount ($149.00). The subscription is monthly so "
                "this is a duplicate payment. Please refund the second charge.",
        "requester_email": "dana.k@northline.co",
        "channel": "email",
    },
    {
        "id": "s2",
        "proves": "clean auto-route, different team",
        "subject": "Locked out after enabling 2FA",
        "body": "I enabled 2FA yesterday and the authenticator app is now showing codes "
                "that are rejected at sign in. I cannot log in on any device. The reset "
                "link goes to an old email I no longer control.",
        "requester_email": "m.orozco@fieldtrust.io",
        "channel": "web",
    },
    {
        "id": "s3",
        "proves": "policy override - critical urgency is never hands-off",
        "subject": "API returning 500 on every request since 09:40 UTC",
        "body": "Production is down. Every call to /v2/orders returns a 500 with an "
                "empty body. Started at 09:40 UTC, all users affected, our checkout is "
                "offline. Stack trace attached.",
        "requester_email": "sre@brightpath.dev",
        "channel": "api",
    },
    {
        "id": "s4",
        "proves": "policy override - abuse_security bypasses automation",
        "subject": "Phishing email pretending to be your billing team",
        "body": "We received an email from billing-support@your-company-invoices.net "
                "asking staff to re-enter card details. Looks like a phishing attempt "
                "impersonating you. Two people may have clicked it.",
        "requester_email": "it@calderworks.com",
        "channel": "email",
    },
    {
        "id": "s5",
        "proves": "low confidence - vague ticket goes to review",
        "subject": "it doesn't work",
        "body": "hi it stopped working can you fix",
        "requester_email": "jr@example.com",
        "channel": "chat",
    },
    {
        "id": "s6",
        "proves": "low confidence - multi-issue ticket resists a single label",
        "subject": "A few things",
        "body": "First, I was billed the annual rate instead of monthly. Also the export "
                "button throws an error when I pick CSV? And also is there any way to "
                "get a second seat added, or do I need a new plan? Sorry for the pile-up.",
        "requester_email": "priya@lanternhq.com",
        "channel": "email",
    },
    {
        "id": "s7",
        "proves": "urgency tracks business impact, not customer tone",
        "subject": "ABSOLUTELY UNACCEPTABLE!!!",
        "body": "This is the third time I am writing. My invoice shows $3.20 more than "
                "the quoted price. I want this fixed immediately, it is completely "
                "unacceptable and I am considering cancelling.",
        "requester_email": "g.holt@mailbox.net",
        "channel": "email",
    },
    {
        "id": "s8",
        "proves": "clean auto-route to logistics",
        "subject": "Tracking number hasn't moved in 6 days",
        "body": "Package TRK-88213 was marked shipped on the 4th and the courier "
                "tracking has not updated since. Delivery estimate has passed.",
        "requester_email": "l.svensson@nordvik.se",
        "channel": "web",
    },
    {
        "id": "s9",
        "proves": "borderline - 0.81 lands just under the 0.85 bar",
        "subject": "Feature request: bulk CSV export of the audit log",
        "body": "It would be great if you could add support for exporting the full audit "
                "log as CSV rather than page by page. Our compliance team pulls it "
                "quarterly and copies it by hand today.",
        "requester_email": "compliance@vestrahealth.org",
        "channel": "web",
    },
    {
        "id": "s10",
        "proves": "non-English ticket - honest low confidence beats a confident guess",
        "subject": "مشكلة في الدفع",
        "body": "لم أتمكن من إتمام عملية الدفع، تظهر رسالة خطأ عند الضغط على زر الدفع.",
        "requester_email": "a.saleh@example.ye",
        "channel": "email",
    },
    {
        "id": "s11",
        "proves": "duplicate protection - same ticket, reflowed whitespace and casing",
        "subject": "Charged   twice for the OCTOBER invoice",
        "body": "Hi,  invoice INV-40921 was charged to my card on Oct 2   and then again "
                "on Oct 3 for the same amount ($149.00).\nThe subscription is monthly so "
                "this is a duplicate payment.\n\nPlease refund the second charge.",
        "requester_email": "Dana.K@Northline.co",
        "channel": "email",
    },
    {
        "id": "s12",
        "proves": "general inquiry fallback",
        "subject": "Thanks for the help yesterday",
        "body": "Just wanted to say the agent who helped me on Tuesday was excellent. "
                "No action needed, no rush.",
        "requester_email": "tomas@bluecrest.io",
        "channel": "email",
    },
]

FAULTS: list[dict] = [
    {"id": "none", "label": "Normal response"},
    {"id": "missing_field", "label": "Missing required fields (urgency, team)"},
    {"id": "unknown_enum", "label": "Invented category + team not in the enum"},
    {"id": "bad_confidence", "label": "Confidence out of range (1.7)"},
    {"id": "wrong_team", "label": "High confidence, but wrong team for the category"},
    {"id": "empty_summary", "label": "Blank summary"},
    {"id": "not_json", "label": "Prose instead of JSON"},
    {"id": "api_error", "label": "Upstream API failure after all retries"},
]
