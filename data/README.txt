Collections data pack
Ledger is current as of 26 August 2026.

customers.csv        Customer list and contractual payment terms.
contacts.csv         Everyone reachable on each account. 'side' is whether the
                     person works for the customer or for us. Customer-side
                     contacts run AP contact, financial controller, CEO, owner.
                     Our side has the account director and the AR analyst.
                     The escalation policy is yours to design; these are simply
                     the people who exist.
invoices.csv         All invoices issued Mar 2025 - Aug 2026. status is 'paid'
                     or 'open'. Exported from the accounting system as-is.
payments.csv         Payments received, by invoice. An invoice may have more
                     than one payment row.
inbound_replies/     Customer email replies received against reminders already
                     sent. Twenty messages, unsorted.

All contact with customers is by email. No other channel is available.

Reminders have been going out manually and inconsistently up to now; there is
no log of what was sent or when.
