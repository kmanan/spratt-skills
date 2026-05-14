#!/usr/bin/env python3
"""
Instacart cart auto-populate — calls cadence.py for due items, then
`instacart-pp-cli add --item-id` per row to stage them in the active
cart without firing a checkout.

Triggered by cron Wed + Sat 7:45am PT, 15 minutes before reorder-nudge
runs. Writes a status JSON to launchd-status/ so reorder-nudge can
reframe its message from "time to buy" to "cart staged" when the run is
fresh. Silent (no outbox) when zero items qualify; reorder-nudge
handles the user-facing message regardless.

On uncaught error: writes status=ERROR and emits an outbox alert per the
Spratt observability rule (failures must reach Manan's phone).
"""

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CLI = f"{HOME}/go/bin/instacart-pp-cli"
CADENCE = f"{HOME}/.config/spratt/infrastructure/instacart/cadence.py"
STATUS_FILE = f"{HOME}/.config/spratt/infrastructure/launchd-status/instacart-cart-build.json"
OUTBOX = f"{HOME}/.config/spratt/infrastructure/outbox/outbox.py"
RECIPIENT = "+13157082088"
SOURCE_TAG = "instacart-cart-build"


def fetch_due():
    r = subprocess.run(
        ["python3", CADENCE, "--due-only", "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout or "[]")


def stage_one(retailer, item_id, qty, dry_run):
    cmd = [CLI, "add", retailer, "--item-id", item_id,
           "--qty", str(qty), "--yes", "--json"]
    if dry_run:
        cmd.append("--dry-run")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = {"exit": r.returncode, "stderr": r.stderr.strip()[:300]}
    if r.stdout.strip():
        try:
            out["result"] = json.loads(r.stdout)
        except json.JSONDecodeError:
            out["stdout"] = r.stdout.strip()[:400]
    return out


def write_status(payload):
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(payload, f, indent=2)


def outbox_send(body, source):
    try:
        subprocess.run(
            ["python3", OUTBOX, "schedule",
             "--to", RECIPIENT, "--body", body, "--at", "now",
             "--source", source, "--created-by", source],
            check=True, timeout=15,
        )
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Pass --dry-run to the CLI (no cart mutation).")
    p.add_argument("--max-items", type=int, default=20)
    args = p.parse_args()

    started = time.time()
    try:
        due = fetch_due()
        due = [d for d in due
               if d.get("status") == "due" and d.get("item_id")
               and d.get("retailer_slug")]
        due = due[:args.max_items]

        staged = []
        errors = []
        for d in due:
            attempt = stage_one(d["retailer_slug"], d["item_id"],
                                d.get("quantity") or 1, args.dry_run)
            entry = {
                "retailer_slug": d["retailer_slug"],
                "item_id": d["item_id"],
                "name": d["item"],
                "quantity": d.get("quantity") or 1,
                "exit": attempt["exit"],
            }
            if attempt["exit"] == 0:
                res = attempt.get("result", {})
                entry["ok"] = True
                entry["resolved_via"] = res.get("resolved_via")
                entry["mutation_status"] = res.get("status")
                staged.append(entry)
            else:
                entry["ok"] = False
                entry["reason"] = attempt.get("stderr") or attempt.get("stdout") or ""
                errors.append(entry)

        write_status({
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": int(time.time() - started),
            "dry_run": args.dry_run,
            "due_count": len(due),
            "staged": staged,
            "errors": errors,
        })
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        outbox_send(
            f"⚠️ Instacart cart-build failed: {type(e).__name__}: {str(e)[:200]}",
            f"{SOURCE_TAG}:error",
        )
        write_status({
            "status": "ERROR",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": int(time.time() - started),
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb[:1500],
        })
        sys.stderr.write(tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
