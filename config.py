"""Configurazione unificata per Data Leak Platform.

Tutte le variabili sono lette dall'ambiente (via .env se presente) con valori
di default sensati. Nessun segreto è hardcodato in questo file.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

# Carica variabili da .env se presente
load_dotenv()

# ---------------------------------------------------------------------------
# RabbitMQ (DataLeakSuperManager pipeline)
# ---------------------------------------------------------------------------
RABBIT_CONNECTION_STRING = os.getenv(
    "RABBIT_CONNECTION_STRING",
    "amqp://localhost/%2F?heartbeat=60&blocked_connection_timeout=300&connection_attempts=3&retry_delay=2",
)
RABBIT_MGMT_USER = os.getenv("RABBIT_MGMT_USER", "dok")
RABBIT_MGMT_PASS = os.getenv("RABBIT_MGMT_PASS", "")
RABBIT_MGMT_HOST = os.getenv("RABBIT_MGMT_HOST", "localhost")
RABBIT_MGMT_PORT = int(os.getenv("RABBIT_MGMT_PORT", "443"))

UNIQUES_QUEUE = os.getenv("UNIQUES_QUEUE", "unique_files")
PASSWORD_QUEUE = os.getenv("PASSWORD_QUEUE", "password_file")
SUCCESS_QUEUE = os.getenv("SUCCESS_QUEUE", "uncompressed_file")
ERROR_QUEUE = os.getenv("ERROR_QUEUE", "uncompress_error")
WRONG_PASSWORD_QUEUE = os.getenv("WRONG_PASSWORD_QUEUE", "wrong_password")
OTHER_ERRORS_QUEUE = os.getenv("OTHER_ERRORS_QUEUE", "other_decompression_errors")
SUBDIR_QUEUE = os.getenv("SUBDIR_QUEUE", "subdirectories")
URI_GOFILE_QUEUE = os.getenv("URI_GOFILE_QUEUE", "uri_gofile")
URI_TELEGRAM_QUEUE = os.getenv("URI_TELEGRAM_QUEUE", "uri_telegram")
URI_GENERIC_QUEUE = os.getenv("URI_GENERIC_QUEUE", "uri_generic")
COMBO_QUEUE = os.getenv("COMBO_QUEUE", "combo_queue")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_CONNECTION_STRING = os.getenv(
    "DB_CONNECTION_STRING",
    "mysql+pymysql://dataleak:dataleak@localhost:3306/dumpdb?charset=utf8mb4",
)
DB_PATH = os.getenv("DB_PATH", "./db/processed.db")

# ---------------------------------------------------------------------------
# Microsoft Graph / M365 (dataleakmail)
# ---------------------------------------------------------------------------
M365_TENANT_ID = os.getenv("M365_TENANT_ID", "")
M365_CLIENT_ID = os.getenv("M365_CLIENT_ID", "")
M365_CLIENT_SECRET = os.getenv("M365_CLIENT_SECRET", "")
M365_USERNAME = os.getenv("M365_USERNAME", "")
M365_AUTHORITY = os.getenv(
    "M365_AUTHORITY", "https://login.microsoftonline.com/common"
)
M365_SCOPES = [
    "Mail.Read",
    "Mail.ReadWrite",
    "Mail.Send",
]
M365_POLL_INTERVAL_SECONDS = int(os.getenv("M365_POLL_INTERVAL_SECONDS", "300"))
M365_LOOKBACK_HOURS = int(os.getenv("M365_LOOKBACK_HOURS", "24"))
M365_INBOX_FOLDER = os.getenv("M365_INBOX_FOLDER", "Inbox")

# ---------------------------------------------------------------------------
# NVIDIA (classificazione intent)
# ---------------------------------------------------------------------------
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_ENDPOINT_URL = os.getenv(
    "NVIDIA_ENDPOINT_URL",
    "https://integrate.api.nvidia.com/v1/chat/completions",
)
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-bolt-e4b")
NVIDIA_TIMEOUT_SECONDS = int(os.getenv("NVIDIA_TIMEOUT_SECONDS", "180"))

# ---------------------------------------------------------------------------
# S/MIME
# ---------------------------------------------------------------------------
SMIME_CERT_PATH = os.getenv("SMIME_CERT_PATH", "./certs/smime.p12")
SMIME_CERT_PASSWORD = os.getenv("SMIME_CERT_PASSWORD", "")
SIGNATURE_HTML_PATH = os.getenv("SIGNATURE_HTML_PATH", "./signature/signature.html")

# ---------------------------------------------------------------------------
# Telegram / Telethon (DataLeakSuperManager pipeline)
# ---------------------------------------------------------------------------
API_ID = os.getenv("API_ID", "")
API_HASH = os.getenv("API_HASH", "")
API_PHONE = os.getenv("API_PHONE", "")
SESSION = os.getenv("TELETHON_SESSION", "tecnologieufficio_nas_file_downloader")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
UNCOMPRESS_DIR = os.getenv("UNCOMPRESS_DIR", "uncompressed_files")
PASSWORD_DUMP_DIR = os.getenv("PASSWORD_DUMP_DIR", "password_dumps")

# ---------------------------------------------------------------------------
# SMTP / Postal (invio email notifiche)
# ---------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "noreply@localhost")
SENDER_NAME = os.getenv("SENDER_NAME", "Data Leak Platform")
UNSUBSCRIBE_URL = os.getenv("UNSUBSCRIBE_URL", "https://unsubscribe.localhost/unsubscribe")
UNSUBSCRIBE_SECRET = os.getenv("UNSUBSCRIBE_SECRET", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "./logs/app.log")

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------
TEST_RECIPIENT = os.getenv("TEST_RECIPIENT", "salvatoreansani@gmail.com")
