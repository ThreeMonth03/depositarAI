"""Freeze trusted resource passes and retain evidence across queue failures.

Pueue schedules processes; existing profiling locks and receipts remain the
authority for actual work. Uncertain execution leaves a durable intent that
blocks subsequent jobs, including after a daemon crash.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import time
import uuid

from search.operations.shared import private_io
from search.serialization import strict_json

from . import execution, profiles
from .pueue import ENVIRONMENT, JobError, MAX_OUTPUT, Pueue, command, require
from .sources import restore_modes


ROOT = Path(__file__).resolve().parents[3]
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
NAME = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
MAX_RECORD = 64 * 1024
TERMINAL = frozenset({"completed", "partial", "checkpoint", "cancelled"})


def registry_path():
    """Use the same account-level registry from every branch and worktree."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".config/depositarai/jobs.json"


def host_identity():
    return hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def queue_identity(registry):
    """Seal the shared execution plane; each request seals its own profile."""
    return digest({key: value for key, value in registry.items() if key != "profiles"})


def no_archive_intent(root):
    try:
        (root / "archive-intent.json").lstat()
    except FileNotFoundError:
        return
    raise JobError("job_archive_recovery_required")


def path(value):
    require(
        type(value) is str
        and value.isascii()
        and all(ord(character) >= 32 for character in value),
        "path_invalid",
    )
    result = Path(value)
    require(result.is_absolute() and result.resolve() == result, "path_invalid")
    return result


def read(filename, *, raw=False, maximum=MAX_RECORD):
    filename = path(str(filename))
    fd = private_io.open_directory(filename.parent)
    try:
        encoded = private_io.read_file(fd, filename.name, mode=0o600, max_bytes=maximum)
        private_io.verify_directory(fd, filename.parent)
        return encoded if raw else strict_json(encoded)
    finally:
        os.close(fd)


def write(filename, value, *, replace=False):
    filename = Path(filename)
    fd = private_io.open_directory(filename.parent)
    try:
        private_io.verify_directory(fd, filename.parent)
        private_io.write_json(
            fd, filename.name, value, replace=replace, max_bytes=MAX_RECORD
        )
        private_io.verify_directory(fd, filename.parent)
    finally:
        os.close(fd)


def revision(source):
    prefix = ["git", "-c", "core.hooksPath=/dev/null", "-C", path(str(source))]
    head = command([*prefix, "rev-parse", "HEAD"])
    clean = command([*prefix, "status", "--porcelain", "--untracked-files=all"])
    require(
        head.returncode == clean.returncode == 0
        and REVISION.fullmatch(head.stdout.decode("ascii").strip())
        and not clean.stdout,
        "source_dirty_or_invalid",
    )
    return head.stdout.decode("ascii").strip()


def common_directory(source):
    result = command(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            source,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ]
    )
    require(result.returncode == 0, "source_invalid")
    return path(result.stdout.decode("ascii").strip())


def load_registry(filename=None):
    value = read(registry_path() if filename is None else filename)
    require(
        type(value) is dict
        and set(value)
        == {
            "schema_version",
            "root",
            "runtime_root",
            "pueue",
            "operator_source",
            "operator_revision",
            "machine_id_sha256",
            "profiles",
        }
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and type(value["operator_revision"]) is str
        and REVISION.fullmatch(value["operator_revision"])
        and type(value["machine_id_sha256"]) is str
        and SHA256.fullmatch(value["machine_id_sha256"]),
        "registry_invalid",
    )
    require(
        host_identity() == value["machine_id_sha256"],
        "host_changed",
    )
    for name in ("root", "runtime_root"):
        os.close(private_io.open_directory(path(value[name])))
    root = path(value["root"])
    require(
        not any((item / ".git").exists() for item in (root, *root.parents)),
        "root_inside_git",
    )
    path(value["pueue"])
    source = path(value["operator_source"])
    require(revision(source) == value["operator_revision"], "operator_changed")
    definitions = value["profiles"]
    require(
        type(definitions) is dict and 1 <= len(definitions) <= 16, "profiles_invalid"
    )
    for name, profile in definitions.items():
        require(
            type(name) is str and NAME.fullmatch(name),
            "profile_invalid",
        )
        profiles.validate(profile, root)
    return value


