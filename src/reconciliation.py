"""Verify customer payment claims against payments.csv.

The pack contains two cases that make the point: INV-2231 is claimed paid
and *is* in payments.csv (ledger just hasn't caught up -- resolve, don't
escalate). INV-2087 is claimed "settled" and has *no* matching payment row
(claim contradicts our records -- hold for a human, do not resolve).
A reply is a claim, not a source of truth; payments.csv is the source of
truth we actually have.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .data_loader import Payment


@dataclass
class ReconciliationResult:
    verified: bool
    note: str


def verify_paid_claim(
    invoice_id: str | None,
    payments_by_invoice: dict[str, list[Payment]],
    as_of: date,
    claimed_amount: Decimal | None = None,
) -> ReconciliationResult:
    if invoice_id is None:
        return ReconciliationResult(
            verified=False,
            note="claim did not reference a resolvable invoice id -- cannot verify",
        )
    rows = [p for p in payments_by_invoice.get(invoice_id, []) if p.payment_date <= as_of]
    if not rows:
        return ReconciliationResult(
            verified=False,
            note=f"no payment on file for {invoice_id} as of {as_of} -- claim unverified, hold",
        )
    if claimed_amount is not None:
        match = any(p.amount == claimed_amount for p in rows)
        if not match:
            return ReconciliationResult(
                verified=False,
                note=f"payment exists for {invoice_id} but amount does not match claim -- hold",
            )
    latest = max(rows, key=lambda p: p.payment_date)
    return ReconciliationResult(
        verified=True,
        note=f"payment confirmed: {invoice_id} paid {latest.payment_date} "
             f"({latest.amount:.2f} via {latest.method}) -- ledger status is stale, safe to resolve",
    )


def invoice_id_known(invoice_id: str | None, known_invoice_ids: set[str]) -> bool:
    return invoice_id is not None and invoice_id in known_invoice_ids
