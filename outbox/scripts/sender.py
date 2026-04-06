#!/usr/bin/env python3
"""
Spratt Outbox Sender — Deterministic message delivery daemon.

Polls the outbox SQLite database every 60 seconds, delivers pending messages
via imsg CLI, marks them delivered/failed. This is the ONLY process that
sends iMessages. Everything else writes to the outbox.

Managed by launchd (com.spratt.outbox-sender).
"""

import sys
import os
import time
import subprocess
import logging
from logging.handlers import RotatingFileHandler

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from outbox import OutboxDB

IMSG_BIN = "/opt/homebrew/bin/imsg"
POLL_INTERVAL = 60
LOG_FILE = os.path.expanduser("~/Library/Logs/spratt/outbox-sender.log")
OWNER_PHONE = "+1XXXXXXXXXX"  # Replace with your phone number

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [outbox-sender] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def send_via_imsg(recipient, body):
    """Send one iMessage via imsg CLI. Returns (success, error_string)."""
    if recipient.startswith("chat_guid:"):
        guid = recipient[len("chat_guid:"):]
        cmd = [IMSG_BIN, "send", "--chat-guid", guid, "--text", body, "--json"]
    elif recipient.isdigit():
        cmd = [IMSG_BIN, "send", "--chat-id", recipient, "--text", body, "--json"]
    else:
        cmd = [IMSG_BIN, "send", "--to", recipient, "--text", body, "--json"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return True, None
        # imsg puts errors on stdout, not stderr
        error = result.stderr.strip() or result.stdout.strip()
        return False, error[:200]
    except subprocess.TimeoutExpired:
        return False, "imsg timed out (15s)"
    except Exception as e:
        return False, str(e)[:200]


def process_cycle(db):
    """Run one delivery cycle. Returns (delivered, failed, pending) counts."""
    pending = db.get_pending()
    delivered = 0
    failed = 0

    for msg in pending:
        msg_id = msg["id"]
        recipient = msg["recipient"]
        body = msg["body"]
        source = msg["source"] or "?"
        retry_count = msg["retry_count"]
        max_retries = msg["max_retries"]

        success, error = send_via_imsg(recipient, body)

        if success:
            db.mark_delivered(msg_id)
            delivered += 1
            log.info(f"delivered [{msg_id}] to={recipient} src={source}")
        else:
            db.increment_retry(msg_id)
            new_retry = retry_count + 1

            if new_retry >= max_retries:
                db.mark_failed(msg_id, error)
                failed += 1
                log.error(f"FAILED [{msg_id}] to={recipient} src={source} error={error}")

                # Alert owner via outbox (the next cycle will deliver this alert)
                if recipient != OWNER_PHONE:
                    db.schedule(
                        recipient=OWNER_PHONE,
                        body=f"[outbox] Failed to deliver to {recipient} (src={source}): {error}. Message: {body[:150]}",
                        send_at="now",
                        source="system:alert",
                        created_by="sender",
                        priority=20,
                    )
            else:
                log.warning(f"retry [{msg_id}] to={recipient} attempt={new_retry}/{max_retries} error={error}")

    # Count remaining pending
    remaining = len(db.list_messages(status="pending"))

    return delivered, failed, remaining


def main():
    log.info("Starting outbox sender daemon")
    db = OutboxDB()

    single_run = "--once" in sys.argv

    if single_run:
        d, f, p = process_cycle(db)
        log.info(f"Single run: {d} delivered, {f} failed, {p} pending")
        db.close()
        sys.exit(0)

    while True:
        try:
            d, f, p = process_cycle(db)
            if d > 0 or f > 0:
                log.info(f"cycle: {d} delivered, {f} failed, {p} pending")
        except Exception as e:
            log.error(f"cycle error: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
