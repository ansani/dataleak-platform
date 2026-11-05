import logging

from sqlalchemy import text

from utils import get_db_engine, setup_logging

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)


def main():
    engine = get_db_engine()

    try:
        with engine.connect() as conn:
            # 1. Table Sizes and Row Counts
            logger.info("--- Table Sizes and Row Counts ---")
            size_query = text("""
                SELECT 
                    TABLE_NAME, 
                    TABLE_ROWS, 
                    ROUND((DATA_LENGTH + INDEX_LENGTH) / 1024 / 1024, 2) AS size_mb,
                    ROUND(DATA_LENGTH / 1024 / 1024, 2) AS data_mb,
                    ROUND(INDEX_LENGTH / 1024 / 1024, 2) AS index_mb
                FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_SCHEMA = 'dumpdb'
                  AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY (DATA_LENGTH + INDEX_LENGTH) DESC;
            """)
            sizes = conn.execute(size_query).fetchall()
            for row in sizes:
                name, rows_count, size_mb, data_mb, index_mb = row
                logger.info(
                    "Table: %-20s Rows: %-12s Total: %8s MB (Data: %7s MB, Index: %7s MB)",
                    name,
                    rows_count if rows_count is not None else "n/a",
                    size_mb if size_mb is not None else "n/a",
                    data_mb if data_mb is not None else "n/a",
                    index_mb if index_mb is not None else "n/a",
                )

            # 2. Status Distribution
            tables_with_status = {
                "downloaded_files": "status",
                "discovered_links": "status",
                "subdirectories": "status",
                "emails_to_send": "status",
            }

            logger.info("\n--- Status Distribution ---")
            for table, col in tables_with_status.items():
                logger.info(f"Table: {table}")
                status_query = text(
                    f"SELECT {col}, COUNT(*) as cnt FROM {table} GROUP BY {col} ORDER BY cnt DESC"
                )
                statuses = conn.execute(status_query).fetchall()
                for row in statuses:
                    logger.info(f"  {row[0]:<20} : {row[1]}")

            # 3. Redundancy Analysis: subdirectories vs analyzed_leaks
            logger.info(
                "\n--- Redundancy Analysis: subdirectories vs analyzed_leaks ---"
            )
            # Check how many subdirectories are in analyzed_leaks
            # Note: analyzed_leaks uses 'analyzed_dir' which is essentially the full path
            # subdirectories doesn't store full path, it's computed in v_subdirectories
            redundancy_query = text("""
                SELECT 
                    (SELECT COUNT(*) FROM subdirectories) as total_subdirs,
                    (SELECT COUNT(*) FROM analyzed_leaks) as total_analyzed,
                    (SELECT COUNT(*) FROM v_subdirectories v 
                     JOIN analyzed_leaks a ON v.parent_directory = a.analyzed_dir) as overlap
            """)
            red = conn.execute(redundancy_query).fetchone()
            logger.info(f"Total subdirectories: {red[0]}")
            logger.info(f"Total analyzed_leaks: {red[1]}")
            logger.info(f"Overlap (exact path match): {red[2]}")

            if red[0] > 0:
                overlap_pct = (red[2] / red[0]) * 100
                logger.info(f"Overlap percentage: {overlap_pct:.2f}%")

            # 4. Check for index fragmentation or large gaps (Optional but useful)
            # Just checking if there are any columns with very large VARCHARs that might be wasteful
            logger.info("\n--- Column Size Audit (VARCHARs > 255) ---")
            col_query = text("""
                SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = 'dumpdb' 
                AND DATA_TYPE = 'varchar' 
                AND CHARACTER_MAXIMUM_LENGTH > 255
            """)
            cols = conn.execute(col_query).fetchall()
            for row in cols:
                logger.info(f"Column {row[0]}: length {row[1]}")

    except Exception as e:
        logger.error("Audit failed: %s", e, exc_info=True)


if __name__ == "__main__":
    main()
