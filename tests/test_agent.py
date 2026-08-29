"""Run with: python3 -m unittest discover -s tests

Covers the three places a mistake would actually matter: reply
classification, tier/amount-band selection, and payment reconciliation.
Not exhaustive -- these pin down the behaviors the pack's trickier replies
depend on, so a future policy change can't silently break them.
"""
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.classifier import classify
from src.policy import load_policy
from src.reconciliation import verify_paid_claim
from src.data_loader import Payment

POLICY_PATH = Path(__file__).parent.parent / "config" / "policy.toml"


class TestClassifier(unittest.TestCase):
    def test_dispute_not_confused_with_wrong_reference(self):
        # both mention "match" -- must not collide
        c1 = classify("RE: Invoice INV-2356 - query",
                       "we show 112 hours, you have invoiced 140. Holding payment until resolved.")
        self.assertEqual(c1.category, "dispute")

        c2 = classify("RE: Invoice INV-2033",
                       "The one you reference doesn't match anything in our system.")
        self.assertEqual(c2.category, "wrong_reference")

    def test_legal_beats_dispute(self):
        c = classify("RE: Invoice INV-2122",
                      "We do not accept the amounts claimed. Direct all correspondence to legal counsel.")
        self.assertEqual(c.category, "legal_escalation")

    def test_bounce_detected(self):
        c = classify("Undeliverable: Invoice INV-2377",
                      "550 5.1.1 The email account that you tried to reach does not exist.")
        self.assertEqual(c.category, "bounce")

    def test_bare_question_mark_is_ambiguous(self):
        c = classify("RE: Invoice INV-2268", "?")
        self.assertEqual(c.category, "ambiguous")

    def test_confirmation_scheduled(self):
        c = classify("RE: Invoice INV-2121", "Confirmed - this is scheduled in our payment run on 29 August.")
        self.assertEqual(c.category, "confirmation_scheduled")


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(POLICY_PATH)

    def test_small_invoice_never_reaches_ceo(self):
        # deep into "would be ceo_escalation" dpd range, but amount is tiny
        tier = self.policy.tier_for(dpd=100, amount=Decimal("314.14"))
        self.assertNotIn(tier.name, ("ceo_escalation", "owner_escalation"))

    def test_large_invoice_reaches_owner_eventually(self):
        tier = self.policy.tier_for(dpd=200, amount=Decimal("50000"))
        self.assertEqual(tier.name, "owner_escalation")

    def test_ceo_and_owner_tiers_are_never_auto_send(self):
        for t in self.policy.tiers:
            if t.name in ("ceo_escalation", "owner_escalation"):
                self.assertFalse(t.auto_send)

    def test_small_band_gets_longer_gap(self):
        self.assertGreater(self.policy.gap_for_band("small"), self.policy.gap_for_band("medium"))


class TestReconciliation(unittest.TestCase):
    def test_verified_claim(self):
        payments = {"INV-1": [Payment("INV-1", date(2026, 8, 11), Decimal("34354.30"), "ACH")]}
        r = verify_paid_claim("INV-1", payments, as_of=date(2026, 8, 19))
        self.assertTrue(r.verified)

    def test_unverified_claim_no_payment_row(self):
        r = verify_paid_claim("INV-2", {}, as_of=date(2026, 8, 20))
        self.assertFalse(r.verified)

    def test_future_payment_not_visible_yet(self):
        # payment happens AFTER the simulated "today" -- must not verify
        payments = {"INV-3": [Payment("INV-3", date(2026, 9, 1), Decimal("100.00"), "ACH")]}
        r = verify_paid_claim("INV-3", payments, as_of=date(2026, 8, 20))
        self.assertFalse(r.verified)


if __name__ == "__main__":
    unittest.main()
