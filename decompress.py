import json
import logging
import os
import shutil
import signal
import threading
from pathlib import Path

from pika.exceptions import (
    AMQPConnectionError,
    ConnectionWrongStateError,
    StreamLostError,
)
from sqlalchemy import text

import config
from utils import (
    DownloadManager,
    RabbitMQConsumer,
    decompress_file,
    get_db_engine,
    get_multipart_regex,
    get_multipart_volume,
    get_publisher,
    handle_failed_publish,
    local_download_path,
    password_dump_path,
    safe_worker_callback,
    setup_logging,
)

logger = logging.getLogger(__name__)

# Path to the external password matrix file (can be overridden via env var)

PASSWORD_MATRIX_FILE = os.environ.get("PASSWORD_MATRIX_FILE", "password_matrix.json")

# Thread-safe container for the live matrix.
# RLock allows the main thread to re-enter the lock if a SIGUSR1 arrives while
# get_password_from_matrix() is already holding it (prevents a potential
# deadlock).
_matrix_lock = threading.RLock()
_password_matrix: dict = {}


def load_password_matrix() -> dict:
    """Load the password lookup matrix from the JSON file.

    Returns an empty dictionary if the file is missing or malformed.
    """
    matrix_path = Path(PASSWORD_MATRIX_FILE)
    try:
        with matrix_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning(
                "'%s' must contain a JSON object, returning empty matrix", matrix_path
            )
            return {}
        logger.info("Loaded %d entries from '%s'", len(data), matrix_path)
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Error loading password matrix from '%s': %s. Returning empty matrix.",
            matrix_path,
            e,
        )
        return {}


def reload_password_matrix() -> None:
    """Reload the password matrix in-place (thread-safe)."""
    new_matrix = load_password_matrix()
    if not new_matrix:
        logger.warning("Reload returned an empty matrix; preserving current matrix")
        return
    with _matrix_lock:
        _password_matrix.clear()
        _password_matrix.update(new_matrix)
    logger.info("Password matrix reloaded: %d entries", len(new_matrix))


def _handle_sigusr1(_signum, _frame):
    """Signal handler: reload the password matrix on SIGUSR1."""
    logger.info(
        "SIGUSR1 received – reloading password matrix from '%s'", PASSWORD_MATRIX_FILE
    )
    reload_password_matrix()


# Perform initial load at import time
reload_password_matrix()


def get_password_from_matrix(file_name: str) -> str:
    """Look up the password for *file_name* in the current live matrix."""
    with _matrix_lock:
        matrix_snapshot = dict(_password_matrix)
    for pattern, password in matrix_snapshot.items():
        if pattern in file_name:
            return password
    return ""


def is_archive_file(file_path: Path) -> bool:
    """Check if the file is an archive based on its extension."""
    return file_path.suffix.lower() in (".zip", ".rar", ".7z")


def get_decompression_error_type(e: Exception) -> tuple[str, str]:
    """Map an exception to an error type and a status value."""
    error_message = str(e)
    error_type = "decompression_error"

    if isinstance(e, FileNotFoundError):
        error_type = "file_not_found"
    elif isinstance(e, RuntimeError) and "Incorrect password" in error_message:
        error_type = "incorrect_password"
    elif isinstance(e, ValueError) and "Unsupported archive type" in error_message:
        error_type = "unsupported_archive"

    status_val = (
        "WRONGPASSWORD" if error_type == "incorrect_password" else "OTHERERRORS"
    )
    return error_type, status_val


