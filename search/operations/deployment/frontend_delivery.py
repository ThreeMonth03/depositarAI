"""Apply an attested CPU frontend release using the existing deployment checker.

Only this frontend Compose project is managed. Downloads precede admission
changes; a bounded intent retains enough identity to recover an interrupted
cutover without stopping an unknown container or changing backend resources.
"""

from contextlib import contextmanager
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

from search.operations.shared import private_io
from search.serialization import strict_json
from search.transport.version import CONVERSATION_CONTRACT_VERSION
from . import frontend_check as check
from . import frontend_policy
from .runtime_docker import Docker


REPOSITORY = "ThreeMonth03/depositarAI"
MAX_RECORD = 256 * 1024
TRANSACTION_LABEL = "io.depositar.ai.frontend-delivery.transaction"
ROLES = ("web", "web-nginx")
CONFIG_FIELDS = {
    "schema_version",
    "repository",
    "repository_id",
    "state_root",
    "frontend_check_file",
    "gh_binary",
    "monitor_lock_file",
}


class FrontendDeliveryError(ValueError):
    """Closed diagnostics; no child output, credentials or private paths."""


def require(condition, code):
    if not condition:
        raise FrontendDeliveryError("frontend_delivery_" + code)


def _path(value):
    require(
        type(value) is str and value.isascii() and not any(ord(c) < 32 for c in value),
        "config_invalid",
    )
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path, "config_invalid")
    return path


def _read_json(path, *, maximum=MAX_RECORD):
    fd = private_io.open_directory(path.parent)
    try:
        return strict_json(
            private_io.read_file(fd, path.name, mode=0o600, max_bytes=maximum)
        )
    finally:
        os.close(fd)


def load_config(path):
    config_path = _path(str(path))
    value = _read_json(config_path, maximum=16384)
    require(type(value) is dict and set(value) == CONFIG_FIELDS, "config_invalid")
    require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["repository"] == REPOSITORY
        and type(value["repository_id"]) is int
        and 0 < value["repository_id"] < 2**63,
        "config_invalid",
    )
    for key in ("state_root", "frontend_check_file", "gh_binary", "monitor_lock_file"):
        value[key] = _path(value[key])
    root = value["state_root"]
    require(
        not any((parent / ".git").exists() for parent in (root, *root.parents)),
        "state_inside_git",
    )
    require(
        all(
            root not in p.parents and p != root
            for p in (config_path, value["frontend_check_file"], value["gh_binary"])
        ),
        "state_overlaps_inputs",
    )
    require(value["monitor_lock_file"].name == ".check.lock", "config_invalid")
    gh = value["gh_binary"].stat()
    require(
        stat.S_ISREG(gh.st_mode)
        and gh.st_uid in (0, os.geteuid())
        and not stat.S_IMODE(gh.st_mode) & 0o022
        and os.access(value["gh_binary"], os.X_OK),
        "gh_binary_unsafe",
    )
    # Publication replaces this mutable canonical file; frozen 0400 profiles
    # remain suitable for the read-only checker, not the delivery transaction.
    check.load_config(value["frontend_check_file"])
    _read_json(value["frontend_check_file"], maximum=16384)
    return value


@contextmanager
def _lock(parent_fd, name, *, create=False, wait=0):
    fd = os.open(
        name,
        os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.geteuid()
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) == 0o600,
            "lock_unsafe",
        )
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, "busy")
                time.sleep(0.1)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        require(
            (before.st_dev, before.st_ino) == (named.st_dev, named.st_ino),
            "lock_changed",
        )
        yield
    finally:
        os.close(fd)


