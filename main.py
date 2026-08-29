#!/usr/bin/env python3
"""Collections agent -- single entry point.

Usage:
    python3 main.py --data-dir data --config config/policy.toml --out-dir outputs

Runs two things and writes their output to --out-dir:
  1. A dry-run replay across the full invoice/payment/reply history
     (outputs/replay_log.csv + .jsonl) -- the main deliverable.
  2. Risk flags for currently-open invoices (outputs/risk_flags.csv).

Nothing is sent anywhere. This never touches a network socket or an SMTP
library on purpose -- see NOTES.md for what would need to be true before
any auto_send row in the log became a real outbound email.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.data_loader import load_pack
from src.policy import load_policy
from src.engine import run_replay
from src.risk import flag_open_invoices


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def write_manifest(args, pack, rows, flags, out_dir: Path) -> Path:
    """Provenance for the run: which policy (by content hash, not just
    filename) and which data produced this log. Without this, a
    replay_log.csv sitting on disk next week can't be tied back to the
    policy.toml that generated it if that file has since changed --
    exactly the kind of thing an auditor asks about first."""
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_dir": str(Path(args.data_dir).resolve()),
        "policy_file": str(Path(args.config).resolve()),
        "policy_sha256_16": _sha256(Path(args.config)),
        "hold_overrides_file": str(Path(args.hold_overrides).resolve()) if args.hold_overrides else None,
        "as_of": pack.as_of.isoformat(),
        "n_invoices": len(pack.invoices),
        "n_replies": len(pack.replies),
        "n_hold_overrides_applied": len(pack.hold_overrides),
        "log_row_count": len(rows),
        "risk_flag_count": len(flags),
    }
    path = out_dir / "run_manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path


def write_replay_log(rows, out_dir: Path):
    csv_path = out_dir / "replay_log.csv"
    jsonl_path = out_dir / "replay_log.jsonl"
    fieldnames = ["date", "invoice_id", "customer_id", "recipient_tier",
                  "recipient_email", "action", "trigger", "detail", "message_body"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r.as_dict())
    with open(jsonl_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r.as_dict()) + "\n")
    return csv_path, jsonl_path


def write_risk_flags(flags, out_dir: Path):
    path = out_dir / "risk_flags.csv"
    fieldnames = ["invoice_id", "customer_id", "customer_name", "amount",
                  "due_date", "days_to_due", "risk_band", "reasons"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for fl in flags:
            w.writerow(fl.as_dict())
    return path


def print_summary(rows, flags):
    auto = sum(1 for r in rows if r.action == "auto_send")
    held = sum(1 for r in rows if r.action == "hold_for_signoff")
    note = sum(1 for r in rows if r.action == "internal_note")
    print(f"\nReplay complete: {len(rows)} log rows over the full history")
    print(f"  auto_send:        {auto}")
    print(f"  hold_for_signoff: {held}")
    print(f"  internal_note:    {note}")

    bands = {"high": 0, "medium": 0, "low": 0}
    for fl in flags:
        bands[fl.band] += 1
    print(f"\nRisk flags on {len(flags)} currently-open invoices:")
    print(f"  high:   {bands['high']}")
    print(f"  medium: {bands['medium']}")
    print(f"  low:    {bands['low']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data", help="path to the collections pack (default: ./data)")
    ap.add_argument("--config", default="config/policy.toml", help="escalation policy file")
    ap.add_argument("--out-dir", default="outputs", help="where to write replay_log.* and risk_flags.csv")
    ap.add_argument("--hold-overrides", default="config/hold_overrides.csv",
                     help="CSV of invoice_id,cleared_date,note -- a human's record of resolved holds "
                          "(default: config/hold_overrides.csv; empty file if none exist yet)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pack = load_pack(args.data_dir, hold_overrides_path=args.hold_overrides)
    policy = load_policy(args.config)

    rows = run_replay(pack, policy)
    csv_path, jsonl_path = write_replay_log(rows, out_dir)

    # feed the replay's holds into risk flagging so an invoice already
    # stuck in a dispute/bounce/etc shows up as elevated risk
    held: dict[str, str] = {}
    for r in rows:
        if r.action == "hold_for_signoff" and r.trigger == "inbound_reply":
            held[r.invoice_id] = r.detail

    flags = flag_open_invoices(pack, held)
    risk_path = write_risk_flags(flags, out_dir)
    manifest_path = write_manifest(args, pack, rows, flags, out_dir)

    print_summary(rows, flags)
    print(f"\nWrote:\n  {csv_path}\n  {jsonl_path}\n  {risk_path}\n  {manifest_path}")


if __name__ == "__main__":
    main()
