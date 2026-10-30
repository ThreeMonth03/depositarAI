"""Normalize only tracked owned files inside a new private source snapshot."""

import os
from pathlib import Path
import tempfile
import unittest

from search.operations.jobs.pueue import JobError
from search.operations.jobs.sources import restore_modes


def tree(*entries):
    return b"".join(
        mode.encode() + b" blob " + b"a" * 40 + b"\t" + name.encode() + b"\0"
        for mode, name in entries
    )


class SourceModesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.source = self.root / "source"
        self.source.mkdir(mode=0o775)

    def test_acl_created_modes_become_git_modes_without_changing_untracked(self):
        (self.source / "nested").mkdir(mode=0o775)
        plain = self.source / "nested/plain.py"
        script = self.source / "run.sh"
        untracked = self.source / "untracked"
        for path in (plain, script, untracked):
            path.write_bytes(b"fixture\n")
        plain.chmod(0o664)
        script.chmod(0o775)
        untracked.chmod(0o664)
        restore_modes(
            self.source, tree(("100644", "nested/plain.py"), ("100755", "run.sh"))
        )
        self.assertEqual(plain.stat().st_mode & 0o777, 0o644)
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)
        self.assertEqual(untracked.stat().st_mode & 0o777, 0o664)
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o700)

    def test_leaf_and_directory_symlinks_do_not_change_external_modes(self):
        external = self.root / "external"
        external.mkdir()
        target = external / "plain.py"
        target.write_bytes(b"fixture\n")
        target.chmod(0o664)
        (self.source / "leaf.py").symlink_to(target)
        (self.source / "linked").symlink_to(external, target_is_directory=True)
        for name in ("leaf.py", "linked/plain.py"):
            with self.subTest(name=name), self.assertRaises((OSError, JobError)):
                restore_modes(self.source, tree(("100644", name)))
            self.assertEqual(target.stat().st_mode & 0o777, 0o664)

    def test_hardlink_and_unsupported_git_entries_are_refused(self):
        external = self.root / "external.py"
        external.write_bytes(b"fixture\n")
        external.chmod(0o664)
        os.link(external, self.source / "linked.py")
        with self.assertRaisesRegex(JobError, "source_file_unsafe"):
            restore_modes(self.source, tree(("100644", "linked.py")))
        self.assertEqual(external.stat().st_mode & 0o777, 0o664)
        for mode, name in (
            ("120000", "link"),
            ("100644", "../external.py"),
            ("100644", "/external.py"),
            ("100644", "."),
        ):
            with (
                self.subTest(mode=mode, name=name),
                self.assertRaisesRegex(JobError, "source_tree_invalid"),
            ):
                restore_modes(self.source, tree((mode, name)))


if __name__ == "__main__":
    unittest.main()
