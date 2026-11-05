import hashlib
import json
import logging
import os
import pty
import re
import shutil
import smtplib
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import dns.resolver
import pika
from pika import BasicProperties
from sqlalchemy import create_engine, text
from telethon import errors
from telethon.tl.types import DocumentAttributeFilename
from tqdm import tqdm

import config

logger = logging.getLogger(__name__)

# Link extraction constants
_URL_RE = re.compile(r"https?://[^\s<>'\"),;]+", re.IGNORECASE)
_GOFILE_BARE_RE = re.compile(
    r"(?<![/\w])gofile\.io/[^\s<>'\"),;]+",
    re.IGNORECASE,
)
_TELEGRAM_BARE_RE = re.compile(
    r"(?<![/\w])(?:t\.me|telegram\.me)/[^\s<>'\"),;]+",
    re.IGNORECASE,
)
_GOFILE_HOSTS = {"gofile.io", "www.gofile.io"}
_TELEGRAM_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}

IGNORED_EXTENSIONS = [
    ".webp",
    ".webm",
    ".mp4",
    ".png",
    ".mp3",
    ".exe",
    ".gif",
    ".pdf",
    ".opk",
    ".tgs",
    ".ipa",
    ".m4v",
    ".mkv",
    ".apk",
    ".svb",
    ".mov",
    ".xlsx",
    ".m3u8",
]


def parse_ignored_extensions(config_value):
    if isinstance(config_value, str):
        raw_items = config_value.split(",")
    else:
        try:
            raw_items = list(config_value)
        except TypeError:
            raw_items = []
    return {
        ext if ext.startswith(".") else f".{ext}"
        for ext in (item.strip().lower() for item in raw_items)
        if ext
    }


IGNORED_EXTENSIONS_SET = parse_ignored_extensions(IGNORED_EXTENSIONS)

URI_QUEUE_MAP = {
    "GOFILE": config.URI_GOFILE_QUEUE,
    "TELEGRAM": config.URI_TELEGRAM_QUEUE,
    "GENERIC": config.URI_GENERIC_QUEUE,
}

IGNORED_PATH_PARTS = {"cookie"}


def path_contains_ignored_part(path: Path) -> bool:
    """True if any path component (case-insensitive) matches IGNORED_PATH_PARTS."""
    return any(part.lower() in IGNORED_PATH_PARTS for part in path.parts)


def _strip_trailing(url: str) -> str:
    return url.strip().rstrip(".,;:!?)\"]'}>")


def classify_uri(uri: str) -> str:
    """Return 'GOFILE', 'TELEGRAM', or 'GENERIC' based on URI content."""
    host = (urlparse(uri).hostname or "").rstrip(".").lower()
    if host in _GOFILE_HOSTS:
        return "GOFILE"
    if host in _TELEGRAM_HOSTS:
        return "TELEGRAM"
    return "GENERIC"


def is_joinable_telegram_link(uri: str) -> bool:
    """Return True if URI is a joinable Telegram channel/group link.

    Valid: t.me/username, t.me/+hash, t.me/joinchat/hash
    Invalid: t.me/username/123 (message), t.me/c/ID (private msg),
             t.me/c/ID/123 (private msg), t.me/botname (bot)
    """
    host = (urlparse(uri).hostname or "").rstrip(".").lower()
    if host not in _TELEGRAM_HOSTS:
        return False
    path = urlparse(uri).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    # t.me/+hash or t.me/joinchat/hash
    if segments[0] in ("+", "joinchat"):
        return len(segments) <= 2
    # t.me/c/... is always a private channel message link
    if segments[0] == "c":
        return False
    # t.me/username -> 1 segment OK; t.me/username/N -> message link
    return len(segments) == 1


def extract_links(text_message: str | None) -> list[str]:
    """Return deduplicated list of normalized links found in *text_message*."""
    if not text_message:
        return []

    seen: set[str] = set()
    links: list[str] = []

    for match in _URL_RE.finditer(text_message):
        url = _strip_trailing(match.group(0))
        key = url.lower()
        if key not in seen:
            seen.add(key)
            links.append(url)

    for match in _GOFILE_BARE_RE.finditer(text_message):
        url = f"https://{_strip_trailing(match.group(0))}"
        key = url.lower()
        if key not in seen:
            seen.add(key)
            links.append(url)

    for match in _TELEGRAM_BARE_RE.finditer(text_message):
        url = f"https://{_strip_trailing(match.group(0))}"
        key = url.lower()
        if key not in seen:
            seen.add(key)
            links.append(url)

    return links


def setup_logging(level=logging.INFO):

    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s")
    logging.getLogger("telethon").setLevel(logging.ERROR)
    logging.getLogger("pika").setLevel(logging.WARNING)


def clean_string(s):
    """Remove null bytes, strip whitespace, and ensure string type."""
    if s is None:
        return ""
    return str(s).replace("\x00", "").strip()


def get_rabbit_connection(url, heartbeat=0, blocked_connection_timeout=300):
    """Create a RabbitMQ connection with heartbeats disabled to prevent
    ConnectionResetError during long-running blocking tasks.

    Args:
        url: RabbitMQ connection URL
        heartbeat: Heartbeat interval (0 disables heartbeats)
        blocked_connection_timeout: Timeout for blocked connections (default: 300s)
    """
    parsed = urllib.parse.urlparse(url)
    vhost = urllib.parse.unquote(parsed.path[1:]) if parsed.path else "/"

    params = pika.ConnectionParameters(
        host=parsed.hostname,
        port=parsed.port or 5672,
        virtual_host=vhost,
        credentials=(
            pika.PlainCredentials(parsed.username, parsed.password)
            if parsed.username
            else None
        ),
        heartbeat=heartbeat,
        blocked_connection_timeout=blocked_connection_timeout,
        socket_timeout=10,
    )

    return pika.BlockingConnection(params)