@contextmanager
def locked(root, name):
    """Retain a nofollow global lock; durable active intent handles process death."""
    parent = private_io.open_directory(root)
    fd = None
    try:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.geteuid()
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) == 0o600,
            "lock_unsafe",
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JobError("job_busy") from None
        require(
            private_io.identity(before)
            == private_io.identity(os.stat(name, dir_fd=parent, follow_symlinks=False)),
            "lock_changed",
        )
        private_io.verify_directory(parent, root)
        yield private_io.identity(os.fstat(fd))
        private_io.verify_directory(parent, root)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def job_root(registry, identifier):
    require(type(identifier) is str and JOB_ID.fullmatch(identifier), "id_invalid")
    locations = []
    for name in ("jobs", "archive"):
        parent = Path(registry["root"]) / name
        try:
            fd = private_io.open_directory(parent)
        except FileNotFoundError:
            continue
        try:
            try:
                os.stat(identifier, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            directory = parent / identifier
            os.close(private_io.open_directory(directory))
            private_io.verify_directory(fd, parent)
            locations.append((name, directory))
        finally:
            os.close(fd)
    if not locations:
        raise FileNotFoundError("job_not_found")
    require(len(locations) == 1, "archive_duplicate_job")
    name, directory = locations[0]
    if name == "archive":
        from .archive import verify_archived

        verify_archived(registry, identifier, directory)
    return directory


def launcher(registry, identifier):
    return [
        "/usr/bin/python3",
        "-I",
        "-S",
        "-B",
        str(Path(registry["operator_source"]) / "scripts/search_jobs.py"),
        "execute",
        "--job",
        identifier,
    ]


def task(registry, identifier, state, task_id):
    value = state["tasks"].get(str(task_id))
    require(
        type(value) is dict
        and value.get("label") == "depositarai-" + identifier
        and value.get("command") == shlex.join(launcher(registry, identifier))
        and value.get("path") == registry["root"],
        "task_identity_changed",
    )
    require(
        value.get("envs")
        in (
            ENVIRONMENT,
            {**ENVIRONMENT, "PUEUE_GROUP": "heavy", "PUEUE_WORKER_ID": "0"},
        ),
        "task_environment_changed",
    )
    return next(iter(value["status"]))


def queue_state(registry):
    state = Pueue(registry).state()
    for task_id, row in state["tasks"].items():
        label = row.get("label")
        require(
            type(label) is str and label.startswith("depositarai-"),
            "queue_peer_invalid",
        )
        identifier = label.removeprefix("depositarai-")
        _, _, queued = inputs(registry, identifier)
        require(str(queued["task_id"]) == task_id, "task_identity_changed")
        task(registry, identifier, state, queued["task_id"])
    return state


def submit(registry, specification):
    require(
        type(specification) is dict
        and set(specification)
        == {"schema_version", "issue", "profile", "source", "revision"}
        and type(specification["schema_version"]) is int
        and specification["schema_version"] == 1
        and type(specification["issue"]) is int
        and 1 <= specification["issue"] <= 10**9
        and type(specification["profile"]) is str
        and specification["profile"] in registry["profiles"]
        and type(specification["revision"]) is str
        and REVISION.fullmatch(specification["revision"]),
        "request_invalid",
    )
    source = path(specification["source"])
    require(revision(source) == specification["revision"], "source_changed")
    require(
        common_directory(source) == common_directory(registry["operator_source"]),
        "source_repository_changed",
    )
    profile = registry["profiles"][specification["profile"]]
    profiles.verify_source(profile, specification["revision"])
    frozen_inputs = profiles.load_inputs(profile)
    root = Path(registry["root"])
    require(
        root != source and root not in source.parents and source not in root.parents,
        "source_overlaps_queue",
    )
    transport = Pueue(registry)
    with locked(root, "submissions.lock"):
        no_archive_intent(root)
        queue_state(registry)
        jobs = root / "jobs"
        os.close(private_io.open_directory(jobs, create=True))
        require(len(list(jobs.iterdir())) < 128, "retention_limit")
        identifier = uuid.uuid4().hex
        destination = jobs / identifier
        os.close(private_io.open_directory(destination, create=True))
        frozen = destination / "source"
        result = command(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                source,
                "worktree",
                "add",
                "--detach",
                "--lock",
                "--reason",
                "queued fixed job",
                frozen,
                specification["revision"],
            ],
            timeout=60,
        )
        require(
            result.returncode == 0 and revision(frozen) == specification["revision"],
            "source_freeze_failed",
        )
        tree = command(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                frozen,
                "ls-tree",
                "-rz",
                "--full-tree",
                specification["revision"],
            ]
        )
        require(tree.returncode == 0, "source_tree_invalid")
        restore_modes(frozen, tree.stdout)
        require(revision(frozen) == specification["revision"], "source_changed")
        profiles.freeze(destination, profile, frozen_inputs)
        request = {
            **specification,
            "job_id": identifier,
            "created_at_ns": time.time_ns(),
            "queue_identity_sha256": queue_identity(registry),
            "profile_definition": registry["profiles"][specification["profile"]],
        }
        write(destination / "request.json", request)
        encoded = transport.call(
            "add",
            "--stashed",
            "--group",
            "heavy",
            "--print-task-id",
            "--working-directory",
            str(root),
            "--label",
            "depositarai-" + identifier,
            "--",
            shlex.join(launcher(registry, identifier)),
        )
        require(
            re.fullmatch(rb"[0-9]+\s*", encoded) is not None,
            "queue_submission_uncertain",
        )
        task_id = int(encoded)
        write(
            destination / "queue.json",
            {"task_id": task_id, "request_sha256": digest(request)},
        )
        transport.seal_environment(task_id)
        transport.call("enqueue", str(task_id))
    return {
        "job_id": identifier,
        "status": "queued",
        "source_revision": specification["revision"],
    }


