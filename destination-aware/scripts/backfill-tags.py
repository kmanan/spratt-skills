#!/usr/bin/env python3
"""One-shot backfill of destination-aware tags on existing reminders.

Scans open reminders in Manan / Harshita / Shared lists, skips already-tagged
items, runs the same closed-enum LLM categorizer that email-scan uses at
creation, and either prints suggestions (dry-run) or applies them.

Behavior:
  --dry-run (default)   Print suggestions, no edits.
  --apply               Apply suggestions at or above --threshold.
                        Lower-confidence items are printed but left untouched.
  --threshold N         Confidence cutoff (default 0.7, matches email-scan).
  --list NAME           Restrict to a single reminder list.
  --notify              When applying, send a one-line iMessage with the diff.

Designed for one-time use. After backfill, email-scan tags new reminders at
creation and the destination-daemon enforces tag-based matching.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

KNOWN_FILE = os.path.expanduser(
    "~/.config/spratt/infrastructure/destination/known-destinations.json"
)
OUTBOX_CLI = os.path.expanduser(
    "~/.config/spratt/infrastructure/outbox/outbox.py"
)
OPENCLAW_MODEL = "openai-codex/gpt-5.5"
REMINDER_LISTS = ("Manan", "Harshita", "Shared")
TAG_RE = re.compile(r"#(\w+)")


def load_allowed():
    try:
        with open(KNOWN_FILE) as f:
            return list(json.load(f).get("categories", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def parse_json_text(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for i, c in enumerate(text):
            if c in "[{":
                try:
                    obj, _ = decoder.raw_decode(text[i:])
                    return obj
                except json.JSONDecodeError:
                    continue
        return None


def call_openclaw_json(prompt, timeout=60):
    cmd = [
        "openclaw", "infer", "model", "run",
        "--gateway",
        "--model", OPENCLAW_MODEL,
        "--json",
        "--prompt", prompt,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout)[:300]
    try:
        outer = json.loads(r.stdout)
        text = outer["outputs"][0]["text"]
        return parse_json_text(text), None
    except Exception as e:
        return None, f"parse failed: {e}"


def categorize(title, due_date, allowed):
    if not allowed:
        return {"tag": "", "confidence": 0.0, "reason": "no allowed categories"}
    enum_str = ", ".join(allowed)
    prompt = f"""Decide which destination this action belongs to, picking from a CLOSED list.
Allowed tags: {enum_str}.
Empty string ("") means no destination fits — pick that when uncertain or when the task
is location-independent (paying bills online, replying to email, filing paperwork).

Return ONLY JSON: {{"tag":"<one of: {enum_str}, or empty>","confidence":0.0,"reason":""}}

Examples:
- task "Take diapers and blanket to Bright Horizons" → {{"tag":"daycare","confidence":0.95,"reason":"daycare drop-off item"}}
- task "Pick up amoxicillin prescription" → {{"tag":"pharmacy","confidence":0.9,"reason":"prescription pickup"}}
- task "Bring imaging records to appointment with Dr. Patel" → {{"tag":"medical","confidence":0.9,"reason":"medical visit prep"}}
- task "Pick up bananas for the week" → {{"tag":"grocery","confidence":0.85,"reason":"grocery shopping item"}}
- task "Pay Comcast bill" → {{"tag":"","confidence":0.95,"reason":"online task, no destination"}}
- task "RSVP to wedding by Friday" → {{"tag":"","confidence":0.9,"reason":"online task, no destination"}}
- task "Unload dishwasher" → {{"tag":"home","confidence":0.85,"reason":"home chore"}}
- task "Prepare for Tuesday review meeting" → {{"tag":"work","confidence":0.85,"reason":"work prep, do at office"}}

