#!/usr/bin/env python3
"""
Card Wallet Refresh — full benefits refresh pipeline.

Two modes:
  dump       Print current DB state (text or JSON). No side effects.
  refresh    Call Claude with web_search to find issuer changes, apply them to
             the DB with an audit trail, and text Manan a summary. This is the
             pipeline the monthly cron invokes.

Usage:
    python3 card-wallet-refresh.py dump              # Human-readable dump
    python3 card-wallet-refresh.py dump-json         # JSON dump (programmatic)
    python3 card-wallet-refresh.py refresh           # Full refresh, apply + notify
    python3 card-wallet-refresh.py refresh --dry-run # Print proposed changes only
"""

import sqlite3
import json
import sys
import os
import subprocess
import urllib.request
import urllib.error
from datetime import date, datetime

CARDS_DB = os.path.expanduser("~/.config/spratt/cards/cards.sqlite")
OUTBOX_CLI = os.path.expanduser("~/.config/spratt/infrastructure/outbox/outbox.py")
MANAN_PHONE = "Manan"  # resolved by outbox.py via contacts.sqlite
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# Sonnet — not Haiku — because the refresh needs strong instruction-following
# and factual accuracy when comparing web-search results against the DB.
# Haiku dry-runs produced hallucinated changes (confusing CSR with CSP, adding
# quarterly categories that aren't in Chase's press release) AND missed real
# changes. Sonnet costs a few pennies more per monthly run and is worth it.
REFRESH_MODEL = "claude-sonnet-4-6"