def inputs(registry, identifier):
    root = job_root(registry, identifier)
    request, queued = read(root / "request.json"), read(root / "queue.json")
    require(
        type(request) is dict
        and set(request)
        == {
            "schema_version",
            "issue",
            "profile",
            "source",
            "revision",
            "job_id",
            "created_at_ns",
            "queue_identity_sha256",
            "profile_definition",
        }
        and request.get("job_id") == identifier
        and type(request.get("schema_version")) is int
        and request["schema_version"] == 1
        and type(request.get("issue")) is int
        and 1 <= request["issue"] <= 10**9
        and type(request.get("revision")) is str
        and REVISION.fullmatch(request["revision"])
        and type(request.get("profile")) is str
        and NAME.fullmatch(request["profile"])
        and type(request.get("source")) is str
        and request["source"].isascii()
        and all(32 <= ord(character) < 127 for character in request["source"])
        and Path(request["source"]).is_absolute()
        and ".." not in Path(request["source"]).parts
        and Path(request["source"]).as_posix() == request["source"]
        and type(request.get("created_at_ns")) is int
        and request["created_at_ns"] > 0
        and request.get("queue_identity_sha256") == queue_identity(registry)
        and type(queued) is dict
        and set(queued) == {"task_id", "request_sha256"}
        and type(queued["task_id"]) is int
        and queued["task_id"] >= 0
        and queued["request_sha256"] == digest(request),
        "request_changed",
    )
    profiles.validate(request["profile_definition"], Path(registry["root"]))
    profiles.verify_source(request["profile_definition"], request["revision"])
    profiles.read_frozen(root, request["profile_definition"])
    return root, request, queued


