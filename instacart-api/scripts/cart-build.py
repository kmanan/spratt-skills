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
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CLI = f"{HOME}/go/bin/instacart-pp-cli"
CLI_DB = f"{HOME}/Library/Application Support/instacart/instacart.db"
CADENCE = f"{HOME}/.config/spratt/infrastructure/instacart/cadence.py"
STATUS_FILE = f"{HOME}/.config/spratt/infrastructure/launchd-status/instacart-cart-build.json"
OUTBOX = f"{HOME}/.config/spratt/infrastructure/outbox/outbox.py"
RECIPIENT = "+13157082088"
SOURCE_TAG = "instacart-cart-build"

STOPWORDS = {
    "a", "an", "and", "bag", "box", "can", "cans", "count", "ct", "each",
    "fl", "fresh", "made", "of", "organic", "oz", "pack", "package",
    "pk", "style", "sweet", "the", "with",
}
CATEGORY_TOKENS = {
    "butter", "cheese", "chips", "cucumber", "ginger", "milk", "onion",
    "onions", "raspberries", "tomato", "tomatoes", "yoghurt", "yogurt",
}
FLAVOR_TOKENS = {
    "berry", "blackberry", "cheddar", "cran", "grapefruit", "guava",
    "lemon", "lime", "orange", "peach", "pure", "raspberry", "sour",
    "tangerine", "vanilla",
}


def _tokens(name):
    return set(re.findall(r"[a-z0-9]+", (name or "").lower()))


def _meaningful_tokens(name):
    return {t for t in _tokens(name) if t not in STOPWORDS and not t.isdigit()}


def _family_key(name):
    toks = _tokens(name)
    if "lacroix" in toks:
        return ("lacroix",)
    if "milk" in toks and not ({"yogurt", "yoghurt", "formula"} & toks):
        if "whole" in toks or "vitamin" in toks:
            return ("whole-milk",)
    if "butter" in toks:
        if "unsalted" in toks:
            return ("butter", "unsalted")
        if "salted" in toks or "salt" in toks:
            return ("butter", "salted")
    if ("onion" in toks or "onions" in toks) and "red" in toks:
        return ("red-onions",)
    if "tomato" in toks or "tomatoes" in toks:
        if not ({"ketchup", "soup", "paste", "sauce"} & toks):
            return ("fresh-tomatoes",)
    return None


def _acceptable_search_match(query, candidate):
    q = _tokens(query)
    c = _tokens(candidate)
    if not q or not c:
        return (False, "empty name")

    if "unsalted" in q and "unsalted" not in c:
        return (False, "unsalted requested but candidate is not unsalted")
    if "unsalted" not in q and ("salted" in q or "salt" in q) and "unsalted" in c:
        return (False, "salted requested but candidate is unsalted")

    q_categories = q & CATEGORY_TOKENS
    if q_categories and not (q_categories & c):
        return (False, f"missing category token: {sorted(q_categories)[0]}")

    q_flavors = q & FLAVOR_TOKENS
    c_flavors = c & FLAVOR_TOKENS
    if q_flavors:
        if not (q_flavors & c):
            return (False, f"missing flavor token: {sorted(q_flavors)[0]}")
        conflicting = c_flavors - q_flavors
        if conflicting:
            return (False, f"conflicting flavor token: {sorted(conflicting)[0]}")

    required = _meaningful_tokens(query)
    optional_misses = {"half", "whole", "vitamin", "sparkling", "water"}
    required -= optional_misses
    required = {t for t in required if len(t) > 2}
    if required:
        overlap = required & c
        if len(overlap) < max(1, min(2, len(required))):
            return (False, "low token overlap")
        if len(overlap) / len(required) < 0.45:
            return (False, "low token overlap")

    return (True, "matched")


