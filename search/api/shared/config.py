"""Small, file-backed configuration. No caller-controlled upstream URLs."""

from dataclasses import dataclass
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class Settings:
    revision: str
    manifest_sha256: str
    bundle_path: Path
    key_file: Path
    rerank_enabled: bool = False
    rerank_candidates: int = 10

    def __post_init__(self):
        if (
            type(self.rerank_enabled) is not bool
            or type(self.rerank_candidates) is not int
            or not 1 <= self.rerank_candidates <= 20
        ):
            raise ValueError("invalid reranker configuration")

    @classmethod
    def from_environment(cls):
        revision = os.environ.get("SOURCE_REVISION", "development")
        if (
            revision != "development"
            and re.fullmatch(r"[0-9a-f]{40}", revision) is None
        ):
            raise ValueError("invalid source revision")
        digest = os.environ.get("SEARCH_API_MANIFEST_SHA256", "")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid catalog manifest digest")
        bundle = Path(os.environ.get("SEARCH_API_BUNDLE_PATH", "/catalog"))
        enabled = os.environ.get("SEARCH_API_RERANK_ENABLED", "0")
        candidates = os.environ.get("SEARCH_API_RERANK_CANDIDATES", "10")
        if (
            enabled not in {"0", "1"}
            or re.fullmatch(r"[1-9][0-9]?", candidates) is None
        ):
            raise ValueError("invalid reranker configuration")
        return cls(
            revision,
            digest,
            bundle,
            Path("/run/secrets/api_key"),
            enabled == "1",
            int(candidates),
        )
