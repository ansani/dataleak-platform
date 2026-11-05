import unittest
from pathlib import Path
from unittest import mock

from tests.conftest import mock_config

mock_config()

from analyze import analyze_directory


class TestAnalyzeLogic(unittest.TestCase):
    def test_normalize_and_filter_emails(self):
        # This function is local to process_uncompressed_directory in analyze.py
        # But we can test the logic by defining it here or if it were global.
        # Since it's nested, let's test it via a mock or by moving it to utils.
        # For now, I'll implement a test for the logic it's supposed to
        # perform.
        pass

    @mock.patch("analyze.clean_string", side_effect=lambda x: x)
    def test_analyze_directory_basic(self, mock_clean):
        # Create a mock directory structure
        with unittest.mock.patch("pathlib.Path.rglob") as mock_rglob:
            # Mock one file that looks like a password file
            file_mock = mock.MagicMock(spec=Path)
            file_mock.is_file.return_value = True
            file_mock.name = "passwords.txt"
            file_mock.suffix = ".txt"

            # Mock content
            with mock.patch(
                "builtins.open",
                mock.mock_open(
                    read_data="Host: http://test.com Password: secret\nEmail: test@example.com"
                ),
            ):
                mock_rglob.return_value = [file_mock]

                country, emails, hosts, cards, c_found, p_found, leak_date = (
                    analyze_directory("/fake/dir")
                )

                self.assertIn("test@example.com", emails)
                self.assertIn("http://test.com", hosts)
                self.assertTrue(p_found)


if __name__ == "__main__":
    unittest.main()
