#!/usr/bin/env python3
"""
Instacart history scraper — deterministic CLI-driven pipeline.

Replaces the LLM-driven "Instacart Order Scraper" cron job. Drives the
OpenClaw browser via `openclaw browser navigate` and `openclaw browser
evaluate --fn`, runs short JS snippets to enumerate order URLs, then runs
the CLI's own extract-one.js on each order detail page to read canonical
product IDs from Apollo. Pipes resulting JSONL through
`instacart-pp-cli history import`.

Per CLAUDE.md: deterministic = exec, not agentTurn. Writes status JSON
for spratt-health. Sends an outbox alert on uncaught failure.

Note: the browser plugin caps each `evaluate` call at ~19.5s, so the
infinite-scroll + Load-more loop runs Python-side with one short JS
snippet per iteration. extract-one.js polls Apollo internally for up to
10s, well inside the cap, so it runs as-is.
"""

import argparse
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CLI = f"{HOME}/go/bin/instacart-pp-cli"
CLI_DB = f"{HOME}/Library/Application Support/instacart/instacart.db"
OUTBOX = f"{HOME}/.config/spratt/infrastructure/outbox/outbox.py"
RECIPIENT = "+13157082088"
STATUS_FILE = f"{HOME}/.config/spratt/infrastructure/launchd-status/instacart-history-scrape.json"
STATE_FILE = f"{HOME}/.config/spratt/db/instacart-scrape-state.json"
CLI_MOD_GLOB = (
    f"{HOME}/go/pkg/mod/github.com/mvanhorn/printing-press-library/"
    f"library/commerce/instacart@*/docs"
)
SOURCE_TAG = "instacart-history-scrape"


def find_js_dir():
    candidates = sorted(glob.glob(CLI_MOD_GLOB))
    if not candidates:
        raise RuntimeError(f"No CLI module dir matching {CLI_MOD_GLOB}")
    return candidates[-1]


def load_extract_one(js_dir):
    src = open(os.path.join(js_dir, "extract-one.js")).read()
    open_marker = "(async () => {"
    close_marker = "})();"
    o = src.find(open_marker)
    c = src.rfind(close_marker)
    if o == -1 or c == -1 or c <= o:
        raise RuntimeError("extract-one.js: cannot locate IIFE boundaries")
    return "async () => {" + src[o + len(open_marker):c] + "}"


COLLECT_IDS_JS = (
    "() => { const out = new Set();"
    " for (const a of document.querySelectorAll('a[href^=\"/store/orders/\"]')) {"
    "   const m = (a.getAttribute('href')||'').match(/\\/store\\/orders\\/(\\d+)/);"
    "   if (m) out.add(m[1]);"
    " }"
    " return Array.from(out); }"
)

SCROLL_JS = "() => { window.scrollTo(0, document.body.scrollHeight); return document.body.scrollHeight; }"

CLICK_LOAD_MORE_JS = (
    "() => { const btn = Array.from(document.querySelectorAll('button'))"
    "   .find(b => /^load more/i.test((b.innerText||'').trim()));"
    " if (!btn) return 'no_button';"
    " btn.scrollIntoView({block:'center'});"
    " btn.click();"
    " return 'clicked'; }"
)


def browser(args, timeout_s=30):
    cmd = ["openclaw", "browser", "--timeout", str(timeout_s * 1000)] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 30)
    if r.returncode != 0:
        raise RuntimeError(
            f"browser {args[:2]}: exit={r.returncode} stderr={r.stderr.strip()[:300]}"
        )
    return r.stdout.strip()


def navigate(url):
    return browser(["navigate", url], timeout_s=30)


def evaluate(fn_src, timeout_s=18):
    raw = browser(["evaluate", "--fn", fn_src], timeout_s=timeout_s)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(decoded, str):
        try:
            return json.loads(decoded)
        except json.JSONDecodeError:
            return decoded
    return decoded