class State:
    def __init__(self, root):
        self.root = root
        self.fd = private_io.open_directory(root)

    def read(self, name):
        private_io.verify_directory(self.fd, self.root)
        try:
            return strict_json(
                private_io.read_file(self.fd, name, mode=0o600, max_bytes=MAX_RECORD)
            )
        except FileNotFoundError:
            return None

    def write(self, name, value):
        private_io.verify_directory(self.fd, self.root)
        private_io.write_json(self.fd, name, value, replace=True, max_bytes=MAX_RECORD)
        return self.root / name

    def remove(self, name):
        private_io.verify_directory(self.fd, self.root)
        try:
            os.unlink(name, dir_fd=self.fd)
            os.fsync(self.fd)
        except FileNotFoundError:
            pass

    def close(self):
        os.close(self.fd)

    @contextmanager
    def locked(self):
        with _lock(self.fd, ".delivery.lock", create=True):
            try:
                yield
            finally:
                self.remove(".profile.json")


def _manifest_key(role):
    return "web_platform_manifest" if role == "web" else "web_nginx_platform_manifest"


def _locator(revision, role):
    return (
        "depositar-search-web:" if role == "web" else "depositar-search-web-nginx:"
    ) + revision


def _native(composition, profile, nonce):
    value = copy.deepcopy(composition)
    for role in ROLES:
        service = value["services"][role]
        service.pop("build", None)
        service["image"] = _locator(profile["frontend_revision"], role)
        if nonce is not None:
            service.setdefault("labels", {})[TRANSACTION_LABEL] = nonce
    frontend_policy.validate(
        value, intranet=profile["deployment_mode"] == "intranet-http"
    )
    return value


def _summary(status, state):
    return {
        "status": status,
        "revision": state["current"]["profile"]["frontend_revision"],
        "previous_revision": state["previous"]["profile"]["frontend_revision"]
        if state["previous"]
        else None,
    }


