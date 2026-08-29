"""Classify an inbound reply into one of a fixed set of categories.

This is deliberately a rule-based classifier, not an LLM call. Two reasons:

1. Auditability. "Why did the agent decide this reply meant X" has to be
   answerable with a line number, not a model weight, for anything that
   touches money and a real customer relationship.
2. The category set is small and closed (see below), and the pack's 20
   samples cover the space well enough that hand-written rules generalize
   fine. An LLM classifier would be the right call the moment free-text
   nuance mattered more than catching a fixed set of known patterns -- and
   is exactly what I'd swap in first if this went to production (see
   NOTES.md).

Ordering matters: rules are checked top to bottom and the first match wins,
so more specific / higher-severity signals are listed first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Classification:
    category: str
    confidence: str  # "high" | "medium" | "low"
    reason: str


_RULES: list[tuple[str, re.Pattern, str]] = [
    ("bounce", re.compile(r"undeliverable|mailer-daemon|does not exist|550 5\.\d", re.I),
     "delivery-failure signal in sender/subject/body"),

    ("auto_reply", re.compile(r"out of office|automatic reply|limited access to email", re.I),
     "out-of-office / autoresponder language"),

    ("ticket_ack", re.compile(r"ticket has been created|ticket #|do not reply to this address", re.I),
     "automated helpdesk/portal acknowledgement"),

    ("legal_escalation", re.compile(r"legal counsel|our lawyers|attorney", re.I),
     "customer has routed the matter to legal"),

    ("hostile", re.compile(r"take (the )?(whole )?account elsewhere|escalate.{0,15}(this|it)|"
                            r"fourth email|do not reply to this with another|management",
                            re.I),
     "language indicating frustration/relationship risk, independent of payment content"),

    ("contact_change", re.compile(r"left the business|no longer (works|with)|"
                                   r"(send|direct).{0,25}(future|all).{0,25}(correspondence|invoices).{0,20}to",
                                   re.I),
     "customer-side contact is stale, new recipient supplied"),

    # Checked BEFORE "dispute": both categories can contain "doesn't match",
    # but wrong_reference is specifically about the invoice/PO *identifier*
    # being unrecognized, which is a data problem, not a billing argument.
    ("wrong_reference", re.compile(r"doesn'?t match anything|never received this invoice|"
                                    r"wrong (rate|po|invoice)|auto-rejects|resend it with|reissuing", re.I),
     "customer disputes the invoice/PO identifier itself, not the underlying debt"),

    ("dispute", re.compile(r"hours billed|billed for.{0,20}match|rate on line|"
                            r"do not accept the amounts|can'?t approve|holding payment|"
                            r"not approve|\bdispute\b", re.I),
     "customer disputes the amount, hours, or rate on the invoice"),

    ("payment_plan_request", re.compile(r"payment plan|installment|instal?ments|"
                                         r"50%.{0,20}(this|next)|balance on the|across the next", re.I),
     "customer is proposing partial/staged payment"),

    ("paid_claim", re.compile(r"already (been )?paid|was paid on|nothing outstanding|"
                               r"we'?ve already settled|settled this", re.I),
     "customer asserts payment already made -- must be reconciled, not trusted"),

    # Checked BEFORE remittance_advice: both can mention "payment run", but
    # this is a promise about a future date, not a document listing
    # invoice/amount line items to reconcile against.
    ("confirmation_scheduled", re.compile(r"scheduled in our payment run on|you'?ll have it|"
                                           r"confirmed.{0,20}(scheduled|payment)", re.I),
     "customer confirms a specific future payment date"),

    ("remittance_advice", re.compile(r"remittance advice|total transmitted", re.I),
     "structured remittance notice listing invoice(s) and amounts"),

    ("document_request", re.compile(r"statement of account|reconcile at our end|"
                                     r"forward (it|the (original )?invoice)|can'?t process anything without",
                                     re.I),
     "customer needs a document before they will action payment"),
]

_AMBIGUOUS_RE = re.compile(r"^\s*\??\s*$")


def classify(subject: str, body: str) -> Classification:
    text = f"{subject}\n{body}"
    for category, pattern, reason in _RULES:
        if pattern.search(text):
            return Classification(category=category, confidence="high", reason=reason)

    if _AMBIGUOUS_RE.match(body.strip()):
        return Classification(category="ambiguous", confidence="low",
                               reason="reply body carries no interpretable content (e.g. a bare '?')")

    if len(body.strip()) < 25:
        return Classification(category="ambiguous", confidence="low",
                               reason="reply too short to classify with confidence")

    return Classification(category="acknowledged_uncategorized", confidence="low",
                           reason="reply text did not match a known pattern -- treat as needing human read")
