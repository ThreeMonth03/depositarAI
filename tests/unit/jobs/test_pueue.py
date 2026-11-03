"""Bind verified transport inputs to real, harmless native child processes."""

from contextlib import nullcontext
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from search.operations.jobs import pueue


SEALS = (
    fcntl.F_SEAL_WRITE
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_SEAL
)
NATIVE_SOURCE = (
    "\n"
    "#define _GNU_SOURCE\n"
    "#include <fcntl.h>\n"
    "#include <stdio.h>\n"
    "#include <stdlib.h>\n"
    "#include <string.h>\n"
    "#include <unistd.h>\n"
    "#ifndef MARKER\n"
    '#define MARKER "native-fixture"\n'
    "#endif\n"
    "\n"
    "int main(int argc, char **argv) {\n"
    '    if (argc < 3 || strcmp(argv[1], "--config")) return 20;\n'
    "    int foreign = argc > 4 ? fcntl(atoi(argv[4]), F_GETFD) : -2;\n"
    "    int binary = open(argv[0], O_RDONLY);\n"
    "    int config = open(argv[2], O_RDONLY);\n"
    "    if (binary < 0 || config < 0) return 21;\n"
    '    printf("%s\\nforeign=%d\\nbinary-seals=%d\\nconfig-seals=%d\\ncon'
    'fig:\\n",\n'
    "           MARKER, foreign, fcntl(binary, F_GET_SEALS), "
    "fcntl(config, F_GET_SEALS));\n"
    "    char buffer[1024];\n"
    "    ssize_t count;\n"
    "    while ((count = read(config, buffer, sizeof(buffer))) > 0) {\n"
    "        if (fwrite(buffer, 1, count, stdout) != (size_t) count) "
    "return 22;\n"
    "    }\n"
    "    close(binary);\n"
    "    close(config);\n"
    "    fflush(stdout);\n"
    '    if (argc > 3 && !strcmp(argv[3], "sleep")) sleep(5);\n'
    '    return argc > 3 && !strcmp(argv[3], "fail") ? 23 : 0;\n'
    "}\n"
)


class PueueDescriptorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="pueue-native-fixture-")
        cls.addClassCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        source = directory / "fixture.c"
        source.write_text(NATIVE_SOURCE)
        cls.native = directory / "fixture"
        compiler = shutil.which("cc", path=pueue.ENVIRONMENT["PATH"])
        if compiler is None:
            raise RuntimeError(
                "native transport fixtures require the host C compiler"
            )
        subprocess.run(
            [compiler, "-Wall", "-Werror", "-o", cls.native, source],
            env=pueue.ENVIRONMENT,
            check=True,
            capture_output=True,
        )
        cls.native.chmod(0o700)
        cls.replacement = directory / "replacement"
        subprocess.run(
            [
                compiler,
                '-DMARKER="replacement-fixture"',
                "-o",
                cls.replacement,
                source,
            ],
            env=pueue.ENVIRONMENT,
            check=True,
            capture_output=True,
        )
        cls.replacement.chmod(0o700)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="pueue-descriptor-fixture-"
        )
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "inputs"
        self.root.mkdir(mode=0o700)
        (self.base / "runtime").mkdir(mode=0o700)
        self.binary = self.root / "pueue"
        self.binary.write_bytes(self.native.read_bytes())
        self.binary.chmod(0o700)
        self.registry = {
            "root": str(self.root),
            "runtime_root": str(self.base / "runtime"),
            "pueue": str(self.binary),
        }
        self.config = pueue.configuration(self.registry)
        for name, encoded in (
            ("pueue.yml", self.config),
            ("aliases.yml", b"{}\n"),
        ):
            path = self.root / name
            path.write_bytes(encoded)
            path.chmod(0o600)
        self.fd_sets, self.processes = [], []
        self.addCleanup(patch.stopall)
        patch.object(
            pueue,
            "BINARY_SHA256",
            hashlib.sha256(self.binary.read_bytes()).hexdigest(),
        ).start()

    def call(self, *arguments, before=None, timeout=None):
        original = subprocess.Popen

        def spawn(argv, **kwargs):
            self.fd_sets.append(tuple(kwargs.get("pass_fds", ())))
            if before is not None:
                before(argv, kwargs)
            process = original(argv, **kwargs)
            self.processes.append(process)
            return process

        command = pueue.command

        def bounded(argv, **kwargs):
            return command(argv, timeout=timeout, **kwargs)

        with patch.object(pueue.subprocess, "Popen", side_effect=spawn):
            if timeout is None:
                return pueue.Pueue(self.registry).call(*arguments)
            with patch.object(pueue, "command", side_effect=bounded):
                return pueue.Pueue(self.registry).call(*arguments)

    def assert_native_config(self, output):
        header, encoded = output.split(b"config:\n", 1)
        self.assertEqual(
            (header.splitlines()[0], encoded), (b"native-fixture", self.config)
        )
        fields = dict(line.split(b"=", 1) for line in header.splitlines()[1:])
        for name in (b"binary-seals", b"config-seals"):
            self.assertEqual(int(fields[name]) & SEALS, SEALS)
        self.assertEqual(encoded, self.config)
        self.assert_handles_closed()
        return fields

    def assert_handles_closed(self):
        for descriptors in self.fd_sets:
            self.assertEqual(len(descriptors), 2)
            for fd in descriptors:
                with self.assertRaises(OSError) as caught:
                    os.fstat(fd)
                self.assertEqual(caught.exception.errno, errno.EBADF)
        for process in self.processes:
            self.assertIsNotNone(process.poll())

    def test_source_parent_replacement_does_not_rebind_the_native_child(self):
        def replace(_argv, _kwargs):
            self.root.rename(self.base / "original-inputs")
            self.root.mkdir(mode=0o700)
            self.binary.write_bytes(self.replacement.read_bytes())
            self.binary.chmod(0o700)
            (self.root / "pueue.yml").write_bytes(
                b"replacement configuration\n"
            )

        self.assert_native_config(self.call("status", before=replace))

    def test_same_inode_rewrite_does_not_change_verified_child_inputs(self):
        before = (
            self.binary.stat().st_ino,
            (self.root / "pueue.yml").stat().st_ino,
        )

        def rewrite(_argv, _kwargs):
            self.binary.write_bytes(self.replacement.read_bytes())
            (self.root / "pueue.yml").write_bytes(
                b"replacement configuration\n"
            )
            self.assertEqual(
                before,
                (
                    self.binary.stat().st_ino,
                    (self.root / "pueue.yml").stat().st_ino,
                ),
            )

        self.assert_native_config(self.call("status", before=rewrite))

    def test_child_receives_only_sealed_binary_and_config_descriptors(self):
        extra = os.open(self.base / "extra", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, extra)
        os.set_inheritable(extra, True)

        def check(argv, kwargs):
            descriptors = kwargs["pass_fds"]
            self.assertEqual(
                argv[:3],
                [
                    f"/proc/self/fd/{descriptors[0]}",
                    "--config",
                    f"/proc/self/fd/{descriptors[1]}",
                ],
            )
            self.assertNotIn(extra, descriptors)
            for fd, mode in zip(descriptors, (0o700, 0o600)):
                self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), mode)
                for mutation in (
                    lambda: os.write(fd, b"changed"),
                    lambda: os.ftruncate(fd, 0),
                    lambda: os.ftruncate(fd, os.fstat(fd).st_size + 1),
                ):
                    with self.assertRaises(OSError) as caught:
                        mutation()
                    self.assertEqual(caught.exception.errno, errno.EPERM)

        fields = self.assert_native_config(
            self.call("status", str(extra), before=check)
        )
        self.assertEqual(fields[b"foreign"], b"-1")

    def test_short_writes_still_produce_the_complete_native_inputs(self):
        original = os.write

        def partial(fd, data):
            return original(fd, data[:17])

        with patch.object(pueue.os, "write", side_effect=partial):
            self.assert_native_config(self.call("status"))

    def test_seal_and_resnapshot_failures_close_without_dispatch(
        self,
    ):
        original_create, original_fcntl = os.memfd_create, fcntl.fcntl
        for failure in (
            "create-second",
            "seal",
            "get-seal",
            "unsealed",
            "zero-write",
        ):
            created = []

            def create(*args):
                if failure == "create-second" and created:
                    raise OSError("fixture allocation failure")
                fd = original_create(*args)
                created.append(fd)
                return fd

            def seal(fd, operation, *args):
                if operation == fcntl.F_ADD_SEALS and failure == "seal":
                    raise OSError("fixture sealing failure")
                if operation == fcntl.F_GET_SEALS and failure == "get-seal":
                    raise OSError("fixture sealing inspection failure")
                if operation == fcntl.F_GET_SEALS and failure == "unsealed":
                    return 0
                return original_fcntl(fd, operation, *args)

            with (
                self.subTest(failure=failure),
                patch.object(pueue.os, "memfd_create", side_effect=create),
                patch.object(pueue.fcntl, "fcntl", side_effect=seal),
                patch.object(pueue.subprocess, "Popen") as spawn,
            ):
                writing = (
                    patch.object(pueue.os, "write", return_value=0)
                    if failure == "zero-write"
                    else nullcontext()
                )
                with writing:
                    with self.assertRaisesRegex(
                        pueue.JobError, "job_sealed_input_failed"
                    ):
                        pueue.Pueue(self.registry).call("status")
                spawn.assert_not_called()
                self.assertTrue(created)
                for fd in created:
                    with self.assertRaises(OSError):
                        os.fstat(fd)

    def test_exec_refusal_has_no_pathname_fallback_and_closes_descriptors(
        self,
    ):
        def refuse(argv, kwargs):
            self.assertTrue(str(argv[0]).startswith("/proc/self/fd/"))
            self.assertEqual(len(kwargs["pass_fds"]), 2)
            raise PermissionError("fixture execution unavailable")

        with self.assertRaisesRegex(pueue.JobError, "job_command_failed"):
            self.call("status", before=refuse)
        self.assertEqual(len(self.fd_sets), 1)
        self.assert_handles_closed()

    def test_unavailable_memfd_refuses_without_dispatch(self):
        with (
            patch.object(
                pueue.os,
                "memfd_create",
                side_effect=OSError(errno.ENOSYS, "fixture"),
            ),
            patch.object(pueue.subprocess, "Popen") as spawn,
            self.assertRaisesRegex(pueue.JobError, "job_sealed_input_failed"),
        ):
            pueue.Pueue(self.registry).call("status")
        spawn.assert_not_called()

    def test_nonzero_exit_output_limit_and_deadline_close_both_descriptors(
        self,
    ):
        with self.assertRaisesRegex(
            pueue.JobError, "job_queue_command_failed"
        ):
            self.call("fail")
        self.assert_handles_closed()
        with patch.object(pueue, "MAX_OUTPUT", 32):
            with self.assertRaisesRegex(
                pueue.JobError, "job_command_output_limit"
            ):
                self.call("status")
        self.assert_handles_closed()
        with self.assertRaisesRegex(pueue.JobError, "job_command_timeout"):
            self.call("sleep", timeout=0.05)
        self.assert_handles_closed()

    def test_invalid_verified_sources_never_create_a_snapshot_or_child(self):
        for name, replacement, error in (
            ("pueue", b"different executable", "binary_changed"),
            ("pueue.yml", b"different configuration", "daemon_config_changed"),
            ("aliases.yml", b"fixture: changed\n", "aliases_changed"),
        ):
            path = self.root / name
            original = path.read_bytes()
            with self.subTest(source=name):
                try:
                    path.write_bytes(replacement)
                    with (
                        patch.object(pueue.os, "memfd_create") as create,
                        patch.object(pueue.subprocess, "Popen") as spawn,
                        self.assertRaisesRegex(pueue.JobError, error),
                    ):
                        pueue.Pueue(self.registry).call("status")
                    create.assert_not_called()
                    spawn.assert_not_called()
                finally:
                    path.write_bytes(original)

    def test_interruption_closes_snapshots_and_reaps_the_native_child(self):
        original = os.read

        def interrupted(fd, size):
            if self.processes and fd == self.processes[-1].stdout.fileno():
                raise KeyboardInterrupt()
            return original(fd, size)

        with (
            patch.object(pueue.os, "read", side_effect=interrupted),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.call("sleep")
        self.assert_handles_closed()
