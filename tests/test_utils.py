import tempfile
import unittest
from unittest import mock

import pika

from tests.conftest import mock_config

cfg = mock_config()

from utils import (
    DownloadManager,
    RabbitMQConsumer,
    RabbitMQPublisher,
    StateManager,
    calculate_file_hash,
    handle_failed_publish,
)


class TestRabbitMQPublisher(unittest.TestCase):
    def setUp(self):
        self.url = cfg.RABBIT_CONNECTION_STRING
        self.queue = "test_queue"

    @mock.patch("utils.get_rabbit_connection")
    def test_publish_success(self, mock_get_conn):
        mock_conn = mock.MagicMock()
        mock_channel = mock.MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.channel.return_value = mock_channel

        publisher = RabbitMQPublisher(self.url, self.queue)
        result = publisher.publish({"test": "data"})

        self.assertTrue(result)
        mock_channel.basic_publish.assert_called_once()

    @mock.patch("utils.get_rabbit_connection")
    def test_publish_retry_on_failure(self, mock_get_conn):
        mock_conn = mock.MagicMock()
        mock_channel = mock.MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.channel.return_value = mock_channel

        # Fail first, succeed second
        mock_channel.basic_publish.side_effect = [
            pika.exceptions.AMQPConnectionError(),
            True,
        ]

        publisher = RabbitMQPublisher(self.url, self.queue)
        # Set backoff to 0 for fast tests
        result = publisher.publish({"test": "data"}, max_retries=2, backoff_factor=0)

        self.assertTrue(result)
        self.assertEqual(mock_channel.basic_publish.call_count, 2)


class TestRabbitMQConsumer(unittest.TestCase):
    def setUp(self):
        self.url = cfg.RABBIT_CONNECTION_STRING
        self.queue = "test_queue"

    @mock.patch("utils.get_rabbit_connection")
    def test_consumer_connects_and_consumes(self, mock_get_conn):
        mock_conn = mock.MagicMock()
        mock_channel = mock.MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.channel.return_value = mock_channel

        # Make start_consuming return immediately to avoid blocking
        mock_channel.start_consuming.side_effect = KeyboardInterrupt()

        callback = mock.MagicMock()
        consumer = RabbitMQConsumer(self.url, self.queue, callback)
        consumer.start()

        mock_channel.queue_declare.assert_called_with(queue=self.queue, durable=True)
        mock_channel.basic_qos.assert_called_with(prefetch_count=1)
        mock_channel.basic_consume.assert_called_once()


class TestDownloadManager(unittest.TestCase):
    def setUp(self):
        self.mock_engine = mock.MagicMock()
        self.manager = DownloadManager(self.mock_engine)

    def test_check_duplicate(self):
        # Mock SQLAlchemy execute().first()
        mock_conn = self.mock_engine.begin.return_value.__enter__.return_value
        mock_conn.execute.return_value.first.return_value = (1,)

        self.assertTrue(self.manager.check_duplicate("key1"))

        mock_conn.execute.return_value.first.return_value = None
        self.assertFalse(self.manager.check_duplicate("key2"))

    def test_start_download(self):
        mock_conn = self.mock_engine.begin.return_value.__enter__.return_value
        self.manager.start_download("k", "orig", "saved", "cid", "msg", 100)
        mock_conn.execute.assert_called_once()


class TestStateManager(unittest.TestCase):
    def setUp(self):
        self.mock_engine = mock.MagicMock()
        self.manager = StateManager(self.mock_engine, "state_table", "k", "v")

    def test_get_value(self):
        mock_conn = self.mock_engine.begin.return_value.__enter__.return_value
        mock_conn.execute.return_value.fetchone.return_value = ("value1",)

        self.assertEqual(self.manager.get("key1"), "value1")

        mock_conn.execute.return_value.fetchone.return_value = None
        self.assertEqual(self.manager.get("key2", "default"), "default")

    def test_save_value(self):
        mock_conn = self.mock_engine.begin.return_value.__enter__.return_value
        self.manager.save("key1", "value1")
        self.assertEqual(mock_conn.execute.call_count, 2)


class TestUtilsHelpers(unittest.TestCase):
    def test_calculate_file_hash(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"hello world")
            tmp_path = tmp.name
        try:
            h = calculate_file_hash(tmp_path)
            self.assertEqual(
                h, "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
            )
        finally:
            import os

            os.unlink(tmp_path)

    @mock.patch("builtins.open", new_callable=mock.mock_open)
    def test_handle_failed_publish(self, mock_file):
        payload = {"id": 1}
        handle_failed_publish(payload, "test_failed.json")
        mock_file.assert_called_with("test_failed.json", "a", encoding="utf-8")
        mock_file().write.assert_called()


if __name__ == "__main__":
    unittest.main()