class RabbitMQPublisher:
    """Thread-safe RabbitMQ publisher with connection pooling and automatic reconnection.

    This class maintains a persistent connection and channel that can be reused across
    multiple publish operations, significantly reducing overhead and connection issues.
    """

    # Class-level connection pool (shared across instances to same URL)
    _connection_pool = {}
    _pool_lock = __import__("threading").Lock()

    def __init__(self, url, queue, heartbeat=0):
        self.url = url
        self.queue = queue
        self.heartbeat = heartbeat
        self.connection = None
        self.channel = None
        self._connect()

    def _connect(self):
        """Establish connection and channel to RabbitMQ with optimized settings."""
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                # Usa heartbeat lungo per ridurre overhead
                self.connection = get_rabbit_connection(
                    self.url, heartbeat=self.heartbeat
                )
                self.channel = self.connection.channel()
                self.channel.queue_declare(queue=self.queue, durable=True)
                # Abilita confirm mode per delivery acknowledgment
                self.channel.confirm_delivery()
                logger.info(
                    "Connected to RabbitMQ queue '%s' (heartbeat=%ds)",
                    self.queue,
                    self.heartbeat,
                )
                return
            except Exception as e:
                logger.error(
                    "Failed to connect to RabbitMQ (attempt %d/%d): %s",
                    attempt + 1,
                    max_attempts,
                    e,
                )
                if attempt < max_attempts - 1:
                    time.sleep(2**attempt)  # Exponential backoff
                else:
                    self.connection = None
                    self.channel = None
                    raise

    def _ensure_connection(self):
        """Verify connection health and reconnect if needed."""
        need_reconnect = False

        # Check connection status
        if (
            self.connection is None
            or self.connection.is_closed
            or self.channel is None
            or self.channel.is_closed
        ):
            need_reconnect = True
        else:
            # Connection exists - process heartbeat to keep alive
            try:
                # Non-blocking heartbeat check
                self.connection.process_data_events(time_limit=0)
            except Exception as e:
                logger.warning("Heartbeat processing failed, will reconnect: %s", e)
                need_reconnect = True

        if need_reconnect:
            logger.info("Reconnecting to RabbitMQ for queue '%s'", self.queue)
            # Clean up old connection
            self._safe_close()
            # Establish new connection
            self._connect()

    def publish(self, message: dict, max_retries=3, backoff_factor=2):
        """Publish message to RabbitMQ with automatic retry and reconnection.

        Args:
            message: Dictionary to publish as JSON
            max_retries: Number of retry attempts (default: 3)
            backoff_factor: Exponential backoff multiplier (default: 2)

        Returns:
            bool: True if published successfully, False otherwise
        """
        for attempt in range(max_retries):
            try:
                # Ensure connection is healthy before publishing
                self._ensure_connection()

                # Publish with delivery confirmation
                self.channel.basic_publish(
                    exchange="",
                    routing_key=self.queue,
                    body=json.dumps(message),
                    properties=BasicProperties(
                        delivery_mode=2,  # Persistent message
                        content_type="application/json",
                    ),
                    mandatory=True,
                )

                logger.debug("Published to '%s': %s", self.queue, message)
                return True

            except (
                pika.exceptions.StreamLostError,
                pika.exceptions.AMQPConnectionError,
                pika.exceptions.ChannelClosedByBroker,
                pika.exceptions.ChannelClosed,
                pika.exceptions.ConnectionClosedByBroker,
                pika.exceptions.ConnectionClosed,
            ) as e:
                logger.warning(
                    "Connection error publishing to '%s' (attempt %d/%d): %s",
                    self.queue,
                    attempt + 1,
                    max_retries,
                    e,
                )

                # Force reconnection on next attempt
                self._safe_close()

                if attempt < max_retries - 1:
                    sleep_time = backoff_factor**attempt
                    logger.info("Retrying in %d seconds...", sleep_time)
                    time.sleep(sleep_time)

            except pika.exceptions.UnroutableError as e:
                # Message couldn't be routed - questo è un errore di
                # configurazione
                logger.error("Message unroutable to queue '%s': %s", self.queue, e)
                return False

            except Exception as e:
                logger.error(
                    "Unexpected error publishing to '%s' (attempt %d/%d): %s",
                    self.queue,
                    attempt + 1,
                    max_retries,
                    e,
                )
                self._safe_close()

                if attempt < max_retries - 1:
                    time.sleep(backoff_factor**attempt)

        logger.error(
            "Failed to publish after %d attempts to '%s': %s",
            max_retries,
            self.queue,
            message,
        )
        return False

    def _safe_close(self):
        """Safely close channel and connection, suppressing errors."""
        try:
            if self.channel and not self.channel.is_closed:
                self.channel.close()
        except Exception as e:
            logger.debug("Error closing channel: %s", e)

        try:
            if self.connection and not self.connection.is_closed:
                self.connection.close()
        except Exception as e:
            logger.debug("Error closing connection: %s", e)

        self.channel = None
        self.connection = None

    def close(self):
        """Close the RabbitMQ connection and channel gracefully."""
        self._safe_close()
        logger.debug("Closed RabbitMQ connection for queue '%s'", self.queue)

    def __enter__(self):
        """Context manager entry - allows 'with RabbitMQPublisher(...) as pub:' usage."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - automatically close connection."""
        self.close()
        return False  # Don't suppress exceptions


_publishers = {}
_publishers_lock = __import__("threading").Lock()


def get_publisher(url, queue):
    """Get or create a reusable RabbitMQ publisher for the specified queue."""
    with _publishers_lock:
        if queue not in _publishers:
            _publishers[queue] = RabbitMQPublisher(url, queue)
            logger.info("Created new publisher for queue '%s'", queue)
        return _publishers[queue]


def safe_worker_callback(process_fn):
    """Wrap a process_message function with standard try/except + ack-on-error."""

    def callback(ch, method, properties, body):
        try:
            process_fn(ch, method, properties, body)
        except Exception as e:
            logger.error("Unhandled error in callback: %s", e)
            try:
                ch.basic_ack(delivery_tag=method.delivery_tag)
            except Exception:
                pass

    return callback


def cleanup_stale_downloading(download_manager, logger=logger):
    """Cleanup stale DOWNLOADING records, log outcome."""
    try:
        count = download_manager.cleanup_stale_downloads()
        if count > 0:
            logger.info("Cleaned up %d stale DOWNLOADING records.", count)
    except Exception as e:
        logger.error("Failed to cleanup stale DOWNLOADING records: %s", e)


class RabbitMQConsumer:
    """Reusable RabbitMQ consumer with automatic reconnection and error handling."""

    def __init__(
        self, url, queue, callback, prefetch_count=1, durable=True, max_messages=None
    ):
        self.url = url
        self.queue = queue
        self.callback = callback
        self.prefetch_count = prefetch_count
        self.durable = durable
        self.max_messages = max_messages
        self.processed_count = 0
        self.connection = None
        self.channel = None

    def _connect(self):
        self.connection = get_rabbit_connection(self.url, heartbeat=0)
        self.channel = self.connection.channel()
        self.channel.queue_declare(queue=self.queue, durable=self.durable)
        self.channel.basic_qos(prefetch_count=self.prefetch_count)

        if self.max_messages:
            # Wrap the callback to track message count
            original_callback = self.callback

            def wrapped_callback(ch, method, properties, body):
                try:
                    original_callback(ch, method, properties, body)
                finally:
                    self.processed_count += 1
                    logger.info(
                        "Processed %d/%d messages (batch limit)",
                        self.processed_count,
                        self.max_messages,
                    )
                    if self.processed_count >= self.max_messages:
                        logger.info(
                            "Reached configured max messages (%d), stopping consumer",
                            self.max_messages,
                        )
                        ch.stop_consuming()

            self.channel.basic_consume(
                queue=self.queue, on_message_callback=wrapped_callback
            )
        else:
            self.channel.basic_consume(
                queue=self.queue, on_message_callback=self.callback
            )

        logger.info(
            "Waiting for messages on '%s' (prefetch=%d)...",
            self.queue,
            self.prefetch_count,
        )

    def start(self):
        while True:
            try:
                self._connect()
                self.channel.start_consuming()
                if self.max_messages and self.processed_count >= self.max_messages:
                    break
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt, shutting down...")
                self.stop()
                break
            except (
                pika.exceptions.StreamLostError,
                pika.exceptions.AMQPConnectionError,
                pika.exceptions.ConnectionWrongStateError,
            ) as e:
                logger.error(
                    "RabbitMQ connection error: %s. Reconnecting in 5 seconds...", e
                )
                time.sleep(5)
            except Exception as e:
                logger.error("Unexpected error in consumer loop: %s", e, exc_info=True)
                logger.info("Restarting in 10 seconds...")
                time.sleep(10)
            finally:
                self.stop()

    def stop(self):
        try:
            if self.channel and not self.channel.is_closed:
                self.channel.stop_consuming()
            if self.connection and not self.connection.is_closed:
                self.connection.close()
        except Exception as e:
            logger.debug("Error during consumer stop: %s", e)
        self.channel = None
        self.connection = None


_db_engines = {}
_db_engine_lock = __import__("threading").Lock()


