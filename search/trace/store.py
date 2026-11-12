"""Persist immutable bounded trace turns under one private operator root."""

import os
from pathlib import Path
import time
import uuid

from search.operations.shared import private_io
from search.serialization import strict_json
from . import files
from .contract import validate_event
from .limits import TraceLimits


class TraceStore:
    def __init__(self, root: Path, limits=None, *, clock=time.time):
        self.root = Path(root)
        self.limits = limits or TraceLimits()
        self.clock = clock
        self.generation = uuid.uuid4().hex

    def _root(self):
        return files.locked_root(self.root, self.limits.max_sessions)

    def _manifest(self, identifier):
        now = self.clock()
        return {
            "trace_version": 1,
            "test_session_id": identifier,
            "created_at": now,
            "expires_at": now + self.limits.ttl_seconds,
            "turns": [],
            "gaps": [],
            "sealed": False,
            "writer_id": self.generation,
        }

    def _read(self, fd, identifier, *, expired=False):
        manifest = files.read_manifest(fd, identifier, self.limits.max_turns)
        if not expired and manifest["expires_at"] <= self.clock():
            raise ValueError("trace_expired")
        owned = files.inventory(
            fd, self.limits.max_turns, self.limits.max_event_bytes
        )
        if ".unsealed.json" in owned:
            marker = strict_json(
                private_io.read_file(
                    fd,
                    ".unsealed.json",
                    mode=0o600,
                    max_bytes=files.MANIFEST_BYTES,
                )
            )
            if marker != self._marker(manifest):
                raise ValueError("trace_marker_invalid")
        return manifest, owned

    def _marker(self, manifest):
        return {
            key: manifest[key]
            for key in ("trace_version", "test_session_id", "writer_id")
        }

    def _unseal(self, fd, manifest, owned):
        if ".unsealed.json" not in owned:
            private_io.write_json(
                fd,
                ".unsealed.json",
                self._marker(manifest),
                max_bytes=files.MANIFEST_BYTES,
            )

    def _usage(self, root):
        total, count = 0, 0
        for identifier in files.names(root, self.limits.max_sessions + 1):
            if identifier == ".trace.lock":
                continue
            with files.child(root, identifier) as fd:
                _, owned = self._read(fd, identifier, expired=True)
                total += sum(info.st_size for info in owned.values())
            count += 1
        return total, count

    def _ensure(self, root, identifier):
        try:
            with files.child(root, identifier):
                pass
        except FileNotFoundError:
            total, count = self._usage(root)
            if count >= self.limits.max_sessions:
                return "session_limit"
            manifest = self._manifest(identifier)
            size = len(private_io.canonical(manifest))
            size += len(private_io.canonical(self._marker(manifest)))
            if size > self.limits.max_session_bytes:
                return "session_limit"
            if total + size > self.limits.max_total_bytes:
                return "total_limit"
            with files.child(root, identifier, create=True) as fd:
                private_io.write_json(
                    fd,
                    "manifest.json",
                    manifest,
                    max_bytes=files.MANIFEST_BYTES,
                )
                self._unseal(fd, manifest, {})
        return "ready"

    def append(self, value, *, allowed=lambda: True):
        """Publish one event only if its fence is still admitted.

        The worker calls ``allowed`` before entering publication. A concurrent
        withdrawal treats an already-entered publication as in flight and
        removes it before acknowledging completed deletion.
        """
        event = validate_event(value)
        raw = private_io.canonical(event)
        if len(raw) > self.limits.max_event_bytes:
            return "oversize"
        with self._root() as root:
            status = self._ensure(root, event["test_session_id"])
            if status != "ready":
                return status
            return self._append(root, event, raw, allowed)

    def _append(self, root, event, raw, allowed):
        identifier = event["test_session_id"]
        with files.child(root, identifier) as fd:
            manifest, owned = self._read(fd, identifier)
            if manifest["writer_id"] != self.generation:
                return "incomplete"
            if event["turn"] in manifest["turns"]:
                return "incomplete"
            if len(manifest["turns"]) >= self.limits.max_turns:
                return "session_limit"
            updated = dict(
                manifest,
                sealed=False,
                turns=sorted(manifest["turns"] + [event["turn"]]),
            )
            delta = len(raw) + len(private_io.canonical(updated))
            delta -= owned["manifest.json"].st_size
            if ".unsealed.json" not in owned:
                delta += len(private_io.canonical(self._marker(manifest)))
            session_bytes = sum(info.st_size for info in owned.values())
            if session_bytes + delta > self.limits.max_session_bytes:
                return "session_limit"
            total, _ = self._usage(root)
            if total + delta > self.limits.max_total_bytes:
                return "total_limit"
            if not allowed():
                return "revoked"
            self._unseal(fd, manifest, owned)
            private_io.write_file(
                fd,
                f"turn-{event['turn']:03d}.json",
                raw,
                max_bytes=self.limits.max_event_bytes,
            )
            private_io.write_json(
                fd,
                "manifest.json",
                updated,
                replace=True,
                max_bytes=files.MANIFEST_BYTES,
            )
        return "captured"

    def gap(self, identifier, turn, status):
        files.session_id(identifier)
        if status == "revoked":
            return
        with self._root() as root:
            if self._ensure(root, identifier) != "ready":
                raise ValueError("trace_gap_capacity")
            with files.child(root, identifier) as fd:
                manifest, owned = self._read(fd, identifier)
                if manifest["writer_id"] != self.generation:
                    raise ValueError("trace_writer_generation_changed")
                gaps = {row["turn"]: row for row in manifest["gaps"]}
                gaps[min(turn, 100)] = {
                    "turn": min(turn, 100),
                    "status": status,
                }
                updated = dict(
                    manifest,
                    sealed=False,
                    gaps=[gaps[n] for n in sorted(gaps)],
                )
                files._gaps(updated["gaps"])
                delta = len(private_io.canonical(updated))
                delta -= owned["manifest.json"].st_size
                if ".unsealed.json" not in owned:
                    delta += len(private_io.canonical(self._marker(manifest)))
                total, _ = self._usage(root)
                current = sum(info.st_size for info in owned.values())
                if (
                    current + delta > self.limits.max_session_bytes
                    or total + delta > self.limits.max_total_bytes
                ):
                    raise ValueError("trace_gap_capacity")
                self._unseal(fd, manifest, owned)
                private_io.write_json(
                    fd,
                    "manifest.json",
                    updated,
                    replace=True,
                    max_bytes=files.MANIFEST_BYTES,
                )

    def _events(self, fd, identifier, owned):
        events = []
        for name in sorted(owned):
            if name in {"manifest.json", ".unsealed.json"}:
                continue
            raw = private_io.read_file(
                fd, name, mode=0o600, max_bytes=self.limits.max_event_bytes
            )
            event = validate_event(strict_json(raw))
            if (
                event["test_session_id"] != identifier
                or name != f"turn-{event['turn']:03d}.json"
            ):
                raise ValueError("trace_event_identity_invalid")
            events.append(event)
        return events

    def export(self, identifier):
        """Read raw turns, retaining explicit gaps without truncation."""
        files.session_id(identifier)
        with self._root() as root, files.child(root, identifier) as fd:
            manifest, owned = self._read(fd, identifier)
            events = self._events(fd, identifier, owned)
            actual = [event["turn"] for event in events]
            complete = (
                manifest["sealed"]
                and ".unsealed.json" not in owned
                and actual == manifest["turns"]
                and actual == list(range(1, len(actual) + 1))
                and not manifest["gaps"]
                and all(event["capture"]["complete"] for event in events)
            )
            return {
                "trace_version": 1,
                "manifest": manifest,
                "complete": complete,
                "events": events,
            }

    def seal(self, identifier, *, allowed=lambda: True):
        """Seal a drained session owned by this writer generation."""
        with self._root() as root, files.child(root, identifier) as fd:
            manifest, owned = self._read(fd, identifier)
            events = self._events(fd, identifier, owned)
            actual = [event["turn"] for event in events]
            if (
                manifest["writer_id"] != self.generation
                or not actual
                or actual != manifest["turns"]
                or actual != list(range(1, len(actual) + 1))
                or manifest["gaps"]
                or any(not event["capture"]["complete"] for event in events)
            ):
                return False
            if not allowed():
                return False
            private_io.write_json(
                fd,
                "manifest.json",
                dict(manifest, sealed=True),
                replace=True,
                max_bytes=files.MANIFEST_BYTES,
            )
            if not allowed():
                return False
            # This final publication starts only while the caller's fence is
            # admitted. An already-entered unlink cannot be cancelled.
            if ".unsealed.json" in owned:
                os.unlink(".unsealed.json", dir_fd=fd)
                os.fsync(fd)
        return True

    def inspect(self, identifier=None):
        if identifier is not None:
            identifiers = [files.session_id(identifier)]
        else:
            with self._root() as root:
                identifiers = sorted(
                    name
                    for name in files.names(root, self.limits.max_sessions + 1)
                    if name != ".trace.lock"
                )
        rows = []
        for name in identifiers:
            value = self.export(name)
            rows.append(
                {
                    "test_session_id": name,
                    "turns": len(value["events"]),
                    "complete": value["complete"],
                    "expires_at": value["manifest"]["expires_at"],
                    "gaps": value["manifest"]["gaps"],
                }
            )
        return rows

    def delete(self, identifier):
        """Delete only schema-validated records, using inode-pinned unlink."""
        files.session_id(identifier)
        with self._root() as root:
            if identifier not in files.names(
                root, self.limits.max_sessions + 1
            ):
                return False
            with files.child(root, identifier) as fd:
                _, owned = self._read(fd, identifier, expired=True)
                self._events(fd, identifier, owned)
                identity = files._directory_identity(os.fstat(fd))
            files.remove_session(root, identifier, owned, identity)
        return True

    def prune(self):
        with self._root() as root:
            expired = []
            for name in files.names(root, self.limits.max_sessions + 1):
                if name == ".trace.lock":
                    continue
                with files.child(root, name) as fd:
                    manifest, _ = self._read(fd, name, expired=True)
                    if manifest["expires_at"] <= self.clock():
                        expired.append(name)
        return sum(self.delete(identifier) for identifier in expired)
