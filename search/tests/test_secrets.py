"""Exercise private key files without a service or root."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from search.secrets import read_secret


KEY = "a1" * 32


class SecretTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-secret-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "key"
        self.path.write_text(KEY, encoding="ascii")
        self.path.chmod(0o600)

    def test_fifo_without_a_writer_is_rejected_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        source = Path(__file__).resolve().parents[2]
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(source)!r})\n"
            "from search.secrets import read_secret\n"
            "try:\n"
            f"    read_secret(Path({str(fifo)!r}))\n"
            "except ValueError as error:\n"
            "    assert str(error) == 'invalid secret file'\n"
            "    print('fifo rejected')\n"
            "else:\n"
            "    raise AssertionError('FIFO accepted as a private key')\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("fifo rejected\n", result.stdout)

    def test_private_keys_accept_both_modes_with_or_without_final_newline(self):
        for mode in (0o400, 0o600):
            for ending in ("", "\n"):
                with self.subTest(mode=oct(mode), newline=bool(ending)):
                    self.path.chmod(0o600)
                    self.path.write_text(KEY + ending, encoding="ascii")
                    self.path.chmod(mode)
                    self.assertEqual(KEY, read_secret(self.path))

    def test_other_file_modes_are_rejected(self):
        for mode in (0o644, 0o440, 0o660, 0o700):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaisesRegex(ValueError, "^invalid secret file$"):
                    read_secret(self.path)

    def test_symbolic_link_to_a_valid_key_is_rejected(self):
        link = self.root / "symlink"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            read_secret(link)

    def test_hard_linked_key_is_rejected(self):
        os.link(self.path, self.root / "hardlink")
        with self.assertRaisesRegex(ValueError, "^invalid secret file$"):
            read_secret(self.path)

    def test_another_owner_is_rejected(self):
        with (
            patch("search.secrets.os.geteuid", return_value=os.geteuid() + 1),
            self.assertRaisesRegex(ValueError, "^invalid secret file$"),
        ):
            read_secret(self.path)

    def test_noncanonical_keys_are_rejected(self):
        for raw in (
            b"a" * 63,
            b"a" * 65,
            b"A" * 64,
            b"g" * 64,
            KEY.encode("ascii") + b"\r\n",
            b"\xff" * 64,
        ):
            with self.subTest(raw=raw[:4], size=len(raw)):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    read_secret(self.path)

    def test_identity_change_during_read_is_rejected_and_descriptor_closed(self):
        original_read = os.read
        descriptors = []

        def change_mode_after_read(descriptor, maximum):
            raw = original_read(descriptor, maximum)
            descriptors.append(descriptor)
            os.fchmod(descriptor, 0o400)
            return raw

        with (
            patch("search.secrets.os.read", side_effect=change_mode_after_read),
            self.assertRaisesRegex(ValueError, "^secret changed during read$"),
        ):
            read_secret(self.path)
        self.assertEqual(1, len(descriptors))
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])


if __name__ == "__main__":
    unittest.main()