def get_db_engine(connection_string: str = config.DB_CONNECTION_STRING):
    """Get or create a SQLAlchemy engine for the given connection string.
    Engines are cached to reuse connection pools.
    """
    with _db_engine_lock:
        if connection_string not in _db_engines:
            # Configure connection arguments based on the driver type
            if "pymysql" in connection_string:
                # PyMySQL specific configuration - remove problematic
                # parameters; force session timezone to UTC so TIMESTAMP columns
                # are returned as UTC (matching Telegram API timestamps)
                connect_args = {
                    "autocommit": False,
                    "charset": "utf8mb4",
                    "init_command": "SET time_zone = '+00:00'",
                }
            else:
                # mysql-connector-python configuration (fallback)
                connect_args = {
                    "auth_plugin": "mysql_native_password",
                    "autocommit": False,
                    "charset": "utf8mb4",
                    "use_unicode": True,
                }

            _db_engines[connection_string] = create_engine(
                connection_string,
                pool_pre_ping=True,
                connect_args=connect_args,
                pool_recycle=3600,  # Recycle connections every hour
                pool_timeout=20,  # Wait up to 20 seconds for a connection
                max_overflow=0,  # Don't allow overflow connections
                echo=False,  # Set to True for SQL debugging
            )
            logger.info(
                "Created new SQLAlchemy database engine for %s", connection_string
            )
        return _db_engines[connection_string]


