#!/usr/bin/env python3
import logging
import subprocess
import time

import requests
from requests.auth import HTTPBasicAuth

import config
from utils import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

# Configuration
QUEUE_NAME = config.SUBDIR_QUEUE
MIN_WORKERS = 1
MAX_WORKERS = 30
MESSAGES_PER_WORKER = 500
TICK_INTERVAL = 30  # seconds
SESSION_NAME = "dataleak_pipeline"


def ensure_session_exists():
    """Ensure that the tmux session exists, creating it if necessary."""
    try:
        subprocess.check_output(
            ["tmux", "has-session", "-t", SESSION_NAME], stderr=subprocess.STDOUT
        )
    except subprocess.CalledProcessError:
        logger.info("Tmux session %s not found. Creating it...", SESSION_NAME)
        # Create a detached session. This creates window 0 by default.
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION_NAME])


def get_queue_depth():
    """Fetch the number of messages in the queue via RabbitMQ Management API."""
    url = f"https://{config.RABBIT_MGMT_HOST}:{config.RABBIT_MGMT_PORT}/api/queues/%2f/{
        QUEUE_NAME
    }"
    try:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(config.RABBIT_MGMT_USER, config.RABBIT_MGMT_PASS),
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("messages", 0)
    except Exception as e:
        logger.error("Failed to fetch queue depth for %s: %s", QUEUE_NAME, e)
        return None


def get_current_worker_count():
    """Count existing analyze windows in tmux."""
    try:
        # List windows and filter those that start with 'analyze_'
        output = subprocess.check_output(
            ["tmux", "list-windows", "-t", SESSION_NAME], text=True
        )
        windows = output.splitlines()
        count = 0
        for w in windows:
            # Window format: index:name[flags]
            if "analyze_" in w:
                count += 1
        return count
    except subprocess.CalledProcessError:
        return 0


def scale_workers(target):
    """Sync current tmux windows with target worker count."""
    current = get_current_worker_count()
    logger.info("Scaling workers: current=%d, target=%d", current, target)

    if target > current:
        # Scale up
        for i in range(current + 1, target + 1):
            window_name = f"analyze_{i}"
            logger.info("Launching new worker window: %s", window_name)
            # Create window and run the command directly. The window will close
            # automatically when the process exits.
            subprocess.run(
                [
                    "tmux",
                    "new-window",
                    "-t",
                    SESSION_NAME,
                    "-n",
                    window_name,
                    ".venv/bin/python analyze.py",
                ]
            )

    elif target < current:
        # Scale down
        for i in range(current, target, -1):
            window_name = f"analyze_{i}"
            logger.info("Killing worker window: %s", window_name)
            subprocess.run(
                ["tmux", "kill-window", "-t", f"{SESSION_NAME}:{window_name}"]
            )


def main():
    logger.info("Starting Analyze Autoscaler...")
    ensure_session_exists()
    logger.info(
        "Config: Queue=%s, Min=%d, Max=%d, Msg/Worker=%d",
        QUEUE_NAME,
        MIN_WORKERS,
        MAX_WORKERS,
        MESSAGES_PER_WORKER,
    )

    while True:
        depth = get_queue_depth()
        if depth is not None:
            # Target = messages / capacity, clamped to min/max
            target = max(
                MIN_WORKERS,
                min(
                    MAX_WORKERS,
                    (depth + MESSAGES_PER_WORKER - 1) // MESSAGES_PER_WORKER,
                ),
            )
            scale_workers(target)

        time.sleep(TICK_INTERVAL)


if __name__ == "__main__":
    main()
