import tempfile
import unittest
from pathlib import Path

from tests.conftest import mock_config

cfg = mock_config()
assert cfg is not None  # must run before importing app modules

from cd_leak import (
    find_info_files,
    guess_pc_name,
    load_pc_info,
    parse_json_info,
    parse_text_info,
    render_pc_info,
)

REDLINE_SYSTEM_TXT = """Date: 17.10.2026 20:33:25
HWID: D421C88AA9B1DCFDB996-a8d58e88-370e-C36F5B5

Windows: Windows 10 Pro 22H2 (Build 19045)
User Name: LinwoodDavidson
Display Resolution: 1440x900
Antivirus: Bitdefender

[Hardware]
Processor: Intel(R) Core(TM) i3-8100K CPU @ 3.60GHz
Cores: 4
Threads: 4
RAM: 16356 MB
VideoCard: NVIDIA GeForce GTX 1050 Ti
"""

REDLINE_USER_INFORMATION_TXT = """Install Date: 12.05.2021 10:00:00
Local Date: 17.10.2026 20:33:25
Country: IT
"""

RACCOON_INFORMATION_TXT = """IP: 93.44.1.2
Country: IT
Date: 17.10.2026 20:33:25
Username: mario.rossi
PC name: DESKTOP-RACCOON
OS: Windows 11 Home 23H2
Screen Resolution: 1920x1080
CPU: AMD Ryzen 5 3600
RAM: 16 GB
GPU: Radeon RX 580
Antivirus: Windows Defender
"""


class TestParseTextInfo(unittest.TestCase):
    def test_redline_system_txt(self):
        fields, _extras = parse_text_info(REDLINE_SYSTEM_TXT)

        self.assertEqual(fields["Date"], "17.10.2026 20:33:25")
        self.assertTrue(fields["HWID"].startswith("D421C88AA9B1DCFDB996"))
        self.assertEqual(fields["Windows"], "Windows 10 Pro 22H2 (Build 19045)")
        self.assertEqual(fields["User Name"], "LinwoodDavidson")
        self.assertEqual(fields["Display Resolution"], "1440x900")
        self.assertEqual(fields["Antivirus"], "Bitdefender")
        self.assertEqual(fields["Cores"], "4")
        self.assertEqual(fields["Threads"], "4")
        self.assertEqual(fields["RAM"], "16356 MB")
        self.assertEqual(fields["VideoCard"], "NVIDIA GeForce GTX 1050 Ti")

    def test_raccoon_alias_labels(self):
        fields, _extras = parse_text_info(RACCOON_INFORMATION_TXT)

        self.assertEqual(fields["User Name"], "mario.rossi")
        self.assertEqual(fields["PC Name"], "DESKTOP-RACCOON")
        self.assertEqual(fields["Windows"], "Windows 11 Home 23H2")
        self.assertEqual(fields["Processor"], "AMD Ryzen 5 3600")
        self.assertEqual(fields["VideoCard"], "Radeon RX 580")
        self.assertEqual(fields["IP"], "93.44.1.2")

    def test_user_information_dates(self):
        fields, _extras = parse_text_info(REDLINE_USER_INFORMATION_TXT)

        self.assertEqual(fields["Install Date"], "12.05.2021 10:00:00")
        self.assertEqual(fields["Date"], "17.10.2026 20:33:25")
        self.assertEqual(fields["Country"], "IT")

    def test_unknown_labels_are_kept_as_extras(self):
        fields, extras = parse_text_info("Build: 12345\nDate: 01.01.2026\n")

        self.assertEqual(fields["Date"], "01.01.2026")
        self.assertEqual(extras, [("Build", "12345")])

    def test_empty_and_garbage_content(self):
        self.assertEqual(parse_text_info(""), ({}, []))
        self.assertEqual(parse_text_info("no labels here\n\n"), ({}, []))

    def test_values_with_colons_are_preserved(self):
        fields, _extras = parse_text_info("IP: 2001:db8::1\n")

        self.assertEqual(fields["IP"], "2001:db8::1")

    def test_stealer_specific_aliases(self):
        content = (
            "Local Time: 17.10.2026 20:33:25\n"
            "Operation System: Windows 11 Pro\n"
            "Screen Size: 1920x1080\n"
            "Installed RAM: 32768 MB\n"
            "User Language: it-IT\n"
        )
        fields, extras = parse_text_info(content)

        self.assertEqual(fields["Date"], "17.10.2026 20:33:25")
        self.assertEqual(fields["Windows"], "Windows 11 Pro")
        self.assertEqual(fields["Display Resolution"], "1920x1080")
        self.assertEqual(fields["RAM"], "32768 MB")
        self.assertEqual(fields["Language"], "it-IT")
        self.assertEqual(extras, [])


