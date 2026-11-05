import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import config
from utils import (
    AnalysisManager,
    RabbitMQConsumer,
    clean_string,
    extract_domain,
    extract_password_patterns,
    get_db_engine,
    normalize_and_filter_emails,
    parse_soft_passwords,
    parse_system_info,
    path_contains_ignored_part,
    setup_logging,
)

logger = logging.getLogger(__name__)

engine = get_db_engine()

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Threshold for ignoring directories with too many emails. Configurable
# via env var.
EMAILS_IGNORE_THRESHOLD = int(os.getenv("ANALYZE_IGNORE_EMAILS_THRESHOLD", "50"))


def update_subdir_status(
    analysis_manager,
    file_id,
    subdir,
    status,
    emails_count=0,
    credit_cards_count=0,
    passwords_count=0,
):
    analysis_manager.update_subdir_status(
        file_id, subdir, status, emails_count, credit_cards_count, passwords_count
    )
    logger.info(
        "Updated subdirectory: status=PROCESSED_%s, emails=%d, credit_cards=%d, passwords=%d for file_id=%s, subdirectory=%s",
        status,
        emails_count,
        credit_cards_count,
        passwords_count,
        file_id,
        Path(subdir).name,
    )


def save_to_db(
    analysis_manager,
    subdir_id,
    parent_path,
    analyzed_dir,
    status,
    leak_date,
    country,
    emails,
    credit_cards,
):
    analysis_manager.save_leak(
        subdir_id,
        parent_path,
        analyzed_dir,
        status,
        leak_date,
        country,
        emails,
        credit_cards,
    )
    logger.info(
        "Saved to database: subdir_id=%s, parent_path=%s, analyzed_dir=%s, status=%s, leak_date=%s, country=%s, emails=%s, credit_cards=%s",
        subdir_id,
        parent_path,
        analyzed_dir,
        status,
        leak_date,
        country,
        emails,
        credit_cards,
    )


def load_email_template(recipient_email, hosts_with_password, total_password_count):
    template_name = "mail_template_en.html"
    template_path = TEMPLATES_DIR / template_name

    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()

        subject_match = re.match(r"Subject:\s*(.+)", template)
        subject = (
            subject_match.group(1)
            if subject_match
            else "Allarme: Possibile Data Breach Sospetto"
        )
        html_content = re.sub(r"Subject:\s*.+\n?", "", template, count=1)

        # Limit to a maximum of 10 services and list only the websites, never passwords
        displayed_hosts = list(hosts_with_password.keys())[:10]
        host_list = "".join(f"<li>{host}</li>" for host in displayed_hosts)
        if not host_list:
            host_list = "<li>No specific data available.</li>"

        html_content = html_content.format(
            recipient_email=recipient_email,
            total_uri_count=total_password_count,
            host_list=host_list,
        )

        return subject, html_content
    except Exception as e:
        logger.error("Error loading template %s: %s", template_path, str(e))
        raise


def is_email_already_processed(analysis_manager, recipient_email):
    if analysis_manager.has_been_sent(
        recipient_email
    ) or analysis_manager.is_email_queued(recipient_email):
        return True
    return False


def store_email_to_send(
    analysis_manager,
    analyzed_dir,
    recipient_email,
    subject,
    html_content,
    leak_date,
    queue_status="PENDING",
):
    logger.debug("Check for email already sent to %s", recipient_email)
    if is_email_already_processed(analysis_manager, recipient_email):
        logger.warning(
            "Email %s ignored: already sent or queued for sending", recipient_email
        )
        return False

    logger.debug("Devo inviare email a %s", recipient_email)
    analysis_manager.store_email(
        analyzed_dir,
        recipient_email,
        subject,
        html_content,
        leak_date,
        queue_status,
    )
    return True


def update_status_to_processed(analysis_manager, analyzed_dir):
    analysis_manager.update_leak_status(analyzed_dir, "PROCESSED")
    logger.info("Status updated to PROCESSED for: %s", analyzed_dir)