Task: {title}
Due date: {due_date or "(none)"}
"""
    data, err = call_openclaw_json(prompt, timeout=60)
    if err or not isinstance(data, dict):
        return {"tag": "", "confidence": 0.0, "reason": err or "parse-fail"}
    raw_tag = (data.get("tag") or "").strip().lower()
    if raw_tag and raw_tag not in allowed:
        raw_tag = ""
    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return {"tag": raw_tag, "confidence": conf, "reason": (data.get("reason") or "")[:120]}


def list_open_untagged(target_lists, allowed):
    out = []
    for lst in target_lists:
        r = subprocess.run(
            ["/opt/homebrew/bin/remindctl", "show", "all", "--list", lst, "--json"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            continue
        try:
            items = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        for it in items:
            if it.get("isCompleted"):
                continue
            title = (it.get("title") or "").strip()
            if not title:
                continue
            existing = {m.lower() for m in TAG_RE.findall(title)} & set(allowed)
            if existing:
                continue
            out.append({
                "id": it.get("id"),
                "list": it.get("listName") or lst,
                "title": title,
                "dueDate": it.get("dueDate"),
            })
    return out


def apply_tag(reminder_id, new_title):
    r = subprocess.run(
        ["/opt/homebrew/bin/remindctl", "edit", reminder_id, "--title", new_title],
        capture_output=True, text=True, timeout=15,
    )
    return r.returncode == 0, (r.stderr or r.stdout).strip()[:200]


def send_outbox(body, source="destination-backfill"):
    if not os.path.exists(OUTBOX_CLI):
        return
    try:
        subprocess.run(
            ["/usr/bin/python3", OUTBOX_CLI, "schedule",
             "--to", "Manan", "--body", body, "--at", "now", "--source", source,
             "--created-by", "backfill-tags"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Apply tag edits. Without this flag, the script is a dry-run.")
    ap.add_argument("--threshold", type=float, default=0.7,
                    help="Min confidence to auto-apply (default 0.7).")
    ap.add_argument("--list", choices=REMINDER_LISTS,
                    help="Restrict to one reminder list.")
    ap.add_argument("--notify", action="store_true",
                    help="Send iMessage summary of changes (only with --apply).")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    allowed = load_allowed()
    if not allowed:
        print("ERROR: no allowed categories in known-destinations.json", file=sys.stderr)
        return 2

    target = (args.list,) if args.list else REMINDER_LISTS
    untagged = list_open_untagged(target, allowed)
    if not untagged:
        print("All open reminders already tagged. Nothing to do.")
        return 0

    suggestions = []
    for r in untagged:
        sug = categorize(r["title"], r.get("dueDate"), allowed)
        suggestions.append({**r, **sug})

    applied = []
    skipped_low_conf = []
    skipped_empty = []
    errors = []

    for s in suggestions:
        tag = s["tag"]
        conf = s["confidence"]
        if not tag:
            skipped_empty.append(s)
            continue
        if conf < args.threshold:
            skipped_low_conf.append(s)
            continue
        if not args.apply:
            applied.append(s)  # would-apply, not actually applied
            continue
        new_title = f"{s['title']} #{tag}"
        ok, detail = apply_tag(s["id"], new_title)
        if ok:
            applied.append(s)
        else:
            errors.append({**s, "error": detail})

    report = {
        "dry_run": not args.apply,
        "threshold": args.threshold,
        "scanned": len(untagged),
        "applied": applied,
        "skipped_low_confidence": skipped_low_conf,
        "skipped_no_tag": skipped_empty,
        "errors": errors,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        verb = "Applied" if args.apply else "Would apply"
        print(f"=== {verb} ({len(applied)}) ===")
        for s in applied:
            print(f"  #{s['tag']:<10} [{s['confidence']:.2f}]  {s['title'][:70]}  ({s.get('reason','')[:60]})")
        if skipped_low_conf:
            print(f"\n=== Low-confidence picks not applied (< {args.threshold}) ({len(skipped_low_conf)}) ===")
            for s in skipped_low_conf:
                print(f"  ?{s['tag']:<10} [{s['confidence']:.2f}]  {s['title'][:70]}  ({s.get('reason','')[:60]})")
        if skipped_empty:
            print(f"\n=== Classifier returned no tag ({len(skipped_empty)}) ===")
            for s in skipped_empty:
                print(f"  --        [{s['confidence']:.2f}]  {s['title'][:70]}  ({s.get('reason','')[:60]})")
        if errors:
            print(f"\n=== Errors ({len(errors)}) ===")
            for s in errors:
                print(f"  !!  {s['title'][:60]}  → {s['error']}")

    if args.apply and args.notify and applied:
        diff = "\n".join(f"#{s['tag']}  {s['title'][:60]}" for s in applied[:10])
        msg = f"Destination-tag backfill applied to {len(applied)} reminder(s):\n{diff}"
        if skipped_low_conf or skipped_empty:
            uncertain = len(skipped_low_conf) + len(skipped_empty)
            msg += f"\n({uncertain} left untagged — see destination-summary)"
        send_outbox(msg)

    return 0


if __name__ == "__main__":
    sys.exit(main())
