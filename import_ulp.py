#!/usr/bin/env python3
"""
Consume files from the combo queue, detect whether each file is ULP
(URI:USER:PASS) or combo (USER:PASS), import into ClickHouse accordingly,
and mark as TO_DELETE.

Usage:
    python import_ulp.py                        # consume one message, process, exit
    python import_ulp.py --loop                 # keep consuming messages in a loop
    python import_ulp.py --file <path>          # import a single file directly
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time

from sqlalchemy import text

import config
from utils import DownloadManager, get_db_engine, get_rabbit_connection, setup_logging

logger = logging.getLogger(__name__)

# Regex for ULP format: URI<sep>USER:PASS where <sep> is : or whitespace
# URL may optionally include a scheme (e.g. https://host/path) or be
# scheme-less (e.g. host/path).  The optional non-capturing group handles
# the scheme:// prefix so that the following [^:]+ captures only the
# host+path portion.
ULP_LINE_RE = re.compile(r"^((?:[a-zA-Z][a-zA-Z0-9+.-]*://)?[^:\s]+)[:\s]([^:]+):(.*)$")
# Regex for combo format: USER:PASS
COMBO_LINE_RE = re.compile(r"^([^:]+):([^:]+)$")

SAMPLE_LINES = 1000
THRESHOLD = 0.8

# ClickHouse stage regex for ULP:  (url) <sep> (user) : (pass)
# Same logic — scheme:// is optional, [^:\s]+ captures the rest of the URL.
ULP_STAGE_REGEX = r"^((?:[a-zA-Z][a-zA-Z0-9+.-]*://)?[^:\s]+)[:\s]([^:]+):(.*)$"
# ClickHouse stage regex for combo:  () : (user) : (pass)  — empty url
COMBO_STAGE_REGEX = r"^()([^:]+):([^:]*)$"

ULP_REFINE_QUERY = """
    INSERT INTO ulp.ulp
    SELECT
        lower(if(match(url_norm, '^[a-zA-Z][a-zA-Z0-9+.-]*://'),
            extract(url_norm, '^([a-zA-Z][a-zA-Z0-9+.-]*)://'),
            '')) AS scheme,
        lower(if(match(url_norm, '^[a-zA-Z][a-zA-Z0-9+.-]*://'),
            extract(url_norm, '^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)'),
            extract(url_norm, '^([^/:?#]+)'))) AS host,
        url_norm AS url,
        user_norm AS user,
        pass_norm AS pass,
        now() AS first_seen,
        count() AS occurrences
    FROM
    (
        SELECT
            trimBoth(url, ' ') AS url_norm,
            trimBoth(user, ' ') AS user_norm,
            trimBoth(pass, ' ') AS pass_norm
        FROM ulp.ulp_stage_url
    )
    WHERE
        url_norm != ''
        AND user_norm != ''
        AND pass_norm != ''
    GROUP BY
        scheme, host, url, user, pass
"""

COMBO_REFINE_QUERY = """
    INSERT INTO ulp.ulp
    SELECT
        'combo' AS scheme,
        '' AS host,
        '' AS url,
        user_norm AS user,
        pass_norm AS pass,
        now() AS first_seen,
        count() AS occurrences
    FROM
    (
        SELECT
            trimBoth(user, ' ') AS user_norm,
            trimBoth(pass, ' ') AS pass_norm
        FROM ulp.ulp_stage_url
    )
    WHERE
        user_norm != ''
        AND pass_norm != ''
    GROUP BY
        user_norm, pass_norm
