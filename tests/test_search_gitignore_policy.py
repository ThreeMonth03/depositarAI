from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def check_ignore(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "check-ignore", "-q", "--", path],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class SearchGitIgnorePolicyTests(unittest.TestCase):
    def test_generated_private_search_paths_are_ignored(self):
        paths = (
            ".env.search-canary",
            ".env.search-api",
            ".env.search-rerank",
            ".env.search-web",
            "deploy/env/.env.search-canary",
            "deploy/env/.env.search-api",
            "deploy/env/.env.search-rerank",
            "search-api-runtime",
            "search-runtime",
            "search-canary-data/opensearch/nodes/0",
            "search-canary-secrets/opensearch-password",
            "search-canary-models/blobs/model",
            "search-canary-index/nodes/0",
            "search-canary-corpus/depositar.jsonl.gz",
            "search/secrets/opensearch-password",
            "search/models/blobs/model",
            "search/index/nodes/0",
            "search/corpus/depositar.jsonl.gz",
        )
        for path in paths:
            with self.subTest(path=path):
                result = check_ignore(path)
                self.assertIn(result.returncode, (0, 1), result.stderr)
                self.assertEqual(0, result.returncode, f"must be ignored: {path}")

    def test_reviewable_search_inputs_are_not_ignored(self):
        paths = (
            "deploy/env/.env.search-canary.example",
            "deploy/env/.env.search-web.example",
            "deploy/env/product.example.json",
        )
        for path in paths:
            with self.subTest(path=path):
                result = check_ignore(path)
                self.assertIn(result.returncode, (0, 1), result.stderr)
                self.assertEqual(
                    1, result.returncode, f"must remain reviewable: {path}"
                )


if __name__ == "__main__":
    unittest.main()