class DownloadManager:
    """Manages the lifecycle of downloaded files in the database."""

    def __init__(self, engine):
        self.engine = engine

    def check_duplicate(self, file_key: str) -> bool:
        """Check if a file key already exists."""
        query = text("SELECT id FROM downloaded_files WHERE file_key = :fk LIMIT 1")
        with self.engine.begin() as conn:
            return conn.execute(query, {"fk": file_key}).first() is not None

    def get_status(self, file_id: int) -> str | None:
        """Get the status of a file by its ID."""
        query = text("SELECT status FROM downloaded_files WHERE id = :id")
        with self.engine.begin() as conn:
            res = conn.execute(query, {"id": file_id}).fetchone()
            return res[0] if res else None

    def check_hash_duplicate(self, file_hash: str) -> bool:
        """Check if a file hash already exists."""
        query = text("SELECT id FROM downloaded_files WHERE hash = :hash LIMIT 1")
        with self.engine.begin() as conn:
            return conn.execute(query, {"hash": file_hash}).first() is not None

    def check_heuristic_duplicate(self, name: str, size: int) -> bool:
        """Check if a file with same name and size already exists."""
        query = text(
            "SELECT id FROM downloaded_files WHERE original_file_name = :name AND file_size = :size LIMIT 1"
        )
        with self.engine.begin() as conn:
            return conn.execute(query, {"name": name, "size": size}).first() is not None

    def get_file_id_by_key(self, file_key: str) -> int | None:
        """Get the file ID for a given file key."""
        query = text("SELECT id FROM downloaded_files WHERE file_key = :fk LIMIT 1")
        with self.engine.begin() as conn:
            row = conn.execute(query, {"fk": file_key}).first()
            return row[0] if row else None

    def get_file_id_by_hash(self, file_hash: str) -> int | None:
        """Get the file ID for a given hash."""
        query = text("SELECT id FROM downloaded_files WHERE hash = :hash LIMIT 1")
        with self.engine.begin() as conn:
            row = conn.execute(query, {"hash": file_hash}).first()
            return row[0] if row else None

    def get_file_id_by_name_size(self, name: str, size: int) -> int | None:
        """Get the file ID for a given original filename and size."""
        query = text(
            "SELECT id FROM downloaded_files WHERE original_file_name = :name AND file_size = :size LIMIT 1"
        )
        with self.engine.begin() as conn:
            row = conn.execute(query, {"name": name, "size": size}).first()
            return row[0] if row else None

    def record_duplicate_origin(
        self, file_id: int, channel_id: str, message_id: str | None = None
    ):
        """Record that a file was also observed in a different channel."""
        query = text("""
            INSERT IGNORE INTO duplicate_origins
                (downloaded_file_id, channel_id, message_id, first_seen)
            VALUES (:fid, :cid, :mid, NOW())
        """)
        with self.engine.begin() as conn:
            conn.execute(query, {"fid": file_id, "cid": channel_id, "mid": message_id})

    def start_download(
        self, file_key: str, original: str, saved: str, cid: str, msg: str, size: int
    ):
        """Mark a file as DOWNLOADING."""
        query = text("""
            INSERT INTO downloaded_files
            (file_key, original_file_name, saved_file_name, channel_id, message_text, file_size, downloaded_at, status)
            VALUES (:fk, :orig, :saved, :cid, :msg, :size, :dt, 'DOWNLOADING')
            """)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.engine.begin() as conn:
            conn.execute(
                query,
                {
                    "fk": file_key,
                    "orig": original,
                    "saved": saved,
                    "cid": cid,
                    "msg": msg,
                    "size": size,
                    "dt": now,
                },
            )

    def complete_download(self, file_key: str, file_hash: str) -> int:
        """Mark a file as DOWNLOADED and store its hash. Returns the file ID."""
        update_query = text(
            "UPDATE downloaded_files SET hash = :hash, status = 'DOWNLOADED' WHERE file_key = :fk"
        )
        id_query = text("SELECT id FROM downloaded_files WHERE file_key = :fk")

        with self.engine.begin() as conn:
            conn.execute(update_query, {"hash": file_hash, "fk": file_key})
            res = conn.execute(id_query, {"fk": file_key}).fetchone()
            return res[0] if res else None

    def register_external_download(
        self, file_key: str, original: str, saved: str, msg: str, file_hash: str
    ) -> int:
        """Register a file that was downloaded externally. Returns the file ID."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        query = text("""
            INSERT INTO downloaded_files
            (file_key, original_file_name, saved_file_name, channel_id, message_text, hash, downloaded_at, status)
            VALUES (:fk, :orig, :saved, 'external', :msg, :hash, :dt, 'DOWNLOADED')
            ON DUPLICATE KEY UPDATE status='DOWNLOADED'
        """)
        with self.engine.begin() as conn:
            conn.execute(
                query,
                {
                    "fk": file_key,
                    "orig": original,
                    "saved": saved,
                    "msg": msg,
                    "hash": file_hash,
                    "dt": now,
                },
            )
            res = conn.execute(
                text("SELECT id FROM downloaded_files WHERE file_key = :fk"),
                {"fk": file_key},
            ).fetchone()
            return res[0]

    def cleanup_download(self, file_key: str):
        """Remove a DOWNLOADING record on failure."""
        query = text("DELETE FROM downloaded_files WHERE file_key = :fk")
        with self.engine.begin() as conn:
            conn.execute(query, {"fk": file_key})

    def cleanup_stale_downloads(self) -> int:
        """Remove DOWNLOADING records older than 24 hours."""
        query = text(
            "DELETE FROM downloaded_files WHERE status = 'DOWNLOADING' AND downloaded_at < NOW() - INTERVAL 1 DAY"
        )
        with self.engine.begin() as conn:
            result = conn.execute(query)
            return result.rowcount

    def update_status(
        self,
        file_id: int,
        status: str,
        error_description: str | None = None,
        subdir_count: int | None = None,
    ):
        """Update status and optional error description for a file by its ID.
        Only updates if values have actually changed to avoid redundant timestamp updates.
        """
        # 1. Check current values
        query_get = text(
            "SELECT status, error_description, subdir_count FROM downloaded_files WHERE id = :id"
        )
        with self.engine.begin() as conn:
            row = conn.execute(query_get, {"id": file_id}).fetchone()
            if row:
                curr_status, curr_err, curr_subdirs = row
                if (
                    curr_status == status
                    and curr_err == error_description
                    and curr_subdirs == subdir_count
                ):
                    return

        # 2. Perform update
        if subdir_count is not None:
            query = text(
                "UPDATE downloaded_files SET status = :status, error_description = :error_description, subdir_count = :subdir_count WHERE id = :id"
            )
            params = {
                "status": status,
                "error_description": error_description,
                "subdir_count": subdir_count,
                "id": file_id,
            }
        else:
            query = text(
                "UPDATE downloaded_files SET status = :status, error_description = :error_description WHERE id = :id"
            )
            params = {
                "status": status,
                "error_description": error_description,
                "id": file_id,
            }

        with self.engine.begin() as conn:
            conn.execute(query, params)

    def get_message_text(self, file_key: str) -> str | None:
        """Get the message text for a given file key."""
        query = text(
            "SELECT message_text FROM downloaded_files WHERE file_key = :fk LIMIT 1"
        )
        with self.engine.begin() as conn:
            res = conn.execute(query, {"fk": file_key}).fetchone()
            return res[0] if res else None

    def update_password(self, file_id: int, password: str):
        """Update the password for a file. Only updates if the password has changed."""
        query_get = text("SELECT password FROM downloaded_files WHERE id = :id")
        with self.engine.begin() as conn:
            row = conn.execute(query_get, {"id": file_id}).fetchone()
            if row and row[0] == password:
                return

        query = text("UPDATE downloaded_files SET password = :password WHERE id = :id")
        with self.engine.begin() as conn:
            conn.execute(query, {"password": password, "id": file_id})

    def update_message_text(self, file_key: str, msg: str):
        """Update the message text for a given file key. Only updates if the text has changed."""
        query_get = text(
            "SELECT message_text FROM downloaded_files WHERE file_key = :fk"
        )
        with self.engine.begin() as conn:
            row = conn.execute(query_get, {"fk": file_key}).fetchone()
            if row and row[0] == msg:
                return

        query = text(
            "UPDATE downloaded_files SET message_text = :msg WHERE file_key = :fk"
        )
        with self.engine.begin() as conn:
            conn.execute(query, {"msg": msg, "fk": file_key})


def get_unique_saved_filename(cid: str, original_filename: str) -> str:
    """Generate a unique saved filename by adding a counter if it already exists."""
    saved = f"{cid}_{original_filename}"
    if os.path.exists(local_download_path(saved)):
        counter = 1
        while True:
            candidate = f"{cid}_{counter}_{original_filename}"
            if not os.path.exists(local_download_path(candidate)):
                return candidate
            counter += 1
    return saved


def get_multipart_regex(filename: str) -> str | None:
    """
    Create a MySQL-compatible REGEXP pattern to match other volumes of the same archive.
    Example: 'test.zip.001' -> '^test\\.zip\\.\\d+$'
    Example: 'test.part1.rar' -> '^test\\.part\\d+\\.rar$'
    """
    # Pattern 1: .partN (e.g., .part1, .part01)
    part_match = re.search(r"\.part(\d+)", filename, re.IGNORECASE)
    if part_match:
        prefix = re.escape(filename[: part_match.start()])
        suffix = re.escape(filename[part_match.end() :])
        return f"^{prefix}\\.part\\d+{suffix}$"

    # Pattern 2: trailing .001, .002 etc.
    trailing_match = re.search(r"\.(\d+)$", filename)
    if trailing_match:
        prefix = re.escape(filename[: trailing_match.start()])
        return f"^{prefix}\\.\\d+$"

    return None


def get_real_extension(filename: str) -> str:
    """Return the real extension of a file, handling multipart volume markers.

    For .partN.EXT files (e.g. file.part1.rar), returns EXT (.rar).
    For .EXT.NNN files (e.g. file.zip.001), returns EXT (.zip).
    For bare .NNN files (e.g. file.001), returns .001 as fallback.
    """
    if get_multipart_volume(filename) is None:
        return os.path.splitext(filename)[1]
    # Pattern 1: .partN.EXT — real extension is after .partN
    part_match = re.search(r"\.part\d+", filename, re.IGNORECASE)
    if part_match:
        after_part = filename[part_match.end():]
        if after_part:
            return after_part
    # Patterns 2 & 3: trailing volume marker (e.g. .zip.001, .001)
    root, ext = os.path.splitext(filename)
    _, real_ext = os.path.splitext(root)
    return real_ext if real_ext else ext


async def download_and_register_document(
    client,
    msg,
    publisher,
    download_manager,
    cid,
    ch_name,
    ignored_extensions_set,
    on_skip_callback=None,
    msg_date_str=None,
):
    """
    Core logic to download a Telegram document, verify it,
    register it in the DB, and publish to RabbitMQ.
    """
    doc = msg.document
    file_key = f"{doc.id}:{doc.access_hash}"
    caption = msg.message or msg.raw_text or ""

    # 1. Filename & Extension Check
    original = next(
        (
            a.file_name
            for a in doc.attributes
            if isinstance(a, DocumentAttributeFilename)
        ),
        None,
    ) or str(doc.id)

    # Determine the real extension, handling multipart volume markers (e.g.,
    # .001, .part1)
    normalized_ext = get_real_extension(original).lower()

    if "cookies" in original.lower() or normalized_ext in ignored_extensions_set:
        logger.info(
            "[#%s] Ignored file %s on %s (ID: %s) - contains 'cookies' or ignored extension",
            msg.id,
            original,
            ch_name,
            cid,
        )
        if on_skip_callback:
            on_skip_callback()
        return False

    saved = get_unique_saved_filename(cid, original)
    total_size = getattr(doc, "size", 0)

    # 2. Duplicate Checks
    existing_msg = download_manager.get_message_text(file_key)
    if existing_msg is not None:
        if not existing_msg and caption:
            logger.info(
                "[#%s] Updating empty message text for duplicate: %s", msg.id, original
            )
            download_manager.update_message_text(file_key, caption)

        orig_id = download_manager.get_file_id_by_key(file_key)
        if orig_id:
            download_manager.record_duplicate_origin(orig_id, cid, str(msg.id))

        logger.info(
            "[#%s] Duplicate: %s, skip on %s (ID: %s).", msg.id, original, ch_name, cid
        )
        if on_skip_callback:
            on_skip_callback()
        return False

    if download_manager.check_heuristic_duplicate(original, total_size):
        orig_id = download_manager.get_file_id_by_key(
            file_key
        ) or download_manager.get_file_id_by_name_size(original, total_size)
        if orig_id:
            download_manager.record_duplicate_origin(orig_id, cid, str(msg.id))

        logger.info(
            "[#%s] Suspected duplicate (same name and size) or currently downloading: %s, skip on %s (ID: %s).",
            msg.id,
            original,
            ch_name,
            cid,
        )
        if on_skip_callback:
            on_skip_callback()
        return False

    # 3. Mark as DOWNLOADING
    try:
        download_manager.start_download(
            file_key, original, saved, cid, caption, total_size
        )
    except Exception as e:
        logger.error("[#%s] Failed to mark file as DOWNLOADING: %s", msg.id, e)
        if on_skip_callback:
            on_skip_callback()
        return False

    # 4. Download process
    tmp = local_download_path(saved + ".part")
    final = local_download_path(saved)

    if os.path.exists(final) and os.path.getsize(final) == total_size:
        logger.info(
            "[#%s] Already downloaded: %s on %s (ID: %s)", msg.id, saved, ch_name, cid
        )
        if on_skip_callback:
            on_skip_callback()
        return False

    offset = 0
    if os.path.exists(tmp):
        offset = os.path.getsize(tmp)
        if offset >= total_size:
            logger.error(
                "[#%s] Temporary file %s is corrupted, restarting on %s (ID: %s)",
                msg.id,
                saved,
                ch_name,
                cid,
            )
            os.remove(tmp)
            offset = 0
        else:
            logger.info(
                "[#%s] Resuming %s from %s on %s (ID: %s)",
                msg.id,
                saved,
                format_bytes(offset),
                ch_name,
                cid,
            )
    else:
        logger.info(
            "[#%s] Starting download of %s (total size: %s) on %s (ID: %s)",
            msg.id,
            saved,
            format_bytes(total_size),
            ch_name,
            cid,
        )

    progress_bar = tqdm(
        total=total_size,
        initial=offset,
        unit="B",
        unit_scale=True,
        desc=f"{ch_name}:{saved}" + (f" ({msg_date_str})" if msg_date_str else ""),
        leave=True,
        dynamic_ncols=True,
    )

    try:
        with open(tmp, "ab") as f:
            async for chunk in client.iter_download(
                file=doc, offset=offset, request_size=2097152
            ):
                current_size = os.path.getsize(tmp)
                if current_size >= total_size:
                    break
                f.write(chunk)
                progress_bar.update(len(chunk))
    except errors.FileReferenceExpiredError:
        logger.error(
            "[#%s] File reference expired for %s on %s (ID: %s)",
            msg.id,
            saved,
            ch_name,
            cid,
        )
        if os.path.exists(tmp):
            os.remove(tmp)
        download_manager.cleanup_download(file_key)
        if on_skip_callback:
            on_skip_callback()
        return False
    except Exception as e:
        logger.error(
            "[#%s] Error during download of %s on %s (ID: %s): %s",
            msg.id,
            saved,
            ch_name,
            cid,
            str(e),
        )
        download_manager.cleanup_download(file_key)
        if on_skip_callback:
            on_skip_callback()
        return False
    finally:
        progress_bar.close()

    # 5. Verify and Move
    if os.path.exists(tmp):
        if os.path.getsize(tmp) > total_size:
            with open(tmp, "ab") as f:
                f.truncate(total_size)

    if os.path.exists(tmp) and os.path.getsize(tmp) >= total_size:
        os.replace(tmp, final)
        logger.info("[#%s] Completed: %s on %s (ID: %s)", msg.id, saved, ch_name, cid)
    else:
        logger.error(
            "[#%s] Download incomplete for %s on %s (ID: %s)",
            msg.id,
            saved,
            ch_name,
            cid,
        )
        download_manager.cleanup_download(file_key)
        if on_skip_callback:
            on_skip_callback()
        return False

    # 6. Hash & Registration
    file_path = local_download_path(saved)
    file_hash = calculate_file_hash(file_path)
    if not file_hash:
        logger.error(
            "Hash calculation failure for %s on %s (ID: %s)", saved, ch_name, cid
        )
        if os.path.exists(file_path):
            os.remove(file_path)
        download_manager.cleanup_download(file_key)
        if on_skip_callback:
            on_skip_callback()
        return False

    if download_manager.check_hash_duplicate(file_hash):
        logger.info("Duplicate hash found for %s on %s (ID: %s)", saved, ch_name, cid)
        orig_id = download_manager.get_file_id_by_hash(file_hash)
        if orig_id:
            download_manager.record_duplicate_origin(orig_id, cid, str(msg.id))
        if os.path.exists(file_path):
            os.remove(file_path)
        download_manager.cleanup_download(file_key)
        if on_skip_callback:
            on_skip_callback()
        return False

    new_id = download_manager.complete_download(file_key, file_hash)

    # 7. RabbitMQ
    payload = {"id": new_id, "file_key": file_key, "saved_file_name": saved}
    if publisher.publish(payload):
        logger.info("Published to RabbitMQ: %s on %s (ID: %s)", payload, ch_name, cid)
    else:
        handle_failed_publish(
            payload, os.path.join(config.DOWNLOAD_DIR, "failed_publish.json")
        )

    return True


class LinkManager:
    """Manages the lifecycle of discovered links in the database."""

    def __init__(self, engine):
        self.engine = engine

    def get_status(self, uri: str) -> str | None:
        """Get the status of a link by its URI."""
        import hashlib

        uri_hash = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        query = text(
            "SELECT status FROM discovered_links WHERE uri_hash = :uri_hash LIMIT 1"
        )
        try:
            with self.engine.begin() as conn:
                row = conn.execute(query, {"uri_hash": uri_hash}).fetchone()
                return row.status if row else None
        except Exception as e:
            logger.error("Failed to get link status for %s: %s", uri, e)
            return None

    def update_status(
        self, uri: str, status: str, error_description: str | None = None
    ):
        """Update the status and optional error description of a link by its URI if they changed."""
        import hashlib

        uri_hash = hashlib.sha256(uri.encode("utf-8")).hexdigest()

        # Normalize None to empty string for consistent comparison if needed,
        # but MySQL NULL != '', so we keep None as is.

        # 1. Check current state to avoid redundant updates
        current_query = text(
            "SELECT status, error_description FROM discovered_links WHERE uri_hash = :uri_hash LIMIT 1"
        )
        with self.engine.begin() as conn:
            row = conn.execute(current_query, {"uri_hash": uri_hash}).fetchone()
            if row:
                # Handle comparison: if both are None or both are equal
                curr_status = row[0]
                curr_err = row[1]
                if curr_status == status and curr_err == error_description:
                    return  # No change needed

        # 2. Perform update only if values differ
        update_query = text("""
            UPDATE discovered_links
            SET status = :status,
                error_description = :error_description,
                updated_at = NOW()
            WHERE uri_hash = :uri_hash
        """)
        with self.engine.begin() as conn:
            conn.execute(
                update_query,
                {
                    "status": status,
                    "error_description": error_description,
                    "uri_hash": uri_hash,
                },
            )


def _check_7z_password_fast(file_path: Path, password: str) -> bool:
    """Quickly verify if a password is correct for a 7z archive.

    Runs 7z test with a short timeout. Wrong passwords are detected at
    the header decryption stage (nearly instant). Returns False only if
    7z explicitly reports 'Wrong password' within the timeout.
    """
    cmd = ["7z", "t", f"-p{password}", file_path.as_posix()]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        output = result.stdout + result.stderr
        if "Wrong password" in output:
            return False
        return True
    except subprocess.TimeoutExpired:
        return True
    except FileNotFoundError:
        raise RuntimeError("7z not installed: Please install p7zip-full")


def decompress_file(
    file_path: Path, password: str, dest_root: str
) -> tuple[Path, int | None]:
    filename = file_path.name
    ext = get_real_extension(filename)

    suffix = ext.lower()

    # Use a clean base name for the destination directory (remove archive
    # extension if present)
    base_name = file_path.stem
    if suffix in (".zip", ".rar", ".7z") and base_name.lower().endswith(suffix):
        base_name = base_name[: -len(suffix)]

    dest_dir = Path(dest_root) / base_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Allow decompression if it's a known archive extension OR if it's a
    # multipart volume
    if suffix not in (".zip", ".rar", ".7z") and get_multipart_volume(suffix) is None:
        raise ValueError(f"Unsupported archive type: {suffix}")

    # -y: assume yes, -bb3: ultra verbose progress, -o: output dir
    cmd = ["7z", "x", "-y", "-bb3", f"-o{dest_dir.as_posix()}"]
    if password:
        cmd += [f"-p{password}"]
    else:
        cmd += ["-p"]

    cmd += [file_path.as_posix()]

    logging.info(f"Running command: {' '.join(cmd)}")
    volume_count = None

    if not shutil.which("7z"):
        raise RuntimeError("7z not installed: Please install p7zip-full")

    if not _check_7z_password_fast(file_path, password):
        logger.info("Fast password check failed for %s", filename)
        raise RuntimeError("Incorrect password")

    master_fd = -1
    try:
        pid, master_fd = pty.fork()
    except OSError as e:
        raise RuntimeError(f"Failed to create pseudo-terminal: {e}")

    if pid == 0:
        try:
            os.execvp("7z", cmd)
        except Exception:
            os._exit(1)

    try:
        output_data = bytearray()
        last_pct = -1
        buf = b""

        while True:
            try:
                chunk = os.read(master_fd, 65536)
            except InterruptedError:
                continue
            except OSError:
                break
            if not chunk:
                break

            output_data.extend(chunk)

            full = buf + chunk
            lines = full.split(b'\n')
            buf = lines.pop()

            for raw_line in lines:
                if not raw_line.strip():
                    continue

                parts = raw_line.split(b'\r')
                output_text = None
                for part in parts:
                    if not part.strip():
                        continue
                    match = re.search(rb'(\d+)%', part)
                    if match:
                        pct = int(match.group(1))
                        if pct >= last_pct + 5 or pct == 100:
                            logger.info(
                                f"Decompression progress: {pct}% for {filename}"
                            )
                            last_pct = pct
                    else:
                        output_text = (
                            part.decode("utf-8", errors="replace").strip()
                        )

                if output_text:
                    logger.info(f"Decompression output: {output_text}")
                    if "Volumes =" in output_text:
                        match = re.search(r"Volumes\s*=\s*(\d+)", output_text)
                        if match:
                            volume_count = int(match.group(1))

        _, status = os.waitpid(pid, 0)
        returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1

        output_str = output_data.decode("utf-8", errors="replace")

        if returncode != 0:
            error_msg = output_str.strip() or "Unknown error"
            if "wrong password" in error_msg.lower():
                if last_pct <= 0:
                    raise RuntimeError("Incorrect password")
                logger.warning(
                    "Partial extraction for %s (exit=%d): password opened archive but "
                    "sub-items had wrong password. Extracted files kept on disk, "
                    "will retry with matrix password.",
                    filename, returncode,
                )
                raise RuntimeError("Incorrect password")
            elif "No such file" in error_msg or "cannot open" in error_msg.lower():
                raise RuntimeError(f"File not found: {file_path}")
            else:
                logger.warning(
                    "Decompression completed with non-zero exit code for %s (exit=%d). "
                    "Extracted files are kept.",
                    filename, returncode,
                )
    finally:
        if master_fd >= 0:
            try:
                os.close(master_fd)
            except OSError:
                pass

    _cleanup_extracted(dest_dir)

    logger.info(f"decompress_file returning: {dest_dir}, {volume_count}")
    return dest_dir, volume_count


def _cleanup_extracted(dest_dir: Path):
    _exclude_names = {"cookie"}
    _exclude_exts = {".exe", ".jpg", ".jpeg", ".png"}
    entries = sorted(dest_dir.rglob("*"), key=lambda p: (not p.is_file(), str(p)))
    for entry in entries:
        if not entry.exists():
            continue
        try:
            if any(name in entry.name.lower() for name in _exclude_names):
                logger.info("Removing extracted entry (name match): %s", entry)
                if entry.is_file():
                    entry.unlink()
                else:
                    shutil.rmtree(entry)
            elif entry.is_file() and entry.suffix.lower() in _exclude_exts:
                logger.info("Removing extracted entry (ext match): %s", entry)
                entry.unlink()
        except Exception:
            pass


def calculate_file_hash(file_path: str) -> str:
    """Calculate the SHA-256 hash of a file."""
    try:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception as e:
        logger.error(f"Failed to calculate hash for {file_path}: {e}")
        return ""


def parse_system_info(file_path: Path):
    """Parse UserInformation.txt or System.txt to extract Install Date, Local Date, and Country.
    Returns (install_date, local_date, country).
    """
    encodings = ["utf-8", "latin-1", "cp1252"]
    content = None

    for encoding in encodings:
        try:
            with open(file_path, "r", encoding=encoding) as f:
                content = f.read()
                break
        except (UnicodeDecodeError, Exception):
            continue

    if content is None:
        return None, None, None

    install_date = None
    local_date = None
    country = None

    try:
        install_date_match = re.search(
            r"Install Date: (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})", content
        )
        local_date_match = re.search(
            r"Local Date: (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})", content
        )
        country_match = re.search(r"^Country:\s*([A-Z]{2})$", content, re.MULTILINE)

        if install_date_match:
            date_str = install_date_match.group(1)
            try:
                parsed_date = datetime.strptime(date_str, "%d.%m.%Y %H:%M:%S")
                install_date = parsed_date.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        if local_date_match:
            date_str = local_date_match.group(1)
            try:
                parsed_date = datetime.strptime(date_str, "%d.%m.%Y %H:%M:%S")
                local_date = parsed_date.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        if country_match:
            country = country_match.group(1).strip()

    except Exception as e:
        logger.debug("Error parsing content of %s: %s", file_path, e)
        return None, None, None

    return install_date, local_date, country


def local_download_path(saved_file_name: str) -> str:
    """Return the full local filesystem path for a downloaded file name.

    Keeps path construction centralized so callers don't need to concatenate
    DOWNLOAD_DIR themselves.
    """
    try:
        return os.path.join(config.DOWNLOAD_DIR, saved_file_name)
    except Exception:
        # Fallback: just return the filename
        return saved_file_name


def password_dump_path(saved_file_name: str) -> str:
    """Return the full local filesystem path for a password dump file."""
    return os.path.join(config.PASSWORD_DUMP_DIR, saved_file_name)


def check_mx_record(domain, logger):
    """Check if a domain has a valid MX record."""
    try:
        answers = dns.resolver.resolve(domain, "MX")
        if answers:
            logger.info(f"Domain: {domain} - MX record found: {answers[0].exchange}")
            return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.Timeout):
        logger.info(f"Domain: {domain} - No MX record found or domain does not exist")
        return False
    except Exception as e:
        logger.error(f"Domain: {domain} - Error in DNS query: {e!s}")
        return False


# Pulizia delle stringhe


def calculate_string_hash(text: str) -> str:
    """Calculate the SHA-256 hash of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def uri_hash(uri: str) -> str:
    """Calculate hash for a normalized URI."""
    return calculate_string_hash(normalize_uri(uri))