"""


def _count_matches(file_path: str, regex: re.Pattern) -> tuple[int, int, int]:
    """Return (matched, total, banner_skipped) for the given regex on the file."""
    matched = 0
    total = 0
    banner_skipped = 0
    started = False

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if started and i - banner_skipped >= SAMPLE_LINES:
                break
            line = line.strip()
            if not line:
                continue
            if not started and regex.match(line):
                started = True
            if not started:
                banner_skipped += 1
                continue
            total += 1
            if regex.match(line):
                matched += 1

    return matched, total, banner_skipped


def detect_format(file_path: str) -> str | None:
    """Detect whether file is ULP, combo, or unrecognized."""
    if not os.path.isfile(file_path):
        return None

    try:
        ulp_matched, ulp_total, ulp_banner = _count_matches(file_path, ULP_LINE_RE)
        combo_matched, combo_total, combo_banner = _count_matches(
            file_path, COMBO_LINE_RE
        )
    except Exception as e:
        logger.error("Error reading %s for format detection: %s", file_path, e)
        return None

    ulp_ratio = ulp_matched / ulp_total if ulp_total > 0 else 0
    combo_ratio = combo_matched / combo_total if combo_total > 0 else 0

    logger.info(
        "Format detection for %s: ULP %d/%d (%.0f%%)  combo %d/%d (%.0f%%)",
        file_path,
        ulp_matched,
        ulp_total,
        ulp_ratio * 100,
        combo_matched,
        combo_total,
        combo_ratio * 100,
    )

    if ulp_ratio >= THRESHOLD and ulp_ratio >= combo_ratio:
        return "ulp"
    if combo_ratio >= THRESHOLD and combo_ratio > ulp_ratio:
        return "combo"
    return None


def clickhouse_import(file_path: str, fmt: str) -> bool:
    """Run ClickHouse import for the detected format (ulp or combo)."""
    stage_regex = ULP_STAGE_REGEX if fmt == "ulp" else COMBO_STAGE_REGEX
    refine_query = ULP_REFINE_QUERY if fmt == "ulp" else COMBO_REFINE_QUERY

    try:
        # Step 1: stage
        logger.info("ClickHouse step 1/3: staging %s as %s", file_path, fmt)
        r1 = subprocess.run(
            [
                "clickhouse-client",
                "--format_regexp",
                stage_regex,
                "--format_regexp_escaping_rule",
                "Raw",
                "--format_regexp_skip_unmatched",
                "1",
                "--query",
                "INSERT INTO ulp.ulp_stage_url FORMAT Regexp",
            ],
            stdin=open(file_path, "rb"),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if r1.returncode != 0:
            logger.error(
                "ClickHouse stage failed (rc=%d): %s", r1.returncode, r1.stderr.strip()
            )
            return False
        logger.info("ClickHouse stage OK")

        # Step 2: refine
        logger.info("ClickHouse step 2/3: refining into ulp.ulp")
        r2 = subprocess.run(
            ["clickhouse-client", "--query", refine_query],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if r2.returncode != 0:
            logger.error(
                "ClickHouse refine failed (rc=%d): %s", r2.returncode, r2.stderr.strip()
            )
            return False
        logger.info("ClickHouse refine OK")

        # Step 3: truncate stage
        logger.info("ClickHouse step 3/3: truncating stage table")
        r3 = subprocess.run(
            ["clickhouse-client", "--query", "TRUNCATE TABLE ulp.ulp_stage_url"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r3.returncode != 0:
            logger.warning(
                "ClickHouse truncate stage failed (rc=%d): %s",
                r3.returncode,
                r3.stderr.strip(),
            )
        else:
            logger.info("ClickHouse truncate stage OK")

        return True

    except FileNotFoundError:
        logger.error("clickhouse-client not found. Is ClickHouse client installed?")
        return False
    except subprocess.TimeoutExpired:
        logger.error("ClickHouse import timed out")
        return False
    except Exception as e:
        logger.error("ClickHouse import error: %s", e)
        return False


def process_message(ch, method, properties, body, engine):
    """Process one message from the combo queue."""
    try:
        message = json.loads(body)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON message: %s", e)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    file_id = message.get("id")
    file_path = message.get("file_path")

    if not file_id or not file_path:
        logger.error("Missing id or file_path in message: %s", message)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    # Resolve actual file path: try given path, then DOWNLOAD_DIR, then
    # PASSWORD_DUMP_DIR
    saved_name = os.path.basename(file_path)
    candidates = [
        (file_path, "given"),
        (os.path.join(config.DOWNLOAD_DIR, saved_name), config.DOWNLOAD_DIR),
        (os.path.join(config.PASSWORD_DUMP_DIR, saved_name), config.PASSWORD_DUMP_DIR),
    ]
    resolved_path = None
    resolved_label = None
    for candidate, label in candidates:
        if os.path.isfile(candidate):
            resolved_path = os.path.realpath(candidate)
            resolved_label = label
            break

    if resolved_path is None:
        logger.error(
            "File %s not found in given path, downloads, or password dumps — discarding message",
            saved_name,
        )
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE downloaded_files SET status = 'OTHERERRORS',"
                        " error_description = 'File not found on disk' WHERE id = :id"
                    ),
                    {"id": file_id},
                )
        except Exception:
            pass
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    logger.info(
        "Processing file_id=%d resolved_path=%s [%s]",
        file_id,
        resolved_path,
        resolved_label,
    )

    # Detect format
    fmt = detect_format(resolved_path)
    if fmt is None:
        logger.warning(
            "File %s does not match ULP or combo format, skipping", resolved_path
        )
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    logger.info("Detected format: %s for %s", fmt, resolved_path)

    # Import into ClickHouse
    if not clickhouse_import(resolved_path, fmt):
        logger.error("ClickHouse import failed for %s, re-queuing", resolved_path)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        return

    # Update status to TO_DELETE
    try:
        download_manager = DownloadManager(engine)
        download_manager.update_status(file_id, "TO_DELETE")
        logger.info("Updated status to TO_DELETE for id=%d", file_id)
    except Exception as e:
        logger.error("Failed to update status for id=%d: %s", file_id, e)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        return

    ch.basic_ack(delivery_tag=method.delivery_tag)
    logger.info("Successfully processed file_id=%d", file_id)


def main():
    parser = argparse.ArgumentParser(
        description="Import ULP/combo files from combo queue into ClickHouse"
    )
    parser.add_argument(
        "--loop", action="store_true", help="Keep consuming messages in a loop"
    )
    parser.add_argument(
        "--file",
        type=str,
        help="Import a single file directly (skips RabbitMQ and DB)",
    )
    args = parser.parse_args()

    setup_logging()

    # --- single-file mode ---
    if args.file:
        file_path = os.path.realpath(args.file)
        if not os.path.isfile(file_path):
            logger.error("File not found: %s", file_path)
            return 1

        logger.info("Direct import of %s", file_path)
        fmt = detect_format(file_path)
        if fmt is None:
            logger.warning(
                "File %s does not match ULP or combo format, skipping", file_path
            )
            return 0

        logger.info("Detected format: %s for %s", fmt, file_path)
        if clickhouse_import(file_path, fmt):
            os.remove(file_path)
            logger.info("Deleted %s after successful import", file_path)
            return 0
        return 1

    # --- RabbitMQ modes ---
    engine = get_db_engine()

    queue = config.COMBO_QUEUE

    def callback(ch, method, properties, body):
        process_message(ch, method, properties, body, engine)

    if args.loop:
        logger.info("Starting import worker in loop mode on '%s'", queue)
        while True:
            try:
                conn = get_rabbit_connection(
                    config.RABBIT_CONNECTION_STRING, heartbeat=600
                )
                channel = conn.channel()
                channel.queue_declare(queue=queue, durable=True)
                channel.basic_qos(prefetch_count=1)
                channel.basic_consume(queue=queue, on_message_callback=callback)
                channel.start_consuming()
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt, shutting down")
                break
            except Exception as e:
                logger.error("Connection error: %s, reconnecting in 5s", e)
                time.sleep(5)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    else:
        logger.info("Starting import worker in single-shot mode on '%s'", queue)
        conn = get_rabbit_connection(config.RABBIT_CONNECTION_STRING, heartbeat=600)
        channel = conn.channel()
        channel.queue_declare(queue=queue, durable=True)
        channel.basic_qos(prefetch_count=1)

        for method_frame, properties, body in channel.consume(
            queue, inactivity_timeout=10
        ):
            if method_frame is None:
                logger.info("No messages in queue, exiting")
                break
            callback(channel, method_frame, properties, body)
            channel.cancel()
            break

        conn.close()


if __name__ == "__main__":
    sys.exit(main())