def collect_order_ids(scroll_iters=40, load_more_iters=40, settle_seconds=1.5):
    """Drive the order-history page to enumerate every visible /store/orders/<id> URL.
    Python-side scroll + Load-more loop, one short evaluate per iteration to stay
    under the browser plugin's ~19.5s per-call cap."""
    seen = set(evaluate(COLLECT_IDS_JS, timeout_s=15) or [])
    stable = 0
    for _ in range(scroll_iters):
        if stable >= 3:
            break
        evaluate(SCROLL_JS, timeout_s=10)
        time.sleep(settle_seconds)
        cur = set(evaluate(COLLECT_IDS_JS, timeout_s=15) or [])
        added = len(cur - seen)
        seen = cur
        stable = stable + 1 if added == 0 else 0
    for _ in range(load_more_iters):
        result = evaluate(CLICK_LOAD_MORE_JS, timeout_s=10)
        if result == "no_button":
            break
        time.sleep(2.0)
        cur = set(evaluate(COLLECT_IDS_JS, timeout_s=15) or [])
        if cur == seen:
            break
        seen = cur
    return sorted(seen)


def check_profile_picker():
    where = evaluate("() => window.location.pathname", timeout_s=10)
    return isinstance(where, str) and where.startswith("/store/profiles")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"processed_urls": []}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        if not isinstance(data.get("processed_urls"), list):
            data["processed_urls"] = []
        return data
    except Exception:
        return {"processed_urls": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def existing_db_order_ids():
    if not os.path.exists(CLI_DB):
        return set()
    conn = sqlite3.connect(CLI_DB)
    try:
        rows = conn.execute("SELECT order_id FROM orders").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def write_status(status, **extra):
    payload = {
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="Process every URL the order-history page exposes, "
                             "ignoring the locally-tracked processed_urls list.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip CLI history import; still navigate and extract.")
    parser.add_argument("--max-orders", type=int, default=50,
                        help="Cap per-run extraction count.")
    args = parser.parse_args()

    started = time.time()
    js_dir = find_js_dir()
    extract_fn = load_extract_one(js_dir)
    state = load_state()
    processed_urls = set(state.get("processed_urls", []))

    try:
        evaluate("() => { localStorage.removeItem('__ic_dumped'); return 'cleared'; }",
                 timeout_s=15)

        navigate("https://www.instacart.com/store/account/orders")
        time.sleep(2)

        if check_profile_picker():
            outbox_send(
                "⚠️ Instacart scraper hit the profile picker. Open the browser tab and pick a profile.",
                f"{SOURCE_TAG}:profile",
            )
            write_status("WARNING", reason="profile_picker",
                         duration_seconds=int(time.time() - started))
            return 0

        seen_ids = collect_order_ids()

        # Newest-first: URL ids carry an MMDD prefix, and we use lex-desc as a
        # cheap proxy for recency within a year. Recent orders are more likely
        # to have hydrated Apollo cache; legacy orders may return
        # cache_key_missing and should be tried last.
        ordered = sorted(seen_ids, reverse=True)
        to_process = (
            list(ordered) if args.backfill
            else [u for u in ordered if u not in processed_urls]
        )
        to_process = to_process[:args.max_orders]

        per_order = []
        per_retailer = {}
        imported_orders = 0
        imported_items = 0

        for url_id in to_process:
            try:
                navigate(f"https://www.instacart.com/store/orders/{url_id}")
                time.sleep(1)
                rec = evaluate(extract_fn, timeout_s=18)
                ok = isinstance(rec, dict) and not rec.get("skipped")
                if not ok:
                    per_order.append({
                        "url_id": url_id,
                        "ok": False,
                        "reason": rec.get("reason") if isinstance(rec, dict) else "non_dict_return",
                    })
                    continue

                apollo_order_id = rec.get("order_id")
                # Pull only this just-extracted record back out of localStorage.
                # evaluate() JSON-decodes the return value for us, so return the
                # object directly (not a JSON string).
                fetch_js = (
                    "() => { const arr = JSON.parse(localStorage.getItem('__ic_dumped')||'[]');"
                    f" return arr.find(r => r && r.order_id === {json.dumps(apollo_order_id)}) || null; }}"
                )
                full = evaluate(fetch_js, timeout_s=10)
                if not isinstance(full, dict):
                    per_order.append({
                        "url_id": url_id,
                        "ok": False,
                        "reason": f"localstorage_miss_after_extract apollo_id={apollo_order_id!r}",
                    })
                    continue
                if full.get("skipped"):
                    per_order.append({
                        "url_id": url_id,
                        "ok": False,
                        "reason": full.get("reason", "skipped_record"),
                    })
                    continue

                if args.dry_run:
                    imported_orders += 1
                    imported_items += full.get("item_count", 0)
                    slug = full.get("retailer_slug") or "?"
                    per_retailer[slug] = per_retailer.get(slug, 0) + 1
                    per_order.append({"url_id": url_id, "ok": True, "dry_run": True})
                    processed_urls.add(url_id)
                    continue

                import_proc = subprocess.run(
                    [CLI, "history", "import", "-", "--json"],
                    input=json.dumps(full) + "\n",
                    capture_output=True, text=True, timeout=60,
                )
                if import_proc.returncode != 0:
                    per_order.append({
                        "url_id": url_id,
                        "ok": False,
                        "reason": f"import_rc={import_proc.returncode}: {import_proc.stderr.strip()[:160]}",
                    })
                    continue

                try:
                    one = json.loads(import_proc.stdout)
                except json.JSONDecodeError:
                    one = {}
                imported_orders += one.get("orders", 1)
                imported_items += one.get("items", full.get("item_count", 0))
                for k, v in (one.get("per_retailer") or {}).items():
                    per_retailer[k] = per_retailer.get(k, 0) + v
                processed_urls.add(url_id)
                per_order.append({
                    "url_id": url_id,
                    "ok": True,
                    "apollo_order_id": apollo_order_id,
                    "items": full.get("item_count", 0),
                })

                # Persist state after every successful import so a crash mid-run
                # does not re-walk what we already imported.
                state["processed_urls"] = sorted(processed_urls)
                state["last_run_at"] = datetime.now(timezone.utc).isoformat()
                save_state(state)

            except Exception as e:
                per_order.append({"url_id": url_id, "ok": False, "reason": str(e)[:160]})

        if imported_orders > 0 and not args.dry_run:
            per_str = ", ".join(f"{k}: {v}" for k, v in per_retailer.items()) or "—"
            tag = "backfill" if args.backfill else "imported"
            outbox_send(
                f"📥 Instacart {tag}: {imported_orders} order(s), {imported_items} item(s) ({per_str})",
                SOURCE_TAG,
            )

        state["processed_urls"] = sorted(processed_urls)
        state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

        db_order_count = len(existing_db_order_ids())
        write_status(
            "ok",
            duration_seconds=int(time.time() - started),
            backfill=args.backfill,
            dry_run=args.dry_run,
            visible_order_count=len(seen_ids),
            processed_this_run=len(to_process),
            extracted_ok=sum(1 for p in per_order if p["ok"]),
            imported_orders=imported_orders,
            imported_items=imported_items,
            per_retailer=per_retailer,
            db_total_orders=db_order_count,
            errors=[p for p in per_order if not p["ok"]][:5],
        )
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        outbox_send(
            f"⚠️ Instacart scraper failed: {type(e).__name__}: {str(e)[:200]}",
            f"{SOURCE_TAG}:error",
        )
        write_status(
            "ERROR",
            error=f"{type(e).__name__}: {e}",
            traceback=tb[:1500],
            duration_seconds=int(time.time() - started),
        )
        sys.stderr.write(tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
