# GAPS.md (supplementary — not one of the required deliverables)

The brief asks for a one-page NOTES.md covering four specific things, so the
detailed gap analysis lives here instead of padding that file past a page.

Found these by testing the edges rather than just the happy path against the
real dataset — recorded here rather than pretending the first draft had none.

- **Money was `float`, now `Decimal`.** Fixed — amounts are now read from the
  raw CSV text via `Decimal(str(...))` rather than through a float
  intermediate, so cent-level drift can't accumulate across the ~1,600
  comparisons a full replay does. The real dataset never happened to expose
  this (verified: no partial payments on any open invoice), which is exactly
  why it needed fixing on principle rather than by observing a wrong number.

- **Held invoices had no way to un-freeze.** Fixed — `config/hold_overrides.csv`
  (invoice_id, cleared_date, note) is a human's record of "I resolved this,
  resume contact." The engine checks it daily and resumes cadence from the
  first tier when a matching date arrives. It's empty by default: nothing in
  the real reply data documents an actual resolution within the simulation
  window, so I didn't invent one — the mechanism is ready, not backfilled.

- **The engine itself had no direct tests.** Fixed — `test_engine_integration.py`
  runs the real `run_replay()` against small hand-built packs and checks the
  behaviors that actually matter: a reminder firing, a dispute freezing
  cadence, a verified claim resolving without a hold, an unverified one
  holding instead, a cleared hold resuming cadence, and a missing contact
  type holding instead of drafting to a placeholder address. Writing these
  caught a bug in the test itself (wrong assumption about which tier would
  hit first) before it shipped — a useful reminder that new tests need
  scrutiny too, not just new code.

- **Missing-contact fallback drafted to a placeholder address.** Fixed — if
  policy calls for a contact type the customer has no record of (e.g. no
  `controller` on file), the invoice now holds for a human to find the right
  person, instead of logging what looks like a successful send to
  `unknown@unknown`. Never triggered on the real data (every customer here
  has a full contact set) but was wrong as written.

- **Reply classification is single-label and rule-based.** A reply that's
  simultaneously hostile *and* disputing an amount only gets tagged with
  whichever pattern is checked first. The 20-sample space didn't need
  multi-label to classify correctly, but a larger, messier corpus would. I'd
  reach for a small supervised classifier (or an LLM call, with the
  rule-based version kept as a fallback / sanity check) once free-text
  nuance started mattering more than matching a closed set of known
  patterns — not before, since that trades away auditability for a problem
  this dataset doesn't actually have yet.

- **A dead contact fully freezes an invoice rather than trying the next tier
  up.** A bounce on the `ap_contact` address holds for a human rather than
  automatically retrying at `controller`. Safer default for a take-home, but
  a real tradeoff between "never contact the wrong person automatically" and
  "don't let a stale email address silently kill collection on an invoice."

- **Per-invoice policy, not per-customer.** A customer with several
  simultaneous disputes is handled as several independent invoices, with no
  account-level view — real dunning systems often need that aggregate signal.

- **O(days × invoices) simulation.** Fine at 432 invoices over 18 months
  (~235k checks, well under a second); would need an event-driven rewrite
  (compute each invoice's next action date directly rather than polling
  daily) well before this reached enterprise volume.
