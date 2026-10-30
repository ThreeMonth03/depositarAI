"""Archive one verified idle job, retaining evidence before exact cleanup."""

import hashlib
import os
from pathlib import Path
from pathlib import PurePosixPath
import stat

from search.operations.shared import private_io
from . import queue, sources
from .pueue import JobError, Pueue, require


MAX_FILE = 8 * 1024**2
MAX_TOTAL = 32 * 1024**2
MAX_FILES = 64
LOCK_REASON = "queued fixed job"
PHASES = ("prepared", "removing_task", "removing_source", "moving")
MANIFEST_FIELDS = {
    "schema_version",
    "job_id",
    "status",
    "request_sha256",
    "result_sha256",
    "files",
    "source_artifact",
    "repository_sha256",
    "source_removed",
    "task_removed",
    "task",
}
INTENT_FIELDS = {
    "schema_version",
    "job_id",
    "request_sha256",
    "files",
    "source",
    "source_artifact",
    "task_id",
    "cancelled",
    "active",
    "phase",
    "locks",
    "task",
}


def _exists(path):
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False


def _inventory(directory, *, source=False):
    found, total = {}, 0

    def walk(path, prefix, depth):
        nonlocal total
        require(depth <= 4, "archive_evidence_limit")
        fd = private_io.open_directory(path)
        try:
            for name in sorted(os.listdir(fd)):
                relative = prefix + name
                require(len(relative) <= 256, "archive_evidence_limit")
                if not prefix and name == "archive.json":
                    continue
                if not prefix and name == "source" and source:
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    before = len(found)
                    walk(path / name, relative + "/", depth + 1)
                    require(len(found) > before, "archive_unknown_directory")
                else:
                    raw = private_io.read_file(fd, name, mode=0o600, max_bytes=MAX_FILE)
                    total += len(raw)
                    require(
                        total <= MAX_TOTAL and len(found) < MAX_FILES,
                        "archive_evidence_limit",
                    )
                    found[relative] = {
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
            private_io.verify_directory(fd, path)
        finally:
            os.close(fd)

    walk(Path(directory), "", 0)
    return found


def _evidence_matches(directory, expected, *, source=False):
    require(
        type(expected) is dict
        and 3 <= len(expected) <= MAX_FILES
        and {"request.json", "queue.json", "result.json"} <= set(expected),
        "archive_evidence_invalid",
    )
    for name, record in expected.items():
        require(
            type(name) is str
            and 1 <= len(name) <= 256
            and not PurePosixPath(name).is_absolute()
            and PurePosixPath(name).as_posix() == name
            and not {".", "..", "source", "archive.json"}.intersection(
                PurePosixPath(name).parts
            )
            and type(record) is dict
            and set(record) == {"bytes", "sha256"}
            and type(record["bytes"]) is int
            and 1 <= record["bytes"] <= MAX_FILE
            and type(record["sha256"]) is str
            and queue.SHA256.fullmatch(record["sha256"]),
            "archive_evidence_invalid",
        )
    require(
        _inventory(directory, source=source) == expected, "archive_evidence_changed"
    )


def _idle(registry, *, absent=None):
    state = queue.queue_state(registry)
    for task_id, row in state["tasks"].items():
        require(
            set(row["status"]) == {"Done"}
            and type(row["status"]["Done"]) is dict
            and row["status"]["Done"].get("result") == "Success",
            "archive_queue_not_idle",
        )
        identifier = row["label"].removeprefix("depositarai-")
        require(
            queue.result(registry, identifier)["status"]
            in queue.TERMINAL - {"cancelled"},
            "archive_peer_unverified",
        )
    if absent is not None:
        require(str(absent) not in state["tasks"], "archive_task_not_removed")
    return state


def _active(registry, *, cancelled):
    path = Path(registry["root"]) / "active.json"
    if not _exists(path):
        require(cancelled, "archive_active_evidence_missing")
        return None
    active = queue.read(path)
    require(
        type(active) is dict
        and set(active) == {"job_id", "status"}
        and type(active["job_id"]) is str
        and queue.JOB_ID.fullmatch(active["job_id"])
        and active["status"] == "clean",
        "archive_recovery_required",
    )
    require(
        queue.result(registry, active["job_id"])["status"]
        in queue.TERMINAL - {"cancelled"},
        "archive_active_unverified",
    )
    return active


def _checkpoint(registry, intent, *, absent=None):
    require(_locks(registry) == intent["locks"], "archive_lock_changed")
    state = _idle(registry, absent=absent)
    require(
        _active(registry, cancelled=intent["cancelled"]) == intent["active"],
        "archive_active_changed",
    )
    require(_locks(registry) == intent["locks"], "archive_lock_changed")
    return state


def _locks(registry):
    proof = {}
    fd = private_io.open_directory(Path(registry["root"]))
    try:
        for name in ("submissions.lock", "execution.lock"):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1,
                "archive_lock_changed",
            )
            proof[name] = list(private_io.identity(info))
    finally:
        os.close(fd)
    return proof


