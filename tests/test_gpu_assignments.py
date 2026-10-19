#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from check_gpu_assignments import (  # noqa: E402
    main,
    parse_available_gpu_inventory,
    validate_gpu_assignments,
)


GPU_A = "GPU-00000000-0000-0000-0000-000000000001"
GPU_B = "GPU-00000000-0000-0000-0000-000000000002"
GPU_C = "GPU-00000000-0000-0000-0000-000000000003"
GPU_D = "GPU-00000000-0000-0000-0000-000000000004"
GPU_E = "GPU-00000000-0000-0000-0000-000000000005"


def inventory(*gpu_uuids: str) -> str:
    return "".join(f"{gpu_uuid}, {index}\n" for index, gpu_uuid in enumerate(gpu_uuids))


class GpuAssignmentTests(unittest.TestCase):
    def test_valid_one_one_two_layout_with_empty_jupyter_pool(self) -> None:
        result = validate_gpu_assignments(
            embedding=GPU_A,
            scope=GPU_B,
            answer=f"{GPU_C},{GPU_D}",
            jupyter="",
            available_inventory=inventory(GPU_A, GPU_B, GPU_C, GPU_D),
        )
        self.assertEqual((GPU_A,), result["embedding"])
        self.assertEqual((GPU_B,), result["scope"])
        self.assertEqual((GPU_C, GPU_D), result["answer"])
        self.assertEqual((), result["jupyter"])

    def test_cpu_embedding_and_nonempty_jupyter_pool_are_accepted(self) -> None:
        result = validate_gpu_assignments(
            embedding="",
            scope=GPU_A,
            answer=f"{GPU_B},{GPU_C}",
            jupyter=f" {GPU_D}, {GPU_E} ",
        )
        self.assertEqual((), result["embedding"])
        self.assertEqual((GPU_D, GPU_E), result["jupyter"])

    def test_role_cardinality_is_enforced(self) -> None:
        cases = (
            {
                "embedding": f"{GPU_A},{GPU_B}",
                "scope": GPU_C,
                "answer": f"{GPU_D},{GPU_E}",
            },
            {"embedding": GPU_A, "scope": "", "answer": f"{GPU_C},{GPU_D}"},
            {
                "embedding": GPU_A,
                "scope": f"{GPU_B},{GPU_C}",
                "answer": f"{GPU_D},{GPU_E}",
            },
            {"embedding": GPU_A, "scope": GPU_B, "answer": GPU_C},
            {"embedding": GPU_A, "scope": GPU_B, "answer": f"{GPU_C},{GPU_D},{GPU_E}"},
        )
        for assignment in cases:
            with self.subTest(assignment=assignment), self.assertRaises(ValueError):
                validate_gpu_assignments(jupyter="", **assignment)

    def test_duplicate_within_role_and_cross_role_overlap_are_rejected(self) -> None:
        cases = (
            dict(embedding="", scope=GPU_A, answer=f"{GPU_B},{GPU_B}", jupyter=""),
            dict(embedding=GPU_A, scope=GPU_A, answer=f"{GPU_B},{GPU_C}", jupyter=""),
            dict(
                embedding=GPU_A, scope=GPU_B, answer=f"{GPU_C},{GPU_D}", jupyter=GPU_D
            ),
        )
        for assignment in cases:
            with self.subTest(assignment=assignment), self.assertRaises(ValueError):
                validate_gpu_assignments(**assignment)

    def test_uuid_hex_case_cannot_bypass_overlap_or_inventory_checks(self) -> None:
        gpu_lower = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gpu_upper = "GPU-AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        with self.assertRaisesRegex(ValueError, "assigned to both"):
            validate_gpu_assignments(
                embedding=gpu_lower,
                scope=gpu_upper,
                answer=f"{GPU_B},{GPU_C}",
                jupyter="",
            )

        result = validate_gpu_assignments(
            embedding="",
            scope=gpu_upper,
            answer=f"{GPU_B},{GPU_C}",
            jupyter="",
            available_inventory=inventory(gpu_lower, GPU_B, GPU_C),
        )
        self.assertEqual((gpu_lower,), result["scope"])
        with self.assertRaisesRegex(ValueError, "duplicate GPU UUID"):
            parse_available_gpu_inventory(f"{gpu_lower}, 0\n{gpu_upper}, 1\n")

    def test_indexes_aliases_mig_and_malformed_values_are_rejected(self) -> None:
        invalid_values = ("0", "all", "GPU-0", "MIG-GPU-00000000/1/0", f"{GPU_A},")
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_gpu_assignments(
                    embedding="",
                    scope=value,
                    answer=f"{GPU_B},{GPU_C}",
                    jupyter="",
                )

    def test_inventory_requires_unique_physical_uuid_and_index_records(self) -> None:
        malformed = (
            "",
            f"{GPU_A}\n",
            f"{GPU_A}, x\n",
            f"{GPU_A}, 0, extra\n",
            f"{GPU_A}, 0\n{GPU_A}, 1\n",
            f"{GPU_A}, 0\n{GPU_B}, 0\n",
            "MIG-GPU-00000000/1/0, 0\n",
        )
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_available_gpu_inventory(value)

    def test_assignment_missing_from_inventory_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not present"):
            validate_gpu_assignments(
                embedding=GPU_A,
                scope=GPU_B,
                answer=f"{GPU_C},{GPU_D}",
                jupyter="",
                available_inventory=inventory(GPU_A, GPU_B, GPU_C),
            )

    def test_cli_accepts_inventory_on_stdin_and_fails_closed(self) -> None:
        argv = [
            "--embedding-gpus",
            GPU_A,
            "--scope-gpus",
            GPU_B,
            "--answer-gpus",
            f"{GPU_C},{GPU_D}",
            "--available-from-stdin",
        ]
        with patch("sys.stdin", io.StringIO(inventory(GPU_A, GPU_B, GPU_C, GPU_D))):
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(0, main(argv))

        argv[1] = GPU_B
        with patch("sys.stdin", io.StringIO(inventory(GPU_A, GPU_B, GPU_C, GPU_D))):
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(1, main(argv))


if __name__ == "__main__":
    unittest.main()