class TestParseJsonInfo(unittest.TestCase):
    def test_nested_json(self):
        content = """
        {
            "pc_name": "LAPTOP-JSON",
            "user_name": "jsonuser",
            "os": "Windows 10 Home",
            "hardware": {"cpu": "Intel i5-9400", "ram": "8192 MB"},
            "antivirus": "Kaspersky"
        }
        """
        fields, _extras = parse_json_info(content)

        self.assertEqual(fields["PC Name"], "LAPTOP-JSON")
        self.assertEqual(fields["User Name"], "jsonuser")
        self.assertEqual(fields["Windows"], "Windows 10 Home")
        self.assertEqual(fields["Processor"], "Intel i5-9400")
        self.assertEqual(fields["RAM"], "8192 MB")
        self.assertEqual(fields["Antivirus"], "Kaspersky")

    def test_invalid_json(self):
        self.assertEqual(parse_json_info("{not json"), ({}, []))

    def test_zero_hardware_values_are_skipped(self):
        content = """
        {
            "PcName": "DESKTOP-ZERO",
            "CpuCores": 0,
            "RAM": 0,
            "IsElevator": false,
            "ZipCode": "00000"
        }
        """
        fields, _extras = parse_json_info(content)

        self.assertEqual(fields["PC Name"], "DESKTOP-ZERO")
        self.assertNotIn("Cores", fields)
        self.assertNotIn("RAM", fields)

    def test_lumma_camel_case_keys(self):
        content = """
        {
            "PcName": "DESKTOP-LUMMA",
            "Os": "Windows 10 Pro",
            "UserName": "lummuser",
            "ScreenSize": "2560x1440",
            "LocalTime": "2026-10-17 20:33:25",
            "CpuCores": 8,
            "RAM": 16384,
            "GPU": ["NVIDIA GeForce RTX 3060"]
        }
        """
        fields, _extras = parse_json_info(content)

        self.assertEqual(fields["PC Name"], "DESKTOP-LUMMA")
        self.assertEqual(fields["Windows"], "Windows 10 Pro")
        self.assertEqual(fields["User Name"], "lummuser")
        self.assertEqual(fields["Display Resolution"], "2560x1440")
        self.assertEqual(fields["Date"], "2026-10-17 20:33:25")
        self.assertEqual(fields["Cores"], "8")
        self.assertEqual(fields["RAM"], "16384")
        self.assertEqual(fields["VideoCard"], "NVIDIA GeForce RTX 3060")


class TestFindInfoFiles(unittest.TestCase):
    def test_finds_known_names_and_skips_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "System.txt").write_text("Date: 01.01.2026\n", encoding="utf-8")
            (root / "pc_info.json").write_text("{}", encoding="utf-8")
            (root / "Passwords.txt").write_text("x", encoding="utf-8")

            names = sorted(path.name for path in find_info_files(root))

            self.assertEqual(names, ["System.txt", "pc_info.json"])

    def test_depth_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "a" / "b" / "c" / "d"
            deep.mkdir(parents=True)
            (deep / "System.txt").write_text("Date: 01.01.2026\n", encoding="utf-8")

            self.assertEqual(find_info_files(root), [])


class TestLoadPcInfo(unittest.TestCase):
    def test_merges_system_and_user_information(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "System.txt").write_text(REDLINE_SYSTEM_TXT, encoding="utf-8")
            (root / "UserInformation.txt").write_text(
                REDLINE_USER_INFORMATION_TXT, encoding="utf-8"
            )

            fields, _extras, sources = load_pc_info(root)

            self.assertEqual(len(sources), 2)
            self.assertEqual(fields["Processor"], "Intel(R) Core(TM) i3-8100K CPU @ 3.60GHz")
            self.assertEqual(fields["Country"], "IT")
            self.assertEqual(fields["Install Date"], "12.05.2021 10:00:00")

    def test_no_info_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_pc_info(Path(tmp)), ({}, [], []))


class TestGuessPcName(unittest.TestCase):
    def test_suffix_date(self):
        self.assertEqual(
            guess_pc_name("/leaks/DESKTOP-8KQ2LMA_17.10.2026_20.33.25"),
            "DESKTOP-8KQ2LMA",
        )

    def test_prefix_date(self):
        self.assertEqual(
            guess_pc_name("/leaks/2023-10-17_DESKTOP-RACCOON"),
            "DESKTOP-RACCOON",
        )

    def test_plain_hostname(self):
        self.assertEqual(guess_pc_name("/leaks/MY-PC"), "MY-PC")

    def test_too_short_or_numeric(self):
        self.assertIsNone(guess_pc_name("/leaks/12"))
        self.assertIsNone(guess_pc_name("/leaks/ab"))


class TestRenderPcInfo(unittest.TestCase):
    def test_display_layout(self):
        fields, extras = parse_text_info(REDLINE_SYSTEM_TXT)

        block = render_pc_info(fields, extras, "/leaks/DESKTOP-8KQ2LMA_17.10.2026")

        lines = block.splitlines()
        self.assertEqual(lines[0], "Date: 17.10.2026 20:33:25")
        self.assertTrue(lines[1].startswith("HWID: "))
        self.assertEqual(lines[2], "")
        self.assertIn("User Name: LinwoodDavidson", lines)
        self.assertIn("PC Name: DESKTOP-8KQ2LMA", lines)
        self.assertIn("[Hardware]", lines)
        self.assertLess(lines.index("[Hardware]"), lines.index("Cores: 4"))

    def test_empty_info(self):
        self.assertEqual(render_pc_info({}, [], "/leaks/12"), "")

    def test_long_values_are_truncated(self):
        long_list = ", ".join(f"process{i}.exe" for i in range(500))
        fields, _extras = parse_text_info("PC Name: DESKTOP-LONG\nDate: 01.01.2026\n")

        block = render_pc_info(fields, [("ProcessList", long_list)], "/leaks/DESKTOP-LONG")

        process_line = next(line for line in block.splitlines() if line.startswith("ProcessList:"))
        value = process_line.split(": ", 1)[1]
        self.assertLessEqual(len(value), 160)
        self.assertTrue(value.endswith("…"))

    def test_long_known_values_are_truncated(self):
        block = render_pc_info(
            {"Windows": "W" * 400}, [], "/leaks/DESKTOP-LONG"
        )

        windows_line = next(line for line in block.splitlines() if line.startswith("Windows:"))
        value = windows_line.split(": ", 1)[1]
        self.assertLessEqual(len(value), 160)
        self.assertTrue(value.endswith("…"))


if __name__ == "__main__":
    unittest.main()
