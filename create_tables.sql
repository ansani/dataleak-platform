-- DataLeakSuperManager Database Schema
-- MySQL table creation script for all tables referenced in the codebase

-- Set charset and collation for consistent Unicode handling
SET NAMES utf8mb4 COLLATE utf8mb4_general_ci;

-- Create database if it doesn't exist
CREATE DATABASE IF NOT EXISTS dumpdb CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE dumpdb;

-- ============================================================================
-- 1. DOWNLOADED_FILES - Central table tracking all downloaded files
-- ============================================================================
CREATE TABLE IF NOT EXISTS downloaded_files (
    id INT AUTO_INCREMENT PRIMARY KEY,
    file_key VARCHAR(255) NOT NULL UNIQUE COMMENT 'Format: doc_id:access_hash',
    original_file_name VARCHAR(500) NOT NULL COMMENT 'Original filename from Telegram',
    saved_file_name VARCHAR(500) NOT NULL COMMENT 'Local saved filename',
    channel_id VARCHAR(50) NOT NULL COMMENT 'Telegram channel ID',
    message_text TEXT COMMENT 'Associated Telegram message text',
    hash VARCHAR(64) DEFAULT NULL COMMENT 'SHA-256 hash of file content',
    file_size BIGINT DEFAULT 0 COMMENT 'Size of the file in bytes',
    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'When file was downloaded',
    status ENUM(
        'DOWNLOADING', 'DOWNLOADED', 'UNCOMPRESSED', 'WALKED_DIR', 'PROCESSED', 'PROCESSED_NOMAIL', 'PROCESSED_EMAILS_ALREADY_SENT',
        'WRONGPASSWORD', 'OTHERERRORS', 'TODUMP_PASSWORD', 'PASSWORD_DUMPED',
        'ERROR_WALKED_DIR', 'DUPLICATE_FILE', 'TO_DELETE', 'DELETED', 'REPROCESS'
    ) DEFAULT 'DOWNLOADED' COMMENT 'Processing status',
    password VARCHAR(255) DEFAULT NULL COMMENT 'Extracted password from message',
    error_description TEXT DEFAULT NULL COMMENT 'Error details if processing failed',
    subdir_count INT DEFAULT 0 COMMENT 'Number of subdirectories found',
    passwords_count INT DEFAULT 0 COMMENT 'Number of passwords found in text files',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_file_key (file_key),
    INDEX idx_hash (hash),
    INDEX idx_status (status),
    INDEX idx_channel_id (channel_id),
    INDEX idx_downloaded_at (downloaded_at),
    INDEX idx_filename_size (original_file_name, file_size)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Central table for tracking downloaded files and their processing status';

-- ============================================================================
-- 2. CHANNEL_LIST - Manages Telegram channels to monitor
-- ============================================================================
CREATE TABLE IF NOT EXISTS channel_list (
    id INT AUTO_INCREMENT PRIMARY KEY,
    channel_name VARCHAR(255) NOT NULL COMMENT 'Human-readable channel name',
    channel_id VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL UNIQUE COMMENT 'Telegram channel ID',
    date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'When channel was added',
    status ENUM('ACTIVE', 'DISABLED', 'LEFT') DEFAULT 'ACTIVE' COMMENT 'Channel monitoring status',
    date_removed TIMESTAMP NULL DEFAULT NULL COMMENT 'When channel was disabled',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_channel_id (channel_id),
    INDEX idx_status (status),
    INDEX idx_date_added (date_added)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Manages list of Telegram channels to monitor';

-- ============================================================================
-- 3. CHANNEL_STATE - Tracks last processed message ID per Telegram channel
-- ============================================================================
CREATE TABLE IF NOT EXISTS channel_state (
    channel_id VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci PRIMARY KEY COMMENT 'Telegram channel ID',
    last_id BIGINT NOT NULL DEFAULT 0 COMMENT 'Last processed message ID',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_last_id (last_id),
    FOREIGN KEY (channel_id) REFERENCES channel_list(channel_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Tracks processing state for each Telegram channel';

-- ============================================================================
-- 3.1 CHANNEL_STATE_GOFILE - Tracks last processed message ID per channel for the GoFile link watcher
-- ============================================================================
CREATE TABLE IF NOT EXISTS channel_state_gofile (
    channel_id VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci PRIMARY KEY COMMENT 'Telegram channel ID',
    last_id BIGINT NOT NULL DEFAULT 0 COMMENT 'Last processed message ID',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_last_id (last_id),
    FOREIGN KEY (channel_id) REFERENCES channel_list(channel_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Tracks GoFile link watcher state (last processed message ID) per Telegram channel';

-- ============================================================================
-- 3.3 DISCOVERED_LINKS - Central table for all URIs found in Telegram messages
-- Same logic as downloaded_files; each URI is stored once (unique constraint).
-- Status starts as INSERTED; downstream workers update it as they process links.
-- Routing to RabbitMQ queues:
--   uri_type = GOFILE    → URI_GOFILE queue
--   uri_type = TELEGRAM  → URI_TELEGRAM queue
--   uri_type = GENERIC   → URI_GENERIC queue
-- ============================================================================
CREATE TABLE IF NOT EXISTS discovered_links (
    id INT AUTO_INCREMENT PRIMARY KEY,
    uri VARCHAR(2048) NOT NULL COMMENT 'Normalized URI',
    uri_hash CHAR(64) NOT NULL COMMENT 'SHA-256 hash of normalized full URI for uniqueness',
    uri_type ENUM('GOFILE', 'TELEGRAM', 'GENERIC') NOT NULL COMMENT 'Link type for queue routing',
    channel_id VARCHAR(50) NOT NULL COMMENT 'Source Telegram channel ID',
    channel_name VARCHAR(255) DEFAULT NULL COMMENT 'Source Telegram channel name',
    message_id BIGINT NOT NULL COMMENT 'Source Telegram message ID',
    message_text TEXT DEFAULT NULL COMMENT 'Full message text where URI was found',
    status ENUM(
        'INSERTED', 'PROCESSING', 'PROCESSED', 'FAILED', 'NEW_SUBSCRIBED', 'ALREADY_SUBSCRIBED', 'JOIN_REQUESTED'

    ) DEFAULT 'INSERTED' COMMENT 'Processing status',
    error_description TEXT DEFAULT NULL COMMENT 'Error details if processing failed',
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'When the URI was first detected',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_uri_hash (uri_hash) COMMENT 'Prevents duplicate full URIs safely',
    INDEX idx_uri_type (uri_type),
    INDEX idx_status (status),
    INDEX idx_channel_id (channel_id),
    INDEX idx_detected_at (detected_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Central table for all URIs found in Telegram messages, routed to typed queues';

-- ============================================================================
-- 3.2 CHANNEL_STATE_MONTHLY - Tracks min/max message IDs processed in monthly runs
-- ============================================================================
CREATE TABLE IF NOT EXISTS channel_state_monthly (
    channel_id VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci PRIMARY KEY COMMENT 'Telegram channel ID',
    min_id BIGINT NOT NULL COMMENT 'Minimum message ID processed in monthly window',
    max_id BIGINT NOT NULL COMMENT 'Maximum message ID processed in monthly window',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_monthly_min_id (min_id),
    INDEX idx_monthly_max_id (max_id),
    FOREIGN KEY (channel_id) REFERENCES channel_list(channel_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Tracks monthly processing ranges to avoid reprocessing';

-- ============================================================================
-- 4. SUBDIRECTORIES - Tracks extracted subdirectories with metadata
-- ============================================================================
-- OPTIMIZATION: Removed redundant saved_file_name and parent_directory columns
-- These are now obtained via JOIN with downloaded_files and computed at runtime
-- See v_subdirectories VIEW for full denormalized access
CREATE TABLE IF NOT EXISTS subdirectories (
    id INT AUTO_INCREMENT PRIMARY KEY,
    downloaded_file_id INT NOT NULL COMMENT 'Reference to downloaded_files.id',
    subdirectory VARCHAR(500) NOT NULL COMMENT 'Subdirectory name',
    install_date DATETIME DEFAULT NULL COMMENT 'Install date from system info',
    local_date DATETIME DEFAULT NULL COMMENT 'Local date from system info',
    country VARCHAR(10) DEFAULT NULL COMMENT 'Country code from system info',
    emails_count INT DEFAULT 0 COMMENT 'Number of email addresses found',
    credit_cards_count INT DEFAULT 0 COMMENT 'Number of credit cards found',
    passwords_count INT DEFAULT 0 COMMENT 'Number of passwords found',
    status ENUM(
        'PENDING', 'PROCESSED_OK', 'PROCESSED_KO', 'PROCESSED_SUSPECT', 
        'PROCESSED_NOMAIL', 'PROCESSED_EMAILS_ALREADY_SENT', 'PROCESSED_EXCLUDED', 
        'PROCESSED_IGNORED_LOT_OF_EMAILS', 'PROCESSED_NOPASSWORD', 'MAIL_SENT'
    ) DEFAULT 'PENDING' COMMENT 'Processing status',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    FOREIGN KEY (downloaded_file_id) REFERENCES downloaded_files(id) ON DELETE CASCADE,
    UNIQUE KEY unique_subdir (downloaded_file_id, subdirectory),
    INDEX idx_downloaded_file_id (downloaded_file_id),
    INDEX idx_status (status),
    INDEX idx_country (country)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Tracks subdirectories found in extracted archives (optimized schema)';

-- ============================================================================
-- 4.5 V_SUBDIRECTORIES - Denormalized VIEW for easy access to full subdir data
-- ============================================================================
-- This VIEW reconstructs the denormalized data by joining subdirectories with downloaded_files
-- Use this VIEW when you need saved_file_name or parent_directory information
-- It combines the storage efficiency of the normalized table with denormalized query convenience
CREATE OR REPLACE VIEW v_subdirectories AS
SELECT 
    s.id,
    s.downloaded_file_id,
    df.saved_file_name,
    s.subdirectory,
    CONCAT(df.saved_file_name, '/', s.subdirectory) AS parent_directory,
    s.install_date,
    s.local_date,
    s.country,
    s.emails_count,
    s.credit_cards_count,
    s.passwords_count,
    s.status,
    s.created_at,
    s.updated_at
FROM subdirectories s
INNER JOIN downloaded_files df ON s.downloaded_file_id = df.id;

-- ============================================================================
-- 5. ANALYZED_LEAKS - Final analysis results with extracted data
-- ============================================================================
CREATE TABLE IF NOT EXISTS analyzed_leaks (
    id INT AUTO_INCREMENT PRIMARY KEY,
    subdirectory_id INT DEFAULT NULL COMMENT 'Reference to subdirectories.id',
    parent_path VARCHAR(1000) NOT NULL COMMENT 'Parent directory path',
    analyzed_dir VARCHAR(500) NOT NULL UNIQUE COMMENT 'Directory that was analyzed',
    status ENUM('OK', 'KO', 'SUSPECT', 'NOMAIL', 'PROCESSED', 'PROCESSED_NOMAIL', 'PROCESSED_EMAILS_ALREADY_SENT', 'MAIL_SENT') 
        NOT NULL COMMENT 'Analysis result status',
    leak_date DATETIME DEFAULT NULL COMMENT 'Date when leak occurred (from system files)',
    country VARCHAR(10) DEFAULT NULL COMMENT 'Country code detected',
    emails INT DEFAULT 0 COMMENT 'Number of email addresses found',
    credit_cards INT DEFAULT 0 COMMENT 'Number of credit cards found',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    FOREIGN KEY (subdirectory_id) REFERENCES subdirectories(id) ON DELETE CASCADE,
    INDEX idx_status (status),
    INDEX idx_leak_date (leak_date),
    INDEX idx_country (country),
    INDEX idx_parent_path (parent_path(255)),
    INDEX idx_subdir_id (subdirectory_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Results of leak analysis for each directory';

-- ============================================================================
-- 6. EMAILS_TO_SEND - Notification queue for affected users
-- ============================================================================
CREATE TABLE IF NOT EXISTS emails_to_send (
    id INT AUTO_INCREMENT PRIMARY KEY,
    subdirectory_id INT DEFAULT NULL COMMENT 'Reference to subdirectories.id',
    analyzed_dir VARCHAR(500) NOT NULL COMMENT 'Directory that was analyzed',
    recipient_email VARCHAR(320) NOT NULL COMMENT 'Email address to notify',
    subject VARCHAR(500) NOT NULL COMMENT 'Email subject line',
    html_content LONGTEXT NOT NULL COMMENT 'HTML email body',
    leak_date DATETIME DEFAULT NULL COMMENT 'Date when leak occurred',
    status ENUM('PENDING', 'PENDING_SUSPECT', 'SENT', 'FAILED', 'CANCELLED') DEFAULT 'PENDING' COMMENT 'Sending status',
    attempts INT DEFAULT 0 COMMENT 'Number of send attempts',
    last_attempt TIMESTAMP NULL DEFAULT NULL COMMENT 'Last send attempt timestamp',
    error_message TEXT DEFAULT NULL COMMENT 'Error details if sending failed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_recipient_email (recipient_email),
    INDEX idx_status (status),
    INDEX idx_analyzed_dir (analyzed_dir),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Queue of emails to send to breach victims';

-- ============================================================================
-- 7. SENT_EMAILS - Log of successfully sent emails (referenced in code)
-- ============================================================================
CREATE TABLE IF NOT EXISTS sent_emails (
    id INT AUTO_INCREMENT PRIMARY KEY,
    subdirectory_id INT DEFAULT NULL COMMENT 'Reference to subdirectories.id',
    recipient_email VARCHAR(320) NOT NULL COMMENT 'Email address that was notified',
    subject VARCHAR(500) NOT NULL COMMENT 'Email subject that was sent',
    analyzed_dir VARCHAR(500) NOT NULL COMMENT 'Directory that was analyzed',
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'When email was successfully sent',
    
    INDEX idx_recipient_email (recipient_email),
    INDEX idx_sent_at (sent_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Log of successfully sent breach notification emails';

-- ============================================================================
-- 8. DUPLICATE_ORIGINS - Tracks additional sources where the same file was
--    observed (prevents re-download loops and tracks cross-channel presence)
-- ============================================================================
CREATE TABLE IF NOT EXISTS duplicate_origins (
    id INT AUTO_INCREMENT PRIMARY KEY,
    downloaded_file_id INT NOT NULL COMMENT 'Reference to downloaded_files.id (the original file)',
    channel_id VARCHAR(50) NOT NULL COMMENT 'Channel/group where the duplicate was observed',
    message_id VARCHAR(50) DEFAULT NULL COMMENT 'Telegram message ID in the duplicate channel',
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'When the duplicate was first observed',
    
    INDEX idx_dup_origins_file_id (downloaded_file_id),
    INDEX idx_dup_origins_channel_id (channel_id),
    FOREIGN KEY (downloaded_file_id) REFERENCES downloaded_files(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Tracks additional sources where the same file was observed';

-- ============================================================================
-- 9. Create indexes for performance optimization
-- ============================================================================

-- Composite indexes for common query patterns
CREATE INDEX idx_downloaded_files_status_channel ON downloaded_files(status, channel_id);
CREATE INDEX idx_subdirectories_file_status ON subdirectories(downloaded_file_id, status);
CREATE INDEX idx_emails_to_send_status_created ON emails_to_send(status, created_at);

-- ============================================================================
-- 10. Insert default data (if needed)
-- ============================================================================

-- Example: Insert initial channels (uncomment and modify as needed)
-- INSERT IGNORE INTO channel_list (channel_name, channel_id, status) VALUES 
-- ('Example Channel 1', '1921972180', 'ACTIVE'),
-- ('Example Channel 2', '2552846525', 'ACTIVE');

-- Example: Insert initial channel states (uncomment and modify as needed)
-- INSERT IGNORE INTO channel_state (channel_id, last_id) VALUES 
-- ('1921972180', 0),
-- ('2552846525', 0);

-- ============================================================================
-- 10. Database maintenance procedures (optional)
-- ============================================================================

-- Create a view for easy status monitoring
CREATE OR REPLACE VIEW processing_status_summary AS
SELECT 
    status,
    COUNT(*) as file_count,
    MIN(downloaded_at) as oldest_download,
    MAX(downloaded_at) as latest_download
FROM downloaded_files 
GROUP BY status
ORDER BY file_count DESC;

-- Create a view for leak analysis summary
CREATE OR REPLACE VIEW leak_analysis_summary AS
SELECT 
    status,
    COUNT(*) as leak_count,
    SUM(emails) as total_emails,
    SUM(credit_cards) as total_credit_cards,
    COUNT(DISTINCT country) as countries_affected
FROM analyzed_leaks 
GROUP BY status
ORDER BY leak_count DESC;

-- ============================================================================
-- 11. Schema validation and comments
-- ============================================================================

-- Show table information
SELECT 
    TABLE_NAME,
    TABLE_COMMENT,
    TABLE_ROWS,
    CREATE_TIME
FROM INFORMATION_SCHEMA.TABLES 
WHERE TABLE_SCHEMA = 'dumpdb' 
ORDER BY TABLE_NAME;

-- Show foreign key relationships
SELECT 
    TABLE_NAME,
    COLUMN_NAME,
    CONSTRAINT_NAME,
    REFERENCED_TABLE_NAME,
    REFERENCED_COLUMN_NAME
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE 
WHERE REFERENCED_TABLE_SCHEMA = 'dumpdb'
ORDER BY TABLE_NAME, COLUMN_NAME;

-- ============================================================================
-- 12. Migration: add TO_DELETE status to downloaded_files (run on existing DBs)
-- ============================================================================

-- Add TO_DELETE to the status ENUM only if it is not already present
SET @downloaded_files_status_has_to_delete := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = 'dumpdb'
      AND TABLE_NAME = 'downloaded_files'
      AND COLUMN_NAME = 'status'
      AND COLUMN_TYPE LIKE '%''TO_DELETE''%'
);

SET @downloaded_files_status_alter_sql := IF(
    @downloaded_files_status_has_to_delete = 0,
    "ALTER TABLE downloaded_files
        MODIFY COLUMN status ENUM(
            'DOWNLOADING', 'DOWNLOADED', 'UNCOMPRESSED', 'WALKED_DIR', 'PROCESSED', 'PROCESSED_NOMAIL',
            'WRONGPASSWORD', 'OTHERERRORS', 'TODUMP_PASSWORD', 'PASSWORD_DUMPED',
            'ERROR_WALKED_DIR', 'DUPLICATE_FILE', 'TO_DELETE'
        ) DEFAULT 'DOWNLOADED' COMMENT 'Processing status'",
    "SELECT 'TO_DELETE already present in downloaded_files.status' AS migration_status"
);

PREPARE downloaded_files_status_stmt FROM @downloaded_files_status_alter_sql;
EXECUTE downloaded_files_status_stmt;
DEALLOCATE PREPARE downloaded_files_status_stmt;

-- ============================================================================
-- 13. Migration: add DELETED status to downloaded_files (run on existing DBs)
-- ============================================================================

SET @downloaded_files_status_has_deleted := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = 'dumpdb'
      AND TABLE_NAME = 'downloaded_files'
      AND COLUMN_NAME = 'status'
      AND COLUMN_TYPE LIKE '%''DELETED''%'
);

SET @downloaded_files_status_alter_deleted := IF(
    @downloaded_files_status_has_deleted = 0,
    "ALTER TABLE downloaded_files
        MODIFY COLUMN status ENUM(
            'DOWNLOADING', 'DOWNLOADED', 'UNCOMPRESSED', 'WALKED_DIR', 'PROCESSED', 'PROCESSED_NOMAIL',
            'WRONGPASSWORD', 'OTHERERRORS', 'TODUMP_PASSWORD', 'PASSWORD_DUMPED',
            'ERROR_WALKED_DIR', 'DUPLICATE_FILE', 'TO_DELETE', 'DELETED'
        ) DEFAULT 'DOWNLOADED' COMMENT 'Processing status'",
    "SELECT 'DELETED already present in downloaded_files.status' AS migration_status"
);

PREPARE downloaded_files_status_deleted_stmt FROM @downloaded_files_status_alter_deleted;
EXECUTE downloaded_files_status_deleted_stmt;
DEALLOCATE PREPARE downloaded_files_status_deleted_stmt;

-- ============================================================================
-- 13.5 Migration: add REPROCESS status to downloaded_files (run on existing DBs)
-- ============================================================================

SET @downloaded_files_status_has_reprocess := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = 'dumpdb'
      AND TABLE_NAME = 'downloaded_files'
      AND COLUMN_NAME = 'status'
      AND COLUMN_TYPE LIKE '%''REPROCESS''%'
);

SET @downloaded_files_status_alter_reprocess := IF(
    @downloaded_files_status_has_reprocess = 0,
    "ALTER TABLE downloaded_files
        MODIFY COLUMN status ENUM(
            'DOWNLOADING', 'DOWNLOADED', 'UNCOMPRESSED', 'WALKED_DIR', 'PROCESSED', 'PROCESSED_NOMAIL',
            'WRONGPASSWORD', 'OTHERERRORS', 'TODUMP_PASSWORD', 'PASSWORD_DUMPED',
            'ERROR_WALKED_DIR', 'DUPLICATE_FILE', 'TO_DELETE', 'DELETED', 'REPROCESS'
        ) DEFAULT 'DOWNLOADED' COMMENT 'Processing status'",
    "SELECT 'REPROCESS already present in downloaded_files.status' AS migration_status"
);

PREPARE downloaded_files_status_reprocess_stmt FROM @downloaded_files_status_alter_reprocess;
EXECUTE downloaded_files_status_reprocess_stmt;
DEALLOCATE PREPARE downloaded_files_status_reprocess_stmt;

-- ============================================================================
-- 14. Migration: add LEFT status to channel_list.status (run on existing DBs)
-- ============================================================================

SET @channel_list_status_has_left := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = 'dumpdb'
      AND TABLE_NAME = 'channel_list'
      AND COLUMN_NAME = 'status'
      AND COLUMN_TYPE LIKE '%''LEFT''%'
);

SET @channel_list_status_alter := IF(
    @channel_list_status_has_left = 0,
    "ALTER TABLE channel_list
        MODIFY COLUMN status ENUM('ACTIVE', 'DISABLED', 'LEFT') DEFAULT 'ACTIVE' COMMENT 'Channel monitoring status'",
    "SELECT 'LEFT already present in channel_list.status' AS migration_status"
);

PREPARE channel_list_status_stmt FROM @channel_list_status_alter;
EXECUTE channel_list_status_stmt;
DEALLOCATE PREPARE channel_list_status_stmt;

-- ============================================================================
-- 15. Migration: add PROCESSED_EMAILS_ALREADY_SENT to downloaded_files.status
-- ============================================================================

SET @has_emails_already_sent := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = 'dumpdb'
      AND TABLE_NAME = 'downloaded_files'
      AND COLUMN_NAME = 'status'
      AND COLUMN_TYPE LIKE '%''PROCESSED_EMAILS_ALREADY_SENT''%'
);

SET @alter_emails_already_sent := IF(
    @has_emails_already_sent = 0,
    "ALTER TABLE downloaded_files
        MODIFY COLUMN status ENUM(
            'DOWNLOADING', 'DOWNLOADED', 'UNCOMPRESSED', 'WALKED_DIR', 'PROCESSED', 'PROCESSED_NOMAIL', 'PROCESSED_EMAILS_ALREADY_SENT',
            'WRONGPASSWORD', 'OTHERERRORS', 'TODUMP_PASSWORD', 'PASSWORD_DUMPED',
            'ERROR_WALKED_DIR', 'DUPLICATE_FILE', 'TO_DELETE', 'DELETED', 'REPROCESS'
        ) DEFAULT 'DOWNLOADED' COMMENT 'Processing status'",
    "SELECT 'PROCESSED_EMAILS_ALREADY_SENT already present in downloaded_files.status' AS migration_status"
);

PREPARE emails_already_sent_stmt FROM @alter_emails_already_sent;
EXECUTE emails_already_sent_stmt;
DEALLOCATE PREPARE emails_already_sent_stmt;

-- ============================================================================
-- Migration: add CHANNEL_LIMIT status to discovered_links (run on existing DBs)
-- ============================================================================

SET @discovered_links_status_has_channel_limit := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = 'dumpdb'
      AND TABLE_NAME = 'discovered_links'
      AND COLUMN_NAME = 'status'
      AND COLUMN_TYPE LIKE '%''CHANNEL_LIMIT''%'
);

SET @discovered_links_status_alter := IF(
    @discovered_links_status_has_channel_limit = 0,
    "ALTER TABLE discovered_links
        MODIFY COLUMN status ENUM(
            'INSERTED', 'PROCESSING', 'PROCESSED', 'FAILED', 'NEW_SUBSCRIBED', 'ALREADY_SUBSCRIBED', 'JOIN_REQUESTED', 'CHANNEL_LIMIT'
        ) DEFAULT 'INSERTED' COMMENT 'Processing status'",
    "SELECT 'CHANNEL_LIMIT already present in discovered_links.status' AS migration_status"
);

PREPARE discovered_links_status_stmt FROM @discovered_links_status_alter;
EXECUTE discovered_links_status_stmt;
DEALLOCATE PREPARE discovered_links_status_stmt;
