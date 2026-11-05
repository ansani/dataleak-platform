import json
import logging
import os
import shutil
import time
from pathlib import Path

from sqlalchemy import text

import config
from utils import (
    AnalysisManager,
    DownloadManager,
    RabbitMQConsumer,
    get_db_engine,
    get_publisher,
    parse_system_info,
    setup_logging,
)

logger = logging.getLogger(__name__)

# SQL statements for subdirectories are now in AnalysisManager
# SQL statements for downloaded_files are now in DownloadManager


def process_message(ch, method, properties, body):
    """Process a message from the uncompressed_file queue."""
    # Usa publisher condiviso invece di crearlo ogni volta
    subdir_publisher = get_publisher(
        config.RABBIT_CONNECTION_STRING, config.SUBDIR_QUEUE
    )

    engine = get_db_engine()
    download_manager = DownloadManager(engine)
    analysis_manager = AnalysisManager(engine)

    try:
        message = json.loads(body)
        file_id = message.get("id")
        directory = message.get("directory")

        if not file_id or not directory:
            logger.error("Invalid message format: %s", message)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        logger.info("Processing message: id=%s, directory=%s", file_id, directory)

        # Controlla lo status del file - se già processato, salta la
        # rielaborazione
        current_status = download_manager.get_status(file_id)
        if current_status and current_status in (
            "WALKED_DIR",
            "DUPLICATE_FILE",
            "PROCESSED",
            "PROCESSED_NOMAIL",
            "PROCESSED_EMAILS_ALREADY_SENT",
        ):
            logger.info(
                "File id=%s already processed with status=%s, skipping",
                file_id,
                current_status,
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        # Verify directory exists
        dir_path = Path(directory)
        if not dir_path.exists() or not dir_path.is_dir():
            logger.error("Directory not found or not a directory: %s", directory)
            download_manager.update_status(
                file_id,
                "OTHERERRORS",
                error_description=f"Directory not found: {directory}",
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        # List subdirectories
        logger.debug("Listing subdirectories in %s", directory)
        subdirectories = [d for d in dir_path.iterdir() if d.is_dir()]
        logger.info("Found %d subdirectories in %s", len(subdirectories), directory)
        if len(subdirectories) == 1:
            # If exactly one subdirectory, use it as the new parent directory
            dir_path = subdirectories[0]
            parent_directory = dir_path.as_posix()
            logger.info(
                "Single subdirectory found, using %s as parent_directory",
                parent_directory,
            )
        else:
            parent_directory = directory

        # List subdirectories of the current directory and process system info
        logger.debug(
            "Listing subdirectories in final parent_directory: %s", parent_directory
        )
        subdirectories = [d for d in dir_path.iterdir() if d.is_dir()]
        logger.info(
            "Processing %d subdirectories from file_id=%s", len(subdirectories), file_id
        )
        inserted_subdirs = 0

        # OPTIMIZATION: Load all existing subdirectories for this file_id at
        # once
        existing_subdir_map = analysis_manager.get_existing_subdirs(file_id)
        logger.debug(
            "Loaded %d existing subdirectories from DB", len(existing_subdir_map)
        )

        # Process subdirectories
        publish_queue = []
        dirs_to_delete = []
        subdirs_to_insert = []

        for subdir in subdirectories:
            logger.debug("Processing subdirectory: %s", subdir.name)
            complete_subdir_path = os.path.join(parent_directory, subdir.name)

            if (len(subdirs_to_insert) + len(dirs_to_delete)) % 50 == 0 and len(
                subdirs_to_insert
            ) > 0:
                logger.info(
                    "Progress: processed %d subdirectories so far",
                    len(subdirs_to_insert) + len(dirs_to_delete),
                )

            if subdir.name in existing_subdir_map:
                existing_file_id = existing_subdir_map[subdir.name]
                if existing_file_id != file_id:
                    logger.warning(
                        "Duplicate subdirectory found in different archive: %s (existing file_id=%s | current file_id=%s). Marking for deletion.",
                        complete_subdir_path,
                        existing_file_id,
                        file_id,
                    )
                    dirs_to_delete.append(complete_subdir_path)
                continue

            install_date, local_date, country = None, None, None
            subdir_path = Path(complete_subdir_path)

            try:
                system_files = list(subdir_path.glob("UserInformation.txt")) + list(
                    subdir_path.glob("System.txt")
                )
                if system_files:
                    install_date, local_date, country = parse_system_info(
                        system_files[0]
                    )
            except Exception as e:
                logger.debug(
                    "Error looking for system info in %s: %s", complete_subdir_path, e
                )

            # Buffer for bulk insertion
            subdirs_to_insert.append(
                {
                    "file_id": file_id,
                    "subdir": subdir.name,
                    "install_date": install_date,
                    "local_date": local_date,
                    "country": country,
                }
            )

        # 1. Bulk Insert all new subdirectories
        if subdirs_to_insert:
            try:
                analysis_manager.insert_subdirs_batch(subdirs_to_insert)
                logger.info(
                    "Bulk inserted %d subdirectories for file_id=%s",
                    len(subdirs_to_insert),
                    file_id,
                )
            except Exception as e:
                logger.error(
                    "Failed bulk insert of subdirectories for file_id=%s: %s",
                    file_id,
                    e,
                )
                # If bulk fails, we might have partial inserts. We'll rely on
                # the DB unique constraint.

        # 2. Fetch the generated IDs for these subdirectories to publish to RabbitMQ
        # We only fetch for the current file_id
        with get_db_engine().begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, subdirectory FROM subdirectories WHERE downloaded_file_id = :id"
                ),
                {"id": file_id},
            ).fetchall()
            id_map = {row.subdirectory: row.id for row in rows}

        # 3. Prepare messages for RabbitMQ based on the mapped IDs
        for subdir in subdirectories:
            if subdir.name in id_map:
                subdir_id = id_map[subdir.name]
                complete_subdir_path = os.path.join(parent_directory, subdir.name)
                publish_queue.append(
                    {
                        "subdir_id": subdir_id,
                        "file_id": file_id,
                        "complete_subdir_path": complete_subdir_path,
                    }
                )

        inserted_subdirs = len(subdirs_to_insert)

        # Update status and count in downloaded_files
        status_val = "DUPLICATE_FILE" if inserted_subdirs == 0 else "WALKED_DIR"
        download_manager.update_status(
            file_id, status_val, subdir_count=inserted_subdirs
        )

        logger.info(
            "Updated status to %s and subdir_count to %d for file_id=%s",
            status_val,
            inserted_subdirs,
            file_id,
        )

        # Publish messages
        for msg in publish_queue:
            published = False
            for attempt in range(3):
                try:
                    if subdir_publisher.publish(msg):
                        logger.info(
                            "Published subdirectory message to %s: subdir_id=%s, file_id=%s",
                            config.SUBDIR_QUEUE,
                            msg["subdir_id"],
                            msg["file_id"],
                        )
                        published = True
                        break
                except Exception as e:
                    logger.error(
                        "Error publishing subdirectory message on attempt %d for subdir_id=%s: %s",
                        attempt + 1,
                        msg.get("subdir_id"),
                        e,
                    )
                time.sleep(1)

            if not published:
                logger.error(
                    "Failed to publish subdirectory message after retries: %s", msg
                )

        # Delete duplicate directories
        for dir_path_to_delete in dirs_to_delete:
            try:
                if os.path.exists(dir_path_to_delete):
                    shutil.rmtree(dir_path_to_delete)
                    logger.info(
                        "Deleted duplicate directory from filesystem: %s",
                        dir_path_to_delete,
                    )
            except Exception as e:
                logger.error(
                    "Failed to delete duplicate directory %s: %s", dir_path_to_delete, e
                )

        ch.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        fid = locals().get("file_id", "unknown")
        logger.error("Error processing message for id=%s: %s", fid, e)
        try:
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            logger.info("Re-queued message for id=%s", fid)
        except Exception as nack_err:
            logger.error("Failed to nack message for id=%s: %s", fid, nack_err)


def callback(ch, method, properties, body):
    """Callback function for RabbitMQ consumer."""
    process_message(ch, method, properties, body)


def main():
    """Main function to start the RabbitMQ consumer with robust connection handling."""
    setup_logging()
    logger.info("Starting subdirectory processor for queue '%s'", config.SUCCESS_QUEUE)

    consumer = RabbitMQConsumer(
        url=config.RABBIT_CONNECTION_STRING,
        queue=config.SUCCESS_QUEUE,
        callback=callback,
    )
    consumer.start()


if __name__ == "__main__":
    main()