def handle_failed_publish(payload: dict, filename: str = "failed_publish.json"):
    """Log a failed publish attempt to a JSON file."""
    try:
        with open(filename, "a", encoding="utf-8") as f:
            json.dump(payload, f)
            f.write("\n")
    except Exception as e:
        logger.error("Failed to log failed publish to %s: %s", filename, e)


def load_active_channels(engine):
    """Load active channels from the channel_list table."""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("SELECT channel_id FROM channel_list WHERE status = 'ACTIVE'")
            ).fetchall()
            return [int(row.channel_id) for row in result]
    except Exception as e:
        logger.error(f"Failed to load active channels from database: {e}")
        return []


def get_normalized_channel_id(msg) -> str | None:
    """Return a stable positive channel id for storage."""
    peer = getattr(msg, "peer_id", None)
    if hasattr(peer, "channel_id"):
        return str(peer.channel_id)

    chat_id = getattr(msg, "chat_id", None)
    if chat_id is None:
        return None

    try:
        return str(abs(int(chat_id)))
    except (ValueError, TypeError):
        return str(chat_id)


def get_multipart_volume(filename: str) -> int | None:
    """
    Determines if a filename belongs to a multipart archive and returns the volume index.
    Returns None if not a multipart file.

    Examples:
    - file.zip.001 -> 1
    - file.part1.rar -> 1
    - file.part2.rar -> 2
    - file.zip.002 -> 2
    - file.7z.001 -> 1
    - file.7z.1 -> 1
    """
    # Pattern 1: .partN (e.g., .part1, .part01)
    part_match = re.search(r"\.part(\d+)", filename, re.IGNORECASE)
    if part_match:
        return int(part_match.group(1))

    # Pattern 2: .zip.N, .7z.N, .rar.N (e.g., .zip.001, .7z.1)
    archive_part_match = re.search(
        r"\.(?:zip|7z|rar)\.(\d+)$", filename, re.IGNORECASE
    )
    if archive_part_match:
        return int(archive_part_match.group(1))

    # Pattern 3: Generic 3-digit volume markers like .001, .002 (from split
    # or similar tools). 4+ digit suffixes (e.g. .2024, report.1234) are
    # NOT treated as multipart to avoid false positives.
    generic_part_match = re.search(r"\.(\d{3})$", filename)
    if generic_part_match:
        return int(generic_part_match.group(1))

    return None