def get_db():
    if not os.path.exists(CARDS_DB):
        sys.stderr.write(
            f"\nFATAL: cards database not found at:\n    {CARDS_DB}\n\n"
            f"Refusing to auto-create (prevents silent data loss if the path is wrong).\n\n"
        )
        sys.exit(1)
    conn = sqlite3.connect(CARDS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def dump_text():
    conn = get_db()
    rows = conn.execute("""
        SELECT
            c.holder,
            c.card_name,
            c.issuer,
            b.name,
            b.merchant,
            b.amount,
            b.cycle,
            b.period_rule,
            b.requires_activation,
            b.auto_applied,
            b.notes,
            b.active
        FROM benefits b
        JOIN cards c ON b.card_id = c.id
        WHERE c.active = 1
        ORDER BY c.holder, c.card_name, b.id
    """).fetchall()

    current_card = None
    for r in rows:
        card_key = f"{r['holder']} — {r['card_name']}"
        if card_key != current_card:
            if current_card is not None:
                print()
            print(f"### {card_key} ({r['issuer']})")
            current_card = card_key

        status = "ACTIVE" if r["active"] else "INACTIVE"
        flags = []
        if r["auto_applied"]:
            flags.append("auto-applied")
        if r["requires_activation"]:
            flags.append("requires activation")
        flag_str = f" [{', '.join(flags)}]" if flags else ""

        notes_str = f" — {r['notes']}" if r["notes"] else ""
        print(f"  [{status}] {r['name']}: ${r['amount']:.2f}/{r['cycle']} "
              f"({r['period_rule']}) @ {r['merchant']}{flag_str}{notes_str}")

    # Also dump reward rates
    print("\n## Reward Rates")
    rates = conn.execute("""
        SELECT c.holder, c.card_name, r.category, r.rate, r.cap_amount, r.cap_period, r.notes
        FROM reward_rates r
        JOIN cards c ON r.card_id = c.id
        WHERE c.active = 1
        ORDER BY c.holder, c.card_name, r.rate DESC
    """).fetchall()

    current_card = None
    for r in rates:
        card_key = f"{r['holder']} — {r['card_name']}"
        if card_key != current_card:
            print(f"\n### {card_key}")
            current_card = card_key
        cap_str = f" (cap: ${r['cap_amount']:.0f}/{r['cap_period']})" if r["cap_amount"] else ""
        notes_str = f" — {r['notes']}" if r["notes"] else ""
        print(f"  {r['category']}: {r['rate']}%{cap_str}{notes_str}")

    # Dump quarterly categories if any
    quarters = conn.execute("""
        SELECT c.card_name, q.year, q.quarter, q.categories, q.activated
        FROM quarterly_categories q
        JOIN cards c ON q.card_id = c.id
        ORDER BY q.year, q.quarter
    """).fetchall()

    if quarters:
        print("\n## Quarterly Categories")
        for q in quarters:
            act = "✅" if q["activated"] else "❌"
            print(f"  {q['card_name']} {q['year']}-Q{q['quarter']}: {q['categories']} {act}")

    conn.close()


def dump_json():
    conn = get_db()
    rows = conn.execute("""
        SELECT
            c.holder,
            c.card_name,
            c.issuer,
            b.id AS benefit_id,
            b.name,
            b.merchant,
            b.amount,
            b.cycle,
            b.period_rule,
            b.requires_activation,
            b.auto_applied,
            b.notes,
            b.active
        FROM benefits b
        JOIN cards c ON b.card_id = c.id
        WHERE c.active = 1
        ORDER BY c.holder, c.card_name, b.id
    """).fetchall()

    data = [dict(r) for r in rows]
    print(json.dumps(data, indent=2))
    conn.close()


# ─── Refresh pipeline ───

# Schema of the JSON Claude must return. We validate shape before touching the DB.
# Every supported action is listed here so the LLM can only propose things
# the apply step actually knows how to handle.
PROPOSAL_SCHEMA_DESCRIPTION = """
Return ONLY a JSON object with this exact shape:

{
  "changes": [
    // Zero or more change objects. Each change has an "action" field and
    // action-specific fields. ONLY these four actions are supported:

    // 1. Update an amount
    {"action": "update_amount", "benefit_id": <int>, "new_amount": <number>, "evidence": "<short quote or URL>"},

    // 2. Update notes (for e.g. Chase Freedom quarterly categories)
    {"action": "update_notes", "benefit_id": <int>, "new_notes": "<text>", "evidence": "<short quote or URL>"},

    // 3. Mark a benefit inactive (benefit was removed by the issuer)
    {"action": "mark_inactive", "benefit_id": <int>, "reason": "<text>", "evidence": "<short quote or URL>"},

    // 4. Add a new benefit
    {"action": "add_benefit", "card_id": <int>, "benefit": {
       "name": "<text>",
       "merchant": "<text>",
       "amount": <number>,
       "cycle": "monthly|quarterly|semi-annual|annual",
       "period_rule": "calendar|december-only|chase-freedom",
       "requires_activation": 0,
       "auto_applied": 0,
       "notes": "<text or null>"
     }, "evidence": "<short quote or URL>"}
  ],
  "summary": "<plain-language recap for the user, or '' if no changes>"
}

Rules:
- Only propose changes you are highly confident about (issuer press release, official benefits page, or multiple trusted sources agreeing).
- Do NOT propose changes that depend on the user's grandfathering status, renewal timing, or other personal factors. If a change only applies to cards opened after a date, do not include it.
- If a benefit's current DB notes are already accurate for the current period, do not propose update_notes.
- If no changes are warranted, return {"changes": [], "summary": ""}.
- Do NOT include markdown, commentary, or explanation outside the JSON.
"""


def build_refresh_system_prompt():
    today = date.today().isoformat()
    return (
        "You are the monthly card-benefits refresh. Your single goal: keep the "
        "user's credit-card benefits database ACCURATE and COMPLETE.\n\n"
        f"Today is {today}.\n\n"
        "For each active card in the database, use web_search to find the current "
        "(2026) benefits from authoritative sources, compare against the DB, and "
        "propose whatever changes bring the DB into agreement with reality.\n\n"

        "SOURCING:\n"
        "- Issuer press releases and official benefits pages (amex.com/card-benefits, "
        "chase.com/personal/credit-cards/*) are DEFINITIVE — one source is enough.\n"
        "- Two reputable third-party sources agreeing (The Points Guy, NerdWallet, "
        "Forbes, CNBC, Doctor of Credit, Frequent Miler, Upgraded Points) is "
        "sufficient.\n"
        "- A single blog post alone is NOT sufficient — skip that change.\n\n"

        "DO propose these, even though they may affect cards differently based on "
        "when they were opened:\n"
        "  - Benefit amount increases or decreases (e.g. $20/mo → $25/mo)\n"
        "  - New benefits added to the card product\n"
        "  - Benefits removed from the card product (only mark_inactive AFTER the "
        "effective date — if a benefit is ending July 1 and today is April, it is "
        "still active, do not mark it inactive yet)\n"
        "  - Quarterly category updates (Chase Freedom rotating categories)\n"
        "  - Changes to merchant, cycle, or period_rule\n\n"

        "The user understands their specific grandfathering / renewal status and will "
        "correct individual items if their own card differs. Your job is to reflect "
        "the CURRENT PRODUCT reality, not guess at the user's personal situation.\n\n"

        "DO NOT propose:\n"
        "  - Targeted Amex Offers or personalized promotions (these are per-user, "
        "not part of the card product).\n"
        "  - Changes where the DB is already correct (check before proposing).\n"
        "  - Speculative / rumored changes from a single unreliable source.\n\n"

        "If you research a card and find the DB is already accurate, that's fine — "
        "just don't propose any changes for that card. The summary field should "
        "briefly note what you verified even when no changes are proposed.\n\n"

        + PROPOSAL_SCHEMA_DESCRIPTION
    )


def build_refresh_user_prompt(state):
    return (
        "Here is the current benefits database:\n\n"
        "```json\n"
        + json.dumps(state, indent=2, default=str)
        + "\n```\n\n"
        "Research each active card's current benefits for 2026 via web_search. "
        "Compare against the database above. Output the JSON proposal."
    )


def current_state_for_refresh(conn):
    """Build the state document the LLM compares against."""
    cards = conn.execute("""
        SELECT id, holder, card_name, issuer
        FROM cards WHERE active=1 ORDER BY holder, card_name
    """).fetchall()
    benefits = conn.execute("""
        SELECT b.id AS benefit_id, b.card_id, c.card_name, c.holder, b.name, b.merchant,
               b.amount, b.cycle, b.period_rule, b.requires_activation, b.auto_applied,
               b.notes, b.active
        FROM benefits b JOIN cards c ON b.card_id=c.id
        WHERE b.active=1 AND c.active=1
        ORDER BY c.holder, c.card_name, b.id
    """).fetchall()
    return {
        "cards": [dict(c) for c in cards],
        "benefits": [dict(b) for b in benefits],
    }


def call_claude_refresh(state, api_key):
    """Single API call to Claude with web_search. Anthropic runs the tool loop
    server-side; we get the final assistant text (JSON) back in one response.

    Uses an assistant-prefill pattern to force the final text to start with `{`,
    which reliably suppresses prose-before-JSON and mid-thought musings between
    searches.

    Returns parsed proposal dict or None on error.
    """
    # Sonnet w/ server tools does not support assistant-message prefill, so we
    # rely on (a) strong system-prompt instructions and (b) post-processing
    # that extracts the JSON between the first `{` and last `}`.
    payload = {
        # 32k output is plenty for ~15 benefits worth of evidence + JSON.
        # Sonnet supports this comfortably; truncation at 8k was clipping real
        # proposals mid-object and producing unparseable output.
        "model": REFRESH_MODEL,
        "max_tokens": 32768,
        "system": build_refresh_system_prompt(),
        "messages": [
            {"role": "user", "content": build_refresh_user_prompt(state)},
        ],
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 12,
            }
        ],
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"Anthropic API HTTP error: {e.code} — {e.read().decode('utf-8', 'ignore')[:500]}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"Anthropic API network error: {e}", file=sys.stderr)
        return None

    if result.get("stop_reason") == "max_tokens":
        print("Warning: response truncated at max_tokens. Proposal may be incomplete.", file=sys.stderr)

    # When using prefill, take only the LAST text block (the final answer).
    # Earlier text blocks are mid-tool-loop narration and must not be concatenated.
    text_blocks = [b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"]
    if not text_blocks:
        print("Claude returned no text content. Full response:", file=sys.stderr)
        print(json.dumps(result, indent=2)[:2000], file=sys.stderr)
        return None

    final_text = text_blocks[-1]

    clean = final_text.strip()
    if clean.startswith("```"):
        lines = [l for l in clean.split("\n") if not l.strip().startswith("```")]
        clean = "\n".join(lines).strip()

    # Find the first '{' — JSON proposal starts there.
    first_brace = clean.find("{")
    if first_brace == -1:
        print(f"No JSON object found in response.\nAll text blocks ({len(text_blocks)}):", file=sys.stderr)
        for i, tb in enumerate(text_blocks):
            print(f"--- block {i} ---\n{tb[:800]}\n", file=sys.stderr)
        return None

    # Use raw_decode to parse exactly one JSON object starting at first_brace.
    # This is robust to any prose AFTER the JSON (which broke the earlier
    # last_brace-based approach when Sonnet added commentary after the object).
    decoder = json.JSONDecoder()
    try:
        proposal, end = decoder.raw_decode(clean[first_brace:])
        return proposal
    except json.JSONDecodeError as e:
        print(f"Claude returned invalid JSON: {e}", file=sys.stderr)
        print(f"First 2000 chars of JSON region:\n{clean[first_brace:first_brace+2000]}", file=sys.stderr)
        return None


def validate_change(change, state):
    """Check that a single change object is well-formed and references real IDs.
    Returns (ok, error_message)."""
    action = change.get("action")
    if action not in ("update_amount", "update_notes", "mark_inactive", "add_benefit"):
        return False, f"unknown action '{action}'"
    if not change.get("evidence"):
        return False, "missing evidence field"

    benefit_ids = {b["benefit_id"] for b in state["benefits"]}
    card_ids = {c["id"] for c in state["cards"]}

    if action in ("update_amount", "update_notes", "mark_inactive"):
        bid = change.get("benefit_id")
        if bid not in benefit_ids:
            return False, f"benefit_id {bid} not in active benefits"
        if action == "update_amount":
            try:
                float(change.get("new_amount"))
            except (TypeError, ValueError):
                return False, "new_amount not numeric"
        elif action == "update_notes":
            if not isinstance(change.get("new_notes"), str):
                return False, "new_notes not a string"
        elif action == "mark_inactive":
            if not change.get("reason"):
                return False, "missing reason for mark_inactive"
    elif action == "add_benefit":
        cid = change.get("card_id")
        if cid not in card_ids:
            return False, f"card_id {cid} not in active cards"
        b = change.get("benefit")
        if not isinstance(b, dict):
            return False, "benefit not a dict"
        for field in ("name", "amount", "cycle", "period_rule"):
            if field not in b:
                return False, f"benefit missing required field '{field}'"
        if b.get("cycle") not in ("monthly", "quarterly", "semi-annual", "annual"):
            return False, f"invalid cycle '{b.get('cycle')}'"
        if b.get("period_rule") not in ("calendar", "december-only", "chase-freedom"):
            return False, f"invalid period_rule '{b.get('period_rule')}'"
    return True, None


def apply_changes(conn, changes):
    """Apply validated changes to DB with audit entries. Returns list of applied change descriptions."""
    applied = []
    for ch in changes:
        action = ch["action"]
        try:
            if action == "update_amount":
                bid = ch["benefit_id"]
                old = conn.execute("SELECT amount, name, card_id FROM benefits WHERE id=?", (bid,)).fetchone()
                conn.execute("UPDATE benefits SET amount=?, updated_at=datetime('now') WHERE id=?",
                             (float(ch["new_amount"]), bid))
                desc = f"{old['name']}: ${old['amount']:.2f} → ${float(ch['new_amount']):.2f}"
                conn.execute("INSERT INTO benefit_changes (card_id, change_type, description) VALUES (?, ?, ?)",
                             (old["card_id"], "update_amount",
                              f"{desc}. Evidence: {ch['evidence']}"))
                applied.append(desc)

            elif action == "update_notes":
                bid = ch["benefit_id"]
                old = conn.execute("SELECT name, card_id FROM benefits WHERE id=?", (bid,)).fetchone()
                conn.execute("UPDATE benefits SET notes=?, updated_at=datetime('now') WHERE id=?",
                             (ch["new_notes"], bid))
                desc = f"{old['name']} notes updated"
                conn.execute("INSERT INTO benefit_changes (card_id, change_type, description) VALUES (?, ?, ?)",
                             (old["card_id"], "update_notes",
                              f"{desc}. New notes: {ch['new_notes']}. Evidence: {ch['evidence']}"))
                applied.append(desc)

            elif action == "mark_inactive":
                bid = ch["benefit_id"]
                old = conn.execute("SELECT name, card_id FROM benefits WHERE id=?", (bid,)).fetchone()
                conn.execute("UPDATE benefits SET active=0, updated_at=datetime('now') WHERE id=?", (bid,))
                desc = f"{old['name']} removed (reason: {ch['reason']})"
                conn.execute("INSERT INTO benefit_changes (card_id, change_type, description) VALUES (?, ?, ?)",
                             (old["card_id"], "mark_inactive",
                              f"{desc}. Evidence: {ch['evidence']}"))
                applied.append(desc)

            elif action == "add_benefit":
                cid = ch["card_id"]
                b = ch["benefit"]
                cur = conn.execute("""
                    INSERT INTO benefits (card_id, name, merchant, amount, cycle, period_rule,
                                          requires_activation, auto_applied, notes, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (cid, b["name"], b.get("merchant"), float(b["amount"]),
                      b["cycle"], b["period_rule"],
                      int(b.get("requires_activation", 0)), int(b.get("auto_applied", 0)),
                      b.get("notes")))
                desc = f"NEW: {b['name']} — ${float(b['amount']):.2f}/{b['cycle']}"
                conn.execute("INSERT INTO benefit_changes (card_id, change_type, description) VALUES (?, ?, ?)",
                             (cid, "add_benefit",
                              f"{desc}. Evidence: {ch['evidence']}"))
                applied.append(desc)

        except Exception as e:
            print(f"Failed to apply change {ch}: {e}", file=sys.stderr)

    conn.commit()
    return applied


def send_summary_to_manan(applied, llm_summary, cards_checked):
    """Schedule outbox message summarizing the refresh.

    Always sent after a real (non-dry-run) refresh — whether changes happened
    or not — so the user has visibility that the monthly refresh ran. A silent
    refresh is worse than a short 'no changes' confirmation.
    """
    if applied:
        lines = [f"🧾 Card benefits refresh — {len(applied)} change(s) applied:"]
        lines.append("")
        for a in applied:
            lines.append(f"  • {a}")
        if llm_summary:
            lines.append("")
            lines.append(llm_summary)
    else:
        header = f"🧾 Card benefits refresh — all {cards_checked} cards verified, no changes needed."
        lines = [header]
        if llm_summary:
            lines.append("")
            lines.append(llm_summary)

    body = "\n".join(lines)
    subprocess.run([
        sys.executable, OUTBOX_CLI, "schedule",
        "--to", MANAN_PHONE,
        "--body", body,
        "--at", "now",
        "--source", "card-wallet:refresh",
        "--created-by", "card-wallet-refresh",
    ], capture_output=True, text=True, timeout=10)


def refresh(dry_run=False):
    """Main refresh entry point."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("FATAL: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    conn = get_db()
    state = current_state_for_refresh(conn)

    print(f"Refresh: {len(state['cards'])} cards, {len(state['benefits'])} active benefits", file=sys.stderr)
    print("Calling Claude with web_search...", file=sys.stderr)

    proposal = call_claude_refresh(state, api_key)
    if proposal is None:
        print("Refresh failed (LLM error or invalid JSON). See stderr above.", file=sys.stderr)
        conn.close()
        return 1

    changes = proposal.get("changes", [])

    # Validate every change before applying ANY of them — all-or-nothing on validation.
    errors = []
    for i, ch in enumerate(changes):
        ok, err = validate_change(ch, state)
        if not ok:
            errors.append(f"  change[{i}] {ch.get('action','?')}: {err}")
    if errors:
        print("Validation errors in proposal — refusing to apply:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        print("\nFull proposal:", file=sys.stderr)
        print(json.dumps(proposal, indent=2), file=sys.stderr)
        conn.close()
        return 1

    if dry_run:
        print("=== DRY RUN — no DB changes ===")
        print(json.dumps(proposal, indent=2))
        conn.close()
        return 0

    cards_checked = len(state["cards"])
    applied = apply_changes(conn, changes) if changes else []
    conn.close()

    # Always notify — silent refresh = indistinguishable from broken refresh.
    send_summary_to_manan(applied, proposal.get("summary", ""), cards_checked)

    if applied:
        print(f"Applied {len(applied)} change(s) and scheduled outbox summary.")
    elif changes:
        print("Proposal had changes but none applied successfully (all errored).")
    else:
        print(f"No changes detected across {cards_checked} cards. Summary message scheduled.")

    return 0


def main():
    if len(sys.argv) < 2:
        print("Usage: card-wallet-refresh.py [dump|dump-json|refresh] [--dry-run]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "dump":
        dump_text()
    elif cmd == "dump-json":
        dump_json()
    elif cmd == "refresh":
        dry_run = "--dry-run" in sys.argv[2:]
        sys.exit(refresh(dry_run=dry_run))
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
