import logging
import os

from sqlalchemy import text

import config
from utils import get_db_engine, setup_logging

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)


def main():
    engine = get_db_engine()

    # We use the view v_subdirectories because it provides the
    # concatenated path
    query = text("SELECT id, parent_directory FROM v_subdirectories")

    to_delete = []
    total_checked = 0

    logger.info("Checking for existing subdirectories on filesystem...")

    try:
        with engine.connect() as conn:
            result = conn.execute(query)
            for row in result:
                total_checked += 1
                # parent_directory is df.saved_file_name / s.subdirectory
                # We prefix it with config.UNCOMPRESS_DIR
                full_path = os.path.join(config.UNCOMPRESS_DIR, row.parent_directory)

                if not os.path.isdir(full_path):
                    to_delete.append(row.id)

    except Exception as e:
        logger.error("Error while querying database: %s", e)
        return

    if not to_delete:
        logger.info("No obsolete subdirectories found. Everything is in sync.")
        return

    logger.info(
        "Found %d obsolete entries out of %d checked.",
        len(to_delete),
        total_checked,
    )

    confirm = input(
        f"Are you sure you want to delete {len(to_delete)} rows "
        "from the 'subdirectories' table? (y/N): "
    )

    if confirm.lower() == "y":
        try:
            with engine.begin() as conn:
                # Use a parameterized query to avoid SQL injection
                # and handle large lists
                # We process in batches if the list is very large
                # to avoid SQL limits
                batch_size = 1000
                for i in range(0, len(to_delete), batch_size):
                    batch = to_delete[i : i + batch_size]
                    # Convert list to comma-separated string for IN clause
                    # Since they are IDs (ints), this is safe
                    ids_str = ",".join(map(str, batch))
                    delete_query = text(
                        f"DELETE FROM subdirectories WHERE id IN ({ids_str})"
                    )
                    conn.execute(delete_query)

            logger.info("Successfully deleted %d obsolete entries.", len(to_delete))
        except Exception as e:
            logger.error("Error while deleting entries: %s", e)
    else:
        logger.info("Operation cancelled by user.")


if __name__ == "__main__":
    main()
