"""Share image source data, a Docker fake and a staging fixture."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment import image_build, image_staging, source_bundle
from search.operations.shared import private_io
from search.operations.state import locked_state


ROOT = Path(__file__).resolve().parents[3]


def blob(raw):
    return hashlib.sha1(
        b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw
    ).hexdigest()


def fixture_sources():
    selected = set().union(
        *(
            recipe["inputs"]
            | {recipe["dockerfile"], recipe["dockerfile"] + ".dockerignore"}
            for recipe in source_bundle.RECIPES.values()
        )
    )
    recipes = {
        path
        for recipe in source_bundle.RECIPES.values()
        for path in (recipe["dockerfile"], recipe["dockerfile"] + ".dockerignore")
    }
    return {
        path: (ROOT / path).read_bytes()
        if path in recipes
        else ("public source: " + path + "\n").encode()
        for path in selected
    }


def fixture_tree(sources):
    return {
        path: {"mode": "100644", "type": "blob", "object": blob(raw)}
        for path, raw in sources.items()
    }


class FakeDocker:
    def __init__(self, root, plan):
        self.root, self.plan = root, plan
        self.calls = []
        self.on_build = None
        self.return_failure = False
        self.info = "linux"
        self.iid_suffix = b"\n"
        self.iid_mode = 0o600
        self.images = {}
        for index, service in enumerate(image_build.SERVICES, 1):
            self.images[service] = {
                "Id": "sha256:" + str(index) * 64,
                "Config": {
                    "Labels": dict(plan["services"][service]["required_labels"]),
                    "Env": [
                        key + "=" + value
                        for key, value in source_bundle._required_environment(
                            service, plan["revision"]
                        ).items()
                    ]
                    + ["PATH=/usr/bin", "PRIVATE_SENTINEL=never-save-this-value"],
                },
            }

    def __call__(self, argv, *, timeout, environment):
        self.calls.append((list(argv), timeout, dict(environment)))
        assert argv[:3] == ["docker", "--host", image_build.DOCKER_HOST]
        assert argv[3] == "--config"
        client = Path(argv[4])
        assert client.stat().st_mode & 0o777 == 0o700
        arguments = argv[5:]
        if arguments[0] == "info":
            assert not list(client.iterdir())
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.info), "")
        if arguments[0] == "build":
            iid = Path(arguments[arguments.index("--iidfile") + 1])
            service = iid.parent.name
            intent = json.loads((iid.parent / "started.json").read_text())
            assert intent["service"] == service
            assert intent["revision"] == self.plan["revision"]
            assert not (iid.parent / "complete.json").exists()
            if self.on_build:
                self.on_build(service, iid)
            if self.return_failure:
                return subprocess.CompletedProcess(
                    argv, 23, b"", b"private build output must not appear"
                )
            if not iid.exists() and not iid.is_symlink():
                iid.write_bytes(self.images[service]["Id"].encode() + self.iid_suffix)
                iid.chmod(self.iid_mode)
            return subprocess.CompletedProcess(argv, 0, b"built", b"")
        assert arguments[:2] == ["image", "inspect"]
        reference = arguments[-1]
        for service, image in self.images.items():
            if reference in {
                image["Id"],
                image_build.TAGS[service] + self.plan["revision"],
            }:
                return subprocess.CompletedProcess(argv, 0, json.dumps(image), "")
        return subprocess.CompletedProcess(argv, 1, b"", b"private not found output")


class ImageStagingFixture:
    """Create private staging evidence with cleanup owned by the running test."""

    def __init__(self, test):
        self.test = test
        temporary = tempfile.TemporaryDirectory(prefix="depositar-image-staging-test-")
        test.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir(mode=0o700)
        self.sources = fixture_sources()
        for path, raw in self.sources.items():
            destination = self.repo / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        (self.repo / "search/catalog/query.py").chmod(0o755)
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--quiet",
            "-m",
            "Fixture",
        )
        self.revision = self.git("rev-parse", "HEAD").strip()
        tree = fixture_tree(self.sources)
        tree["search/catalog/query.py"]["mode"] = "100755"
        self.plan = source_bundle.plan_source_bundle(self.revision, tree, self.sources)
        self.config = ProductConfig(
            1,
            self.root / "product",
            self.root / "api-models",
            self.root / "embedding-models",
            self.root / "reranker-models",
            GPUConfig("GPU-a", ("GPU-b", "GPU-c"), "GPU-d"),
        )
        self.parent = self.config.state_root / "image-builds"
        self.slot = self.parent / ("source-" + self.revision)
        self.docker = FakeDocker(self.slot, self.plan)

    def git(self, *arguments):
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.test.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def invoke(self, *, source_revision=None):
        with locked_state(self.config, create=True) as store:
            return image_staging.stage_product_images(
                self.config,
                store,
                self.revision if source_revision is None else source_revision,
                repo_root=self.repo,
                docker_runner=self.docker,
            )

    def record(self, phase):
        return self.parent / (self.slot.name + "." + phase + ".json")

    def read(self, phase):
        return json.loads(self.record(phase).read_bytes())

    def change_record(self, phase, value):
        self.record(phase).write_bytes(private_io.canonical(value))
