# DataLeakSuperManager - AI Coding Agent Instructions

## Project Overview
DataLeakSuperManager is a distributed data breach analysis system that monitors Telegram channels for leaked files, extracts archive contents, analyzes data leaks, and sends breach notifications. It processes terabytes of data using a multi-stage message queue pipeline.

## Architecture & Data Flow

### Core Pipeline (Message-Driven Processing)
Files flow through RabbitMQ queues with database state tracking:
1. **Download** ([download.py](download.py)) → `UNIQUES_QUEUE` → `downloaded_files` DB (status: DOWNLOADED)
2. **Password Extraction** ([password.py](password.py)) → consumes `UNIQUES_QUEUE` → publishes to `PASSWORD_QUEUE`
3. **Decompression** ([decompress.py](decompress.py)) → consumes `PASSWORD_QUEUE` → publishes to `SUCCESS_QUEUE` or `PASSWORDDUMP_QUEUE` (for .txt files)
4. **Subdirectory Walking** ([process_subdirectories.py](process_subdirectories.py)) → consumes `SUCCESS_QUEUE` → publishes to `SUBDIR_QUEUE`
5. **Analysis** ([analyze.py](analyze.py)) → consumes `SUBDIR_QUEUE` → extracts PII and populates `emails_to_send`
6. **Password Dumping** ([process_passworddump.py](process_passworddump.py)) → consumes `PASSWORDDUMP_QUEUE` → appends to combo.txt
7. **Email Notification** ([send_mail.py](send_mail.py)) → processes `emails_to_send` table → sends breach notifications via SMTP

### Key Files & Patterns

#### Database Schema ([create_tables.sql](create_tables.sql))
- **downloaded_files**: Central tracking table with status lifecycle (DOWNLOADED → UNCOMPRESSED → WALKED_DIR → PROCESSED)
- **channel_state**: Per-channel message ID tracking (prevents reprocessing)
- **subdirectories**: Archive structure metadata with system info (install_date, country)
- **analyzed_leaks**: Final analysis with extracted PII counts
- **emails_to_send**: Notification queue with status tracking

Status enums are critical - always use exact values like `'PROCESSED_OK'`, `'WRONGPASSWORD'`, `'OTHERERRORS'`

#### Message Queue Pattern ([utils.py#RabbitMQPublisher](utils.py))
```python
# Standard publish pattern with retry/backoff
publisher = RabbitMQPublisher(RABBIT_CONNECTION_STRING, QUEUE_NAME)
payload = {"id": file_id, "field": value}
publisher.publish(payload)  # Auto-retries with exponential backoff (3 attempts)
```
Always wrap publish calls with checks for failed_publish.json fallback logging.

#### Download Module ([download.py](download.py))
- **Telegram streaming**: Uses Telethon async client with iter_download (2MB chunks)
- **Resume capability**: .part files track progress for interruption recovery
- **Deduplication**: By file_key (doc_id:access_hash) and content hash
- **Channel management**: Loaded from DB, reloaded hourly; disabled channels on auth errors
- **State persistence**: channel_state table tracks last_id per channel to resume

#### Decompression Module ([decompress.py](decompress.py#is_archive_file))
- **7zip integration**: External process for extraction; archives must be unpacked recursively
- **Password lookup**: PASSWORD_LOOKUP_MATRIX maps channel patterns to passwords (e.g., "@Trident_Cloud" → URL)
- **Text file handling**: .txt files bypass decompression, publish to passworddump queue
- **Status transitions**: UNCOMPRESSED → WALKED_DIR (post-extraction)

#### Analysis Module ([analyze.py](analyze.py))
- **PII extraction patterns**:
  - Emails: RFC-compliant regex (stored in emails_to_send table)
  - Credit cards: Luhn algorithm validation, masked for display
  - Passwords: SOFT block parsing (URL:\nUSER:\nPASS:) with configurable thresholds
- **System metadata parsing**: Extracts install_date/local_date/country from UserInformation.txt or System.txt
- **Leak hashing**: SHA256(analyzed_dir) creates unique leak_hash for deduplication
- **Threshold filtering**: EMAILS_IGNORE_THRESHOLD env var (default 50) excludes noisy directories

