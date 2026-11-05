# DataLeak NAS Pipeline

Distributed Python workers connected via **RabbitMQ** queues and a **MySQL** database.

## Pipeline Flow
Main sequence: `download.py` → `password.py` → `decompress.py` → `process_subdirectories.py` → `analyze.py` → `send_mail.py`

Side workers: `download_links.py`, `subscribe_channels.py`, `download_gofile.py`, `import_ulp.py`, `analyze_scaler.py`

## Key Commands
- **Tests**: `python -m unittest discover tests` (std unittest, no pytest; conftest.py auto-bootstraps mock config)
- **Tests**: `python -m unittest discover tests` (std unittest, no pytest; tests call `mock_config()` from `tests/conftest.py` before importing app modules)
- **Lint**: `ruff check .` (ruff 0.16.0 in `.venv/bin/`). `ruff.toml` pins the exact 413-rule default set of 0.16.0 so future ruff upgrades can't silently change results; keep it in sync with the rule set you intend. Core files `utils.py`, `analyze.py`, `send_mail.py` are lint-clean.
- **Start all workers in tmux**: `./start_pipeline.sh` (uses `.venv/bin/python`)
- **Reprocess**: `reprocess_marked.py [--dry-run] [--limit N]`, `reprocess_pending_subdirs.py`, `enqueue_todump_password.py`
- **Maintenance**: `delete_to_delete.py [-y]`, `check_orphans.py [-y]`, `db_audit.py`, `cleanup_subdirectories.py`, `cleanup_telegram_links.py`, `requeue_telegram_uris.py`
- **Regen queued emails**: `regen_pending_emails.py [--dry-run] [--limit N]` re-renders PENDING/PENDING_SUSPECT rows with the current template
- **Search leaks**: `search_email_leaks.py <email>`
- **Send email**: `send_mail.py <count> [-f <filter>] [-t <test_email>] [-n] [--loop]`
- **Channels**: `channels_list.py` (sync), `channels_stats.py` (stats)

## Gotchas
- **`config.py` is gitignored**. Tests mock it via `sys.modules["config"] = mock.MagicMock()` in `tests/conftest.py`.
- **Two IGNORED_EXTENSIONS**: one in `utils.py` (link workers), another in `config.py` (download workers).
- **`password_matrix.json`** reloaded by `decompress.py` on **SIGUSR1** (`kill -USR1 <pid>`).
- **`send_mail.py --loop`** sends 150 emails every 10 min as daemon.
- **`import_ulp.py`** consumes `COMBO_QUEUE`, detects ULP vs combo format, imports to ClickHouse.
- **`failed_publish.json`** fallback in `downloads/` when RabbitMQ publish fails.
- **Logs**: `logs/` dir auto-created by `setup_logging`.
- **Tunnels**: `./tunnels.sh` creates SSH tunnels for remote DB/RabbitMQ via `chiave_dataleak` key.
- **Queue names** defined in `config.py`: `UNIQUES_QUEUE`, `PASSWORD_QUEUE`, `SUCCESS_QUEUE`, `SUBDIR_QUEUE`, `URI_GOFILE_QUEUE`, `URI_TELEGRAM_QUEUE`, `URI_GENERIC_QUEUE`, `COMBO_QUEUE`.
- **File statuses** (exact enum): `DOWNLOADING`, `DOWNLOADED`, `UNCOMPRESSED`, `WALKED_DIR`, `PROCESSED`, `PROCESSED_NOMAIL`, `PROCESSED_EMAILS_ALREADY_SENT`, `WRONGPASSWORD`, `OTHERERRORS`, `ERROR_WALKED_DIR`, `TODUMP_PASSWORD`, `PASSWORD_DUMPED`, `DUPLICATE_FILE`, `TO_DELETE`, `DELETED`, `REPROCESS`.

## System Dependencies
- **p7zip-full** (`sudo apt install p7zip-full`) — required for 7z archive extraction
- **MySQL 5.7+** with utf8mb4 collation
- **RabbitMQ** message broker
- **Playwright Chromium** (`python -m playwright install chromium`) — for GoFile link resolution

## Developer Workflows
1. **Create `config.py`** (gitignored) with DB, RabbitMQ, Telegram API, SMTP credentials
2. **Initialize database**: `mysql -u user -p < create_tables.sql`
3. **Install deps**: `pip install -r requirements.txt` + `python -m playwright install chromium`
4. **Setup SSH tunnel** (if remote): `./tunnels.sh`
5. **Start workers**: `./start_pipeline.sh` or run individually

## References
- `.github/copilot-instructions.md` — verbose docs with config.py template and detailed developer workflows
- `create_tables.sql` — full DB schema with all tables, indexes, views, and migrations