def status(registry, identifier, *, state=None):
    root, request, queued = inputs(registry, identifier)
    try:
        result = read(root / "result.json")
    except FileNotFoundError:
        state = queue_state(registry) if state is None else state
        phase = task(registry, identifier, state, queued["task_id"])
        return {
            "job_id": identifier,
            "status": {
                "Stashed": "stashed",
                "Queued": "queued",
                "Running": "running",
                "Paused": "interrupted",
                "Done": "recovery_required",
            }.get(phase, "recovery_required"),
            "queue_paused": state["groups"]["heavy"]["status"] == "Paused",
        }
    require(
        type(result) is dict
        and result.get("job_id") == identifier
        and result.get("request_sha256") == digest(request)
        and result.get("status") in TERMINAL | {"blocked", "recovery_required"}
        and set(result)
        <= {
            "job_id",
            "status",
            "request_sha256",
            "operation_sha256",
            "source_revision",
            "artifacts",
            "log_status",
            "error",
        },
        "result_invalid",
    )
    projected = {
        "job_id": identifier,
        "status": result["status"],
        "request_sha256": digest(request),
        "source_revision": request["revision"],
    }
    if result["status"] in {"completed", "partial", "checkpoint"}:
        value = read(root / "operation.json")
        require(
            result.get("operation_sha256") == digest(value), "operation_result_changed"
        )
        projected["operation_sha256"] = digest(value)
        try:
            active = read(Path(registry["root"]) / "active.json")
        except FileNotFoundError:
            raise JobError("job_active_evidence_missing") from None
        if active == {"job_id": identifier, "status": "running"}:
            projected["status"] = "recovery_required"
    return projected


def result(registry, identifier):
    projected = status(registry, identifier)
    root, request, _ = inputs(registry, identifier)
    try:
        receipt = read(root / "result.json")
    except FileNotFoundError:
        return projected
    artifacts = receipt.get("artifacts")
    if artifacts is not None:
        require(
            type(artifacts) is dict and set(artifacts) == {"stdout.bin", "stderr.bin"},
            "artifacts_invalid",
        )
        for name, metadata in artifacts.items():
            require(
                type(metadata) is dict
                and set(metadata) == {"bytes", "sha256"}
                and type(metadata["bytes"]) is int
                and 0 <= metadata["bytes"] <= MAX_OUTPUT,
                "artifacts_invalid",
            )
            encoded = _artifact(root, name, metadata["bytes"])
            require(
                len(encoded) == metadata["bytes"]
                and hashlib.sha256(encoded).hexdigest() == metadata["sha256"],
                "artifact_changed",
            )
    if receipt["status"] in {"completed", "partial", "checkpoint"}:
        require(
            set(receipt)
            == {
                "job_id",
                "status",
                "request_sha256",
                "operation_sha256",
                "source_revision",
                "artifacts",
                "log_status",
            }
            and receipt["source_revision"] == request["revision"]
            and receipt["log_status"] == "complete"
            and artifacts is not None,
            "result_invalid",
        )
        value = read(root / "operation.json")
        require(
            execution.verify(
                value,
                execution.expected_code(value, request["profile_definition"]),
                root,
                request,
            )
            == receipt["status"],
            "operation_result_unverified",
        )
    if receipt["status"] == "cancelled":
        require(
            set(receipt) == {"job_id", "status", "request_sha256"}, "result_invalid"
        )
        for name in ("operation.json", "stdout.bin", "stderr.bin", "capture"):
            _artifact(root, name, 0)
    return projected