#### Subdirectory Processing ([process_subdirectories.py](process_subdirectories.py))
- **Recursive unpacking**: Walks all extracted directories, parses system info at each level
- **Metadata tracking**: Stores install_date/country/subdir_count in subdirectories table
- **Unique constraint**: (downloaded_file_id, subdirectory, parent_directory) prevents duplicates

## Developer Workflows

### Setup & Configuration
1. **Create config.py** (gitignored for security):
   ```python
   # Database connection
   DB_CONNECTION_STRING = "mysql+pymysql://user:pass@host:port/dumpdb?charset=utf8mb4"

   # RabbitMQ connection
   RABBIT_CONNECTION_STRING = "amqp://user:pass@host:port/%2F"

   # Queue names (must match across all workers)
   UNIQUES_QUEUE = "unique_files"
   PASSWORD_QUEUE = "password_file"
   SUCCESS_QUEUE = "uncompressed_file"
   SUBDIR_QUEUE = "subdirectories"
   PASSWORDDUMP_QUEUE = "passworddump"
   INPUT_QUEUE = "unique_files"  # Alias for UNIQUES_QUEUE

   # Telegram API credentials
   API_ID = 12345678
   API_HASH = "your_api_hash_here"
   API_PHONE = "+1234567890"
   SESSION = "telegram_session_name"

   # Storage paths
   DOWNLOAD_DIR = "downloads"
   UNCOMPRESS_DIR = "uncompressed_files"

   # File filtering
   IGNORED_EXTENSIONS = [".exe", ".dll", ".so", ".bin"]  # Or comma-separated string

   # SMTP configuration
   SMTP_HOST = "smtp.example.com"
   SMTP_PORT = 587
   SMTP_USER = "user@example.com"
   SMTP_PASSWORD = "smtp_password"
   SENDER_EMAIL = "noreply@example.com"
   SENDER_NAME = "Data Breach Alert"
   UNSUBSCRIBE_URL = "https://example.com/unsubscribe"
   UNSUBSCRIBE_SECRET = "secret_key_for_hmac"
   ```

2. **Initialize database**: `mysql -u user -p < create_tables.sql`
3. **Install system dependencies**: `sudo apt install p7zip-full` (required for archive extraction)
4. **Install Python dependencies**: `pip install -r requirements.txt`
5. **Setup SSH tunnel** (if remote DB/RabbitMQ): `./tunnels.sh` (forwards MySQL:3307, RabbitMQ:5672)

### Running the Pipeline Workers
Each worker is a standalone process consuming from specific RabbitMQ queues:

```bash
# 1. Start Telegram downloader (long-running, monitors channels)
python download.py

# 2. Start password extractor (extracts passwords from message text)
python password.py

# 3. Start decompression worker (unpacks archives with passwords)
python decompress.py

# 4. Start subdirectory walker (walks extracted directory trees)
python process_subdirectories.py

# 5. Start analysis worker (extracts PII from files)
python analyze.py

# 6. Start password dump processor (consolidates text files to combo.txt)
python process_passworddump.py

# 7. Send breach notification emails (one-shot, not a daemon)
python send_mail.py 100  # Send 100 emails
python send_mail.py 0 -f '%@gmail.com'  # Send all emails to Gmail users
python send_mail.py 10 -s  # Send 10 emails, sorted by leak_date
python send_mail.py 0 -t test@example.com  # Send test email
```

### Utility Scripts
- **Reprocess single file**: `python reprocess_single_id.py <file_id>`
- **Reprocess by SQL query**: `python reprocess_id_from_query.py "SELECT id, file_key, saved_file_name FROM downloaded_files WHERE status='OTHERERRORS'"`
- **Delete files by message pattern**: `python delete_by_message.py "search_text" [-y]`
- **List/sync Telegram channels**: `python channels_list.py` (updates `channel_list` table)
- **Populate download queue**: `python populate_download_queue.py` (republishes all downloaded_files to queue)