def _git(registry, *arguments):
    result = queue.command(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            registry["operator_source"],
            *arguments,
        ],
        timeout=60,
    )
    require(result.returncode == 0, "archive_git_failed")
    return result.stdout


def _registration(registry, frozen):
    blocks = _git(registry, "worktree", "list", "--porcelain", "-z").split(b"\0\0")
    matching = [
        block.split(b"\0")
        for block in blocks
        if block.startswith(b"worktree " + os.fsencode(frozen) + b"\0")
    ]
    require(len(matching) <= 1, "archive_source_registration_changed")
    return set(matching[0]) - {b""} if matching else None


def _source_removed(registry, frozen):
    require(
        not _exists(frozen) and _registration(registry, frozen) is None,
        "archive_source_not_removed",
    )


def _source(registry, directory, request, *, expected=None, unlocked=False):
    frozen = directory / "source"
    fd = private_io.open_directory(frozen)
    try:
        info = os.fstat(fd)
        require(
            str(frozen) != request["source"]
            and queue.path(request["source"]) != frozen,
            "archive_source_invalid",
        )
        require(queue.revision(frozen) == request["revision"], "archive_source_changed")
        common = queue.common_directory(frozen)
        require(
            common == queue.common_directory(registry["operator_source"]),
            "archive_source_repository_changed",
        )
        clean = queue.command(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                frozen,
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignored",
            ]
        )
        require(clean.returncode == 0 and not clean.stdout, "archive_source_not_empty")
        rows = _registration(registry, frozen)
        fixed = {
            b"worktree " + os.fsencode(frozen),
            b"HEAD " + request["revision"].encode(),
            b"detached",
        }
        require(
            rows == fixed | {b"locked " + LOCK_REASON.encode()}
            or unlocked
            and rows == fixed,
            "archive_source_registration_changed",
        )
        proof = {
            "revision": request["revision"],
            "inode": [info.st_dev, info.st_ino],
            "repository_sha256": queue.digest(str(common)),
        }
        require(expected is None or expected == proof, "archive_source_changed")
        private_io.verify_directory(fd, frozen)
        return proof, b"locked " + LOCK_REASON.encode() in rows
    finally:
        os.close(fd)


def _artifact_directory(registry, revision, *, create=False):
    require(
        type(revision) is str and queue.REVISION.fullmatch(revision),
        "archive_source_invalid",
    )
    parent = Path(registry["root"]) / "archive-sources"
    os.close(private_io.open_directory(parent, create=create))
    return parent / revision


def _task_proof(registry, identifier, queued, task, *, cancelled):
    if cancelled:
        require(task is None, "archive_task_unverified")
        return
    require(
        type(task) is dict
        and task.get("group") == "heavy"
        and type(task.get("status")) is dict
        and set(task["status"]) == {"Done"}
        and type(task["status"]["Done"]) is dict
        and task["status"]["Done"].get("result") == "Success",
        "archive_task_unverified",
    )
    queue.task(
        registry,
        identifier,
        {"tasks": {str(queued["task_id"]): task}},
        queued["task_id"],
    )


def _verify_archived(registry, identifier, directory):
    """Verify committed archive bytes without calling queue lookup recursively."""
    require(
        type(identifier) is str and queue.JOB_ID.fullmatch(identifier), "id_invalid"
    )
    directory = queue.path(str(directory))
    require(
        directory == Path(registry["root"]) / "archive" / identifier,
        "archive_path_invalid",
    )
    record = queue.read(directory / "archive.json")
    require(
        type(record) is dict
        and set(record) == MANIFEST_FIELDS
        and type(record["schema_version"]) is int
        and record["schema_version"] == 1
        and record["job_id"] == identifier
        and record["status"] == "archived"
        and record["source_removed"] is True
        and record["task_removed"] is True,
        "archive_manifest_invalid",
    )
    require(not _exists(directory / "source"), "archive_evidence_changed")
    _source_removed(registry, Path(registry["root"]) / "jobs" / identifier / "source")
    _evidence_matches(directory, record["files"])
    request, result = (
        queue.read(directory / "request.json"),
        queue.read(directory / "result.json"),
    )
    require(
        type(request) is dict
        and type(result) is dict
        and request.get("job_id") == identifier
        and type(request.get("revision")) is str
        and queue.REVISION.fullmatch(request["revision"])
        and record["request_sha256"] == queue.digest(request)
        and record["result_sha256"] == record["files"]["result.json"]["sha256"],
        "archive_manifest_invalid",
    )
    require(
        result.get("job_id") == identifier and result.get("status") in queue.TERMINAL,
        "archive_result_invalid",
    )
    queued = queue.read(directory / "queue.json")
    _task_proof(
        registry,
        identifier,
        queued,
        record["task"],
        cancelled=result["status"] == "cancelled",
    )
    require(
        record["repository_sha256"]
        == queue.digest(str(queue.common_directory(registry["operator_source"]))),
        "archive_source_repository_changed",
    )
    slot = _artifact_directory(registry, request["revision"])
    require(
        sources.verify_bundle(registry["operator_source"], slot, request["revision"])
        == record["source_artifact"],
        "archive_source_artifact_changed",
    )
    return record