def decompress_with_retry(
    file_path: Path,
    password: str,
    retry_count: int,
    file_id: int,
    passwordfile_publisher,
) -> tuple[Path, int | None]:
    """Helper function to decompress a file with retry logic for password."""
    try:
        logger.info(f"Calling decompress_file for {file_path}")
        result = decompress_file(file_path, password, config.UNCOMPRESS_DIR)
        if result is None:
            raise RuntimeError("decompress_file returned None")

        extracted_dir, volume_count = result
        logger.info(f"decompress_file returned: {extracted_dir}, {volume_count}")

        # Check if the extracted directory contains exactly one archive file
        extracted_files = list(extracted_dir.glob("*"))
        if len(extracted_files) == 1 and is_archive_file(extracted_files[0]):
            logger.info("Nested archive detected: %s", extracted_files[0])
            nested_file = extracted_files[0]
            nested_password = get_password_from_matrix(nested_file.name) or password
            logger.info(
                "Decompressing nested archive %s with password '%s'",
                nested_file,
                nested_password,
            )

            nested_result = decompress_file(
                nested_file, nested_password, config.UNCOMPRESS_DIR
            )
            if nested_result is None:
                raise RuntimeError("nested decompress_file returned None")

            nested_extracted_dir, nested_volume_count = nested_result
            logger.info(
                f"Nested decompress_file returned: {nested_extracted_dir}, {nested_volume_count}"
            )
            logger.info("Nested archive decompressed to %s", nested_extracted_dir)

            # Remove the intermediate archive file
            try:
                nested_file.unlink()
                logger.info("Removed intermediate archive file: %s", nested_file)
            except Exception as e:
                logger.error(
                    "Failed to remove intermediate archive file %s: %s", nested_file, e
                )

            return nested_extracted_dir, (
                volume_count if volume_count else nested_volume_count
            )

        return extracted_dir, volume_count
    except Exception as e:
        logger.error(f"Exception in decompress_with_retry: {type(e).__name__}: {e}")
        if (
            isinstance(e, RuntimeError)
            and "Incorrect password" in str(e)
            and retry_count < 1
        ):
            new_password = get_password_from_matrix(file_path.name)
            if new_password:
                logger.info(
                    "Retry with matrix password '%s' for %s", new_password, file_path
                )
                retry_message = {
                    "id": file_id,
                    "saved_file_name": file_path.name,
                    "password": new_password,
                    "retry_count": retry_count + 1,
                }
                if passwordfile_publisher.publish(retry_message):
                    logger.info(
                        "Published retry message with new password to %s for %s",
                        config.PASSWORD_QUEUE,
                        file_path,
                    )
                else:
                    logger.error(
                        "Failed to publish retry message to %s for %s",
                        config.PASSWORD_QUEUE,
                        file_path,
                    )
                    handle_failed_publish(retry_message)
                raise Exception("Retry triggered")
        raise


# Module-level publishers are now managed by utils.get_publisher