### Adding a New Processing Queue
1. Define queue constant in [config.py](config.py) (e.g., `NEW_QUEUE = 'queue_name'`)
2. Create publisher: `pub = RabbitMQPublisher(RABBIT_CONNECTION_STRING, NEW_QUEUE)`
3. Create consumer worker script (async with `pika.BlockingConnection`)
4. Update database schema if new status states are needed
5. Add queue to RabbitMQ setup (durable queues for persistence)

### Modifying PII Extraction Logic
- Regex patterns live in [analyze.py](analyze.py) (email, credit card, password parsing)
- Test against actual leaked data samples before deployment
- Outbound notification emails list only the affected websites (hosts): passwords and credit card numbers are never included, not even masked. `load_email_template` in [analyze.py](analyze.py) renders the site-only list.
- Remember: EMAILS_IGNORE_THRESHOLD can exclude high-volume noise (configurable per run)

### Extending Channel Monitoring
- Add channels to channel_list table with status='ACTIVE'
- Download module auto-reloads from DB hourly
- On auth/access errors, channel auto-disables via disable_channel()
- To resume: manually set status='ACTIVE' in DB (last_id preserved in channel_state)

## Project-Specific Conventions

### SQL & Database
- Use SQLAlchemy `text()` for all queries (prepared statements with bound parameters)
- Always use context managers: `with get_db_engine().begin() as conn: conn.execute(...)`
- Status updates are transactional; errors trigger implicit rollback
- Foreign keys enforce referential integrity (cascading deletes on downloaded_files)

### Logging
- Use module-level logger: `logger = logging.getLogger(__name__)`
- Log file location: `logs/` directory (auto-created by setup_logging)
- Suppress verbose logs: Telethon/Pika set to WARNING level
- Include context: file_id, channel_id, saved_file_name in all messages

### Error Handling
- **RabbitMQ failures**: Auto-retry with exponential backoff; fallback to failed_publish.json
- **File corruption**: Detect oversized .part files (size > expected), remove and restart
- **Archive extraction**: Catch rar/7z/zip errors, mark status as 'WRONGPASSWORD' or 'OTHERERRORS'
- **Database errors**: Log full context, rollback transaction, disable problematic channels

### File Paths
- Use `local_download_path(filename)` utility to resolve DOWNLOAD_DIR/UNCOMPRESS_DIR
- All file operations check existence before read/delete (safety for concurrent access)
- Archive extraction output goes to UNCOMPRESS_DIR

## Critical Integration Points

1. **Config Injection**: All credentials/URLs in config.py (DB, RabbitMQ, Telegram API, SMTP)
2. **Channel State Sync**: channel_state table is source-of-truth for resumption
3. **Hash Deduplication**: content hash checked AFTER download to prevent duplicate processing
4. **Email Template**: [templates/mail_template_en.html](templates/mail_template_en.html) for breach notifications
5. **IGNORED_EXTENSIONS**: Files matching list are skipped (configurable in config.py, normalized to lowercase + dot prefix)

## Testing & Debugging

- **Unit tests**: `python -m unittest tests/test_password_extraction.py` - test PII regex patterns
- **Dry-run**: Set channel status to 'DISABLED' to pause processing without deleting state
- **Queue inspection**: Use RabbitMQ management UI (localhost:15672) to peek at queued messages
- **Database snapshots**: Create temp tables to test extraction logic without modifying live data
- **Monitor logs**: Check `logs/` directory for detailed execution traces (auto-created by setup_logging)
- **Failed publishes**: Check `failed_publish.json` for messages that couldn't be queued (manual retry needed)

## Environment Variables
- `DOWNLOAD_DIR`: Storage location for Telegram files (default: 'downloads')
- `UNCOMPRESS_DIR`: Extraction output directory (default: 'uncompressed_files')
- `TELETHON_SESSION`: Telegram session name (default: 'tecnologieufficio_nas_file_downloader')
- `ANALYZE_IGNORE_EMAILS_THRESHOLD`: Skip dirs with >N emails (default: 50)
- `SMTP_*`: Email sending credentials (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD)

## Known System Dependencies
- **p7zip-full**: Required for 7z archive extraction (install via apt: `sudo apt install p7zip-full`)
- **MySQL 5.7+**: Database backend with utf8mb4 collation
- **RabbitMQ**: Message broker for distributed queuing