class Delivery:
    def __init__(self, config, state, docker, runner):
        self.config, self.state, self.docker, self.runner = (
            config,
            state,
            docker,
            runner,
        )

    def profile(self, value):
        return check.load_config(self.state.write(".profile.json", value))

    def checked(self, profile):
        path = self.state.write(".profile.json", profile)
        return check.check(path, runner=self.runner)

    def canonical(self):
        return check.load_config(self.config["frontend_check_file"])

    def render(self, profile):
        self.docker.environment["SOURCE_REVISION"] = profile["frontend_revision"]
        return check._compose(self.docker, profile)

    def record(self, value):
        require(
            type(value) is dict
            and set(value) == {"profile", "composition", "runtime", "nonce"},
            "state_invalid",
        )
        profile = self.profile(value["profile"])
        require(
            value["nonce"] is None
            or (
                type(value["nonce"]) is str
                and len(value["nonce"]) == 32
                and all(c in "0123456789abcdef" for c in value["nonce"])
            ),
            "state_invalid",
        )
        require(
            _native(value["composition"], profile, value["nonce"])
            == value["composition"],
            "state_invalid",
        )
        runtime = value["runtime"]
        require(
            runtime is None or (type(runtime) is dict and set(runtime) == set(ROLES)),
            "state_invalid",
        )
        if runtime is not None:
            for role in ROLES:
                row = runtime[role]
                require(
                    type(row) is dict
                    and set(row)
                    == {"container_id", "container_image_index", "platform_manifest"}
                    and type(row["container_id"]) is str
                    and check.HEX64.fullmatch(row["container_id"])
                    and type(row["container_image_index"]) is str
                    and check.IMAGE.fullmatch(row["container_image_index"])
                    and row["platform_manifest"] == profile[_manifest_key(role)],
                    "state_invalid",
                )
        return value

    def saved_state(self, value):
        require(
            type(value) is dict
            and set(value) == {"schema_version", "current", "previous"}
            and type(value["schema_version"]) is int
            and value["schema_version"] == 1,
            "state_invalid",
        )
        self.record(value["current"])
        require(value["current"]["runtime"] is not None, "state_invalid")
        if value["previous"] is not None:
            self.record(value["previous"])
            require(value["previous"]["runtime"] is not None, "state_invalid")
        return value

    def baseline(self):
        profile = self.canonical()
        proof = self.checked(profile)
        saved = self.state.read("state.json")
        if saved is not None:
            self.saved_state(saved)
            require(
                saved["current"]["profile"] == profile
                and saved["current"]["runtime"] == proof["runtime"],
                "current_changed",
            )
            return saved
        # Initial adoption is based on an actual successful checker run, not a
        # revision guess or a receipt copied from another machine.
        return {
            "schema_version": 1,
            "current": {
                "profile": profile,
                "composition": _native(self.render(profile), profile, None),
                "runtime": proof["runtime"],
                "nonce": None,
            },
            "previous": None,
        }

    def verify_images(self, record):
        profile = record["profile"]
        for role in ROLES:
            manifest = profile[_manifest_key(role)]
            check.inspect_frontend_image(
                self.docker,
                _locator(profile["frontend_revision"], role),
                manifest,
                profile["frontend_revision"],
                role,
            )

    def network_ids(self, record):
        ids = {}
        project = record["composition"]["name"]
        allowed = {r["container_id"] for r in record["runtime"].values()}
        for spec in record["composition"]["networks"].values():
            name = spec["name"]
            row = strict_json(
                self.docker.call(["network", "inspect", "--format", "{{json .}}", name])
            )
            require(
                row["Labels"].get("com.docker.compose.project") == project
                and set(row["Containers"]) <= allowed,
                "ownership_changed",
            )
            ids[name] = row["Id"]
        return ids

    def guard(self, intent):
        before, candidate = intent["before_state"]["current"], intent["candidate"]
        project = before["composition"]["name"]
        require(
            self.canonical() in (before["profile"], candidate["profile"]),
            "canonical_changed",
        )
        raw = self.docker.call(
            [
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=" + project,
                "--format",
                "{{.ID}}",
            ]
        )
        identifiers = raw.decode("ascii").splitlines()
        require(
            len(identifiers) <= 2
            and len(set(identifiers)) == len(identifiers)
            and all(check.HEX64.fullmatch(x) for x in identifiers),
            "ownership_changed",
        )
        roles = set()
        for identifier in identifiers:
            row = strict_json(
                self.docker.call(["inspect", "--format", check.INSPECT, identifier])
            )
            labels = row["Config"]["Labels"]
            role = labels.get("com.docker.compose.service")
            require(
                role in ROLES
                and role not in roles
                and labels.get("com.docker.compose.project") == project,
                "ownership_changed",
            )
            roles.add(role)
            if identifier == before["runtime"][role]["container_id"]:
                expected = before
            else:
                require(
                    labels.get(TRANSACTION_LABEL) == intent["nonce"],
                    "ownership_changed",
                )
                choices = [
                    r
                    for r in (before, candidate)
                    if labels.get("org.opencontainers.image.revision")
                    == r["profile"]["frontend_revision"]
                ]
                require(len(choices) == 1, "ownership_changed")
                expected = choices[0]
            require(
                labels.get("org.opencontainers.image.revision")
                == expected["profile"]["frontend_revision"],
                "ownership_changed",
            )
            check._descriptor(
                row.get("ImageManifestDescriptor"),
                expected["profile"][_manifest_key(role)],
            )
        for name, expected_id in intent["network_ids"].items():
            row = strict_json(
                self.docker.call(["network", "inspect", "--format", "{{json .}}", name])
            )
            require(
                row["Id"] == expected_id
                and row["Labels"].get("com.docker.compose.project") == project
                and set(row["Containers"]) <= set(identifiers),
                "ownership_changed",
            )

    def apply(self, target, intent):
        # The immutable snapshot has no build recipe and names only this
        # already-checked frontend project. Admission closes before BFF changes.
        require(
            target["composition"]
            == _native(
                self.render(target["profile"]), target["profile"], target["nonce"]
            ),
            "composition_changed",
        )
        path = self.state.write("compose.json", target["composition"])
        prefix = [
            "compose",
            "--project-name",
            target["composition"]["name"],
            "-f",
            str(path),
        ]
        self.verify_images(target)
        self.guard(intent)
        self.docker.call([*prefix, "stop", "--timeout", "10", "web-nginx"], timeout=20)
        self.guard(intent)
        self.verify_images(target)
        self.docker.call(
            [
                *prefix,
                "up",
                "--no-build",
                "--pull",
                "never",
                "-d",
                "--wait",
                "--wait-timeout",
                "180",
            ],
            timeout=210,
        )
        self.guard(intent)
        proof = self.checked(target["profile"])
        target["runtime"] = proof["runtime"]
        return target

    def publish(self, profile, allowed):
        require(self.canonical() in allowed, "canonical_changed")
        path = self.config["frontend_check_file"]
        fd = private_io.open_directory(path.parent)
        try:
            private_io.write_json(fd, path.name, profile, replace=True, max_bytes=16384)
        finally:
            os.close(fd)

    def intent(self, value):
        require(
            type(value) is dict
            and set(value)
            == {"schema_version", "before_state", "candidate", "nonce", "network_ids"}
            and type(value["schema_version"]) is int
            and value["schema_version"] == 1,
            "intent_invalid",
        )
        self.saved_state(value["before_state"])
        self.record(value["candidate"])
        stable = check.FIELDS - {
            "frontend_revision",
            "web_platform_manifest",
            "web_nginx_platform_manifest",
        }
        require(
            all(
                value["before_state"]["current"]["profile"][key]
                == value["candidate"]["profile"][key]
                for key in stable
            ),
            "intent_invalid",
        )
        require(
            type(value["nonce"]) is str
            and len(value["nonce"]) == 32
            and all(c in "0123456789abcdef" for c in value["nonce"])
            and value["candidate"]["nonce"] == value["nonce"],
            "intent_invalid",
        )
        names = {
            v["name"]
            for v in value["before_state"]["current"]["composition"][
                "networks"
            ].values()
        }
        require(
            type(value["network_ids"]) is dict
            and set(value["network_ids"]) == names
            and all(
                type(v) is str and check.HEX64.fullmatch(v)
                for v in value["network_ids"].values()
            ),
            "intent_invalid",
        )
        return value

    def restore(self, intent):
        before = copy.deepcopy(intent["before_state"])
        target = before["current"]
        self.guard(intent)
        self.verify_images(target)
        target["nonce"] = intent["nonce"]
        target["composition"] = _native(
            target["composition"], target["profile"], target["nonce"]
        )
        self.apply(target, intent)
        self.publish(
            target["profile"], (target["profile"], intent["candidate"]["profile"])
        )
        self.state.write("state.json", before)
        self.state.remove("intent.json")
        return before

    @contextmanager
    def monitor_lock(self):
        path = self.config["monitor_lock_file"]
        fd = private_io.open_directory(path.parent)
        try:
            with _lock(fd, path.name, wait=140):
                yield
        finally:
            os.close(fd)

    def switch(self, before, candidate):
        with self.monitor_lock():
            require(self.baseline() == before, "current_changed")
            intent = {
                "schema_version": 1,
                "before_state": before,
                "candidate": candidate,
                "nonce": candidate["nonce"],
                "network_ids": self.network_ids(before["current"]),
            }
            self.state.write("intent.json", intent)
            try:
                self.apply(candidate, intent)
                self.publish(candidate["profile"], (before["current"]["profile"],))
                result = {
                    "schema_version": 1,
                    "current": candidate,
                    "previous": before["current"],
                }
                self.state.write("state.json", result)
                self.state.remove("intent.json")
                return result
            except BaseException:
                try:
                    self.restore(intent)
                except BaseException:
                    raise FrontendDeliveryError(
                        "frontend_delivery_recovery_required"
                    ) from None
                raise FrontendDeliveryError(
                    "frontend_delivery_cutover_failed_rolled_back"
                ) from None

    def deploy(self, revision):
        require(
            type(revision) is str and check.HEX40.fullmatch(revision),
            "revision_invalid",
        )
        canonical = self.canonical()
        if canonical["frontend_revision"] == revision:
            saved = self.baseline()
            if self.state.read("state.json") is None:
                self.state.write("state.json", saved)
            return _summary("unchanged", saved)
        from .frontend_artifact import fetch_release, validate_archive

        # A failed provenance check must happen before any Docker command.
        with tempfile.TemporaryDirectory(
            prefix=".download-", dir=self.state.root
        ) as directory:
            destination = Path(directory)
            try:
                manifest = fetch_release(
                    revision,
                    self.config["repository"],
                    destination,
                    self.config["gh_binary"],
                    repository_id=self.config["repository_id"],
                )
            except (ValueError, OSError):
                raise FrontendDeliveryError(
                    "frontend_delivery_artifact_verification_failed"
                ) from None
            require(
                manifest["revision"] == revision
                and type(manifest["conversation_contract_version"]) is int
                and manifest["conversation_contract_version"]
                == CONVERSATION_CONTRACT_VERSION,
                "artifact_invalid",
            )
            before = self.baseline()
            profile = {
                **before["current"]["profile"],
                "frontend_revision": revision,
                **{
                    _manifest_key(role): manifest["images"][role]["platform_manifest"]
                    for role in ROLES
                },
            }
            self.profile(profile)
            for role in ROLES:
                entry = manifest["images"][role]
                validate_archive(destination / entry["archive"], entry, revision, role)
            for role in ROLES:
                entry = manifest["images"][role]
                archive = destination / entry["archive"]
                self.docker.call(["load", "--input", str(archive)], timeout=180)
                check.inspect_frontend_image(
                    self.docker,
                    _locator(revision, role),
                    profile[_manifest_key(role)],
                    revision,
                    role,
                )
            nonce = uuid.uuid4().hex
            candidate = {
                "profile": profile,
                "composition": _native(self.render(profile), profile, nonce),
                "runtime": None,
                "nonce": nonce,
            }
            return _summary("deployed", self.switch(before, candidate))