def update_status_to_processed_nomail(analysis_manager, analyzed_dir):
    analysis_manager.update_leak_status(analyzed_dir, "PROCESSED_NOMAIL")
    logger.info(
        "Status updated to PROCESSED_NOMAIL for: %s (no emails stored)", analyzed_dir
    )


def update_status_to_emails_already_sent(analysis_manager, analyzed_dir):
    analysis_manager.update_leak_status(analyzed_dir, "PROCESSED_EMAILS_ALREADY_SENT")
    logger.info(
        "Status updated to PROCESSED_EMAILS_ALREADY_SENT for: %s (all emails already sent)",
        analyzed_dir,
    )


def analyze_directory(directory, pre_country=None, pre_date=None):
    country = pre_country or "Not found"
    emails = set()
    hosts_with_password = {}
    credit_cards = []
    country_found = pre_country is not None
    data_leak = pre_date

    country_files = ["information.txt", "system.txt"]
    excluded_dirs = {"ok", "ko", "suspect", "leaks", "nomail"}

    files_to_analyze = [
        f
        for f in Path(directory).rglob("*")
        if not any(
            excluded_dir in [part.lower() for part in f.parts]
            for excluded_dir in excluded_dirs
        )
        and not path_contains_ignored_part(f)
    ]

    if not files_to_analyze:
        return (
            country,
            emails,
            hosts_with_password,
            credit_cards,
            country_found,
            False,
            data_leak,
        )

    total_items = len(files_to_analyze)
    logger.info(
        "Found %d items (files and directories) to analyze under %s",
        total_items,
        directory,
    )
    start_time = time.time()
    files_processed = 0

    logger.info("Starting analyze_directory for %s", directory)

    for idx, file_path in enumerate(files_to_analyze, start=1):
        if not file_path.is_file():
            continue
        # Skip SQLite database files to avoid binary parsing and false
        # positives
        if (
            file_path.suffix.lower() == ".sqlite"
            or file_path.suffix.lower() == ".sqlite-shm"
            or file_path.suffix.lower() == ".sqlite-wal"
        ):
            logger.info("Skipping .sqlite file for password search: %s", file_path)
            continue
        file_name = file_path.name.lower()
        logger.info("Analyzing file %s", file_path)
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = clean_string(f.read())
                logger.debug("File content loaded for %s", file_path)
                if file_name == "system.txt" and not country_found:
                    try:
                        _, local_date_str, country_val = parse_system_info(file_path)
                        if country_val:
                            country = country_val
                            country_found = True
                            logger.info("Country found in %s: %s", file_path, country)
                        if local_date_str:
                            try:
                                data_leak = datetime.strptime(
                                    local_date_str, "%Y-%m-%d %H:%M:%S"
                                )
                                logger.debug(
                                    "Data leak found in %s: %s", file_path, data_leak
                                )
                            except ValueError as e:
                                logger.warning(
                                    "Error parsing date in %s: %s, %s",
                                    file_path,
                                    local_date_str,
                                    e,
                                )
                    except Exception as e:
                        logger.debug(
                            "Error using parse_system_info in %s: %s", file_path, e
                        )
                    continue
                logger.info("Checking for country files in %s", file_path)
                if file_name in country_files and not country_found:
                    try:
                        _, _, country_val = parse_system_info(file_path)
                        if country_val:
                            country = country_val
                            country_found = True
                            logger.debug("Country found in %s: %s", file_path, country)
                    except Exception as e:
                        logger.debug(
                            "Error using parse_system_info in %s: %s", file_path, e
                        )
                    continue
                logger.info("Searching for passwords in %s", file_path)
                if "password" in file_name:
                    logger.debug("Password file found: %s", file_path)
                    logger.debug("Using safe parser to find passwords in %s", file_path)
                    try:
                        new_matches = parse_soft_passwords(content)
                    except Exception as e:
                        logger.error(
                            "Error parsing SOFT blocks in %s: %s", file_path, e
                        )
                        new_matches = []
                    logger.debug(
                        "Safe parser matches found in %s: %d",
                        file_path,
                        len(new_matches),
                    )
                    for host, user, pwd in new_matches:
                        domain = extract_domain(host)
                        hosts_with_password[domain] = clean_string(pwd)
                        if re.match(
                            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", user
                        ):
                            emails.add(clean_string(user).lower())

                    # Use centralized pattern extractor for Host:Password and
                    # fallback
                    password_matches = extract_password_patterns(content)
                    for host, pwd in password_matches:
                        domain = extract_domain(host)
                        hosts_with_password[domain] = clean_string(pwd)

                    email_matches = re.findall(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", content
                    )
                    emails.update(
                        clean_string(email).lower() for email in email_matches
                    )
        except Exception as e:
            logger.error(f"Error analyzing {file_path}: {e}")
            # Only count files (not directories) for progress
            if file_path.is_file():
                files_processed += 1
                # log more frequently for small directories
                if files_processed % 10 == 0 or files_processed <= 10:
                    elapsed = time.time() - start_time
                    logger.debug(
                        "Processed %d files (item %d/%d), elapsed %.1fs, last file: %s",
                        files_processed,
                        idx,
                        total_items,
                        elapsed,
                        file_path,
                    )

    for dir_path in files_to_analyze:
        if dir_path.is_dir() and "creditcard" in dir_path.name.lower():
            logger.info(f"Found CreditCards directory: {dir_path}")
            for cc_file in dir_path.rglob("*"):
                if cc_file.is_file():
                    try:
                        with open(cc_file, "r", encoding="utf-8", errors="ignore") as f:
                            content = clean_string(f.read())
                            cc_pattern = re.compile(
                                r"CN:\s*(\d{16})\nDATE:\s*(\d{1,2}/\d{4})\nNAME:.*?\nTARGET:",
                                re.DOTALL,
                            )
                            cc_matches = cc_pattern.findall(content)
                            for cn, date in cc_matches:
                                credit_cards.append(
                                    (clean_string(cn), clean_string(date))
                                )
                                logger.info(
                                    "Found credit card in %s: CN=%s, DATE=%s",
                                    cc_file,
                                    cn,
                                    date,
                                )
                    except Exception as e:
                        logger.error("Error analyzing file %s: %s", cc_file, e)

    passwords_file_found = bool(hosts_with_password or credit_cards)
    total_elapsed = time.time() - start_time
    logger.info(
        "Finished analyze_directory: files_processed=%d, hosts_with_password=%d, credit_cards=%d, emails=%d, elapsed=%.1fs",
        files_processed,
        len(hosts_with_password),
        len(credit_cards),
        len(emails),
        total_elapsed,
    )
    if data_leak is None:
        logger.warning(
            "Data leak not found for %s, left as NULL in database", directory
        )

    return (
        country,
        emails,
        hosts_with_password,
        credit_cards,
        country_found,
        passwords_file_found,
        data_leak,
    )


