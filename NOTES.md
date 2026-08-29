# NOTES

## Why the policy is shaped this way

The manual process this replaces has one visible failure mode in the data itself:
`INV-2177` is a $314 invoice, and the customer's reply on it (`07_reply.txt`) is
someone furious about a *fourth* email over nine years of being a customer. The
process wasn't wrong to chase it — it was wrong to chase it the same way it would
chase a $30k invoice. So the policy's central idea is that **escalation intensity
scales with dollar amount, not just days late.** Small invoices get a slower
cadence, a hard cap of 3 automated touches, and can never reach the CEO or owner
tier regardless of how overdue they get — past that cap, it's a human's call
(phone, write-off, or a different approach), not another email. Large invoices
escalate faster and hit a human sooner, because that's where getting it wrong
costs more.

The second idea is that **a reply is a decision point, not an input to a template.**
Anything that isn't a plain, unambiguous "yes, paying by X" freezes the automated
cadence for that invoice: disputes, "we already paid" claims, wrong invoice/PO
references, bounces, legal referrals, payment-plan requests, even a bare "?".
Silence gets automation. A response gets a person. This also caught two things
worth naming: a "this was already paid" claim that *was* confirmed by payments.csv
(ledger just hadn't caught up — resolved automatically) versus one that *wasn't*
in payments.csv at all (held, because a customer's assertion isn't a source of
truth on its own). And a remittance advice listing an invoice ID that doesn't
exist anywhere in our records — flagged for a human rather than silently dropped
or wrongly matched to the invoice named in the subject line.

## What the agent may do without a human

Send scheduled reminders up through the controller tier, for invoices with no
open reply-driven hold, capped by amount band and contact-frequency limits above.
Read and classify inbound replies, reconcile payment claims against payments.csv,
and update its own internal state (holds, contact routing).

## What it may not do

Send anything to a CEO or owner (always held for sign-off — that's a
relationship decision, not a scheduling one). Send anything at all once an
invoice has an active hold. Resolve a payment claim it cannot verify against
payments.csv. Act on an invoice/PO reference it cannot match to a real record.
Guess at a correct recipient when a contact has bounced.

## Where I drew the line, and what must be true before this touches a real customer

The line is: **the agent may decide to stay quiet, but never decides alone to
escalate socially** (new tier of human) or to close a financial question
(payment received). Both are held for a person even when the agent is fairly
confident. Before this sends a real email: the reconciliation logic needs to run
against the live accounting system, not a CSV snapshot, since "ledger is stale"
was one of the two live cases in just 20 sample replies. The classifier needs a
larger labeled sample before I'd trust its false-negative rate outside the small
category set here. And every `hold_for_signoff` row needs an actual human queue
and SLA, or "held" quietly becomes "never sent."

## AI use

Used Claude (via chat) as the primary build partner — architecture, the full
Python implementation, and the classifier's rule patterns, working from data I
read and decisions I made about the policy. I directed the design (amount-based
escalation, freeze-on-reply, zero external dependencies) and reviewed the
generated dry-run log rather than trusting it blind. One place I overrode it:
the first cadence draft used a flat 6-day gap for every invoice regardless of
amount; running it against the pack surfaced `INV-2177` getting nine automated
touches over six weeks on a $314 balance before a human ever saw it — the exact
complaint in `07_reply.txt`. I rejected that version and added the small-band
cadence cap and hard contact limit that's in the current policy.
