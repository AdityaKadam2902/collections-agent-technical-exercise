"""Renders the actual email body for a given tier + invoice.

Plain f-strings, not Jinja -- there are 7 tiers and none of them need loops
or conditionals inside the template. Adding a real templating engine for
this would be exactly the kind of unnecessary dependency the rest of this
project is trying to avoid.
"""
from __future__ import annotations


def render(tier_name: str, *, contact_name: str, invoice_id: str, amount,
           due_date: str, dpd: int, customer_name: str) -> str:
    amt = f"${amount:,.2f}"

    if tier_name == "pre_due_notice":
        return (
            f"Hi {contact_name},\n\n"
            f"Just a heads-up that invoice {invoice_id} ({amt}) for {customer_name} "
            f"is due on {due_date}. No action needed if it's already scheduled -- "
            f"flagging in case it slipped.\n\nThanks,\nAccounts Receivable"
        )

    if tier_name == "first_reminder":
        return (
            f"Hi {contact_name},\n\n"
            f"Invoice {invoice_id} ({amt}) for {customer_name} was due {due_date} "
            f"and shows as outstanding. Could you confirm payment status or expected date?\n\n"
            f"Thanks,\nAccounts Receivable"
        )

    if tier_name == "second_reminder":
        return (
            f"Hi {contact_name},\n\n"
            f"Following up on invoice {invoice_id} ({amt}), now {dpd} days past its {due_date} "
            f"due date. Please let us know the status -- happy to help if anything is blocking payment "
            f"(missing PO, documentation, etc).\n\nThanks,\nAccounts Receivable"
        )

    if tier_name == "controller_escalation":
        return (
            f"Hi {contact_name},\n\n"
            f"Invoice {invoice_id} ({amt}) for {customer_name} is now {dpd} days past due "
            f"({due_date}). This has gone unacknowledged through our standard reminders, so we're "
            f"looping you in directly as financial controller. Please advise on status or timeline.\n\n"
            f"Regards,\nAccounts Receivable"
        )

    if tier_name == "ceo_escalation":
        return (
            f"Hi {contact_name},\n\n"
            f"I'm reaching out directly because invoice {invoice_id} ({amt}) for {customer_name} "
            f"is now {dpd} days past due ({due_date}) and prior attempts to reach your AP and finance "
            f"contacts have not resolved it. We'd like to resolve this without further escalation -- "
            f"could we set up a brief call this week?\n\nRegards,\nAccounts Receivable"
        )

    if tier_name == "owner_escalation":
        return (
            f"Hi {contact_name},\n\n"
            f"I'm writing to you directly, as owner, regarding invoice {invoice_id} ({amt}) for "
            f"{customer_name}, now {dpd} days past due ({due_date}). This has not been resolved "
            f"through the finance chain and we'd like to find a path forward before this affects "
            f"the account relationship. Could we speak this week?\n\nRegards,\nAccounts Receivable"
        )

    return f"[no template for tier {tier_name}] invoice {invoice_id}, {amt}, {dpd} dpd"


def render_internal_note(kind: str, **ctx) -> str:
    """Internal-only note (to the AR analyst / account director), never sent
    to the customer. Used for hold-for-signoff queue entries and reply
    triage so a human reviewing the log understands *why* without re-reading
    the raw customer email."""
    return f"[INTERNAL -- {kind}] " + "; ".join(f"{k}={v}" for k, v in ctx.items())