def verify_archived(registry, identifier, directory):
    """Check archived bytes with closed errors and no recursive queue lookup."""
    try:
        return _verify_archived(registry, identifier, directory)
    except JobError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise JobError("job_archive_manifest_invalid") from None


def _clear_intent(registry, intent):
    root = Path(registry["root"])
    require(
        queue.read(root / "archive-intent.json") == intent, "archive_intent_changed"
    )
    require(_locks(registry) == intent["locks"], "archive_lock_changed")
    fd = private_io.open_directory(root)
    try:
        os.unlink("archive-intent.json", dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def _summary(identifier):
    return {"job_id": identifier, "status": "archived"}


def _archive(registry, identifier, locks):
    root = Path(registry["root"])
    source, target = root / "jobs" / identifier, root / "archive" / identifier
    intent_file = root / "archive-intent.json"
    require(_locks(registry) == locks, "archive_lock_changed")
    intent = queue.read(intent_file) if _exists(intent_file) else None
    if intent is not None:
        require(
            type(intent) is dict
            and set(intent) == INTENT_FIELDS
            and type(intent["schema_version"]) is int
            and intent["schema_version"] == 1
            and intent.get("job_id") == identifier
            and intent.get("phase") in PHASES
            and type(intent["task_id"]) is int
            and intent["task_id"] >= 0
            and type(intent["cancelled"]) is bool,
            "archive_recovery_required",
        )
        require(intent["locks"] == locks, "archive_lock_changed")
    require(not (_exists(source) and _exists(target)), "archive_duplicate_job")
    if _exists(target):
        manifest = verify_archived(registry, identifier, target)
        if intent is not None:
            require(
                intent["phase"] == "moving"
                and intent["files"] == manifest["files"]
                and intent["request_sha256"] == manifest["request_sha256"]
                and intent["source_artifact"] == manifest["source_artifact"]
                and intent["task"] == manifest["task"]
                and intent["source"]["repository_sha256"]
                == manifest["repository_sha256"],
                "archive_recovery_required",
            )
            _checkpoint(registry, intent, absent=intent["task_id"])
            _clear_intent(registry, intent)
        return _summary(identifier)

    directory, request, queued = queue.inputs(registry, identifier)
    require(directory == source, "archive_path_invalid")
    require(
        intent is None
        or intent["phase"] == "moving"
        or not _exists(directory / "archive.json"),
        "archive_recovery_required",
    )
    result = queue.result(registry, identifier)
    require(result["status"] in queue.TERMINAL, "archive_not_terminal")
    files = queue.verified_evidence(registry, identifier)
    _evidence_matches(directory, files, source=True)
    cancelled = result["status"] == "cancelled"
    if cancelled:
        require(
            set(queue.read(directory / "result.json"))
            == {"job_id", "status", "request_sha256"}
            and set(files)
            <= {
                "request.json",
                "queue.json",
                "result.json",
                "native-config.json",
                "quality-cases.json",
            },
            "archive_cancel_unverified",
        )
    active = _active(registry, cancelled=cancelled)
    state = _idle(registry)
    task_id = queued["task_id"]
    if intent is None:
        require(not _exists(directory / "archive.json"), "archive_recovery_required")
        require(
            (str(task_id) not in state["tasks"]) == cancelled, "archive_task_unverified"
        )
        os.close(private_io.open_directory(root / "archive", create=True))
        _artifact_directory(registry, request["revision"], create=True)
        proof, _ = _source(registry, directory, request)
        intent = {
            "schema_version": 1,
            "job_id": identifier,
            "request_sha256": queue.digest(request),
            "files": files,
            "source": proof,
            "source_artifact": None,
            "task_id": task_id,
            "cancelled": cancelled,
            "active": active,
            "phase": "prepared",
            "locks": locks,
            "task": state["tasks"].get(str(task_id)),
        }
        queue.write(intent_file, intent)
    else:
        require(
            intent["request_sha256"] == queue.digest(request)
            and intent["files"] == files
            and intent["task_id"] == task_id
            and intent["cancelled"] is cancelled
            and intent["active"] == active,
            "archive_intent_changed",
        )
    _task_proof(registry, identifier, queued, intent["task"], cancelled=cancelled)

    if intent["phase"] == "prepared":
        state = _checkpoint(registry, intent)
        require(
            (str(task_id) not in state["tasks"]) == cancelled, "archive_task_unverified"
        )
        _source(registry, directory, request, expected=intent["source"])
        intent["source_artifact"] = sources.preserve_bundle(
            directory / "source",
            _artifact_directory(registry, request["revision"], create=True),
            request["revision"],
        )
        state = _checkpoint(registry, intent)
        require(
            (str(task_id) not in state["tasks"]) == cancelled, "archive_task_unverified"
        )
        intent["phase"] = "removing_task"
        queue.write(intent_file, intent, replace=True)
    else:
        require(
            sources.verify_bundle(
                registry["operator_source"],
                _artifact_directory(registry, request["revision"]),
                request["revision"],
            )
            == intent["source_artifact"],
            "archive_source_artifact_changed",
        )
    if intent["phase"] == "removing_task":
        state = _checkpoint(registry, intent)
        if str(task_id) in state["tasks"]:
            require(not cancelled, "archive_cancel_unverified")
            require(
                state["tasks"][str(task_id)] == intent["task"], "archive_task_changed"
            )
            Pueue(registry).call("remove", str(task_id))
        _checkpoint(registry, intent, absent=task_id)
        intent["phase"] = "removing_source"
        queue.write(intent_file, intent, replace=True)
    if intent["phase"] == "removing_source":
        _checkpoint(registry, intent, absent=task_id)
        if _exists(directory / "source"):
            _, locked = _source(
                registry, directory, request, expected=intent["source"], unlocked=True
            )
            if locked:
                _checkpoint(registry, intent, absent=task_id)
                _git(registry, "worktree", "unlock", str(directory / "source"))
            _source(
                registry, directory, request, expected=intent["source"], unlocked=True
            )
            _checkpoint(registry, intent, absent=task_id)
            _git(registry, "worktree", "remove", str(directory / "source"))
        _source_removed(registry, directory / "source")
        intent["phase"] = "moving"
        queue.write(intent_file, intent, replace=True)
    _source_removed(registry, directory / "source")
    _evidence_matches(directory, files)
    _checkpoint(registry, intent, absent=task_id)
    manifest = {
        "schema_version": 1,
        "job_id": identifier,
        "status": "archived",
        "request_sha256": intent["request_sha256"],
        "result_sha256": files["result.json"]["sha256"],
        "files": files,
        "source_artifact": intent["source_artifact"],
        "repository_sha256": intent["source"]["repository_sha256"],
        "source_removed": True,
        "task_removed": True,
        "task": intent["task"],
    }
    if _exists(directory / "archive.json"):
        require(
            queue.read(directory / "archive.json") == manifest,
            "archive_manifest_changed",
        )
    else:
        queue.write(directory / "archive.json", manifest)
    archive_root = root / "archive"
    archive_fd = private_io.open_directory(archive_root, create=True)
    jobs_fd = private_io.open_directory(root / "jobs")
    try:
        require(not _exists(target), "archive_duplicate_job")
        os.rename(identifier, identifier, src_dir_fd=jobs_fd, dst_dir_fd=archive_fd)
        os.fsync(jobs_fd)
        os.fsync(archive_fd)
    finally:
        os.close(jobs_fd)
        os.close(archive_fd)
    verify_archived(registry, identifier, target)
    _checkpoint(registry, intent, absent=task_id)
    _clear_intent(registry, intent)
    return _summary(identifier)


def archive(registry, identifier):
    """Archive only an idle verified job; leave uncertain steps for reconciliation."""
    require(
        type(identifier) is str and queue.JOB_ID.fullmatch(identifier), "id_invalid"
    )
    root = Path(registry["root"])
    with (
        queue.locked(root, "submissions.lock") as submitted,
        queue.locked(root, "execution.lock") as executed,
    ):
        try:
            require(
                type(submitted) is tuple and type(executed) is tuple,
                "archive_lock_proof_invalid",
            )
            locks = {
                "submissions.lock": list(submitted),
                "execution.lock": list(executed),
            }
            return _archive(registry, identifier, locks)
        except (Exception, KeyboardInterrupt) as error:
            if _exists(root / "archive-intent.json"):
                raise JobError("job_archive_recovery_required") from None
            if isinstance(error, JobError):
                raise
            raise JobError("job_archive_invalid") from None
