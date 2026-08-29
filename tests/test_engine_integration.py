"""Integration tests for engine.run_replay against small, hand-built packs.

These exist because test_agent.py only tests the engine's *ingredients*
(classifier, policy, reconciliation) in isolation -- nothing previously
exercised the day-by-day simulation loop itself: cadence firing, a reply
freezing it, a payment resolving it, or a hold being cleared. This file
is that missing layer.
"""
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data_loader import Pack, Customer, Contact, Invoice, Payment, InboundReply
from src.engine import run_replay
from src.policy import load_policy

POLICY_PATH = Path(__file__).parent.parent / "config" / "policy.toml"


def _mini_pack(invoices, payments=None, replies=None, hold_overrides=None, as_of=date(2026, 9, 1)):
    customers = {"C-01": Customer("C-01", "Test Customer Inc", "Net 30")}
    contacts = {
        "C-01": [
            Contact("C-01", "customer", "ap_contact", "Ann Contact", "ap@test.example", "AP"),
            Contact("C-01", "customer", "controller", "Cal Controller", "controller@test.example", "Controller"),
            Contact("C-01", "customer", "ceo", "Cee CEO", "ceo@test.example", "CEO"),
            Contact("C-01", "customer", "owner", "Ollie Owner", "owner@test.example", "Owner"),
        ]
    }
    return Pack(
        customers=customers, contacts=contacts, invoices=invoices,
        payments=payments or {}, replies=replies or [],
        hold_overrides=hold_overrides or {}, as_of=as_of,
    )