def operate(config_path, operation, revision=None, *, runner=None):
    require(operation in ("deploy", "rollback", "status"), "operation_invalid")
    config = load_config(config_path)
    state = State(config["state_root"])
    try:
        with state.locked():
            with tempfile.TemporaryDirectory(
                prefix=".docker-", dir=state.root
            ) as directory:
                docker = Docker(
                    directory,
                    runner,
                    deadline=time.monotonic() + 1200,
                    max_commands=128,
                )
                delivery = Delivery(config, state, docker, runner)
                pending = state.read("intent.json")
                if pending is not None:
                    delivery.intent(pending)
                    if operation == "status":
                        return _summary("recovery_required", pending["before_state"])
                    require(operation == "rollback", "recovery_required")
                    with delivery.monitor_lock():
                        restored = delivery.restore(pending)
                    return _summary("recovered", restored)
                if operation == "deploy":
                    return delivery.deploy(revision)
                before = delivery.baseline()
                if operation == "status":
                    return _summary("ready", before)
                require(before["previous"] is not None, "no_previous_release")
                target = copy.deepcopy(before["previous"])
                delivery.verify_images(target)
                target["nonce"] = uuid.uuid4().hex
                target["composition"] = _native(
                    target["composition"], target["profile"], target["nonce"]
                )
                return _summary("rolled_back", delivery.switch(before, target))
    finally:
        state.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="operation", required=True)
    deploy = commands.add_parser("deploy")
    deploy.add_argument("--revision", required=True)
    commands.add_parser("rollback")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        result = operate(args.config, args.operation, getattr(args, "revision", None))
    except KeyboardInterrupt:
        print('{"error":"frontend_delivery_interrupted"}')
        return 130
    except FrontendDeliveryError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        print('{"error":"frontend_delivery_failed"}')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result["status"] == "recovery_required" else 0