def format_bytes(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def validate_email(email):
    """Basic email validation."""
    if not email:
        return False
    return bool(re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email))


def normalize_and_filter_emails(email_iterable):
    """
    Normalize emails to lowercase, clean strings, validate basic format and remove duplicates.
    Returns a list preserving stable order (first occurrence wins).
    """
    seen = set()
    result = []
    for e in email_iterable:
        if not e:
            continue
        clean_e = clean_string(e).lower()
        # basic validation
        if not validate_email(clean_e):
            logger.debug("Skipping invalid email: %s", e)
            continue
        if clean_e in seen:
            logger.debug("Duplicate email skipped: %s", clean_e)
            continue
        seen.add(clean_e)
        result.append(clean_e)
    return result


def normalize_uri(uri: str) -> str:
    """Normalize a URI for consistent hashing and storage."""
    try:
        parsed = urlsplit(uri)
        if not parsed.scheme or not parsed.netloc:
            return uri.lower()
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower().rstrip(".")
        # Always use https for Telegram URIs
        if netloc in _TELEGRAM_HOSTS:
            scheme = "https"
        return urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return uri.lower()


def extract_domain(uri):
    """Extract domain from URI."""
    try:
        parsed = urlparse(uri)
        return f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        return uri


def parse_soft_passwords(text, max_blocks=1000, max_block_size=10_000):
    """
    Parse blocks that look like:
    SOFT: ...\nURL: https://...\nUSER: user\nPASS: pass

    Returns a list of tuples (url, user, pass). Limits the number
    of blocks and per-block size to avoid DoS with huge files.
    """
    results = []
    lines = text.splitlines()
    block = []
    blocks_found = 0
    for ln in lines:
        if ln.strip().upper().startswith("SOFT:"):
            if block:
                candidate = "\n".join(block)
                if len(candidate) <= max_block_size:
                    url = user = pwd = None
                    for bline in block:
                        b = bline.strip()
                        if b.upper().startswith("URL:"):
                            url = b[4:].strip()
                        elif b.upper().startswith("USER:"):
                            user = b[5:].strip()
                        elif b.upper().startswith("PASS:"):
                            pwd = b[5:].strip()
                    if url and user and pwd:
                        results.append((url, user, pwd))
                        blocks_found += 1
                        if blocks_found >= max_blocks:
                            break
            block = [ln]
        else:
            if block is not None:
                block.append(ln)
    if block and blocks_found < max_blocks:
        candidate = "\n".join(block)
        if len(candidate) <= max_block_size:
            url = user = pwd = None
            for bline in block:
                b = bline.strip()
                if b.upper().startswith("URL:"):
                    url = b[4:].strip()
                elif b.upper().startswith("USER:"):
                    user = b[5:].strip()
                elif b.upper().startswith("PASS:"):
                    pwd = b[5:].strip()
            if url and user and pwd:
                results.append((url, user, pwd))
    return results


