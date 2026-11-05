import unittest
from pathlib import Path
from unittest import mock

from tests.conftest import mock_config

mock_config()

from utils import decompress_file


class TestDecompress(unittest.TestCase):
    @mock.patch("os.close")
    @mock.patch("os.waitpid")
    @mock.patch("os.read")
    @mock.patch("pty.fork")
    @mock.patch("shutil.which", return_value="/usr/bin/7z")
    @mock.patch("subprocess.run")
    def test_decompress_file_success(
        self, mock_run, mock_which, mock_fork, mock_read, mock_waitpid, mock_close
    ):
        mock_run.return_value = mock.MagicMock(
            returncode=0, stdout="", stderr=""
        )
        mock_fork.return_value = (12345, 3)
        mock_read.side_effect = [b"Extracted\n", b""]
        mock_waitpid.return_value = (12345, 0)

        file_path = Path("test.zip")
        dest_root = "/tmp/uncompressed"

        with mock.patch.object(Path, "mkdir"):
            result, _ = decompress_file(file_path, "password123", dest_root)

        self.assertEqual(result, Path(dest_root) / "test")

    @mock.patch("os.close")
    @mock.patch("os.waitpid")
    @mock.patch("os.read")
    @mock.patch("pty.fork")
    @mock.patch("shutil.which", return_value="/usr/bin/7z")
    @mock.patch("subprocess.run")
    def test_decompress_wrong_password(
        self, mock_run, mock_which, mock_fork, mock_read, mock_waitpid, mock_close
    ):
        mock_run.return_value = mock.MagicMock(
            returncode=1, stdout="", stderr="Wrong password"
        )
        # pty.fork is still called after fast check fails — no, it won't be
        # because the function raises before reaching PTY.
        # These mocks won't be reached, but keep them for cleanup safety.

        file_path = Path("test.zip")
        with mock.patch.object(Path, "mkdir"):
            with self.assertRaises(RuntimeError) as cm:
                decompress_file(file_path, "wrong", "/tmp")
            self.assertEqual(str(cm.exception), "Incorrect password")


if __name__ == "__main__":
    unittest.main()
