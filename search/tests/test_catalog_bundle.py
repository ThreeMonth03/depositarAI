import ast
import contextlib
import errno
import gzip
import hashlib
import io
import importlib.util
import json
import os
import stat
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from search.catalog.build import builder, cli, identity, policy, storage
from search.catalog.bundle import CatalogBundle


UUID_1 = "00000000-0000-4000-8000-000000000001"
UUID_2 = "00000000-0000-4000-8000-000000000002"

EXPECTED_FESS_FIELDS = {
    "url",
    "title",
    "important_content",
    "content",
    "mimetype",
    "filetype",
    "label",
    "lang",
    "role",
    "boost",
    "virtual_host",
    "dataset_id",
    "name",
    "notes",
    "organization_id",
    "organization_name",
    "organization_title",
    "tags",
    "groups",
    "resource_formats",
    "license_id",
    "license_title",
    "metadata_created",
    "metadata_modified",
    "resource_count",
}

EXPECTED_BUILDER_SOURCES = {
    "search/__init__.py",
    "search/catalog/__init__.py",
    "search/catalog/policy.py",
    "search/catalog/build/__init__.py",
    "search/catalog/build/__main__.py",
    "search/catalog/build/builder.py",
    "search/catalog/build/cli.py",
    "search/catalog/build/codec.py",
    "search/catalog/build/identity.py",
    "search/catalog/build/normalize.py",
    "search/catalog/build/policy.py",
    "search/catalog/build/snapshot.py",
    "search/catalog/build/storage.py",
}


def _dataset(identifier=UUID_1, slug="dataset-one"):
    return {
        "id": identifier,
        "name": slug,
        "title": "Dataset One",
        "notes": "A public dataset",
        "remarks": "Verified",
        "process_step": "Collected in the field",
        "private": False,
        "state": "active",
        "type": "dataset",
        "author": "Researcher",
        "maintainer": "Curator",
        "organization": {"id": "org-1", "name": "lab", "title": "Research Lab"},
        "tags": [{"name": "climate"}],
        "groups": [{"display_name": "Earth Science"}],
        "keywords": ["rainfall", "temperature"],
        "language": ["en", "zh_TW"],
        "data_type": ["CSV"],
        "license_id": "cc-by",
        "license_title": "Creative Commons Attribution",
        "start_time": "2020-01-01",
        "end_time": "2020-12-31",
        "created_time": "2021-01-02",
        "x_min": "120.0",
        "y_min": "21.0",
        "x_max": "122.0",
        "y_max": "25.0",
        "spatial": '{"type":"Polygon","coordinates":[]}',
        "metadata_created": "2021-01-02T00:00:00",
        "metadata_modified": "2022-01-02T00:00:00",
        "resources": [
            {
                "id": "resource-1",
                "name": "measurements.csv",
                "description": "Hourly measurements",
                "format": "CSV",
                "mimetype": "text/csv",
                "url": "https://files.example/private-path.csv?token=secret",
                "size": 1234,
                "created": "2021-01-02",
                "last_modified": "2022-01-02",
            }
        ],
    }


def _make_writable(path):
    if not os.path.lexists(path):
        return
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        for name in files:
            candidate = os.path.join(root, name)
            if not os.path.islink(candidate):
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    pass
        for name in directories:
            candidate = os.path.join(root, name)
            if not os.path.islink(candidate):
                try:
                    os.chmod(candidate, 0o700)
                except OSError:
                    pass


class CatalogBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(_make_writable, self.temporary.name)
        self.base = Path(self.temporary.name)
        self.output = self.base / "bundles"
        self.output.mkdir(mode=0o700)

    def write_jsonl(self, records, name="datasets.jsonl", compressed=False):
        path = self.base / name
        opener = gzip.open if compressed else open
        with opener(path, "wb") as stream:
            for record in records:
                stream.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                stream.write(b"\n")
        os.chmod(path, 0o600)
        return path

    @staticmethod
    def read_jsonl(path):
        with gzip.open(path, "rt", encoding="utf-8", errors="strict") as stream:
            return [json.loads(line) for line in stream]

    def test_builds_rich_corpus_and_allowlisted_fess_document(self):
        package = _dataset(slug="Ｄata set/測試")
        package["title"] = "<b>Ｔitle</b>\u202e"
        package["notes"] = (
            "<p>Contact\nowner@example.org</p><script>do not index</script>"
        )
        package["resources"][0]["description"] = "Ask helper@example.net for details"
        source = self.write_jsonl([package], name="datasets.jsonl.gz", compressed=True)

        result = builder.build_bundle(source, self.output)

        self.assertTrue(result["created"])
        bundle = Path(result["bundle_path"])
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o500)
        corpus = self.read_jsonl(bundle / "corpus.v2.jsonl.gz")[0]
        fess = self.read_jsonl(bundle / "fess-documents.jsonl.gz")[0]
        raw = self.read_jsonl(bundle / "source.jsonl.gz")[0]

        self.assertEqual(corpus["schema_version"], 2)
        self.assertEqual(corpus["name"], "Data set/測試")
        self.assertEqual(
            corpus["url"],
            "https://data.depositar.io/dataset/Data%20set%2F%E6%B8%AC%E8%A9%A6",
        )
        self.assertEqual(corpus["title"], "Title")
        self.assertEqual(corpus["notes"], "Contact owner@example.org")
        self.assertEqual(corpus["bbox"]["status"], "complete")
        self.assertEqual(corpus["temporal"]["status"], "complete")
        self.assertEqual(corpus["keywords"], ["rainfall", "temperature"])
        self.assertEqual(corpus["language"], ["en", "zh_TW"])
        self.assertEqual(corpus["data_type"], ["CSV"])
        self.assertEqual(corpus["license_title"], "Creative Commons Attribution")
        self.assertEqual(raw["spatial"], package["spatial"])

        self.assertEqual(set(fess), EXPECTED_FESS_FIELDS)
        self.assertEqual(policy.FESS_FIELD_ALLOWLIST, EXPECTED_FESS_FIELDS)
        self.assertEqual(fess["role"], [policy.FIXED_SEARCH_ROLE])
        self.assertEqual(fess["boost"], 1.0)
        self.assertEqual(fess["virtual_host"], [policy.FIXED_VIRTUAL_HOST])
        self.assertEqual(fess["label"], [policy.FIXED_LABEL])
        for searchable_metadata in (
            "Data set/測試",
            "Title",
            "Research Lab",
            "rainfall",
            "CSV",
            "Contact [email removed]",
        ):
            self.assertIn(searchable_metadata, fess["content"])
        self.assertTrue(fess["content"].strip())
        serialized_fess = json.dumps(fess, ensure_ascii=False)
        self.assertNotIn("owner@example.org", serialized_fess)
        self.assertNotIn("helper@example.net", serialized_fess)
        self.assertNotIn(package["resources"][0]["url"], serialized_fess)
        self.assertNotIn("do not index", serialized_fess)
        self.assertNotIn("spatial", serialized_fess)
        self.assertGreater(
            result["manifest"]["normalization_counters"]["bidi_characters_removed"], 0
        )

    def test_suppressed_html_cannot_be_reopened_by_a_mismatched_close_tag(self):
        package = _dataset()
        package["notes"] = (
            "visible <noscript></style>HIDDEN-ONE</noscript> tail "
            "<script><noscript></script>HIDDEN-TWO</noscript> end"
        )
        source = self.write_jsonl([package])
        result = builder.build_bundle(source, self.output)
        corpus = self.read_jsonl(Path(result["bundle_path"]) / "corpus.v2.jsonl.gz")[0]
        self.assertIn("visible", corpus["notes"])
        self.assertIn("tail", corpus["notes"])
        self.assertNotIn("HIDDEN-ONE", corpus["notes"])
        self.assertNotIn("HIDDEN-TWO", corpus["notes"])
        self.assertNotIn("end", corpus["notes"])

    def test_condition_metadata_is_manifest_bound_immutable_and_not_update_time(self):
        package = _dataset()
        source = self.write_jsonl([package])
        built = builder.build_bundle(source, self.output)
        path = Path(built["bundle_path"])
        digest = hashlib.sha256(
            (path / "bundle-manifest.json").read_bytes()
        ).hexdigest()
        bundle = CatalogBundle.load(path, expected_manifest_sha256=digest)
        metadata = bundle.condition_metadata[UUID_1]
        self.assertEqual(metadata["temporal"]["start"], "2020-01-01")
        self.assertNotIn("metadata_modified", metadata)
        self.assertNotIn("resources", metadata)
        self.assertIsInstance(metadata["resource_formats"], tuple)
        with self.assertRaises(TypeError):
            metadata["temporal"]["start"] = "2099"
        with self.assertRaises(TypeError):
            bundle.condition_metadata[UUID_1] = {}

    def test_nested_script_close_cannot_reopen_outer_noscript(self):
        package = _dataset()
        package["notes"] = (
            "before <noscript><script></noscript></script>HIDDEN</noscript> VISIBLE"
        )
        source = self.write_jsonl([package])
        result = builder.build_bundle(source, self.output)
        corpus = self.read_jsonl(Path(result["bundle_path"]) / "corpus.v2.jsonl.gz")[0]
        self.assertIn("before", corpus["notes"])
        self.assertIn("VISIBLE", corpus["notes"])
        self.assertNotIn("HIDDEN", corpus["notes"])

    def test_nested_script_close_cannot_reopen_outer_style(self):
        package = _dataset()
        package["notes"] = (
            "before <style><script></style></script>HIDDEN</style> VISIBLE"
        )
        source = self.write_jsonl([package])
        result = builder.build_bundle(source, self.output)
        corpus = self.read_jsonl(Path(result["bundle_path"]) / "corpus.v2.jsonl.gz")[0]
        self.assertIn("before", corpus["notes"])
        self.assertIn("VISIBLE", corpus["notes"])
        self.assertNotIn("HIDDEN", corpus["notes"])

    def test_fess_email_scrub_is_conservative_bounded_and_loader_compatible(self):
        package = _dataset()
        package["title"] = "owner@example.org聯絡"
        package["notes"] = " ".join(
            [
                "alice@localhost",
                "alice@例子.測試",
                "alice@[127.0.0.1]",
                '"alice"@example.org',
            ]
            + ["a@b.co"] * 4_000
        )
        package["organization"]["title"] = "contact@example.org"
        package["tags"] = [
            {"name": "alpha@example.org"},
            {"name": "beta@example.org"},
        ]
        source = self.write_jsonl([package])
        result = builder.build_bundle(source, self.output)
        bundle = Path(result["bundle_path"])
        fess = self.read_jsonl(bundle / "fess-documents.jsonl.gz")[0]
        serialized = json.dumps(fess, ensure_ascii=False)
        self.assertNotIn("@", serialized)
        self.assertIn("[email removed]", fess["notes"])
        self.assertLessEqual(len(fess["notes"]), policy.STRING_LIMITS["notes"])
        self.assertEqual(["[email removed]"], fess["tags"])
        loaded = CatalogBundle.load(
            bundle, expected_manifest_sha256=result["manifest_sha256"]
        )
        self.assertEqual(1, len(loaded.documents))

    def test_dataset_slug_rejects_every_at_sign_form(self):
        for index, slug in enumerate(
            (
                "alice@localhost",
                "alice@例子.測試",
                "alice@[127.0.0.1]",
                "alice＠example.org",
            ),
            1,
        ):
            with self.subTest(slug=slug):
                source = self.write_jsonl(
                    [
                        _dataset(
                            identifier="00000000-0000-4000-8000-%012d" % index,
                            slug=slug,
                        )
                    ],
                    name="slug-%d.jsonl" % index,
                )
                with self.assertRaisesRegex(policy.BundleError, "at-sign"):
                    builder.build_bundle(source, self.output)

    def test_manifest_binds_the_exact_shared_artifact_contract(self):
        source = self.write_jsonl([_dataset()], "source.jsonl.gz", compressed=True)
        result = builder.build_bundle(source, self.output)
        bundle = Path(result["bundle_path"])
        manifest_raw = (bundle / policy.MANIFEST_NAME).read_bytes()
        manifest = json.loads(manifest_raw)

        self.assertRegex(manifest["bundle_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            Path(result["bundle_path"]).name, "sha256-" + manifest["bundle_id"]
        )
        self.assertEqual(
            result["manifest_sha256"], hashlib.sha256(manifest_raw).hexdigest()
        )
        self.assertEqual(
            set(manifest),
            {
                "schema_version",
                "bundle_format",
                "bundle_id",
                "artifacts",
                "dataset_count",
                "resource_count",
                "dataset_ids_sha256",
                "builder",
                "policy",
                "normalization_counters",
            },
        )
        self.assertEqual(set(manifest["artifacts"]), set(policy.ARTIFACT_NAMES))
        for name, descriptor in manifest["artifacts"].items():
            artifact_raw = (bundle / name).read_bytes()
            with gzip.open(bundle / name, "rb") as stream:
                expanded = stream.read()
            self.assertEqual(
                set(descriptor),
                {
                    "sha256",
                    "bytes",
                    "expanded_bytes",
                    "records",
                    "schema_version",
                    "media_type",
                },
            )
            self.assertEqual(
                descriptor["sha256"], hashlib.sha256(artifact_raw).hexdigest()
            )
            self.assertEqual(descriptor["bytes"], len(artifact_raw))
            self.assertEqual(descriptor["expanded_bytes"], len(expanded))
            self.assertEqual(expanded.count(b"\n"), descriptor["records"])
            self.assertEqual(artifact_raw[:2], b"\x1f\x8b")
            self.assertEqual(artifact_raw[4:8], b"\x00\x00\x00\x00")
            self.assertEqual(stat.S_IMODE((bundle / name).stat().st_mode), 0o400)
        self.assertEqual(
            stat.S_IMODE((bundle / policy.MANIFEST_NAME).stat().st_mode), 0o400
        )
        self.assertEqual(
            manifest["builder"]["source_sha256"],
            identity.source_digest(),
        )
        expected_ids = (UUID_1 + "\n").encode("ascii")
        self.assertEqual(
            manifest["dataset_ids_sha256"], hashlib.sha256(expected_ids).hexdigest()
        )

    def test_build_cli_prints_only_a_bounded_summary(self):
        source = self.write_jsonl([_dataset()], "cli.jsonl")
        standard_output = io.StringIO()
        standard_error = io.StringIO()
        with (
            contextlib.redirect_stdout(standard_output),
            contextlib.redirect_stderr(standard_error),
        ):
            status_code = cli.main(
                ["build", "--input", str(source), "--output-root", str(self.output)]
            )
        summary = json.loads(standard_output.getvalue())
        self.assertEqual(status_code, 0)
        self.assertEqual(standard_error.getvalue(), "")
        self.assertEqual(summary["dataset_count"], 1)
        self.assertRegex(summary["bundle_id"], r"^[0-9a-f]{64}$")
        self.assertRegex(summary["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("documents", summary)

    def copied_builder_sources(self):
        root = self.base / "builder-source"
        source_root = Path(__file__).resolve().parents[2]
        for name in EXPECTED_BUILDER_SOURCES:
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((source_root / name).read_bytes())
        return root

    @staticmethod
    def isolated_source_digest(root):
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from search.catalog.build.identity import source_digest\n"
            "print(source_digest())\n"
        )
        return subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(root)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_builder_identity_binds_every_source_in_the_closed_recipe(self):
        root = self.copied_builder_sources()
        hashes = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in EXPECTED_BUILDER_SOURCES
        }
        payload = json.dumps(
            hashes,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected = hashlib.sha256(payload).hexdigest()
        result = self.isolated_source_digest(root)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(expected + "\n", result.stdout)
        for name in sorted(EXPECTED_BUILDER_SOURCES):
            with self.subTest(source=name):
                path = root / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n# Changed recipe input.\n")
                try:
                    result = self.isolated_source_digest(root)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertNotEqual(expected + "\n", result.stdout)
                finally:
                    path.write_bytes(original)

    def test_builder_identity_rejects_missing_and_unlisted_source_files(self):
        root = self.copied_builder_sources()
        required = root / "search/catalog/build/normalize.py"
        original = required.read_bytes()
        required.unlink()
        result = self.isolated_source_digest(root)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        required.write_bytes(original)
        for name in ("unlisted.py", "nested/unlisted.py"):
            with self.subTest(source=name):
                unknown = root / "search/catalog/build" / name
                unknown.parent.mkdir(exist_ok=True)
                unknown.write_text("# Untracked recipe input.\n")
                try:
                    result = self.isolated_source_digest(root)
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual("", result.stdout)
                finally:
                    unknown.unlink()

    def test_builder_import_closure_is_exactly_the_sealed_standard_library_recipe(self):
        root = Path(__file__).resolve().parents[2]
        sources = {}
        packages = set()
        for path in EXPECTED_BUILDER_SOURCES:
            name = path[:-3].replace("/", ".")
            if name.endswith(".__init__"):
                name = name.removesuffix(".__init__")
                packages.add(name)
            sources[name] = path
        standard_library = Path(sysconfig.get_path("stdlib")).resolve()
        graph = {}
        for module, path in sources.items():
            dependencies = set()
            package = module if module in packages else module.rpartition(".")[0]
            tree = ast.parse((root / path).read_bytes())
            dynamic_imports = {
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if (node.module, alias.name)
                in {("importlib", "import_module"), ("builtins", "__import__")}
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    function = node.func
                    self.assertFalse(
                        isinstance(function, ast.Name)
                        and function.id in dynamic_imports | {"__import__"},
                        (module, node.lineno),
                    )
                    self.assertFalse(
                        isinstance(function, ast.Attribute)
                        and function.attr in {"import_module", "__import__"},
                        (module, node.lineno),
                    )
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    name = node.module or ""
                    if node.level:
                        name = importlib.util.resolve_name(
                            "." * node.level + name, package
                        )
                    imports = [name]
                    if name in packages:
                        imports.extend(name + "." + alias.name for alias in node.names)
                else:
                    continue
                for name in imports:
                    if name == "search" or name.startswith("search."):
                        self.assertIn(name, sources, (module, name))
                        parts = name.split(".")
                        dependencies.update(
                            ".".join(parts[:count])
                            for count in range(1, len(parts) + 1)
                        )
                    else:
                        top_level = name.split(".")[0]
                        spec = importlib.util.find_spec(top_level)
                        self.assertIsNotNone(spec, (module, name))
                        if spec.origin in ("built-in", "frozen"):
                            continue
                        origin = Path(spec.origin).resolve()
                        self.assertIn(standard_library, origin.parents, (module, name))
                        self.assertFalse(
                            {"site-packages", "dist-packages"} & set(origin.parts),
                            (module, name),
                        )
            graph[module] = dependencies
        pending = ["search.catalog.build.__main__"]
        closure = set()
        while pending:
            module = pending.pop()
            if module not in closure:
                closure.add(module)
                pending.extend(graph[module] - closure)
        self.assertEqual(
            set(identity.SOURCE_FILES), {sources[name] for name in closure}
        )

    def test_deterministic_across_input_order_and_idempotent(self):
        first = _dataset(UUID_1, "one")
        second = _dataset(UUID_2, "two")
        source_a = self.write_jsonl([second, first], "a.jsonl")
        source_b = self.write_jsonl([first, second], "b.jsonl.gz", compressed=True)

        result_a = builder.build_bundle(source_a, self.output)
        bytes_before = {
            name: (Path(result_a["bundle_path"]) / name).read_bytes()
            for name in policy.ARTIFACT_NAMES + (policy.MANIFEST_NAME,)
        }
        result_b = builder.build_bundle(source_b, self.output)

        self.assertEqual(result_a["bundle_id"], result_b["bundle_id"])
        self.assertEqual(result_a["bundle_path"], result_b["bundle_path"])
        self.assertFalse(result_b["created"])
        for name, expected in bytes_before.items():
            self.assertEqual(
                (Path(result_b["bundle_path"]) / name).read_bytes(), expected
            )

    def test_ceph_compatible_publish_is_fail_closed_and_idempotent(self):
        source = self.write_jsonl([_dataset()], "ceph.jsonl")
        with mock.patch.object(
            storage,
            "_rename_noreplace",
            side_effect=OSError(errno.EINVAL, "operation unsupported"),
        ):
            first = builder.build_bundle(source, self.output)
            second = builder.build_bundle(source, self.output)
        destination = Path(first["bundle_path"])
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(0o500, stat.S_IMODE(destination.stat().st_mode))
        self.assertEqual(
            set(policy.ARTIFACT_NAMES + (policy.MANIFEST_NAME,)),
            {path.name for path in destination.iterdir()},
        )

    def test_artifact_mutation_after_manifest_hash_is_caught_before_publish(self):
        source = self.write_jsonl([_dataset()], "mutation.jsonl")
        original = storage._artifact_description
        calls = 0

        def mutate_after_hash(
            directory_fd, name, records, schema_version, expanded_bytes
        ):
            nonlocal calls
            result = original(
                directory_fd, name, records, schema_version, expanded_bytes
            )
            calls += 1
            if calls == 1:
                os.chmod(name, 0o600, dir_fd=directory_fd)
                descriptor = os.open(
                    name, os.O_WRONLY | os.O_APPEND, dir_fd=directory_fd
                )
                try:
                    os.write(descriptor, b"changed-after-hash")
                finally:
                    os.close(descriptor)
                os.chmod(name, 0o400, dir_fd=directory_fd)
            return result

        with mock.patch.object(
            storage,
            "_artifact_description",
            side_effect=mutate_after_hash,
        ):
            with self.assertRaisesRegex(policy.BundleError, "artifact does not match"):
                builder.build_bundle(source, self.output)
        self.assertFalse(
            any(path.name.startswith("sha256-") for path in self.output.iterdir())
        )

    def test_missing_renameat2_and_enosys_use_portable_publication(self):
        for index, failure in enumerate(
            (
                OSError(errno.ENOSYS, "missing syscall"),
                OSError(errno.EINVAL, "unsupported filesystem"),
            ),
            1,
        ):
            with self.subTest(error=failure.errno):
                output = self.base / ("portable-%d" % index)
                output.mkdir(mode=0o700)
                source = self.write_jsonl(
                    [_dataset(slug="portable-%d" % index)],
                    "portable-%d.jsonl" % index,
                )
                with mock.patch.object(
                    storage, "_rename_noreplace", side_effect=failure
                ):
                    result = builder.build_bundle(source, output)
                self.assertTrue(result["created"])
                self.assertEqual(
                    0o500, stat.S_IMODE(Path(result["bundle_path"]).stat().st_mode)
                )

    def test_rejects_non_public_inactive_and_non_dataset_records(self):
        changes = (("private", True), ("state", "deleted"), ("type", "showcase"))
        for index, (key, value) in enumerate(changes):
            with self.subTest(key=key):
                package = _dataset()
                package[key] = value
                source = self.write_jsonl([package], "selection-%d.jsonl" % index)
                with self.assertRaises(policy.BundleError):
                    builder.build_bundle(source, self.output)

    def test_rejects_duplicate_uuid_and_duplicate_normalized_url(self):
        source = self.write_jsonl(
            [_dataset(UUID_1, "one"), _dataset(UUID_1, "two")], "duplicate-id.jsonl"
        )
        with self.assertRaisesRegex(policy.BundleError, "duplicate dataset UUID"):
            builder.build_bundle(source, self.output)

        source = self.write_jsonl(
            [_dataset(UUID_1, "same"), _dataset(UUID_2, "ｓａｍｅ")],
            "duplicate-url.jsonl",
        )
        with self.assertRaisesRegex(
            policy.BundleError, "duplicate normalized dataset URL"
        ):
            builder.build_bundle(source, self.output)

    def test_rejects_gzip_expansion_bomb(self):
        source = self.base / "bomb.jsonl.gz"
        with gzip.open(source, "wb", compresslevel=9) as stream:
            stream.write(b" " * 4096)
        os.chmod(source, 0o600)
        with mock.patch.object(policy, "MAX_EXPANDED_BYTES", 1024):
            with self.assertRaisesRegex(policy.BundleError, "expanded-size"):
                builder.build_bundle(source, self.output)

    def test_rejects_compressed_input_over_limit(self):
        source = self.write_jsonl([_dataset()], "too-large.jsonl")
        with mock.patch.object(policy, "MAX_COMPRESSED_BYTES", 16):
            with self.assertRaisesRegex(policy.BundleError, "compressed-size"):
                builder.build_bundle(source, self.output)

    def test_rejects_overlong_line(self):
        package = _dataset()
        package["notes"] = "x" * 1024
        source = self.write_jsonl([package], "long-line.jsonl")
        with mock.patch.object(policy, "MAX_LINE_BYTES", 128):
            with self.assertRaisesRegex(policy.BundleError, "line-size"):
                builder.build_bundle(source, self.output)

    def test_rejects_output_artifacts_outside_importer_caps(self):
        source = self.write_jsonl([_dataset()], "output-cap.jsonl")
        with mock.patch.object(policy, "MAX_ARTIFACT_LINE_BYTES", 128):
            with self.assertRaisesRegex(policy.BundleError, "artifact record"):
                builder.build_bundle(source, self.output)
        with mock.patch.object(policy, "MAX_ARTIFACT_EXPANDED_BYTES", 128):
            with self.assertRaisesRegex(policy.BundleError, "expanded-size"):
                builder.build_bundle(source, self.output)
        with mock.patch.object(policy, "MAX_ARTIFACT_COMPRESSED_BYTES", 1):
            with self.assertRaisesRegex(policy.BundleError, "compressed-size"):
                builder.build_bundle(source, self.output)

    def test_rejects_row_and_array_caps_and_oversized_slug(self):
        source = self.write_jsonl([_dataset(UUID_1), _dataset(UUID_2)], "rows.jsonl")
        with mock.patch.object(policy, "MAX_ROWS", 1):
            with self.assertRaisesRegex(policy.BundleError, "row limit"):
                builder.build_bundle(source, self.output)

        package = _dataset()
        package["keywords"] = ["keyword-%d" % index for index in range(129)]
        source = self.write_jsonl([package], "array-cap.jsonl")
        with self.assertRaisesRegex(policy.BundleError, "keywords exceeds"):
            builder.build_bundle(source, self.output)

        package = _dataset(slug="s" * 257)
        source = self.write_jsonl([package], "slug-cap.jsonl")
        with self.assertRaisesRegex(policy.BundleError, "name exceeds"):
            builder.build_bundle(source, self.output)

    def test_rejects_excessive_json_nesting(self):
        package = _dataset()
        nested = "value"
        for _ in range(policy.MAX_NESTING + 1):
            nested = [nested]
        package["ignored"] = nested
        source = self.write_jsonl([package], "nested.jsonl")
        with self.assertRaisesRegex(policy.BundleError, "nesting"):
            builder.build_bundle(source, self.output)

    def test_rejects_invalid_utf8_and_duplicate_json_keys(self):
        invalid = self.base / "invalid.jsonl"
        invalid.write_bytes(b'{"bad":"\xff"}\n')
        os.chmod(invalid, 0o600)
        with self.assertRaisesRegex(policy.BundleError, "UTF-8"):
            builder.build_bundle(invalid, self.output)

        duplicate = self.base / "duplicate-key.jsonl"
        duplicate.write_bytes(
            ('{"id":"%s","id":"%s"}\n' % (UUID_1, UUID_2)).encode("ascii")
        )
        os.chmod(duplicate, 0o600)
        with self.assertRaisesRegex(policy.BundleError, "duplicate JSON object key"):
            builder.build_bundle(duplicate, self.output)

    def test_rejects_surrogates_and_nonfinite_numeric_results(self):
        template = (
            '{"id":"%s","name":"safe","title":%s,"private":false,'
            '"state":"active","type":"dataset","resources":[],"ignored":%s}\n'
        )
        surrogate = self.base / "surrogate.jsonl"
        surrogate.write_bytes((template % (UUID_1, '"\\ud800"', "0")).encode("ascii"))
        os.chmod(surrogate, 0o600)
        with self.assertRaisesRegex(policy.BundleError, "Unicode scalar"):
            builder.build_bundle(surrogate, self.output)

        overflow = self.base / "overflow.jsonl"
        overflow.write_bytes((template % (UUID_1, '"safe"', "1e9999")).encode("ascii"))
        os.chmod(overflow, 0o600)
        with self.assertRaisesRegex(policy.BundleError, "non-finite JSON"):
            builder.build_bundle(overflow, self.output)

    def test_rejects_too_many_resources(self):
        package = _dataset()
        package["resources"] = [
            {"id": "resource-%d" % index, "url": "https://example/%d" % index}
            for index in range(policy.MAX_RESOURCES + 1)
        ]
        source = self.write_jsonl([package], "resources.jsonl")
        with self.assertRaisesRegex(policy.BundleError, "resource limit"):
            builder.build_bundle(source, self.output)

    def test_rejects_symlink_hardlink_and_writable_input(self):
        source = self.write_jsonl([_dataset()], "source.jsonl")
        symlink = self.base / "source-link.jsonl"
        symlink.symlink_to(source)
        with self.assertRaisesRegex(policy.BundleError, "symlink"):
            builder.build_bundle(symlink, self.output)

        hardlink = self.base / "source-hardlink.jsonl"
        os.link(source, hardlink)
        with self.assertRaisesRegex(policy.BundleError, "hard-linked"):
            builder.build_bundle(source, self.output)
        hardlink.unlink()

        os.chmod(source, 0o666)
        with self.assertRaisesRegex(policy.BundleError, "group/world writable"):
            builder.build_bundle(source, self.output)

    def test_rejects_unsafe_output_root_and_redirected_bundle(self):
        source = self.write_jsonl([_dataset()], "source.jsonl")
        unsafe = self.base / "unsafe"
        unsafe.mkdir(mode=0o755)
        os.chmod(unsafe, 0o755)
        with self.assertRaisesRegex(policy.BundleError, "0700"):
            builder.build_bundle(source, unsafe)

        root_link = self.base / "root-link"
        root_link.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(policy.BundleError, "symlink"):
            builder.build_bundle(source, root_link)

        nested_source = self.output / "nested-source.jsonl"
        nested_source.write_bytes(source.read_bytes())
        os.chmod(nested_source, 0o600)
        with self.assertRaisesRegex(policy.BundleError, "inside the output root"):
            builder.build_bundle(nested_source, self.output)
        nested_source.unlink()

        result = builder.build_bundle(source, self.output)
        redirected_root = self.base / "redirected"
        redirected_root.mkdir(mode=0o700)
        redirected = redirected_root / Path(result["bundle_path"]).name
        redirected.symlink_to(result["bundle_path"], target_is_directory=True)
        with self.assertRaisesRegex(policy.BundleError, "safe directory"):
            builder.build_bundle(source, redirected_root)

    def test_source_leaf_is_opened_relative_to_its_pinned_parent(self):
        source_parent = self.base / "source-parent"
        source_parent.mkdir(mode=0o700)
        source = source_parent / "snapshot.jsonl"
        source.write_bytes(
            json.dumps(_dataset(slug="pinned-source"), separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
        os.chmod(source, 0o600)

        redirected_parent = self.base / "redirected-source-parent"
        redirected_parent.mkdir(mode=0o700)
        redirected = redirected_parent / source.name
        redirected.write_bytes(
            json.dumps(
                _dataset(slug="redirected-source"), separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )
        os.chmod(redirected, 0o600)
        pinned_parent = self.base / "pinned-source-parent"

        original = storage._open_directory_chain
        swapped = False

        def swap_after_parent_open(path, description):
            nonlocal swapped
            descriptor = original(path, description)
            if path == str(source_parent) and not swapped:
                source_parent.rename(pinned_parent)
                source_parent.symlink_to(redirected_parent, target_is_directory=True)
                swapped = True
            return descriptor

        try:
            with mock.patch.object(
                storage,
                "_open_directory_chain",
                side_effect=swap_after_parent_open,
            ):
                result = builder.build_bundle(source, self.output)
        finally:
            if source_parent.is_symlink():
                source_parent.unlink()
            if pinned_parent.exists():
                pinned_parent.rename(source_parent)

        corpus = self.read_jsonl(Path(result["bundle_path"]) / "corpus.v2.jsonl.gz")[0]
        self.assertEqual("pinned-source", corpus["name"])

    def test_output_writes_stay_on_pinned_root_after_ancestor_swap(self):
        source = self.write_jsonl([_dataset(slug="fd-target")], "fd-target.jsonl")
        managed_parent = self.base / "managed-parent"
        managed_parent.mkdir(mode=0o700)
        output = managed_parent / "bundles"
        output.mkdir(mode=0o700)
        redirected_parent = self.base / "redirected-output-parent"
        redirected_parent.mkdir(mode=0o700)
        redirected_output = redirected_parent / "bundles"
        redirected_output.mkdir(mode=0o700)
        pinned_parent = self.base / "pinned-output-parent"

        original = storage._open_output_root

        def swap_after_root_open(path):
            descriptor = original(path)
            managed_parent.rename(pinned_parent)
            managed_parent.symlink_to(redirected_parent, target_is_directory=True)
            return descriptor

        try:
            with mock.patch.object(
                storage,
                "_open_output_root",
                side_effect=swap_after_root_open,
            ):
                result = builder.build_bundle(source, output)
            self.assertFalse(
                any(
                    path.name.startswith("sha256-")
                    for path in redirected_output.iterdir()
                )
            )
            self.assertTrue(
                any(
                    path.name.startswith("sha256-")
                    for path in (pinned_parent / "bundles").iterdir()
                )
            )
        finally:
            if managed_parent.is_symlink():
                managed_parent.unlink()
            if pinned_parent.exists():
                pinned_parent.rename(managed_parent)

        self.assertTrue(Path(result["bundle_path"]).is_dir())

    def test_existing_bundle_is_verified_not_overwritten(self):
        source = self.write_jsonl([_dataset()], "source.jsonl")
        result = builder.build_bundle(source, self.output)
        bundle = Path(result["bundle_path"])
        artifact = bundle / "corpus.v2.jsonl.gz"
        os.chmod(bundle, 0o700)
        os.chmod(artifact, 0o600)
        with artifact.open("ab") as stream:
            stream.write(b"corrupt")
        os.chmod(artifact, 0o400)
        os.chmod(bundle, 0o500)

        with self.assertRaisesRegex(policy.BundleError, "artifact does not match"):
            builder.build_bundle(source, self.output)


if __name__ == "__main__":
    unittest.main()