def extract_password_patterns(content):
    """
    Extract host/password pairs using standard and fallback patterns.
    Returns a list of tuples (host, password).
    """
    results = []

    # Pattern 1: Host: ... Password: ...
    # Must not capture `Soft:` (block separator) as password, which happens when
    # `Password:` is followed by whitespace-only content (re.DOTALL allows `.*?`
    # to cross line boundaries and capture the next block's header).
    host_password_matches = re.findall(
        r"Host:\s*(https?://[^\s]+)\s*.*?Password:\s*(?!(?:Soft|Host|Login|Password):)([^\s]+)",
        content,
        re.DOTALL,
    )
    for host, pwd in host_password_matches:
        results.append((host, pwd))

    # Pattern 2: Fallback regex for password patterns (if no Host:Password
    # found)
    if not host_password_matches:
        password_matches = re.findall(
            r"(?i)(?:password|pass|pwd|key)[\s:-]+([^\s]+)(?:\s+(https?://[^\s]+))?",
            content,
        )
        for match in password_matches:
            pwd = match[0]
            uri = match[1] if len(match) > 1 and match[1] else None
            if uri:
                results.append((uri, pwd))

    return results


def send_simple_email(recipient: str, subject: str, body: str):
    """Send a simple text email using SMTP configuration from config.py."""
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = f"{config.SENDER_NAME} <{config.SENDER_EMAIL}>"
        msg["To"] = recipient
        msg["Date"] = formatdate(localtime=True)
        msg["Message-Id"] = make_msgid(domain="kirurusec.com")

        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Simple email sent to %s", recipient)
        return True
    except Exception as e:
        logger.error("Failed to send simple email to %s: %s", recipient, e)
        return False


class StateManager:
    """Manages key-value state in a database table."""

    def __init__(self, engine, table_name, key_column, value_column):
        self.engine = engine
        self.table_name = table_name
        self.key_column = key_column
        self.value_column = value_column

    def load_all(self) -> dict:
        """Load all key-value pairs from the table."""
        query = text(
            f"SELECT {self.key_column}, {self.value_column} FROM {self.table_name}"
        )
        with self.engine.begin() as conn:
            result = conn.execute(query).fetchall()
            return {str(row[0]): row[1] for row in result}

    def get(self, key, default=None):
        """Get a value for a key."""
        query = text(
            f"SELECT {self.value_column} FROM {self.table_name} WHERE {
                self.key_column
            } = :key"
        )
        with self.engine.begin() as conn:
            result = conn.execute(query, {"key": str(key)}).fetchone()
            return result[0] if result else default

    def save(self, key, value):
        """Upsert a single key-value pair. Only updates if the value has changed."""
        # 1. Check if value is already the same to avoid redundant UPDATE (and
        # timestamp change)
        get_query = text(
            f"SELECT {self.value_column} FROM {self.table_name} WHERE {
                self.key_column
            } = :key"
        )
        with self.engine.begin() as conn:
            row = conn.execute(get_query, {"key": str(key)}).fetchone()
            if row and row[0] == value:
                return

        # 2. Perform upsert
        query = text(f"""
            INSERT INTO {self.table_name} ({self.key_column}, {self.value_column})
            VALUES (:key, :value)
            ON DUPLICATE KEY UPDATE {self.value_column} = :value
        """)
        with self.engine.begin() as conn:
            conn.execute(query, {"key": str(key), "value": value})


