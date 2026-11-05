"""Test bootstrap: mock unavailable modules before test imports.

This file is discovered automatically by pytest, but the bootstrap is also
exposed as a regular function so unittest-based tests can import and call it
explicitly before importing application modules.
"""

import sys
from unittest import mock


def mock_config():
    """Install a lightweight mock for the gitignored config module."""
    fake_config = mock.MagicMock()
    fake_config.PASSWORD_QUEUE = "password_file"
    fake_config.SUCCESS_QUEUE = "uncompressed_file"
    fake_config.COMBO_QUEUE = "combo_queue"
    fake_config.PASSWORDDUMP_QUEUE = "passworddump_queue"
    fake_config.RABBIT_CONNECTION_STRING = "amqp://guest:guest@localhost/"
    fake_config.UNCOMPRESS_DIR = "/tmp/uncompressed"
    fake_config.DOWNLOAD_DIR = "/tmp/downloads"
    fake_config.DB_CONNECTION_STRING = "mysql+pymysql://user:pass@localhost/db"
    sys.modules["config"] = fake_config
    return fake_config


def bootstrap_test_modules():
    """Install lightweight test doubles for optional local modules.

    config.py is gitignored and not present in the repository.
    Provides a lightweight mock so that modules importing it can be loaded in
    tests.
    """
    fake_config = mock_config()
    sys.modules.setdefault("utils", mock.MagicMock())
    sys.modules.setdefault("pika", mock.MagicMock())
    sys.modules.setdefault("pika.exceptions", mock.MagicMock())
    sys.modules.setdefault("sqlalchemy", mock.MagicMock())


def pytest_configure(config):
    bootstrap_test_modules()
