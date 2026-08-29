"""Load the collections pack into plain dataclasses.

Kept dependency-free on purpose: csv + re + dataclasses is all this needs,
and it means the loader has no opinions about anything except the file
format it's reading.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


def _pdate(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _pmoney(s: str) -> Decimal:
    # Decimal(str(x)), not Decimal(float) -- constructing from the raw CSV
    # string avoids ever routing a dollar amount through binary floating
    # point, which is what actually causes cent-level drift after enough
    # arithmetic. The CSV values are already exact decimal text, so this is
    # a lossless read.
    return Decimal(s.strip())


@dataclass
class Customer:
    customer_id: str
    customer_name: str
    payment_terms: str


@dataclass
class Contact:
    customer_id: str
    side: str            # "customer" | "provider"
    contact_type: str     # ap_contact | controller | ceo | owner | sales_owner | collections
    name: str
    email: str
    title: str


@dataclass
class Invoice:
    invoice_id: str
    customer_id: str
    issue_date: date
    due_date: date
    amount: Decimal
    terms: str
    status: str  # ledger snapshot status ("paid"/"open") -- see NOTES.md
                 # on why the engine does not trust this blindly for dpd math


@dataclass
class Payment:
    invoice_id: str
    payment_date: date
    amount: Decimal
    method: str


@dataclass
class InboundReply:
    reply_id: str
    from_email: str
    reply_date: date
    subject: str
    body: str
    referenced_invoice_id: str | None  # best-effort extraction from subject/body


_INVOICE_RE = re.compile(r"\bINV-\d{3,6}\b")


def _extract_invoice_id(subject: str, body: str) -> str | None:
    """Pull the first invoice id mentioned in the subject, falling back to
    body. Subject is preferred: it's the thread anchor. A reply may mention
    a *different* invoice id in the body (see 12_reply.txt, 19_reply.txt) --
    that mismatch is exactly what the classifier/reconciliation layer is for,
    so we deliberately don't try to be clever here."""
    m = _INVOICE_RE.search(subject)
    if m:
        return m.group(0)
    m = _INVOICE_RE.search(body)
    return m.group(0) if m else None


def load_customers(data_dir: Path) -> dict[str, Customer]:
    out = {}
    with open(data_dir / "customers.csv", newline="") as f:
        for row in csv.DictReader(f):
            out[row["customer_id"]] = Customer(
                row["customer_id"], row["customer_name"], row["payment_terms"]
            )
    return out


def load_contacts(data_dir: Path) -> dict[str, list[Contact]]:
    """customer_id -> list of Contact (both sides included)."""
    out: dict[str, list[Contact]] = {}
    with open(data_dir / "contacts.csv", newline="") as f:
        for row in csv.DictReader(f):
            c = Contact(
                row["customer_id"], row["side"], row["contact_type"],
                row["name"], row["email"], row["title"],
            )
            out.setdefault(c.customer_id, []).append(c)
    return out


def load_invoices(data_dir: Path) -> list[Invoice]:
    out = []
    with open(data_dir / "invoices.csv", newline="") as f:
        for row in csv.DictReader(f):
            out.append(Invoice(
                row["invoice_id"], row["customer_id"],
                _pdate(row["issue_date"]), _pdate(row["due_date"]),
                _pmoney(row["amount"]), row["terms"], row["status"],
            ))
    return out


def load_payments(data_dir: Path) -> dict[str, list[Payment]]:
    """invoice_id -> list of Payment (an invoice can have >1 payment row)."""
    out: dict[str, list[Payment]] = {}
    with open(data_dir / "payments.csv", newline="") as f:
        for row in csv.DictReader(f):
            p = Payment(row["invoice_id"], _pdate(row["payment_date"]),
                        _pmoney(row["amount"]), row["method"])
            out.setdefault(p.invoice_id, []).append(p)
    return out


def load_inbound_replies(data_dir: Path) -> list[InboundReply]:
    out = []
    reply_dir = data_dir / "inbound_replies"
    for path in sorted(reply_dir.glob("*.txt")):
        text = path.read_text()
        headers, _, body = text.partition("\n\n")
        from_email = ""
        reply_date = None
        subject = ""
        for line in headers.splitlines():
            if line.lower().startswith("from:"):
                from_email = line.split(":", 1)[1].strip()
            elif line.lower().startswith("date:"):
                reply_date = _pdate(line.split(":", 1)[1].strip())
            elif line.lower().startswith("subject:"):
                subject = line.split(":", 1)[1].strip()
        if reply_date is None:
            raise ValueError(f"{path} missing a parseable Date: header")
        ref = _extract_invoice_id(subject, body)
        out.append(InboundReply(
            reply_id=path.stem, from_email=from_email, reply_date=reply_date,
            subject=subject, body=body.strip(), referenced_invoice_id=ref,
        ))
    return out


def load_hold_overrides(path: str | Path) -> dict[str, tuple[date, str]]:
    """A human's record of 'I resolved this, resume normal contact.'

    Without this file a held invoice stays held for the rest of the
    simulation -- resolving a dispute has to lead somewhere, and 'a human
    looked at it and cleared it' is that somewhere. Format: invoice_id,
    cleared_date, note. Empty by default; nothing in the current pack's
    history includes a documented resolution, so there's nothing to
    populate it with yet -- the mechanism exists and is ready for use, not
    backfilled with invented data.
    """
    out: dict[str, tuple[date, str]] = {}
    p = Path(path)
    if not p.exists():
        return out
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("invoice_id"):
                continue
            out[row["invoice_id"]] = (_pdate(row["cleared_date"]), row.get("note", ""))
    return out


@dataclass
class Pack:
    customers: dict[str, Customer]
    contacts: dict[str, list[Contact]]
    invoices: list[Invoice]
    payments: dict[str, list[Payment]]
    replies: list[InboundReply]
    hold_overrides: dict[str, tuple[date, str]] = field(default_factory=dict)
    as_of: date = field(default_factory=lambda: date(2026, 8, 26))  # README: "current as of 26 August 2026"

    def contact(self, customer_id: str, contact_type: str) -> Contact | None:
        for c in self.contacts.get(customer_id, []):
            if c.contact_type == contact_type:
                return c
        return None


def load_pack(data_dir: str | Path, hold_overrides_path: str | Path | None = None) -> Pack:
    data_dir = Path(data_dir)
    overrides = load_hold_overrides(hold_overrides_path) if hold_overrides_path else {}
    return Pack(
        customers=load_customers(data_dir),
        contacts=load_contacts(data_dir),
        invoices=load_invoices(data_dir),
        payments=load_payments(data_dir),
        replies=load_inbound_replies(data_dir),
        hold_overrides=overrides,
    )