class TestReplayEngine(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(POLICY_PATH)

    def test_reminder_fires_after_due_date(self):
        inv = Invoice("INV-T1", "C-01", date(2026, 7, 1), date(2026, 8, 1), Decimal("500.00"), "Net 30", "open")
        pack = _mini_pack([inv], as_of=date(2026, 8, 5))
        rows = run_replay(pack, self.policy)
        cadence_rows = [r for r in rows if r.trigger == "scheduled_cadence"]
        self.assertTrue(any(r.action == "auto_send" for r in cadence_rows),
                         "expected at least one auto_send reminder for an overdue, unheld invoice")

    def test_payment_resolves_invoice_and_stops_reminders(self):
        inv = Invoice("INV-T2", "C-01", date(2026, 7, 1), date(2026, 8, 1), Decimal("500.00"), "Net 30", "open")
        payments = {"INV-T2": [Payment("INV-T2", date(2026, 8, 3), Decimal("500.00"), "ACH")]}
        pack = _mini_pack([inv], payments=payments, as_of=date(2026, 8, 20))
        rows = run_replay(pack, self.policy)
        after_payment = [r for r in rows if r.invoice_id == "INV-T2" and r.date > date(2026, 8, 3)]
        self.assertEqual(after_payment, [], "no contact should happen after the invoice is fully paid")

    def test_dispute_reply_freezes_cadence(self):
        inv = Invoice("INV-T3", "C-01", date(2026, 7, 1), date(2026, 8, 1), Decimal("5000.00"), "Net 30", "open")
        reply = InboundReply("r1", "ap@test.example", date(2026, 8, 10), "RE: Invoice INV-T3",
                              "We do not accept the amounts claimed, the hours billed do not match our records.",
                              "INV-T3")
        pack = _mini_pack([inv], replies=[reply], as_of=date(2026, 9, 1))
        rows = run_replay(pack, self.policy)
        after_reply = [r for r in rows if r.invoice_id == "INV-T3" and r.trigger == "scheduled_cadence"
                       and r.date > date(2026, 8, 10)]
        self.assertEqual(after_reply, [], "a dispute reply should stop all further scheduled cadence")
        dispute_row = [r for r in rows if r.invoice_id == "INV-T3" and r.trigger == "inbound_reply"]
        self.assertEqual(len(dispute_row), 1)
        self.assertEqual(dispute_row[0].action, "hold_for_signoff")

    def test_verified_paid_claim_resolves_without_holding(self):
        inv = Invoice("INV-T4", "C-01", date(2026, 7, 1), date(2026, 8, 1), Decimal("500.00"), "Net 30", "open")
        payments = {"INV-T4": [Payment("INV-T4", date(2026, 8, 5), Decimal("500.00"), "ACH")]}
        reply = InboundReply("r2", "ap@test.example", date(2026, 8, 10), "RE: Invoice INV-T4",
                              "This was already paid on 5 Aug, nothing outstanding on our side.", "INV-T4")
        pack = _mini_pack([inv], payments=payments, replies=[reply], as_of=date(2026, 8, 20))
        rows = run_replay(pack, self.policy)
        after_claim = [r for r in rows if r.invoice_id == "INV-T4" and r.date > date(2026, 8, 10)]
        self.assertEqual(after_claim, [], "a verified paid claim should end all activity, with no hold")
        reply_row = [r for r in rows if r.invoice_id == "INV-T4" and r.trigger == "inbound_reply"][0]
        self.assertEqual(reply_row.action, "internal_note")  # not held -- claim was verified

    def test_unverified_paid_claim_holds_instead_of_resolving(self):
        inv = Invoice("INV-T5", "C-01", date(2026, 7, 1), date(2026, 8, 1), Decimal("500.00"), "Net 30", "open")
        reply = InboundReply("r3", "ap@test.example", date(2026, 8, 10), "RE: Invoice INV-T5",
                              "This was already paid, nothing outstanding on our side.", "INV-T5")
        pack = _mini_pack([inv], replies=[reply], as_of=date(2026, 8, 25))
        rows = run_replay(pack, self.policy)
        reply_row = [r for r in rows if r.invoice_id == "INV-T5" and r.trigger == "inbound_reply"][0]
        self.assertEqual(reply_row.action, "hold_for_signoff")
        after_claim = [r for r in rows if r.invoice_id == "INV-T5" and r.trigger == "scheduled_cadence"
                       and r.date > date(2026, 8, 10)]
        self.assertEqual(after_claim, [], "an unverified claim should hold, not resume reminders")

    def test_hold_override_resumes_cadence(self):
        inv = Invoice("INV-T6", "C-01", date(2026, 7, 1), date(2026, 8, 1), Decimal("5000.00"), "Net 30", "open")
        reply = InboundReply("r4", "ap@test.example", date(2026, 8, 5), "RE: Invoice INV-T6",
                              "We dispute the amount billed, do not accept the amounts claimed.", "INV-T6")
        overrides = {"INV-T6": (date(2026, 8, 15), "resolved on call with customer")}
        pack = _mini_pack([inv], replies=[reply], hold_overrides=overrides, as_of=date(2026, 9, 1))
        rows = run_replay(pack, self.policy)

        clear_rows = [r for r in rows if r.trigger == "hold_override"]
        self.assertEqual(len(clear_rows), 1)
        self.assertEqual(clear_rows[0].date, date(2026, 8, 15))

        resumed = [r for r in rows if r.invoice_id == "INV-T6" and r.trigger == "scheduled_cadence"
                   and r.date > date(2026, 8, 15)]
        self.assertTrue(len(resumed) > 0, "cadence should resume after the hold is cleared")

    def test_missing_contact_holds_instead_of_fake_recipient(self):
        # customer with no ceo/owner contact at all
        customers = {"C-02": Customer("C-02", "No CEO On File LLC", "Net 30")}
        contacts = {"C-02": [Contact("C-02", "customer", "ap_contact", "Ann", "ap@test2.example", "AP")]}
        inv = Invoice("INV-T7", "C-02", date(2026, 1, 1), date(2026, 2, 1), Decimal("40000.00"), "Net 30", "open")
        pack = Pack(customers=customers, contacts=contacts, invoices=[inv], payments={},
                    replies=[], hold_overrides={}, as_of=date(2026, 4, 1))  # deep enough dpd for ceo tier
        rows = run_replay(pack, self.policy)
        self.assertTrue(all(r.recipient_email != "unknown@unknown" for r in rows),
                         "must never draft to a placeholder address")
        missing_contact_rows = [r for r in rows if "cannot draft a real recipient" in r.detail]
        self.assertTrue(len(missing_contact_rows) > 0,
                         "should surface a hold when policy calls for a contact type that doesn't exist")


if __name__ == "__main__":
    unittest.main()
