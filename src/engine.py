"""The replay engine.

Simulates one calendar day at a time, from the earliest invoice issue date
through the pack's "as of" date. On each day the agent only knows what has
happened on or before that day -- no invoice, payment, or reply dated after
"today" in the simulation is visible to it. This is what the brief asks for
("at each date it sees only what was known then") and it's also just the
only way to trust a backtest: anything that peeks at the future would make
the dry-run log meaningless as a rehearsal of the real thing.

Three independent things happen each day:
  1. Any inbound reply dated today gets classified and processed.
  2. Any hold with a matching entry in hold_overrides dated today is
     cleared, and cadence resumes from the first tier.
  3. Any invoice that's open, unpaid, and not on hold gets checked against
     the escalation policy for a possible reminder/escalation.

Replies can freeze #3 for an invoice (see policy.toml). A reply is always
processed even for a frozen invoice -- freezing stops us contacting the
customer again, it doesn't stop us reading what they sent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .classifier import classify
from .data_loader import Pack, Invoice
from .policy import Policy
from .reconciliation import verify_paid_claim, invoice_id_known
from .render import render, render_internal_note


@dataclass
class InvoiceState:
    resolved: bool = False
    resolved_date: date | None = None
    resolved_reason: str = ""
    held: bool = False
    hold_reason: str = ""
    last_tier: str | None = None
    last_contact_date: date | None = None
    auto_contact_count: int = 0
    alt_ap_email: str | None = None  # set on contact_change replies
    notes: list[str] = field(default_factory=list)


@dataclass
class LogRow:
    date: date
    invoice_id: str
    customer_id: str
    recipient_tier: str
    recipient_email: str
    message_body: str
    action: str          # "auto_send" | "hold_for_signoff" | "internal_note"
    trigger: str          # "scheduled_cadence" | "inbound_reply"
    detail: str

    def as_dict(self):
        return {
            "date": self.date.isoformat(),
            "invoice_id": self.invoice_id,
            "customer_id": self.customer_id,
            "recipient_tier": self.recipient_tier,
            "recipient_email": self.recipient_email,
            "action": self.action,
            "trigger": self.trigger,
            "detail": self.detail,
            "message_body": self.message_body,
        }


def _cumulative_paid(payments, invoice_id: str, as_of: date) -> Decimal:
    return sum((p.amount for p in payments.get(invoice_id, []) if p.payment_date <= as_of),
               start=Decimal("0"))


def run_replay(pack: Pack, policy: Policy) -> list[LogRow]:
    invoices_by_id: dict[str, Invoice] = {inv.invoice_id: inv for inv in pack.invoices}
    known_ids = set(invoices_by_id)
    replies_by_date: dict[date, list] = {}
    for r in pack.replies:
        replies_by_date.setdefault(r.reply_date, []).append(r)

    state: dict[str, InvoiceState] = {inv.invoice_id: InvoiceState() for inv in pack.invoices}
    log: list[LogRow] = []

    start = min(inv.issue_date for inv in pack.invoices)
    end = pack.as_of
    day = start
    while day <= end:
        # ---- 1. inbound replies dated today ----
        for reply in replies_by_date.get(day, []):
            log.extend(_process_reply(reply, day, invoices_by_id, known_ids, pack, state))

        # ---- 2. hold clearances dated today (human resolved something) ----
        for inv in pack.invoices:
            st = state[inv.invoice_id]
            if not st.held:
                continue
            override = pack.hold_overrides.get(inv.invoice_id)
            if override and override[0] == day:
                cleared_reason = st.hold_reason
                st.held = False
                st.hold_reason = ""
                st.last_tier = None          # resume cadence from the top, not mid-escalation
                st.last_contact_date = None
                log.append(LogRow(
                    date=day, invoice_id=inv.invoice_id, customer_id=inv.customer_id,
                    recipient_tier="internal", recipient_email="collections@provider.example",
                    message_body=render_internal_note("hold cleared", invoice_id=inv.invoice_id,
                                                        note=override[1]),
                    action="internal_note", trigger="hold_override",
                    detail=f"hold cleared by human override ({override[1] or 'no note'}); "
                           f"was held for: {cleared_reason}. Cadence resumes from first_reminder tier.",
                ))

        # ---- 3. scheduled cadence ----
        for inv in pack.invoices:
            st = state[inv.invoice_id]
            if st.resolved or st.held:
                continue
            paid_so_far = _cumulative_paid(pack.payments, inv.invoice_id, day)
            remaining_balance = inv.amount - paid_so_far
            if remaining_balance <= Decimal("0"):
                st.resolved = True
                st.resolved_date = day
                st.resolved_reason = "payment received (per payments.csv)"
                continue

            dpd = (day - inv.due_date).days
            # tier/band decisions use the outstanding balance, not the
            # original invoice amount -- a $30k invoice that's 95% paid
            # should not still be escalating like a $30k invoice
            tier = policy.tier_for(dpd, remaining_balance)
            if tier is None:
                continue

            band = policy.band_for_amount(remaining_balance)
            if band == "small" and st.auto_contact_count >= policy.max_auto_contacts_small:
                st.held = True
                st.hold_reason = (f"small-dollar remaining balance ({remaining_balance:.2f}) reached "
                                   f"{policy.max_auto_contacts_small} unanswered automated contacts -- "
                                   f"stopping automated cadence, needs a human call or write-off decision "
                                   f"rather than continued email")
                log.append(LogRow(
                    date=day, invoice_id=inv.invoice_id, customer_id=inv.customer_id,
                    recipient_tier="internal", recipient_email="collections@provider.example",
                    message_body=render_internal_note("auto-contact cap reached", invoice_id=inv.invoice_id,
                                                        contacts_sent=st.auto_contact_count),
                    action="hold_for_signoff", trigger="scheduled_cadence",
                    detail=st.hold_reason,
                ))
                continue

            gap = policy.gap_for_band(band)
            fire = False
            if st.last_tier != tier.name:
                fire = True
            elif st.last_contact_date is None or (day - st.last_contact_date).days >= gap:
                fire = True
            if not fire:
                continue

            contact = pack.contact(inv.customer_id, tier.recipient)
            if contact is None and not (tier.recipient == "ap_contact" and st.alt_ap_email):
                # No contact of the required type on file for this tier.
                # Previously this drafted to a placeholder "unknown@unknown"
                # address, which is worse than doing nothing -- it looks
                # like a successful send in the log. Hold for a human to
                # find the right person instead.
                st.held = True
                st.hold_reason = f"no '{tier.recipient}' contact on file for this customer"
                log.append(LogRow(
                    date=day, invoice_id=inv.invoice_id, customer_id=inv.customer_id,
                    recipient_tier="internal", recipient_email="collections@provider.example",
                    message_body=render_internal_note("missing contact", invoice_id=inv.invoice_id,
                                                        needed_contact_type=tier.recipient),
                    action="hold_for_signoff", trigger="scheduled_cadence",
                    detail=f"escalation policy calls for tier '{tier.name}' (recipient={tier.recipient}) "
                           f"but no such contact exists in contacts.csv for {inv.customer_id} -- "
                           f"cannot draft a real recipient, held for a human to locate one",
                ))
                continue

            recipient_email = st.alt_ap_email if (tier.recipient == "ap_contact" and st.alt_ap_email) else contact.email
            recipient_name = contact.name if contact else tier.recipient
            customer = pack.customers[inv.customer_id]

            body = render(
                tier.name, contact_name=recipient_name, invoice_id=inv.invoice_id,
                amount=remaining_balance, due_date=inv.due_date.isoformat(), dpd=dpd,
                customer_name=customer.customer_name,
            )
            action = "auto_send" if tier.auto_send else "hold_for_signoff"
            log.append(LogRow(
                date=day, invoice_id=inv.invoice_id, customer_id=inv.customer_id,
                recipient_tier=tier.name, recipient_email=recipient_email,
                message_body=body, action=action, trigger="scheduled_cadence",
                detail=f"dpd={dpd}, invoice_amount={inv.amount:.2f}, remaining_balance={remaining_balance:.2f}, "
                       f"band={policy.band_for_amount(remaining_balance)}"
                       + ("" if tier.auto_send else " -- tier requires human sign-off before send"),
            ))
            st.last_tier = tier.name
            st.last_contact_date = day
            st.auto_contact_count += 1

        day += timedelta(days=1)

    return log


def _process_reply(reply, day, invoices_by_id, known_ids, pack, state) -> list[LogRow]:
    cls = classify(reply.subject, reply.body)
    inv_id = reply.referenced_invoice_id
    rows: list[LogRow] = []

    # unresolvable / unknown invoice id -> always a human problem
    if inv_id is None or not invoice_id_known(inv_id, known_ids):
        rows.append(LogRow(
            date=day, invoice_id=inv_id or "UNKNOWN", customer_id="UNKNOWN",
            recipient_tier="internal", recipient_email="collections@provider.example",
            message_body=render_internal_note(
                "unresolvable reply", reply_id=reply.reply_id, from_=reply.from_email,
                category=cls.category, subject=reply.subject,
            ),
            action="hold_for_signoff", trigger="inbound_reply",
            detail=f"reply {reply.reply_id} references invoice id '{inv_id}' which does not exist "
                   f"in our records -- classified as {cls.category} ({cls.reason}); needs a human to "
                   f"identify the correct invoice before any action",
        ))
        return rows

    inv = invoices_by_id[inv_id]
    st = state[inv.invoice_id]

    if cls.category == "paid_claim":
        result = verify_paid_claim(inv_id, pack.payments, day)
        if result.verified:
            st.resolved = True
            st.resolved_date = day
            st.resolved_reason = result.note
            action, tier_label = "internal_note", "internal"
        else:
            st.held = True
            st.hold_reason = f"paid_claim unverified: {result.note}"
            action, tier_label = "hold_for_signoff", "internal"
        rows.append(LogRow(
            date=day, invoice_id=inv_id, customer_id=inv.customer_id,
            recipient_tier=tier_label, recipient_email="collections@provider.example",
            message_body=render_internal_note("paid-claim reconciliation", reply_id=reply.reply_id,
                                                verified=result.verified, note=result.note),
            action=action, trigger="inbound_reply",
            detail=f"reply {reply.reply_id}: customer claims payment; reconciliation={'verified' if result.verified else 'unverified'}",
        ))
        return rows

    if cls.category == "remittance_advice":
        # A remittance can list more than one invoice (see 19_reply.txt,
        # which lists a real open invoice AND an id that doesn't exist
        # anywhere in our records). Walk every INV-#### mentioned in the
        # body, not just the one the subject line anchored on -- otherwise
        # a bogus line item silently passes through unreviewed.
        import re as _re
        all_ids = sorted(set(_re.findall(r"\bINV-\d{3,6}\b", reply.body)))
        if inv_id not in all_ids:
            all_ids.append(inv_id)

        for rid in all_ids:
            if rid not in invoices_by_id:
                rows.append(LogRow(
                    date=day, invoice_id=rid, customer_id="UNKNOWN",
                    recipient_tier="internal", recipient_email="collections@provider.example",
                    message_body=render_internal_note("remittance line item unrecognized",
                                                        reply_id=reply.reply_id, invoice_id=rid),
                    action="hold_for_signoff", trigger="inbound_reply",
                    detail=f"reply {reply.reply_id}: remittance advice lists '{rid}', which does not "
                           f"match any invoice in our records -- do not assume it maps to the invoice "
                           f"in the subject line; needs a human to identify what this line item is",
                ))
                continue
            line_inv = invoices_by_id[rid]
            line_st = state[rid]
            result = verify_paid_claim(rid, pack.payments, day)
            line_st.held = not result.verified
            if result.verified:
                line_st.resolved, line_st.resolved_date, line_st.resolved_reason = True, day, result.note
            else:
                line_st.hold_reason = f"remittance received, not yet in payments ledger: {result.note}"
            rows.append(LogRow(
                date=day, invoice_id=rid, customer_id=line_inv.customer_id,
                recipient_tier="internal", recipient_email="collections@provider.example",
                message_body=render_internal_note("remittance reconciliation", reply_id=reply.reply_id,
                                                    verified=result.verified, note=result.note),
                action="internal_note" if result.verified else "hold_for_signoff",
                trigger="inbound_reply",
                detail=f"reply {reply.reply_id}: remittance advice for {rid}; "
                       f"reconciliation={'verified' if result.verified else 'pending, held for AR to confirm receipt'}",
            ))
        return rows

    if cls.category == "contact_change":
        import re as _re
        m = _re.search(r"[\w.\-]+@[\w.\-]+", reply.body)
        if m:
            st.alt_ap_email = m.group(0)
        rows.append(LogRow(
            date=day, invoice_id=inv_id, customer_id=inv.customer_id,
            recipient_tier="internal", recipient_email="collections@provider.example",
            message_body=render_internal_note("contact updated", reply_id=reply.reply_id,
                                                new_contact=st.alt_ap_email or "not found in body"),
            action="internal_note", trigger="inbound_reply",
            detail=f"reply {reply.reply_id}: customer-side contact changed, future AP correspondence "
                   f"redirected to {st.alt_ap_email or '(no address found -- needs human lookup)'}",
        ))
        return rows

    if cls.category == "confirmation_scheduled":
        # does not freeze cadence -- logged so a human can see the promise,
        # but the clock keeps running in case it doesn't land
        rows.append(LogRow(
            date=day, invoice_id=inv_id, customer_id=inv.customer_id,
            recipient_tier="internal", recipient_email="collections@provider.example",
            message_body=render_internal_note("payment promised", reply_id=reply.reply_id),
            action="internal_note", trigger="inbound_reply",
            detail=f"reply {reply.reply_id}: customer confirmed a specific payment date; "
                   f"cadence NOT frozen -- will resume contact if payment doesn't land",
        ))
        return rows

    if cls.category in ("auto_reply", "ticket_ack"):
        rows.append(LogRow(
            date=day, invoice_id=inv_id, customer_id=inv.customer_id,
            recipient_tier="internal", recipient_email="collections@provider.example",
            message_body=render_internal_note("non-substantive auto-response", reply_id=reply.reply_id,
                                                category=cls.category),
            action="internal_note", trigger="inbound_reply",
            detail=f"reply {reply.reply_id}: {cls.category}, no human decision required, cadence continues",
        ))
        return rows

    # everything else (dispute, legal_escalation, payment_plan_request,
    # wrong_reference, document_request, hostile, ambiguous,
    # acknowledged_uncategorized, bounce) freezes the cadence and goes to a
    # human. Severity differs but the *action* -- stop automated contact,
    # put it in front of a person -- is the same, which is the point: we'd
    # rather over-hold than auto-send into a live dispute or an angry
    # customer.
    st.held = True
    st.hold_reason = f"{cls.category}: {cls.reason}"
    rows.append(LogRow(
        date=day, invoice_id=inv_id, customer_id=inv.customer_id,
        recipient_tier="internal", recipient_email="collections@provider.example",
        message_body=render_internal_note("reply requires human review", reply_id=reply.reply_id,
                                            category=cls.category, from_=reply.from_email),
        action="hold_for_signoff", trigger="inbound_reply",
        detail=f"reply {reply.reply_id} classified as {cls.category} ({cls.reason}); "
               f"automated cadence frozen for {inv_id} pending human resolution",
    ))
    return rows