def process_uncompressed_directory(
    uncompressed_dir, file_id, subdir_id, analysis_manager
):
    if not Path(uncompressed_dir).exists():
        logger.warning(f"Directory not found: {uncompressed_dir}")
        return

    logger.info(f"Analyzing directory: {uncompressed_dir}")

    excluded_dirs = {"ok", "ko", "suspect", "leaks", "nomail"}
    directory = Path(uncompressed_dir)
    directory_name = directory.name

    if directory_name.lower() in excluded_dirs:
        logger.info(f"Excluded subdirectory: {uncompressed_dir}")
        analysis_manager.update_subdir_status(file_id, uncompressed_dir, "EXCLUDED")
        return

    t0 = time.time()

    # OPTIMIZATION: Retrieve system info from DB instead of re-parsing files
    country, local_date, install_date = analysis_manager.get_subdir_info(
        file_id, uncompressed_dir
    )
    country_found = country is not None
    data_leak = None

    if country_found:
        # Use local_date as the data_leak date if available
        if local_date:
            try:
                data_leak = datetime.strptime(local_date, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

    # If not found in DB, we can fallback to parsing (optional, but for consistency we use DB)
    # For this optimization, we trust the DB data populated by
    # process_subdirectories.py

    # Now analyze the content of the directory
    emails = set()
    hosts_with_password = {}
    credit_cards = []

    # Logic to find passwords/emails/ccs
    # We still need to call a modified analyze_directory or just integrate the logic
    # Let's call analyze_directory but pass the already found country/date

    # To keep it clean, I'll modify analyze_directory to accept optional
    # pre-found info
    (
        country,
        emails,
        hosts_with_password,
        credit_cards,
        country_found_update,
        passwords_file_found,
        data_leak_update,
    ) = analyze_directory(directory, pre_country=country, pre_date=data_leak)

    # Merge findings
    final_country = country if country else "Not found"
    final_data_leak = data_leak_update if data_leak_update else data_leak

    t_analyze = time.time() - t0
    logger.info(
        "analyze_directory returned: country=%s, emails=%d, hosts_with_password=%d, credit_cards=%d, country_found=%s, passwords_file_found=%s, leak_date=%s (took %.1fs)",
        final_country,
        len(emails),
        len(hosts_with_password),
        len(credit_cards),
        country_found or country_found_update,
        passwords_file_found,
        final_data_leak,
        t_analyze,
    )

    has_useful_info = bool(emails or hosts_with_password or credit_cards)
    # If the directory contains an excessive number of emails, mark it as
    # ignored and skip processing
    if len(emails) > EMAILS_IGNORE_THRESHOLD:
        logger.warning(
            "Directory %s contains %d emails which exceeds threshold %d: marking as PROCESSED_IGNORED_LOT_OF_EMAILS and skipping processing",
            uncompressed_dir,
            len(emails),
            EMAILS_IGNORE_THRESHOLD,
        )
        try:
            analysis_manager.update_subdir_status(
                file_id,
                uncompressed_dir,
                "IGNORED_LOT_OF_EMAILS",
                emails_count=len(emails),
                credit_cards_count=len(credit_cards),
                passwords_count=len(hosts_with_password),
            )
        except Exception as e:
            logger.error(
                "Failed to update subdirectory status to PROCESSED_IGNORED_LOT_OF_EMAILS for %s: %s",
                uncompressed_dir,
                e,
            )

        return
    if len(emails) > 15:
        status = "SUSPECT"
    elif has_useful_info:
        if len(emails) == 0:
            status = "NOMAIL"
        else:
            status = "OK"
    else:
        status = "KO"

    # Calculate counts for subdirectory update
    emails_count = len(emails)
    credit_cards_count = len(credit_cards)
    passwords_count = len(hosts_with_password)

    # Save analysis result to DB and update subdir status with timing
    t_db_start = time.time()
    logger.info("Saving analyzed_leak to DB for %s", uncompressed_dir)
    save_to_db(
        analysis_manager,
        subdir_id,
        str(Path(uncompressed_dir).parent),
        str(uncompressed_dir),
        status,
        final_data_leak,
        final_country,
        emails_count,
        credit_cards_count,
    )
    logger.info(
        "Saved analyzed_leak to DB for %s (%.2fs)",
        uncompressed_dir,
        time.time() - t_db_start,
    )

    t_update_start = time.time()
    logger.info("Updating subdir status for %s to %s", uncompressed_dir, status)
    update_subdir_status(
        analysis_manager,
        file_id,
        uncompressed_dir,
        status,
        emails_count,
        credit_cards_count,
        passwords_count,
    )
    logger.info(
        "Updated subdir status for %s (%.2fs)",
        uncompressed_dir,
        time.time() - t_update_start,
    )

    recipient_emails = normalize_and_filter_emails(emails)
    total_password_count = len(hosts_with_password)

    # If there are no discovered passwords (leaks), do not send/store emails.
    # Mark the subdirectory as PROCESSED_NOPASSWORD and return early.
    if total_password_count == 0:
        logger.info(
            "No passwords (leaks) found in %s (emails=%d, credit_cards=%d): marking as PROCESSED_NOPASSWORD and skipping email storage",
            uncompressed_dir,
            len(recipient_emails),
            len(credit_cards),
        )
        try:
            update_subdir_status(
                analysis_manager,
                file_id,
                uncompressed_dir,
                "NOPASSWORD",
                emails_count=len(recipient_emails),
                credit_cards_count=len(credit_cards),
                passwords_count=0,
            )
        except Exception as e:
            logger.error(
                "Failed to update subdirectory status to PROCESSED_NOPASSWORD for %s: %s",
                uncompressed_dir,
                e,
            )
        return

    if not recipient_emails:
        logger.warning("No valid emails to send report for: %s", uncompressed_dir)
        update_status_to_processed_nomail(analysis_manager, uncompressed_dir)
        return

    logger.debug(
        "Country: %s, Extracted emails: %s, Total passwords: %d, Credit cards: %d",
        final_country,
        ", ".join(emails),
        total_password_count,
        len(credit_cards),
    )
    logger.info(
        "Country: %s, Extracted emails: %d, Total passwords: %d, Credit cards: %d",
        final_country,
        len(emails),
        total_password_count,
        len(credit_cards),
    )

    emails_stored = 0
    total_recipients = len(recipient_emails)
    for idx, email in enumerate(recipient_emails, start=1):
        logger.debug(
            "Preparing email [%d/%d] for recipient=%s", idx, total_recipients, email
        )
        t_template = time.time()
        subject, html_content = load_email_template(
            email, hosts_with_password, total_password_count
        )
        logger.debug("Loaded template for %s (%.2fs)", email, time.time() - t_template)
        t_store = time.time()
        qstatus = "PENDING_SUSPECT" if status == "SUSPECT" else "PENDING"
        stored = store_email_to_send(
            analysis_manager,
            uncompressed_dir,
            email,
            subject,
            html_content,
            final_data_leak,
            queue_status=qstatus,
        )
        elapsed = time.time() - t_store
        logger.info(
            "store_email_to_send [%d/%d] for %s returned=%s (%.2fs)",
            idx,
            total_recipients,
            email,
            stored,
            elapsed,
        )
        if stored:
            emails_stored += 1

    if emails_stored > 0:
        logger.info(
            "Emails stored: %d/%d, marking processed", emails_stored, total_recipients
        )
        update_status_to_processed(analysis_manager, uncompressed_dir)
        logger.info(
            "Marked %s as PROCESSED [%d/%d]",
            uncompressed_dir,
            emails_stored,
            total_recipients,
        )
    elif total_recipients > 0:
        logger.info(
            "No new emails stored (0/%d), marking emails_already_sent",
            total_recipients,
        )
        update_status_to_emails_already_sent(analysis_manager, uncompressed_dir)
        logger.info(
            "Marked %s as PROCESSED_EMAILS_ALREADY_SENT [0/%d]",
            uncompressed_dir,
            total_recipients,
        )
    else:
        logger.info(
            "No emails found, marking processed_nomail"
        )
        update_status_to_processed_nomail(analysis_manager, uncompressed_dir)
        logger.info(
            "Marked %s as PROCESSED_NOMAIL [0]", uncompressed_dir
        )


def main():
    setup_logging()
    engine = get_db_engine()
    analysis_manager = AnalysisManager(engine)
    logger.info("Starting analysis process via RabbitMQ queue")

    # Prefetch and max_messages configuration
    max_messages = 100
    prefetch_count = 10

    def callback(ch, method, properties, body):
        try:
            message = json.loads(body)
            file_id = message.get("file_id")
            subdir_id = message.get("subdir_id")
            complete_subdir_path = message.get("complete_subdir_path")

            if not file_id or not complete_subdir_path:
                logger.error(f"Invalid message format: {message}")
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return

            logger.info(
                f"Processing message: file_id={file_id}, subdir_id={subdir_id}, complete_subdir_path={complete_subdir_path}"
            )
            process_uncompressed_directory(
                complete_subdir_path, file_id, subdir_id, analysis_manager
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            ch.basic_ack(delivery_tag=method.delivery_tag)

    consumer = RabbitMQConsumer(
        url=config.RABBIT_CONNECTION_STRING,
        queue=config.SUBDIR_QUEUE,
        callback=callback,
        prefetch_count=prefetch_count,
        max_messages=max_messages,
    )
    consumer.start()


if __name__ == "__main__":
    main()
