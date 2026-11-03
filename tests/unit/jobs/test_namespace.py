"""Keep queue transport path selection on the shared namespace policy."""

from pathlib import Path
import tempfile
import unittest

from search.operations.jobs import namespace
from search.operations.shared.namespace import NamespaceError


class TransportNamespaceTests(unittest.TestCase):
    def test_transport_requires_all_three_private_directories(self):
        with tempfile.TemporaryDirectory(
            prefix="queue-namespace-"
        ) as temporary:
            root = Path(temporary)
            paths = [root / name for name in ("state", "runtime", "client")]
            for path in paths:
                path.mkdir(mode=0o700)
            registry = {
                "root": str(paths[0]),
                "runtime_root": str(paths[1]),
                "pueue": str(paths[2] / "pueue"),
            }
            namespace.check_transport(registry)
            for path in paths:
                with self.subTest(path=path.name):
                    path.chmod(0o755)
                    try:
                        with self.assertRaisesRegex(
                            NamespaceError,
                            "^namespace_private_directory_unsafe$",
                        ):
                            namespace.check_transport(registry)
                    finally:
                        path.chmod(0o700)
            self.assertFalse(hasattr(namespace, "check_directory"))


if __name__ == "__main__":
    unittest.main()
