import unittest

from password import extract_password


class TestPasswordExtraction(unittest.TestCase):
    def test_handle_priority(self):
        text = "Password FULL LOGS - @BurnCloudLogs"
        self.assertEqual(extract_password(text), "@BurnCloudLogs")

    def test_https_url(self):
        text = "Pass: https://example.com/x"
        self.assertEqual(extract_password(text), "https://example.com/x")

    def test_tme_next_line(self):
        text = "Password Found:\n https://t.me/+abcdef"
        self.assertEqual(extract_password(text), "https://t.me/+abcdef")

    def test_cyrillic_a(self):
        text = "p\u0430ss: SECRET123"  # pаss with Cyrillic 'a'
        self.assertEqual(extract_password(text), "SECRET123")

    def test_no_marker(self):
        text = "No marker here"
        self.assertEqual(extract_password(text), "")

    def test_strip_trailing_punct(self):
        text = "Password: https://example.org/path),"  # trailing ),
        self.assertEqual(extract_password(text), "https://example.org/path")

    def test_stopword_skip_generic(self):
        text = "Password: FULL logs combo SECRET"
        # Should skip FULL/logs/combo and pick SECRET
        self.assertEqual(extract_password(text), "SECRET")

    def test_tme_plain(self):
        text = "Password: t.me/somechannel"
        self.assertEqual(extract_password(text), "t.me/somechannel")

    def test_https_tme_full_url(self):
        text = "❕ Password: https://t.me/Cloud_Rolex"
        self.assertEqual(extract_password(text), "https://t.me/Cloud_Rolex")

    def test_pw_marker(self):
        text = "pw https://t.me/+jglATzWg88A4ODg8"
        self.assertEqual(extract_password(text), "https://t.me/+jglATzWg88A4ODg8")

    def test_pw_generic(self):
        text = "PW: MySecretPassword"
        self.assertEqual(extract_password(text), "MySecretPassword")


if __name__ == "__main__":
    unittest.main()