def process_message(ch, method, properties, body):
    """Process a message from the password_file queue."""
    # Usa publisher condivisi invece di crearli ogni volta
    success_publisher = get_publisher(
        config.RABBIT_CONNECTION_STRING, config.SUCCESS_QUEUE
    )
    combo_publisher = get_publisher(config.RABBIT_CONNECTION_STRING, config.COMBO_QUEUE)
    passwordfile_publisher = get_publisher(
        config.RABBIT_CONNECTION_STRING, config.PASSWORD_QUEUE
    )

    engine = get_db_engine()
    download_manager = DownloadManager(engine)

    try:
        message = json.loads(body)
        file_id = message.get("id")
        saved_file_name = message.get("saved_file_name")
        # Use empty password if not specified
        password = message.get("password", "")
        retry_count = message.get("retry_count", 0)

        if not saved_file_name:
            logger.error("Message missing saved_file_name: %s", message)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        # Handle multipart files: only process the first volume, mark others as
        # TO_DELETE
        vol_index = get_multipart_volume(saved_file_name)
        logger.info(
            "Multipart check for %s: volume_index=%s", saved_file_name, vol_index
        )
        if vol_index is not None and vol_index > 1:
            logger.info(
                "File %s is a multipart volume (%d), marking as TO_DELETE and skipping",
                saved_file_name,
                vol_index,
            )
            download_manager.update_status(file_id, "TO_DELETE")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        if "cookies" in saved_file_name.lower():
            logger.info(
                "File %s contains 'cookies', marking as TO_DELETE and skipping decompression",
                saved_file_name,
            )
            download_manager.update_status(file_id, "TO_DELETE")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        logger.info(
            "Processing %s (id=%s) from %s with password '%s' (retry_count=%s)",
            saved_file_name,
            file_id,
            local_download_path(saved_file_name),
            password,
            retry_count,
        )

        # Check if the file is text (.txt)
        if Path(saved_file_name).suffix.lower() == ".txt":
            # Define paths for relocation
            src_path = local_download_path(saved_file_name)
            dst_path = password_dump_path(saved_file_name)

            try:
                # Ensure the password dump directory exists
                os.makedirs(config.PASSWORD_DUMP_DIR, exist_ok=True)

                # Move the file to the dedicated dump directory
                if os.path.exists(src_path):
                    shutil.move(src_path, dst_path)
                    logger.info(
                        "Moved text file to dump storage: %s -> %s", src_path, dst_path
                    )
                else:
                    logger.warning("Text file not found at expected path: %s", src_path)
            except Exception as e:
                logger.error("Failed to move text file to dump storage: %s", e)

            # Update status and error_description in downloaded_files
            download_manager.update_status(file_id, "TODUMP_PASSWORD")
            logger.info("Updated status to TODUMP_PASSWORD for id=%s", file_id)

            # Publish the message to combo queue with the NEW path
            passworddump_message = {"id": file_id, "file_path": dst_path}
            if combo_publisher.publish(passworddump_message):
                logger.info(
                    "Text file detected and queued for combo processing: %s",
                    passworddump_message,
                )
            else:
                logger.error(
                    "Failed to publish message to %s for %s",
                    config.COMBO_QUEUE,
                    saved_file_name,
                )
                handle_failed_publish(passworddump_message)
            try:
                ch.basic_ack(delivery_tag=method.delivery_tag)
            except (
                StreamLostError,
                ConnectionWrongStateError,
                AMQPConnectionError,
            ) as e:
                logger.error(
                    "Failed to acknowledge message for %s: %s", saved_file_name, e
                )
            return

        # Proceed with decompression for non-text files
        try:
            file_path = Path(local_download_path(saved_file_name))
            extracted_dir, volume_count = decompress_with_retry(
                file_path, password, retry_count, file_id, passwordfile_publisher
            )

            # Proactively mark other volumes as TO_DELETE if this is a
            # multipart archive
            if volume_count and volume_count > 1:
                try:
                    # Fetch the original file name to create a regex pattern
                    # for other volumes
                    with engine.connect() as conn:
                        original_name = conn.execute(
                            text(
                                "SELECT original_file_name FROM downloaded_files WHERE id = :id"
                            ),
                            {"id": file_id},
                        ).scalar()

                    if original_name:
                        pattern = get_multipart_regex(original_name)
                        if pattern:
                            logger.info(
                                "Multipart archive detected (%d volumes). Marking others as TO_DELETE using pattern: %s",
                                volume_count,
                                pattern,
                            )
                            # Use a transaction to mark other volumes
                            with engine.begin() as trans_conn:
                                trans_conn.execute(
                                    text(
                                        "UPDATE downloaded_files SET status = 'TO_DELETE' WHERE original_file_name REGEXP :pattern AND id != :id"
                                    ),
                                    {"pattern": pattern, "id": file_id},
                                )
                except Exception as e:
                    logger.error(
                        "Failed to proactively mark multipart volumes as TO_DELETE: %s",
                        e,
                    )

            # Update status and error_description in downloaded_files
            download_manager.update_status(file_id, "UNCOMPRESSED")
            logger.info("Updated status to UNCOMPRESSED for id=%s", file_id)

            success_message = {"id": file_id, "directory": extracted_dir.as_posix()}
            if success_publisher.publish(success_message):
                logger.info(
                    "Decompressed to %s, success message published", extracted_dir
                )
            else:
                logger.error(
                    "Failed to publish success message for %s", saved_file_name
                )
                handle_failed_publish(success_message)
            # Remove the original archive file after successful decompression
            # and publication
            try:
                if file_path.exists():
                    file_path.unlink()
                    logger.info("Removed original archive file: %s", file_path)
            except Exception as e:
                logger.warning(
                    "Failed to remove original archive file %s: %s", file_path, e
                )
        except Exception as e:
            # If we intentionally triggered a retry, ack and exit
            if str(e) == "Retry triggered":
                try:
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                except (
                    StreamLostError,
                    ConnectionWrongStateError,
                    AMQPConnectionError,
                ) as ack_e:
                    logger.error(
                        "Failed to acknowledge message for %s: %s",
                        saved_file_name,
                        ack_e,
                    )
                return

            error_type, status_val = get_decompression_error_type(e)
            error_message = str(e)

            # Update status and error_description in downloaded_files
            download_manager.update_status(file_id, status_val, error_message)
            logger.info(
                "Updated status to %s and error_description to %s for id=%s",
                status_val,
                error_message,
                file_id,
            )

            try:
                logger.info(
                    "Decompression error detected for %s; status updated in DB, skipping RabbitMQ publication",
                    saved_file_name,
                )
            except Exception as e:
                logger.error(
                    "Unexpected error during error handling for %s: %s",
                    saved_file_name,
                    e,
                )

        try:
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except (StreamLostError, ConnectionWrongStateError, AMQPConnectionError) as e:
            logger.error("Failed to acknowledge message for %s: %s", saved_file_name, e)
    except Exception as e:
        # Log the unexpected error and attempt to ACK the message.
        # Use a safe fallback for saved_file_name if not available in locals().
        file_for_log = saved_file_name if "saved_file_name" in locals() else "unknown"
        logger.error("Unexpected error processing message for %s: %s", file_for_log, e)
        try:
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except (
            StreamLostError,
            ConnectionWrongStateError,
            AMQPConnectionError,
        ) as ack_e:
            logger.error(
                "Failed to acknowledge message for %s: %s", file_for_log, ack_e
            )


callback = safe_worker_callback(process_message)


def main():
    """Main function to start the RabbitMQ consumer."""
    setup_logging()
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, _handle_sigusr1)
        logger.info(
            "SIGUSR1 handler registered – send 'kill -USR1 %d' to reload the password matrix",
            os.getpid(),
        )
    else:
        logger.info(
            "SIGUSR1 is not available on this platform; hot-reload of the password matrix is disabled"
        )
    logger.info("Starting decompress worker for queue '%s'", config.PASSWORD_QUEUE)

    consumer = RabbitMQConsumer(
        url=config.RABBIT_CONNECTION_STRING,
        queue=config.PASSWORD_QUEUE,
        callback=callback,
    )
    consumer.start()


if __name__ == "__main__":
    main()