class AnalysisManager:
    """Manages the analysis phase database operations."""

    def __init__(self, engine):
        self.engine = engine

    # Subdirectories table
    def get_existing_subdirs(self, file_id: int) -> dict:
        """Get existing subdirectories for a file."""
        query = text(
            "SELECT subdirectory, downloaded_file_id FROM subdirectories WHERE downloaded_file_id = :file_id"
        )
        with self.engine.begin() as conn:
            result = conn.execute(query, {"file_id": file_id}).fetchall()
            return {row[0]: row[1] for row in result}

    def insert_subdir(
        self,
        file_id: int,
        name: str,
        install_date: str | None,
        local_date: str | None,
        country: str | None,
    ) -> int:
        """Insert a subdirectory and return its ID."""
        query = text("""
            INSERT INTO subdirectories (downloaded_file_id, subdirectory, install_date, local_date, country)
            VALUES (:file_id, :name, :install_date, :local_date, :country)
        """)
        with self.engine.begin() as conn:
            conn.execute(
                query,
                {
                    "file_id": file_id,
                    "name": name,
                    "install_date": install_date,
                    "local_date": local_date,
                    "country": country,
                },
            )
            return conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()

    def update_subdir_status(
        self,
        file_id: int,
        subdir: str,
        status: str,
        emails_count=0,
        credit_cards_count=0,
        passwords_count=0,
    ):
        """Update status and counts for a subdirectory."""
        query = text("""
            UPDATE subdirectories
            SET status = :status, emails_count = :emails_count,
                credit_cards_count = :credit_cards_count, passwords_count = :passwords_count
            WHERE downloaded_file_id = :file_id AND subdirectory = :subdir
        """)
        with self.engine.begin() as conn:
            conn.execute(
                query,
                {
                    "status": f"PROCESSED_{status}",
                    "emails_count": emails_count,
                    "credit_cards_count": credit_cards_count,
                    "passwords_count": passwords_count,
                    "file_id": file_id,
                    "subdir": Path(subdir).name,
                },
            )

    # Analyzed Leaks table
    def save_leak(
        self,
        subdirectory_id: int | None,
        parent_path: str,
        analyzed_dir: str,
        status: str,
        leak_date: datetime | None,
        country: str,
        emails: int,
        credit_cards: int,
    ):
        """Save analyzed leak info into analyzed_leaks table."""
        query = text("""
            INSERT INTO analyzed_leaks
            (subdirectory_id, parent_path, analyzed_dir, status, leak_date, country, emails, credit_cards)
            VALUES (:subdir_id, :parent_path, :analyzed_dir, :status, :leak_date, :country, :emails, :credit_cards)
            ON DUPLICATE KEY UPDATE
                status = VALUES(status),
                leak_date = VALUES(leak_date),
                country = VALUES(country),
                emails = VALUES(emails),
                credit_cards = VALUES(credit_cards),
                subdirectory_id = VALUES(subdirectory_id)
        """)
        with self.engine.begin() as conn:
            conn.execute(
                query,
                {
                    "subdir_id": subdirectory_id,
                    "parent_path": parent_path,
                    "analyzed_dir": analyzed_dir,
                    "status": status,
                    "leak_date": leak_date,
                    "country": country,
                    "emails": emails,
                    "credit_cards": credit_cards,
                },
            )

    def update_leak_status(self, analyzed_dir: str, status: str):
        """Update status of an analyzed leak."""
        query = text(
            "UPDATE analyzed_leaks SET status = :status WHERE analyzed_dir = :analyzed_dir"
        )
        with self.engine.begin() as conn:
            conn.execute(query, {"status": status, "analyzed_dir": str(analyzed_dir)})

    # Emails to send table
    def is_email_queued(self, recipient_email: str) -> bool:
        """Check if email is in PENDING or PENDING_SUSPECT status."""
        query = text(
            "SELECT COUNT(*) FROM emails_to_send WHERE recipient_email = :email AND status IN ('PENDING','PENDING_SUSPECT')"
        )
        with self.engine.begin() as conn:
            return conn.execute(query, {"email": recipient_email}).scalar() > 0

    def store_email(
        self,
        analyzed_dir: str,
        recipient_email: str,
        subject: str,
        html_content: str,
        leak_date: datetime | None,
        status: str,
    ):
        """Store email to be sent."""
        query = text("""
            INSERT INTO emails_to_send (analyzed_dir, recipient_email, subject, html_content, leak_date, status)
            VALUES (:analyzed_dir, :recipient_email, :subject, :html_content, :leak_date, :status)
        """)
        with self.engine.begin() as conn:
            conn.execute(
                query,
                {
                    "analyzed_dir": analyzed_dir,
                    "recipient_email": recipient_email,
                    "subject": subject,
                    "html_content": html_content,
                    "leak_date": leak_date,
                    "status": status,
                },
            )

    def update_email_status(self, email_id: int, status: str):
        """Update the status of a queued email."""
        query = text(
            "UPDATE emails_to_send SET status = :status, last_attempt = NOW() WHERE id = :id"
        )
        with self.engine.begin() as conn:
            conn.execute(query, {"status": status, "id": email_id})

    def mark_email_as_sent(self, email_id: int):
        """Mark email as sent and clear subject + body to save space."""
        query = text("""
            UPDATE emails_to_send
            SET status = 'SENT', subject = '', html_content = '', last_attempt = NOW()
            WHERE id = :id
        """)
        with self.engine.begin() as conn:
            conn.execute(query, {"id": email_id})

    def get_subdir_info(
        self, file_id: int, subdir_name: str
    ) -> tuple[str | None, str | None, str | None]:
        """Get install_date, local_date, and country for a specific subdirectory."""
        query = text("""
            SELECT install_date, local_date, country
            FROM subdirectories
            WHERE downloaded_file_id = :file_id AND subdirectory = :subdir
        """)
        with self.engine.begin() as conn:
            row = conn.execute(
                query, {"file_id": file_id, "subdir": subdir_name}
            ).fetchone()
            return (row[0], row[1], row[2]) if row else (None, None, None)

    def insert_subdirs_batch(self, subdirs_data: list[tuple]):
        """
        Bulk insert subdirectories.
        subdirs_data: list of (downloaded_file_id, subdirectory, install_date, local_date, country)
        """
        if not subdirs_data:
            return

        query = text("""
            INSERT INTO subdirectories (downloaded_file_id, subdirectory, install_date, local_date, country)
            VALUES (:file_id, :subdir, :install_date, :local_date, :country)
        """)

        with self.engine.begin() as conn:
            conn.execute(query, subdirs_data)

    def store_emails_batch(self, emails_data: list[dict]):
        """
        Bulk insert emails to send.
        emails_data: list of dicts containing keys: analyzed_dir, recipient_email, subject, html_content, leak_date, status
        """
        if not emails_data:
            return

        query = text("""
            INSERT INTO emails_to_send (analyzed_dir, recipient_email, subject, html_content, leak_date, status)
            VALUES (:analyzed_dir, :recipient_email, :subject, :html_content, :leak_date, :status)
        """)

        with self.engine.begin() as conn:
            conn.execute(query, emails_data)

    def get_pending_emails(
        self, limit: int, email_filter: str | None, sort_by_leak_date: bool
    ) -> list:
        """Get pending emails for processing."""
        base = "SELECT id, analyzed_dir, recipient_email, subject, html_content, leak_date FROM emails_to_send WHERE status = 'PENDING'"
        if email_filter:
            # Simple version for internal use, use params in real query
            base += f" AND recipient_email LIKE '{email_filter}'"

        # Re-implementing with params for safety
        query_str = "SELECT id, analyzed_dir, recipient_email, subject, html_content, leak_date FROM emails_to_send WHERE status = 'PENDING'"
        params = {}
        if email_filter:
            query_str += " AND recipient_email LIKE :filter"
            params["filter"] = email_filter

        query_str += " ORDER BY " + (
            "leak_date DESC" if sort_by_leak_date else "created_at"
        )
        if limit != 0:
            query_str += " LIMIT :limit"
            params["limit"] = limit

        with self.engine.begin() as conn:
            return conn.execute(text(query_str), params).fetchall()

    # Sent Emails table
    def has_been_sent(self, recipient_email: str) -> datetime | None:
        """Check if email was already sent."""
        query = text("SELECT sent_at FROM sent_emails WHERE recipient_email = :email")
        with self.engine.begin() as conn:
            row = conn.execute(query, {"email": recipient_email}).fetchone()
            return row[0] if row else None

    def save_sent_email(self, recipient_email: str, subject: str, analyzed_dir: str):
        """Record sent email."""
        query = text("""
            INSERT INTO sent_emails (recipient_email, subject, analyzed_dir, sent_at)
            VALUES (:email, :subject, :analyzed_dir, NOW())
        """)
        with self.engine.begin() as conn:
            conn.execute(
                query,
                {
                    "email": recipient_email,
                    "subject": subject,
                    "analyzed_dir": analyzed_dir,
                },
            )
