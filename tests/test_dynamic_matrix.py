"""Tests for the dynamic password matrix feature in decompress.py."""

import json
import os
import signal
import tempfile
import threading
import unittest
from unittest import mock


class TestLoadPasswordMatrix(unittest.TestCase):
    """Tests for load_password_matrix()."""

    def test_load_from_valid_json(self):
        """Matrix should be loaded from a well-formed JSON file."""
        data = {"@TestChannel": "https://t.me/TestChannel", "OtherKey": "pass123"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            json.dump(data, fh)
            tmp_path = fh.name
        try:
            import decompress

            with mock.patch.object(decompress, "PASSWORD_MATRIX_FILE", tmp_path):
                result = decompress.load_password_matrix()
            self.assertEqual(result, data)
        finally:
            os.unlink(tmp_path)

    def test_fallback_when_file_missing(self):
        """load_password_matrix() should return an empty dict when the file is absent."""
        import decompress

        with mock.patch.object(
            decompress, "PASSWORD_MATRIX_FILE", "/nonexistent/path/matrix.json"
        ):
            result = decompress.load_password_matrix()
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 0)

    def test_fallback_when_json_invalid(self):
        """load_password_matrix() should return an empty dict on parse errors."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            fh.write("NOT VALID JSON{{{{")
            tmp_path = fh.name
        try:
            import decompress

            with mock.patch.object(decompress, "PASSWORD_MATRIX_FILE", tmp_path):
                result = decompress.load_password_matrix()
            self.assertIsInstance(result, dict)
            self.assertEqual(len(result), 0)
        finally:
            os.unlink(tmp_path)

    def test_fallback_when_json_not_object(self):
        """load_password_matrix() should return an empty dict when JSON is not a dict."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            json.dump(["list", "not", "object"], fh)
            tmp_path = fh.name
        try:
            import decompress

            with mock.patch.object(decompress, "PASSWORD_MATRIX_FILE", tmp_path):
                result = decompress.load_password_matrix()
            self.assertIsInstance(result, dict)
            self.assertEqual(len(result), 0)
        finally:
            os.unlink(tmp_path)

    def test_fallback_when_os_error(self):
        """load_password_matrix() should return an empty dict on OSError (e.g. PermissionError)."""
        import decompress

        with mock.patch.object(decompress, "PASSWORD_MATRIX_FILE", "/some/matrix.json"):
            with mock.patch(
                "builtins.open", side_effect=PermissionError("Permission denied")
            ):
                result = decompress.load_password_matrix()
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 0)


class TestReloadPasswordMatrix(unittest.TestCase):
    """Tests for reload_password_matrix() and thread-safety."""

    def test_reload_updates_live_matrix(self):
        """reload_password_matrix() should update _password_matrix in-place."""
        new_data = {"@NewChannel": "new_password"}
        import decompress

        with mock.patch.object(
            decompress, "load_password_matrix", return_value=new_data
        ):
            decompress.reload_password_matrix()
        with decompress._matrix_lock:
            self.assertIn("@NewChannel", decompress._password_matrix)
            self.assertEqual(decompress._password_matrix["@NewChannel"], "new_password")

    def test_reload_skipped_on_empty_result(self):
        """reload_password_matrix() should keep the current matrix when load returns {} (edge case)."""
        import decompress

        # Put something in the matrix first
        with decompress._matrix_lock:
            decompress._password_matrix.clear()
            decompress._password_matrix["@Existing"] = "existing_pass"

        with mock.patch.object(decompress, "load_password_matrix", return_value={}):
            decompress.reload_password_matrix()

        with decompress._matrix_lock:
            self.assertIn("@Existing", decompress._password_matrix)

    def test_matrix_lock_is_reentrant(self):
        """_matrix_lock must be an RLock so that the signal handler cannot deadlock the main thread."""
        import threading

        import decompress

        self.assertIsInstance(decompress._matrix_lock, type(threading.RLock()))

    def test_concurrent_reload_is_safe(self):
        """Multiple threads reloading simultaneously should not raise exceptions."""
        import decompress

        errors = []

        def worker():
            try:
                decompress.reload_password_matrix()
            except BaseException as exc:  # pylint: disable=broad-except
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Errors during concurrent reload: {errors}")


class TestGetPasswordFromMatrix(unittest.TestCase):
    """Tests for get_password_from_matrix()."""

    def setUp(self):
        import decompress

        with decompress._matrix_lock:
            decompress._password_matrix.clear()
            decompress._password_matrix.update(
                {
                    "@Trident_Cloud": "https://t.me/Trident_Cloud",
                    "Echo Cloud": "https://t.me/echocloudlink",
                }
            )

    def test_match_by_pattern(self):
        import decompress

        self.assertEqual(
            decompress.get_password_from_matrix("file_@Trident_Cloud_20240101.zip"),
            "https://t.me/Trident_Cloud",
        )

    def test_no_match_returns_empty_string(self):
        import decompress

        self.assertEqual(decompress.get_password_from_matrix("unrelated_file.zip"), "")

    def test_partial_pattern_match(self):
        import decompress

        self.assertEqual(
            decompress.get_password_from_matrix("Echo Cloud - January.zip"),
            "https://t.me/echocloudlink",
        )


class TestSIGUSR1Handler(unittest.TestCase):
    """Tests for SIGUSR1 signal handler."""

    @unittest.skipUnless(
        hasattr(signal, "SIGUSR1"), "SIGUSR1 not available on this platform"
    )
    def test_sigusr1_triggers_reload(self):
        """Sending SIGUSR1 to the current process should trigger reload_password_matrix."""
        import decompress

        with mock.patch.object(decompress, "reload_password_matrix") as mock_reload:
            # Manually invoke the handler (avoids actually sending signals in
            # tests)
            decompress._handle_sigusr1(signal.SIGUSR1, None)
            mock_reload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
