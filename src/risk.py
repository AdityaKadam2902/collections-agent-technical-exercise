"""Risk flagging for open invoices, as of the pack's ledger date.

Three independent signals, combined into a band, each stated in the output
so "why" is never a mystery:

  1. Customer lateness history -- of this customer's *closed* invoices, what
     fraction were paid after their due date, and by how many days on
     average? Past behaviour is the strongest signal we have for future
     behaviour with no external credit data.
  2. Where the open invoice already sits relative to its own due date.
  3. Whether the replay surfaced a hold on this invoice (dispute, wrong
     reference, bounce, etc) -- an invoice already stuck in a human queue
     is a materially different risk than one that's simply not due yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .data_loader import Pack


@dataclass
class CustomerLatenessProfile:
    n_closed: int
    n_late: int
    avg_days_late_when_late: float

    @property
    def late_rate(self) -> float:
        return self.n_late / self.n_closed if self.n_closed else 0.0


def build_customer_profiles(pack: Pack) -> dict[str, CustomerLatenessProfile]:
    profiles: dict[str, CustomerLatenessProfile] = {}
    by_customer: dict[str, list] = {}
    for inv in pack.invoices:
        by_customer.setdefault(inv.customer_id, []).append(inv)

    for cust_id, invs in by_customer.items():
        n_closed = 0
        n_late = 0
        late_days_total = 0
        for inv in invs:
            payments = sorted(pack.payments.get(inv.invoice_id, []), key=lambda p: p.payment_date)
            if not payments:
                continue  # still open, not part of the historical base rate
            paid_date = payments[-1].payment_date
            n_closed += 1
            delta = (paid_date - inv.due_date).days
            if delta > 0:
                n_late += 1
                late_days_total += delta
        avg_late = (late_days_total / n_late) if n_late else 0.0
        profiles[cust_id] = CustomerLatenessProfile(n_closed, n_late, avg_late)
    return profiles


@dataclass
class RiskFlag:
    invoice_id: str
    customer_id: str
    customer_name: str
    amount: Decimal
    due_date: date
    days_to_due: int  # negative = already overdue
    band: str          # "low" | "medium" | "high"
    reasons: list[str]

    def as_dict(self):
        return {
            "invoice_id": self.invoice_id,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "amount": f"{self.amount:.2f}",
            "due_date": self.due_date.isoformat(),
            "days_to_due": self.days_to_due,
            "risk_band": self.band,
            "reasons": "; ".join(self.reasons),
        }


def flag_open_invoices(pack: Pack, held_invoice_ids: dict[str, str]) -> list[RiskFlag]:
    """held_invoice_ids: invoice_id -> hold_reason, surfaced by the replay engine."""
    profiles = build_customer_profiles(pack)
    flags: list[RiskFlag] = []

    for inv in pack.invoices:
        if inv.status != "open":
            continue
        paid_so_far = sum((p.amount for p in pack.payments.get(inv.invoice_id, [])), start=Decimal("0"))
        if paid_so_far >= inv.amount:
            continue  # payments.csv shows it settled even though ledger status lags

        days_to_due = (inv.due_date - pack.as_of).days
        profile = profiles.get(inv.customer_id)
        reasons: list[str] = []
        score = 0

        if profile and profile.n_closed >= 3:
            if profile.late_rate >= 0.5:
                score += 2
                reasons.append(f"{profile.late_rate:.0%} of this customer's past invoices paid late "
                                f"(avg {profile.avg_days_late_when_late:.0f}d late)")
            elif profile.late_rate >= 0.2:
                score += 1
                reasons.append(f"{profile.late_rate:.0%} of this customer's past invoices paid late")
            else:
                reasons.append(f"clean payment history ({profile.late_rate:.0%} late historically)")
        else:
            reasons.append("insufficient closed-invoice history for this customer to establish a base rate")

        if days_to_due < 0:
            score += 2
            reasons.append(f"already {-days_to_due} days past due")
        elif days_to_due <= 7:
            score += 1
            reasons.append(f"due within {days_to_due} days")

        if inv.invoice_id in held_invoice_ids:
            score += 2
            reasons.append(f"agent currently holding this invoice: {held_invoice_ids[inv.invoice_id]}")

        band = "high" if score >= 4 else "medium" if score >= 2 else "low"
        flags.append(RiskFlag(
            invoice_id=inv.invoice_id, customer_id=inv.customer_id,
            customer_name=pack.customers[inv.customer_id].customer_name,
            amount=inv.amount, due_date=inv.due_date, days_to_due=days_to_due,
            band=band, reasons=reasons,
        ))

    flags.sort(key=lambda f: (-{"high": 2, "medium": 1, "low": 0}[f.band], f.days_to_due))
    return flags
