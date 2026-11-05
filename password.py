import json
import logging
import re

from pika.exceptions import (
    AMQPConnectionError,
    ConnectionWrongStateError,
    StreamLostError,
)

from config import (
    DB_CONNECTION_STRING,
    PASSWORD_QUEUE,
    RABBIT_CONNECTION_STRING,
    UNIQUES_QUEUE,
)
from utils import (
    DownloadManager,
    RabbitMQConsumer,
    get_db_engine,
    get_publisher,
    handle_failed_publish,
    safe_worker_callback,
    setup_logging,
)

# Identify common password markers. Supports obfuscated "pаss" (with Cyrillic 'а') and
# multiline markers like "Password Found:". We capture the remainder of the line after
# the marker for post-processing to prioritize @handles and links over
# generic tokens.
MARKER_TAIL_REGEX = re.compile(
    r"(?:p[aа]ss(?:word)?(?:\s+found)?|pw)\b([^\r\n]{0,200})",
    re.IGNORECASE | re.MULTILINE,
)

# Token patterns ordered by priority when extracting from the tail
HANDLE_REGEX = re.compile(r"@[A-Za-z0-9_.-]+")
TME_REGEX = re.compile(r"t\.me/\+?[^\s,;\)\]]+", re.IGNORECASE)
URL_REGEX = re.compile(r"https?://[^\s,;\)\]]+", re.IGNORECASE)
GENERIC_TOKEN_REGEX = re.compile(r"[^\s,;:!?)\"']+")

# Common words to ignore when falling back to a generic token
FALLBACK_STOPWORDS = {
    "full",
    "logs",
    "log",
    "combo",
    "completo",
    "dump",
    "free",
    "premium",
    "email",
    "user",
    "username",
    "password",
    "pass",
    "found",
    "leak",
    "leaks",
    "link",
    "file",
    "files",
    "telegram",
    "http",
    "https",
    "download",
    "mega",
    "mediafire",
}


def _extract_best_token(text: str) -> str:
    """Helper to extract the highest priority password token from a string."""
    if not text:
        return ""

    # Priority 1: @handle
    m = HANDLE_REGEX.search(text)
    if m:
        return m.group(0).rstrip(".,;:!?)\"'")

    # Priority 2: http(s) URL (before t.me to capture https://t.me/... fully)
    m = URL_REGEX.search(text)
    if m:
        return m.group(0).rstrip(".,;:!?)\"'")

    # Priority 3: t.me link (without https:// prefix)
    m = TME_REGEX.search(text)
    if m:
        return m.group(0).rstrip(".,;:!?)\"'")

    # Priority 4: fallback generic token not in stopwords; pick the first
    # meaningful one
    for tok in GENERIC_TOKEN_REGEX.findall(text):
        t = tok.strip().strip("-_").lower()
        if not t or t in FALLBACK_STOPWORDS:
            continue
        return tok.rstrip(".,;:!?)\"'")

    return ""


def extract_password(text_message: str) -> str:
    match = MARKER_TAIL_REGEX.search(text_message or "")
    if not match:
        return ""

    tail = match.group(1) or ""
    res = _extract_best_token(tail)
    if res:
        return res

    # If nothing on the same line, also peek into the immediate next line
    rest = (text_message or "")[match.end() :]
    if rest:
        # Skip any immediate newlines to get the next content line
        rest_after_newline = rest.lstrip("\r\n")
        next_line = (
            rest_after_newline.splitlines()[0]
            if rest_after_newline.splitlines()
            else ""
        )
        return _extract_best_token(next_line)

    return ""


def process_message(ch, method, properties, body):
    """Process message with reusable publisher and heartbeat handling."""
    # Use shared publisher instead of creating new one
    publisher = get_publisher(RABBIT_CONNECTION_STRING, PASSWORD_QUEUE)

    payload = json.loads(body)
    file_key = payload["file_key"]
    file_id = payload["id"]
    saved_name = payload["saved_file_name"]

    logging.info(f"Processing record id={file_id}, file_key={file_key}")

    # Process heartbeat before long DB operation
    try:
        if hasattr(ch, "connection") and ch.connection and not ch.connection.is_closed:
            ch.connection.process_data_events(time_limit=0)
    except Exception as e:
        logging.debug(f"Heartbeat processing error (non-fatal): {e}")

    engine = get_db_engine(DB_CONNECTION_STRING)
    download_manager = DownloadManager(engine)

    # Extract password from downloaded_files
    message_text = download_manager.get_message_text(file_key) or ""
    password = extract_password(message_text)

    # Update the existing record
    download_manager.update_password(file_id, password)
    logging.info(f"Updated record id={file_id} with password")

    # Process heartbeat after DB operation, before publish
    try:
        if hasattr(ch, "connection") and ch.connection and not ch.connection.is_closed:
            ch.connection.process_data_events(time_limit=0)
    except Exception as e:
        logging.debug(f"Heartbeat processing error (non-fatal): {e}")

    # Publish the message to the PASSWORD_QUEUE
    if publisher.publish(
        {"id": file_id, "saved_file_name": saved_name, "password": password}
    ):
        logging.info(f"Published password record id={file_id} to '{PASSWORD_QUEUE}'")
    else:
        logging.error(
            f"Failed to publish password record id={file_id} to '{PASSWORD_QUEUE}'"
        )
        # Save payload to a dead-letter file for later processing
        handle_failed_publish(
            {"id": file_id, "saved_file_name": saved_name, "password": password}
        )

    try:
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except (StreamLostError, ConnectionWrongStateError, AMQPConnectionError) as e:
        logging.error(f"Failed to acknowledge message for id={file_id}: {e}")


callback = safe_worker_callback(process_message)


def main():
    """Main function using RabbitMQConsumer."""
    setup_logging()
    logging.info(f"Starting password extractor for queue '{UNIQUES_QUEUE}'")

    consumer = RabbitMQConsumer(
        url=RABBIT_CONNECTION_STRING, queue=UNIQUES_QUEUE, callback=callback
    )
    consumer.start()


if __name__ == "__main__":
    main()
