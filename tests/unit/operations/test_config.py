"""Product configuration policy, with no daemon, models, or real secrets."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from search.operations.config import (
    load_config,
    validate_config,
    validate_gpu_inventory,
)


UUIDS = tuple(f"GPU-00000000-0000-0000-0000-{index:012x}" for index in range(1, 5))


class ProductConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state_parent = self.root / "private"
        self.state_parent.mkdir(mode=0o700)
        self.models = []
        for name in ("api", "embedding", "reranker"):
            path = self.root / name
            path.mkdir(mode=0o700)
            self.models.append(path)
        self.value = {
            "schema_version": 2,
            "catalog_storage": "docker-volume",
            "state_root": str(self.state_parent / "state"),
            "api_model_root": str(self.models[0]),
            "embedding_model_root": str(self.models[1]),
            "reranker_model_root": str(self.models[2]),
            "gpus": {
                "scope": UUIDS[0],
                "planner": list(UUIDS[1:3]),
                "reranker": UUIDS[3],
            },
            "cache_ttl_seconds": 0,
        }
        self.path = self.root / "product.json"
        self.save()

    def save(self, raw=None):
        self.path.write_text(json.dumps(self.value) if raw is None else raw)
        self.path.chmod(0o600)

    def validate(self, value=None):
        return validate_config(
            self.value if value is None else value, repo_root=self.repo
        )

    def load(self):
        return load_config(self.path, repo_root=self.repo)

    def test_round_trip_and_missing_state_are_read_only(self):
        before = self.path.read_bytes()
        config = self.load()
        self.assertEqual(config.to_dict(), self.value)
        self.assertEqual(config.gpus.planner, UUIDS[1:3])
        self.assertFalse(config.state_root.exists())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(config.cache_ttl_seconds, 0)

    def test_existing_state_requires_private_operator_directory(self):
        state = self.state_parent / "state"
        state.mkdir(mode=0o700)
        self.validate()
        state.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "directory_unsafe"):
            self.validate()

    def test_missing_state_parent_is_not_created(self):
        self.value["state_root"] = str(self.root / "missing" / "state")
        with self.assertRaises(OSError):
            self.validate()
        self.assertFalse((self.root / "missing").exists())

    def test_state_parent_must_be_private(self):
        self.state_parent.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "directory_unsafe"):
            self.validate()

    def test_model_directory_must_exist_and_not_be_group_writable(self):
        self.models[0].chmod(0o775)
        with self.assertRaisesRegex(ValueError, "directory_unsafe"):
            self.validate()
        self.models[0].chmod(0o755)
        self.validate()
        self.value["api_model_root"] = str(self.root / "missing-model")
        with self.assertRaises(OSError):
            self.validate()

    def test_unknown_and_missing_fields_fail(self):
        for value in (
            {**self.value, "token": "not-a-real-secret"},
            {key: item for key, item in self.value.items() if key != "gpus"},
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "fields"),
            ):
                self.validate(value)

    def test_schema_and_cache_values_are_strict(self):
        for key, invalid in (
            ("schema_version", True),
            ("schema_version", 1),
            ("schema_version", 3),
            ("schema_version", 2.0),
            ("schema_version", "2"),
            ("cache_ttl_seconds", True),
            ("cache_ttl_seconds", -1),
            ("cache_ttl_seconds", 3601),
            ("cache_ttl_seconds", "0"),
        ):
            value = {**self.value, key: invalid}
            with self.subTest(key=key, value=invalid), self.assertRaises(ValueError):
                self.validate(value)
        self.validate({**self.value, "cache_ttl_seconds": 3600})

    def test_broad_repo_relative_home_and_mount_injection_paths_fail(self):
        bad = (
            "/",
            str(Path.home()),
            str(self.repo),
            str(self.repo / "data"),
            str(self.repo.parent),
            "relative",
            str(self.root / ".." / "escape"),
            str(self.models[0]) + ",readonly=false",
            str(self.models[0]) + "\n",
        )
        for path in bad:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate({**self.value, "api_model_root": path})

    def test_all_root_pairs_cannot_overlap(self):
        for first, second in (
            ("state_root", "api_model_root"),
            ("api_model_root", "embedding_model_root"),
            ("embedding_model_root", "reranker_model_root"),
        ):
            value = {**self.value, first: self.value[second]}
            with (
                self.subTest(first=first),
                self.assertRaisesRegex(ValueError, "roots_overlap"),
            ):
                self.validate(value)
        with self.assertRaisesRegex(ValueError, "roots_overlap"):
            self.validate({**self.value, "state_root": str(self.models[0] / "state")})

    def test_symlink_roots_and_ancestors_fail(self):
        link = self.root / "linked"
        link.symlink_to(self.models[0], target_is_directory=True)
        for path in (link, link / "nested"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "symlink"):
                self.validate({**self.value, "api_model_root": str(path)})

    def test_configuration_file_modes_owner_and_size_fail(self):
        for mode in (0o400, 0o644, 0o666):
            self.path.chmod(mode)
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(ValueError, "file_unsafe"),
            ):
                self.load()
        self.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "file_unsafe"):
            load_config(self.path, repo_root=self.repo, uid=os.geteuid() + 1)
        self.save(" " * (16 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "file_unsafe"):
            self.load()

    def test_configuration_links_and_fifo_fail_without_reading(self):
        link = self.root / "config-link"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "symlink"):
            load_config(link, repo_root=self.repo)
        hardlink = self.root / "config-hardlink"
        os.link(self.path, hardlink)
        with self.assertRaisesRegex(ValueError, "file_unsafe"):
            self.load()
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(ValueError, "file_unsafe"):
            load_config(fifo, repo_root=self.repo)

    def test_duplicate_keys_invalid_json_and_nonobjects_fail(self):
        for raw in (
            '{"schema_version":1,"schema_version":1}',
            '{"gpus":{"scope":"a","scope":"b"}}',
            "{",
            "null",
            "[]",
        ):
            self.save(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.load()

    def test_changed_configuration_is_rejected(self):
        real_read = os.read

        def changed(fd, count):
            raw = real_read(fd, count)
            self.path.write_bytes(raw + b" ")
            return raw

        with mock.patch("search.operations.config.os.read", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "config_changed"):
                self.load()

    def test_gpu_fields_and_cardinality_are_fixed(self):
        for raw in (
            {**self.value["gpus"], "embedding": UUIDS[3]},
            {**self.value["gpus"], "planner": UUIDS[1]},
            {**self.value["gpus"], "planner": [UUIDS[1]]},
            {**self.value["gpus"], "planner": list(UUIDS[1:])},
        ):
            with self.subTest(gpus=raw), self.assertRaises(ValueError):
                self.validate({**self.value, "gpus": raw})

    def test_gpu_uuid_indices_all_mig_and_overlap_fail(self):
        for invalid in (
            "all",
            "0",
            "MIG-" + UUIDS[0],
            UUIDS[0] + "," + UUIDS[1],
            UUIDS[0] + " ",
            None,
        ):
            value = copy.deepcopy(self.value)
            value["gpus"]["scope"] = invalid
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.validate(value)
        value = copy.deepcopy(self.value)
        value["gpus"]["reranker"] = UUIDS[0]
        with self.assertRaisesRegex(ValueError, "gpus_overlap"):
            self.validate(value)

    def test_uuid_case_is_canonical_and_overlap_case_insensitive(self):
        value = copy.deepcopy(self.value)
        value["gpus"]["scope"] = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        value["gpus"]["reranker"] = value["gpus"]["scope"].upper()
        with self.assertRaisesRegex(ValueError, "gpus_overlap"):
            self.validate(value)
        value["gpus"]["reranker"] = UUIDS[3]
        value["gpus"]["scope"] = value["gpus"]["scope"].upper()
        self.assertEqual(
            self.validate(value).gpus.scope, "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        )

    def test_gpu_inventory_presence_duplicates_and_malformed_records(self):
        config = self.validate()
        inventory = "\n".join(
            f"{identifier},{index}" for index, identifier in enumerate(UUIDS)
        )
        validate_gpu_inventory(config, inventory)
        for invalid in (
            "",
            "\n".join(inventory.splitlines()[:3]),
            inventory + "\n" + inventory.splitlines()[0],
            inventory.replace(",3", ",0"),
            inventory.replace(",3", ",oops"),
            inventory + ",extra",
        ):
            with self.subTest(inventory=invalid), self.assertRaises(ValueError):
                validate_gpu_inventory(config, invalid)


if __name__ == "__main__":
    unittest.main()
