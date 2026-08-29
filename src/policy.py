"""Loads config/policy.toml and turns it into decisions.

This module is intentionally the only place that knows what a "tier" means.
The engine asks it two questions: which tier applies right now, and is a
given reply category allowed to auto-send. Everything numeric lives in the
TOML file, not here.
"""
from __future__ import annotations

import tomllib
from decimal import Decimal
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Tier:
    name: str
    dpd_min: int
    dpd_max: int
    recipient: str
    cc: list[str]
    auto_send: bool
    min_band: str


_BAND_ORDER = ["small", "medium", "large"]


@dataclass
class Policy:
    tiers: list[Tier]
    min_days_between_contacts: int
    min_days_between_contacts_small: int
    max_auto_contacts_small: int
    pre_due_reminder_days: int
    small_max: Decimal
    medium_max: Decimal
    freezes_cadence: set[str]
    does_not_freeze: set[str]

    def gap_for_band(self, band: str) -> int:
        return self.min_days_between_contacts_small if band == "small" else self.min_days_between_contacts

    def band_for_amount(self, amount: Decimal) -> str:
        if amount <= self.small_max:
            return "small"
        if amount <= self.medium_max:
            return "medium"
        return "large"

    def tier_for(self, dpd: int, amount: Decimal) -> Tier | None:
        """The tier whose dpd window best fits `dpd`, restricted to tiers
        this amount's band is allowed to reach.

        Once dpd runs past the highest tier a band is allowed to reach
        (e.g. a small invoice past controller_escalation's dpd_max), the
        invoice stays at that ceiling tier rather than falling through to
        None -- a small invoice 200 days overdue should keep getting
        controller-tier attention indefinitely, not silently go quiet
        because it outran its own dpd window.
        """
        band = self.band_for_amount(amount)
        band_rank = _BAND_ORDER.index(band)
        unlocked = [t for t in self.tiers if _BAND_ORDER.index(t.min_band) <= band_rank]
        if not unlocked:
            return None

        in_window = [t for t in unlocked if t.dpd_min <= dpd <= t.dpd_max]
        if in_window:
            return max(in_window, key=lambda t: t.dpd_min)

        # dpd is past every unlocked tier's window -> stay at the ceiling
        past = [t for t in unlocked if dpd > t.dpd_max]
        if past:
            return max(past, key=lambda t: t.dpd_max)
        return None

    def reply_freezes_cadence(self, category: str) -> bool:
        if category in self.does_not_freeze:
            return False
        return category in self.freezes_cadence or True  # default: freeze unknowns too


def load_policy(path: str | Path) -> Policy:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    tiers = [
        Tier(
            name=t["name"], dpd_min=t["dpd_min"], dpd_max=t["dpd_max"],
            recipient=t["recipient"], cc=t.get("cc", []),
            auto_send=t["auto_send"], min_band=t["min_band"],
        )
        for t in raw["tiers"]
    ]
    return Policy(
        tiers=tiers,
        min_days_between_contacts=raw["cadence"]["min_days_between_contacts"],
        min_days_between_contacts_small=raw["cadence"]["min_days_between_contacts_small"],
        max_auto_contacts_small=raw["cadence"]["max_auto_contacts_small"],
        pre_due_reminder_days=raw["cadence"]["pre_due_reminder_days"],
        small_max=Decimal(str(raw["amount_bands"]["small_max"])),
        medium_max=Decimal(str(raw["amount_bands"]["medium_max"])),
        freezes_cadence=set(raw["reply_handling"]["freezes_cadence"]),
        does_not_freeze=set(raw["reply_handling"]["does_not_freeze"]),
    )