def fetch_due():
    r = subprocess.run(
        ["python3", CADENCE, "--due-only", "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout or "[]")


def _add_by_id(retailer, item_id, qty, dry_run):
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


def _search_matching_item_id(retailer, query):
    """Return (item_id, matched_name, rejected) for a safe search hit.
    Used as a fallback when a stored item-id is rejected as notFoundBasketProduct."""
    rejected = []
    try:
        r = subprocess.run(
            [CLI, "search", query, "--store", retailer, "--limit", "5", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return (None, None, rejected)
        hits = json.loads(r.stdout)
        if not hits:
            return (None, None, rejected)
        for hit in hits:
            item_id = hit.get("item_id")
            name = hit.get("name") or ""
            ok, reason = _acceptable_search_match(query, name)
            if ok and item_id:
                return (item_id, name, rejected)
            rejected.append({"item_id": item_id, "name": name, "reason": reason})
        return (None, None, rejected)
    except Exception:
        return (None, None, rejected)


def _refresh_cart(retailer):
    subprocess.run(
        [CLI, "cart", "show", retailer, "--json"],
        capture_output=True, text=True, timeout=30,
    )


def _current_cart_items(retailer):
    try:
        _refresh_cart(retailer)
    except Exception:
        pass
    if not os.path.exists(CLI_DB):
        return []
    conn = sqlite3.connect(CLI_DB)
    conn.row_factory = sqlite3.Row
    try:
        cart = conn.execute(
            """
            SELECT cart_id FROM carts
            WHERE retailer_slug = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (retailer,),
        ).fetchone()
        if not cart:
            return []
        rows = conn.execute(
            "SELECT item_id, name FROM cart_items WHERE cart_id = ?",
            (cart["cart_id"],),
        ).fetchall()
        return [{"item_id": r["item_id"], "name": r["name"] or ""} for r in rows]
    finally:
        conn.close()


def _cart_contains(existing_items, item_id, item_name):
    target_family = _family_key(item_name)
    target_name = _meaningful_tokens(item_name)
    for item in existing_items:
        if item.get("item_id") == item_id:
            return (True, "same item_id")
        current_name = item.get("name") or ""
        if target_family and _family_key(current_name) == target_family:
            return (True, f"same family: {'/'.join(target_family)}")
        current_tokens = _meaningful_tokens(current_name)
        if target_name and current_tokens and len(target_name & current_tokens) / len(target_name) >= 0.8:
            return (True, "same item name")
    return (False, None)


def stage_one(retailer, item_id, qty, dry_run, item_name=None):
    """Stage one item. On notFoundBasketProduct (stored ID rot — typically a
    RetailerID-prefixed historical ID being rejected in favor of the
    LocationID-prefixed current ID), fall back to a name search and retry once
    with the fresh ID. Records the fallback details in the returned dict so the
    status JSON shows what happened."""
    out = _add_by_id(retailer, item_id, qty, dry_run)
    blob = (out.get("stderr") or "") + " " + (out.get("stdout") or "")
    if out["exit"] == 0 or "notFoundBasketProduct" not in blob or not item_name:
        return out

    fresh_id, fresh_name, rejected = _search_matching_item_id(retailer, item_name)
    if not fresh_id or fresh_id == item_id:
        out["fallback_attempted"] = True
        out["fallback_reason"] = "search returned no safe result"
        out["fallback_rejected"] = rejected[:5]
        return out

    retry = _add_by_id(retailer, fresh_id, qty, dry_run)
    retry["fallback_attempted"] = True
    retry["fallback_from_item_id"] = item_id
    retry["fallback_to_item_id"] = fresh_id
    retry["fallback_matched_name"] = fresh_name
    retry["fallback_rejected"] = rejected[:5]
    retry["original_error"] = "notFoundBasketProduct"
    return retry


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
               and d.get("retailer_slug") and d.get("auto_stage")]
        due = due[:args.max_items]

        staged = []
        errors = []
        skipped = []
        cart_items_by_retailer = {}
        for d in due:
            retailer = d["retailer_slug"]
            if retailer not in cart_items_by_retailer:
                cart_items_by_retailer[retailer] = _current_cart_items(retailer)
            exists, exists_reason = _cart_contains(
                cart_items_by_retailer[retailer], d["item_id"], d.get("item") or "",
            )
            if exists:
                skipped.append({
                    "retailer_slug": retailer,
                    "item_id": d["item_id"],
                    "name": d["item"],
                    "reason": exists_reason,
                })
                continue
            attempt = stage_one(d["retailer_slug"], d["item_id"],
                                d.get("quantity") or 1, args.dry_run,
                                item_name=d.get("item"))
            entry = {
                "retailer_slug": d["retailer_slug"],
                "item_id": d["item_id"],
                "name": d["item"],
                "quantity": d.get("quantity") or 1,
                "exit": attempt["exit"],
            }
            if attempt.get("fallback_attempted"):
                entry["fallback_attempted"] = True
                if attempt.get("fallback_to_item_id"):
                    entry["fallback_to_item_id"] = attempt["fallback_to_item_id"]
                    entry["fallback_matched_name"] = attempt.get("fallback_matched_name")
                if attempt.get("fallback_rejected"):
                    entry["fallback_rejected"] = attempt.get("fallback_rejected")
                if attempt.get("fallback_reason"):
                    entry["fallback_reason"] = attempt.get("fallback_reason")
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
            "skipped": skipped,
            "errors": errors,
        })

        # No-silent-failures: alert Manan if anything is off, even when the
        # script "completed". Dry-run mode is excluded — manual tests shouldn't
        # spam alerts.
        if not args.dry_run:
            if len(due) > 0 and len(staged) == 0:
                outbox_send(
                    f"❌ Instacart cart-build: {len(due)} items were due but 0 staged successfully. "
                    f"All failed. First error: {(errors[0].get('reason') if errors else 'unknown')[:200]}",
                    f"{SOURCE_TAG}:all-failed",
                )
            elif errors:
                first = errors[0]
                outbox_send(
                    f"⚠️ Instacart cart-build: {len(staged)}/{len(due)} items staged, "
                    f"{len(errors)} failed. Example: {first.get('name','?')} → "
                    f"{(first.get('reason') or '')[:150]}",
                    f"{SOURCE_TAG}:partial-failed",
                )
            # Defensive: catch a misconfigured run where mutations report as
            # dry-run even though --dry-run wasn't passed.
            phantom_dry = [s for s in staged if "dry-run" in (s.get("mutation_status") or "")]
            if phantom_dry:
                outbox_send(
                    f"⚠️ Instacart cart-build: {len(phantom_dry)} items report dry-run "
                    f"status but --dry-run was not passed. Cart may not be actually staged. "
                    f"Check {STATUS_FILE}.",
                    f"{SOURCE_TAG}:phantom-dry-run",
                )
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