def verified_evidence(registry, identifier):
    """Return the bounded metadata inventory of a fully verified terminal job."""
    outcome = result(registry, identifier)
    require(outcome["status"] in TERMINAL, "result_unverified")
    root, request, _ = inputs(registry, identifier)
    receipt = read(root / "result.json")
    names = {name: MAX_RECORD for name in ("request.json", "queue.json", "result.json")}
    names.update(profiles.frozen_names(request["profile_definition"]))
    if receipt["status"] != "cancelled":
        names["operation.json"] = MAX_RECORD
        names.update(execution.native_names(request["profile_definition"]))
        names.update(
            {
                name: MAX_OUTPUT
                for name, entry in receipt["artifacts"].items()
                if entry["bytes"]
            }
        )
    inventory, total = {}, 0
    for name, maximum in names.items():
        raw = read(root / name, raw=True, maximum=maximum)
        total += len(raw)
        require(total <= execution.MAX_EVIDENCE_BYTES, "evidence_too_large")
        inventory[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return inventory


def _artifact(root, name, size):
    if size:
        return read(root / name, raw=True, maximum=MAX_OUTPUT)
    fd = private_io.open_directory(root)
    try:
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return b""
        raise JobError("job_artifact_changed")
    finally:
        os.close(fd)


def statuses(registry):
    directory = Path(registry["root"]) / "jobs"
    os.close(private_io.open_directory(directory))
    identifiers = sorted(item.name for item in directory.iterdir())
    require(
        len(identifiers) <= 128 and all(JOB_ID.fullmatch(item) for item in identifiers),
        "jobs_invalid",
    )
    state = queue_state(registry)
    return {
        "jobs": [
            status(registry, identifier, state=state) for identifier in identifiers
        ]
    }


def cancel(registry, identifier):
    with locked(Path(registry["root"]), "execution.lock"):
        root, request, queued = inputs(registry, identifier)
        require(not (root / "result.json").exists(), "not_queued")
        transport = Pueue(registry)
        require(
            task(registry, identifier, queue_state(registry), queued["task_id"])
            in {"Queued", "Stashed"},
            "not_queued",
        )
        transport.call("remove", str(queued["task_id"]))
        require(
            str(queued["task_id"]) not in queue_state(registry)["tasks"],
            "cancel_uncertain",
        )
        result = {
            "job_id": identifier,
            "status": "cancelled",
            "request_sha256": digest(request),
        }
        write(root / "result.json", result)
        return result


def _capture(root, completed):
    artifacts = {}
    fd = private_io.open_directory(root)
    try:
        for name, encoded in (
            ("stdout.bin", completed.stdout),
            ("stderr.bin", completed.stderr),
        ):
            if encoded:
                private_io.write_file(fd, name, encoded, max_bytes=MAX_OUTPUT)
            else:
                _artifact(root, name, 0)
            artifacts[name] = {
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
    finally:
        os.close(fd)
    return artifacts


def execute(registry, identifier):
    root, request, queued = inputs(registry, identifier)
    queue_root = Path(registry["root"])
    require(not (root / "result.json").exists(), "already_finished")
    started = False
    result = {
        "job_id": identifier,
        "request_sha256": digest(request),
        "status": "blocked",
        "log_status": "unavailable",
    }
    try:
        with locked(queue_root, "execution.lock"):
            no_archive_intent(queue_root)
            require(ROOT == Path(registry["operator_source"]), "operator_changed")
            require(
                task(registry, identifier, queue_state(registry), queued["task_id"])
                == "Running",
                "not_running",
            )
            active = queue_root / "active.json"
            try:
                previous = read(active)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                require(
                    type(previous) is dict
                    and set(previous) == {"job_id", "status"}
                    and JOB_ID.fullmatch(previous["job_id"])
                    and previous["status"] == "clean",
                    "recovery_required",
                )
            require(revision(root / "source") == request["revision"], "source_changed")
            require(
                common_directory(root / "source")
                == common_directory(registry["operator_source"]),
                "source_repository_changed",
            )
            profiles.read_frozen(root, request["profile_definition"])
            if request["profile_definition"]["type"] == "resource-pass":
                profiles.verify_resource_plan(request["profile_definition"])
            write(
                active,
                {"job_id": identifier, "status": "running"},
                replace=previous is not None,
            )
            started = True
            profile = request["profile_definition"]
            completed = command(
                execution.argv(root, request),
                timeout=execution.timeout(profile),
                cwd=root / "source",
            )
            result["artifacts"] = _capture(root, completed)
            result["log_status"] = "complete"
            value = strict_json(completed.stdout)
            write(root / "operation.json", value)
            normalized = execution.verify(value, completed.returncode, root, request)
            require(revision(root / "source") == request["revision"], "source_changed")
            result.update(
                status=normalized,
                operation_sha256=digest(value),
                source_revision=request["revision"],
            )
            write(root / "result.json", result)
            write(active, {"job_id": identifier, "status": "clean"}, replace=True)
            return result
    except (Exception, KeyboardInterrupt) as error:
        result["status"] = "recovery_required" if started else "blocked"
        result["error"] = (
            str(error)
            if isinstance(error, JobError)
            else "job_interrupted"
            if isinstance(error, KeyboardInterrupt)
            else "job_failed"
        )
        if not (root / "result.json").exists():
            write(root / "result.json", result)
        if (
            not started
            and isinstance(error, JobError)
            and str(error) == "job_archive_recovery_required"
        ):
            raise error from None
        raise JobError("job_recovery_required" if started else "job_blocked") from None
