"""Bounded vector scheduling, verification and recovery reports."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)

from .admin import (
    AdminAccess,
    AdminSession,
    TOKEN_CLEANUP_SECONDS,
    TOKEN_EXPIRY_MARGIN_SECONDS,
    VECTOR_TOKEN_LIFETIME_MINUTES,
)
from .bundle import CatalogBundle
from .codec import _exact_int
from .errors import CatalogError, ConfigurationError, ProtocolError, VectorBuildError
from .inventory import InventorySnapshot, _search_list_rows
from .journal import VectorJournal, _vector_event
from .policy import ID_RE, NONCE_RE, SHA256_RE
from .session import FessSession
from .transport import Deadline

VECTOR_JOB_NAME = "Content Chunk Vector Indexer"


VECTOR_SCRIPT_TYPE = "groovy"


VECTOR_SCRIPT_DATA = (
    'return container.getComponent("chunkVectorJob").logLevel("info")'
    '.sessionId("CHUNK").execute(executor);'
)


VECTOR_SCRIPT_RESULT = "Session Id: CHUNK"


MAX_VECTOR_DEADLINE_SECONDS = 1800.0


DEFAULT_VECTOR_PASS_DEADLINE_SECONDS = 1200.0


MAX_VECTOR_PASS_DOCUMENTS = 256


VECTOR_REGISTRATION_SECONDS = 60.0


VECTOR_RECOVERY_SECONDS = 240.0


VECTOR_START_QUIESCENCE_SECONDS = 60.0


VECTOR_VERIFICATION_SECONDS = 60.0


MAX_VECTOR_TOKEN_API_SECONDS = (
    VECTOR_TOKEN_LIFETIME_MINUTES * 60
    - TOKEN_CLEANUP_SECONDS
    - TOKEN_EXPIRY_MARGIN_SECONDS
)


if (
    MAX_VECTOR_DEADLINE_SECONDS + VECTOR_RECOVERY_SECONDS > MAX_VECTOR_TOKEN_API_SECONDS
    or VECTOR_START_QUIESCENCE_SECONDS + VECTOR_VERIFICATION_SECONDS
    > VECTOR_RECOVERY_SECONDS
):
    raise RuntimeError(
        "vector deadline constants exceed the ephemeral-token safety budget"
    )


@dataclass(frozen=True)
class VectorProgressSnapshot:
    """A canonical, exhaustive partition of the exact document inventory.

    Counts are derived from document identities, never sufficient on their own
    to authorize continuing an interrupted vector build.
    """

    inventory: InventorySnapshot
    done_entries: Tuple[Tuple[str, str, str], ...]
    pending_entries: Tuple[Tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.inventory, InventorySnapshot)
            or type(self.done_entries) is not tuple
            or type(self.pending_entries) is not tuple
        ):
            raise ProtocolError("vector progress adapter returned an unsupported shape")
        InventorySnapshot(self.done_entries)
        InventorySnapshot(self.pending_entries)
        done, pending = set(self.done_entries), set(self.pending_entries)
        if done & pending or tuple(sorted(done | pending)) != self.inventory.entries:
            raise ProtocolError("vector progress does not partition its inventory")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "document_count": self.inventory.count,
            "done_entries": [list(row) for row in self.done_entries],
            "pending_entries": [list(row) for row in self.pending_entries],
        }

    @property
    def sha256(self) -> str:
        encoded = (
            json.dumps(
                self.as_dict(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        return hashlib.sha256(encoded).hexdigest()


class VectorProgressAdapter(Protocol):
    def snapshot(
        self, session: FessSession, token: str, deadline: Deadline
    ) -> VectorProgressSnapshot: ...


def _vector_options(
    deadline_seconds: float, poll_seconds: float, nonce: Optional[str]
) -> str:
    for value, maximum, error in (
        (poll_seconds, 10, "vector poll interval is outside its cap"),
        (
            deadline_seconds,
            MAX_VECTOR_DEADLINE_SECONDS,
            "vector deadline is outside its token-safe cap",
        ),
    ):
        valid = False
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            try:
                valid = math.isfinite(float(value)) and 0 < float(value) <= maximum
            except (OverflowError, ValueError):
                pass
        if not valid:
            raise ConfigurationError(error)
    nonce = nonce or secrets.token_hex(6)
    if not isinstance(nonce, str) or NONCE_RE.fullmatch(nonce) is None:
        raise ConfigurationError(
            "import token nonce must be 12 lowercase hex characters"
        )
    return nonce


class SearchListVectorProgress:
    """Use only pinned status values; fail/skipped/unknown are not pending."""

    def snapshot(
        self, session: FessSession, token: str, deadline: Deadline
    ) -> VectorProgressSnapshot:
        rows = _search_list_rows(session, token, deadline)
        if any(status not in (None, "done") for *_entry, status in rows):
            raise ProtocolError("vector progress contains a non-resumable status")
        return VectorProgressSnapshot(
            inventory=InventorySnapshot(tuple(row[:3] for row in rows)),
            done_entries=tuple(row[:3] for row in rows if row[3] == "done"),
            pending_entries=tuple(row[:3] for row in rows if row[3] is None),
        )


@dataclass(frozen=True)
class VectorPassReport:
    """Helper proof only; it never grants host cleanup or promotion rights."""

    status: str
    run_id: str
    recipe_sha256: str
    bundle_id: str
    manifest_sha256: str
    document_count: int
    before_done_count: int
    done_count: int
    pending_count: int
    progress_count: int
    before_state_sha256: str
    after_state_sha256: str
    job_id: Optional[str]
    scheduler_id: str
    elapsed_seconds: float
    no_op: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 2,
            "status": self.status,
            "run_id": self.run_id,
            "recipe_sha256": self.recipe_sha256,
            "bundle_id": self.bundle_id,
            "manifest_sha256": self.manifest_sha256,
            "document_count": self.document_count,
            "before_done_count": self.before_done_count,
            "done_count": self.done_count,
            "pending_count": self.pending_count,
            "progress_count": self.progress_count,
            "before_state_sha256": self.before_state_sha256,
            "after_state_sha256": self.after_state_sha256,
            "job_name": VECTOR_JOB_NAME if not self.no_op else None,
            "job_id": self.job_id,
            "scheduler_id": self.scheduler_id,
            "elapsed_seconds": self.elapsed_seconds,
            "scheduler_restored_disabled": True,
            "job_terminal_verified": True,
            "token_cleanup_verified": True,
            "progress_verified": True,
            "no_op": self.no_op,
            "generation_enabled": False,
        }


class VectorBuilder:
    """Run and verify bounded vector jobs with durable recovery evidence."""

    def __init__(
        self,
        bundle: CatalogBundle,
        access: AdminAccess,
        *,
        vector_progress: Optional[VectorProgressAdapter] = None,
    ) -> None:
        self.bundle = bundle
        self.access = access
        self.vector_progress = vector_progress or SearchListVectorProgress()

    @staticmethod
    def _vector_scheduler(response: Mapping[str, Any]) -> Mapping[str, Any]:
        settings = response.get("settings")
        total = response.get("total")
        if (
            not isinstance(settings, list)
            or len(settings) > 100
            or not _exact_int(total, minimum=0, maximum=100)
            or total != len(settings)
        ):
            raise VectorBuildError("scheduler response shape is invalid")
        seen = set()
        for candidate in settings:
            identifier = candidate.get("id") if isinstance(candidate, Mapping) else None
            if (
                not isinstance(identifier, str)
                or ID_RE.fullmatch(identifier) is None
                or identifier in seen
            ):
                raise VectorBuildError(
                    "scheduler listing contains an invalid identifier"
                )
            seen.add(identifier)
        matches = [
            row
            for row in settings
            if isinstance(row, Mapping) and row.get("name") == VECTOR_JOB_NAME
        ]
        if len(matches) != 1:
            raise VectorBuildError("vector scheduler is not uniquely configured")
        row = matches[0]
        if (
            not isinstance(row.get("id"), str)
            or ID_RE.fullmatch(row["id"]) is None
            or row.get("available") != "false"
            or row.get("cron_expression") not in (None, "")
            or row.get("running") is not False
            or row.get("target") != "all"
            or row.get("script_type") != VECTOR_SCRIPT_TYPE
            or row.get("script_data") != VECTOR_SCRIPT_DATA
            or row.get("crawler") != "false"
            or row.get("job_logging") != "true"
            or row.get("sort_order") != 14
            or not _exact_int(row.get("version_no"), minimum=-1)
        ):
            raise VectorBuildError("vector scheduler is not disabled and stopped")
        return row

    @staticmethod
    def _scheduler_detail(
        response: Mapping[str, Any],
        scheduler_id: str,
        *,
        expected_available: str,
    ) -> Mapping[str, Any]:
        setting = response.get("setting")
        if not isinstance(setting, Mapping):
            raise VectorBuildError("scheduler detail response shape is invalid")
        required = {
            "id",
            "version_no",
            "name",
            "target",
            "script_type",
            "script_data",
            "crawler",
            "job_logging",
            "available",
            "sort_order",
            "running",
        }
        if not required.issubset(setting) or any(
            key not in required | {"cron_expression"} for key in setting
        ):
            raise VectorBuildError("scheduler detail fields changed")
        if (
            setting.get("id") != scheduler_id
            or not _exact_int(setting.get("version_no"), minimum=-1)
            or setting.get("name") != VECTOR_JOB_NAME
            or setting.get("target") != "all"
            or setting.get("cron_expression") not in (None, "")
            or setting.get("script_type") != VECTOR_SCRIPT_TYPE
            or setting.get("script_data") != VECTOR_SCRIPT_DATA
            or setting.get("crawler") != "false"
            or setting.get("job_logging") != "true"
            or setting.get("available") != expected_available
            or setting.get("sort_order") != 14
            or not isinstance(setting.get("running"), bool)
        ):
            raise VectorBuildError(
                "scheduler detail does not match the sealed vector job"
            )
        return setting

    @staticmethod
    def _scheduler_update_body(
        setting: Mapping[str, Any], available: str
    ) -> Dict[str, Any]:
        if available not in ("true", "false"):
            raise ConfigurationError("internal scheduler state is invalid")
        body = {
            "id": setting["id"],
            "version_no": setting["version_no"],
            "name": setting["name"],
            "target": setting["target"],
            "cron_expression": "",
            "script_type": setting["script_type"],
            "script_data": setting["script_data"],
            "crawler": setting["crawler"],
            "job_logging": setting["job_logging"],
            "available": available,
            "sort_order": setting["sort_order"],
        }
        return body

    def _set_vector_scheduler_available(
        self,
        session: AdminSession,
        token: str,
        scheduler_id: str,
        available: str,
        deadline: Deadline,
    ) -> Mapping[str, Any]:
        current_expected = "false" if available == "true" else "true"
        current = self._scheduler_detail(
            session.scheduler_setting(token, scheduler_id, deadline),
            scheduler_id,
            expected_available=current_expected,
        )
        response = session.update_scheduler(
            token,
            self._scheduler_update_body(current, available),
            deadline,
        )
        if (
            set(response) != {"status", "version", "id", "created"}
            or response.get("status") != 0
            or response.get("id") != scheduler_id
            or response.get("created") is not False
            or not isinstance(response.get("version"), str)
            or not 1 <= len(response["version"]) <= 100
        ):
            raise VectorBuildError("scheduler update response is invalid")
        return self._scheduler_detail(
            session.scheduler_setting(token, scheduler_id, deadline),
            scheduler_id,
            expected_available=available,
        )

    @staticmethod
    def _job_rows(
        response: Mapping[str, Any], expected_job_id: str
    ) -> List[Mapping[str, Any]]:
        logs = response.get("logs")
        total = response.get("total")
        if (
            not isinstance(logs, list)
            or len(logs) > 1
            or not _exact_int(total, minimum=0, maximum=1)
            or total != len(logs)
        ):
            raise VectorBuildError("job-log response shape is invalid")
        result: List[Mapping[str, Any]] = []
        seen = set()
        for row in logs:
            identifier = row.get("id") if isinstance(row, Mapping) else None
            if (
                not isinstance(identifier, str)
                or ID_RE.fullmatch(identifier) is None
                or identifier != expected_job_id
                or identifier in seen
            ):
                raise VectorBuildError("job-log identifier is invalid")
            seen.add(identifier)
            result.append(row)
        return result

    @staticmethod
    def _terminal_vector_job(row: Mapping[str, Any], job_id: str) -> bool:
        if row.get("id") != job_id:
            return False
        status_value = row.get("job_status")
        if status_value == "running":
            return False
        if (
            row.get("job_name") != VECTOR_JOB_NAME
            or status_value not in ("ok", "fail")
            or row.get("end_time") in (None, "")
            or row.get("script_type") != VECTOR_SCRIPT_TYPE
            or row.get("script_data") != VECTOR_SCRIPT_DATA
            or row.get("target") != "all"
        ):
            raise VectorBuildError("vector job failed or its immutable fields changed")
        return True

    @classmethod
    def _completed_vector_job(cls, row: Mapping[str, Any], job_id: str) -> bool:
        if not cls._terminal_vector_job(row, job_id):
            return False
        if row.get("job_status") != "ok":
            raise VectorBuildError("vector job failed or its immutable fields changed")
        script_result = row.get("script_result")
        if (
            not isinstance(script_result, str)
            or len(script_result) > 4096
            or script_result.strip() != VECTOR_SCRIPT_RESULT
        ):
            raise VectorBuildError("vector job did not return the exact success marker")
        return True

    def _restore_vector_scheduler(
        self,
        session: AdminSession,
        token: str,
        scheduler_id: str,
        poll_seconds: float,
        *,
        recovery_deadline: Deadline,
        require_quiescence: bool = False,
        stop_job_id: Optional[str] = None,
        journal: Optional[VectorJournal] = None,
    ) -> bool:
        recovery = recovery_deadline
        try:
            response = session.scheduler_setting(token, scheduler_id, recovery)
            setting = response.get("setting")
            if not isinstance(setting, Mapping):
                raise VectorBuildError("scheduler recovery detail is invalid")
            availability = setting.get("available")
            if availability not in ("true", "false"):
                raise VectorBuildError("scheduler recovery state is invalid")
            checked = self._scheduler_detail(
                response,
                scheduler_id,
                expected_available=availability,
            )
            # Disable first.  A launchNow() call may have been accepted but not
            # reached isExecutingNow when its HTTP response was lost.  Waiting
            # for one false running sample before disabling cannot distinguish
            # that queued launch from genuine quiescence.
            if checked["available"] == "true":
                self._set_vector_scheduler_available(
                    session, token, scheduler_id, "false", recovery
                )

            if stop_job_id is not None:
                # stopNow is non-idempotent. Dispatch it once, only after the
                # start response bound this scheduler to an exact job-log ID.
                # A lost stop response is resolved from state, never by retry.
                evidence_ok = True
                try:
                    _vector_event(
                        journal,
                        "scheduler-stop-intent",
                        {"scheduler_id": scheduler_id, "job_id": stop_job_id},
                    )
                except CatalogError:
                    # The durable start intent also obliges bounded recovery.
                    # Continue emergency cleanup, but never report it as clean.
                    evidence_ok = False
                try:
                    session.stop_scheduler(token, scheduler_id, recovery)
                except CatalogError:
                    pass

                while True:
                    terminal = False
                    try:
                        rows = self._job_rows(
                            session.job_logs(token, stop_job_id, recovery),
                            stop_job_id,
                        )
                        if len(rows) == 1:
                            terminal = self._terminal_vector_job(rows[0], stop_job_id)
                    except CatalogError:
                        terminal = False
                    try:
                        response = session.scheduler_setting(
                            token, scheduler_id, recovery
                        )
                        checked = self._scheduler_detail(
                            response,
                            scheduler_id,
                            expected_available="false",
                        )
                        stopped = checked["running"] is False
                    except CatalogError:
                        stopped = False
                    if terminal and stopped:
                        return evidence_ok
                    self.access.sleep(recovery.remaining(float(poll_seconds)))

            quiet_since: Optional[float] = None
            while True:
                response = session.scheduler_setting(token, scheduler_id, recovery)
                checked = self._scheduler_detail(
                    response,
                    scheduler_id,
                    expected_available="false",
                )
                if checked["running"] is True:
                    quiet_since = None
                elif not require_quiescence:
                    return True
                elif quiet_since is None:
                    quiet_since = self.access.clock()
                else:
                    quiet_remaining = VECTOR_START_QUIESCENCE_SECONDS - (
                        self.access.clock() - quiet_since
                    )
                    if quiet_remaining <= 0:
                        return True
                wait = float(poll_seconds)
                if quiet_since is not None:
                    wait = min(
                        wait,
                        max(
                            0.001,
                            VECTOR_START_QUIESCENCE_SECONDS
                            - (self.access.clock() - quiet_since),
                        ),
                    )
                self.access.sleep(recovery.remaining(wait))
        except CatalogError:
            return False

    def _run_vector_job(
        self,
        session: AdminSession,
        token: str,
        deadline: Deadline,
        token_api_deadline: Deadline,
        poll_seconds: float,
        *,
        journal: Optional[VectorJournal] = None,
        scheduler_id: Optional[str] = None,
    ) -> Tuple[Mapping[str, Any], str]:
        """One non-retried start and bounded recovery, shared by both contracts."""

        if scheduler_id is None:
            scheduler = self._vector_scheduler(
                session.scheduler_settings(token, deadline)
            )
            scheduler_id = str(scheduler["id"])
        checked = self._scheduler_detail(
            session.scheduler_setting(token, scheduler_id, deadline),
            scheduler_id,
            expected_available="false",
        )
        if checked["running"] is not False:
            raise VectorBuildError("vector scheduler is not disabled and stopped")
        declared_job_id: Optional[str] = None
        completed: Optional[Mapping[str, Any]] = None
        failure: Optional[CatalogError] = None
        scheduler_may_be_enabled = False
        start_dispatched = False
        start_response_confirmed = False
        try:
            _vector_event(
                journal, "scheduler-enable-intent", {"scheduler_id": scheduler_id}
            )
            scheduler_may_be_enabled = True
            enabled = self._set_vector_scheduler_available(
                session, token, scheduler_id, "true", deadline
            )
            if enabled["running"] is not False:
                raise VectorBuildError(
                    "vector scheduler became active before explicit start"
                )
            _vector_event(journal, "scheduler-enabled", {"scheduler_id": scheduler_id})
            # The asynchronous Fess monitor registers DB-enabled schedules.
            # Never retry a start, including a generic status-9 response.
            self.access.sleep(deadline.remaining(VECTOR_REGISTRATION_SECONDS))
            _vector_event(journal, "job-start-intent", {"scheduler_id": scheduler_id})
            start_dispatched = True
            start_response = session.start_scheduler(token, scheduler_id, deadline)
            if start_response.get("status") != 0:
                raise VectorBuildError("vector scheduler start failed")
            raw_declared_job_id = start_response.get("job_log_id")
            if (
                not isinstance(raw_declared_job_id, str)
                or ID_RE.fullmatch(raw_declared_job_id) is None
            ):
                raise VectorBuildError(
                    "vector scheduler returned an invalid new job ID"
                )
            declared_job_id = raw_declared_job_id
            start_response_confirmed = True
            _vector_event(
                journal,
                "job-started",
                {"scheduler_id": scheduler_id, "job_id": declared_job_id},
            )
            while True:
                deadline.remaining(self.access.request_timeout)
                rows = self._job_rows(
                    session.job_logs(token, declared_job_id, deadline), declared_job_id
                )
                if rows and self._completed_vector_job(rows[0], declared_job_id):
                    completed = rows[0]
                    _vector_event(
                        journal,
                        "job-completed",
                        {"scheduler_id": scheduler_id, "job_id": declared_job_id},
                    )
                    break
                self.access.sleep(float(poll_seconds))
        except CatalogError as exc:
            failure = exc
        recovery_deadline = Deadline(
            min(token_api_deadline.end, self.access.clock() + VECTOR_RECOVERY_SECONDS),
            self.access.clock,
        )
        evidence_ok = True
        if scheduler_may_be_enabled:
            try:
                _vector_event(
                    journal,
                    "scheduler-restore-intent",
                    {"scheduler_id": scheduler_id, "job_id": declared_job_id},
                )
            except CatalogError:
                evidence_ok = False
            scheduler_safe = self._restore_vector_scheduler(
                session,
                token,
                scheduler_id,
                float(poll_seconds),
                recovery_deadline=recovery_deadline,
                require_quiescence=start_dispatched and not start_response_confirmed,
                stop_job_id=(
                    declared_job_id
                    if failure is not None and start_response_confirmed
                    else None
                ),
                journal=journal,
            )
        else:
            scheduler_safe = True
        if start_dispatched and not start_response_confirmed:
            # Quiescence is not proof of termination without an exact job ID.
            scheduler_safe = False
        if scheduler_safe and scheduler_may_be_enabled:
            try:
                _vector_event(
                    journal,
                    "scheduler-restored",
                    {"scheduler_id": scheduler_id, "job_id": declared_job_id},
                )
            except CatalogError:
                evidence_ok = False
        if (
            failure is not None
            or completed is None
            or not scheduler_safe
            or not evidence_ok
        ):
            raise VectorBuildError(
                "vector build failed or scheduler isolation could not be re-established",
                possibly_polluted=not scheduler_safe,
            ) from None
        return completed, scheduler_id

    def _progress_snapshot(
        self, session: AdminSession, token: str, deadline: Deadline
    ) -> VectorProgressSnapshot:
        state = self.vector_progress.snapshot(session, token, deadline)
        if (
            not isinstance(state, VectorProgressSnapshot)
            or state.inventory.entries != self.bundle.expected_inventory_entries
        ):
            raise VectorBuildError(
                "vector progress requires the exact sealed inventory"
            )
        return state

    @staticmethod
    def _verify_vector_progress(
        before: VectorProgressSnapshot, after: VectorProgressSnapshot, *, no_op: bool
    ) -> int:
        before_done, after_done = set(before.done_entries), set(after.done_entries)
        before_pending, after_pending = (
            set(before.pending_entries),
            set(after.pending_entries),
        )
        progress = len(after_done - before_done)
        if (
            before.inventory != after.inventory
            or not before_done <= after_done
            or not after_pending <= before_pending
            or progress > MAX_VECTOR_PASS_DOCUMENTS
            or (no_op and (before != after or before.pending_entries))
            or (not no_op and progress == 0)
        ):
            raise VectorBuildError(
                "vector progress is non-monotonic, empty or exceeds its cap"
            )
        return progress

    def build_vector_pass(
        self,
        admin_username: str,
        admin_password: str,
        *,
        journal: VectorJournal,
        run_id: str,
        recipe_sha256: str,
        expected_before_state_sha256: Optional[str] = None,
        nonce: Optional[str] = None,
        deadline_seconds: float = DEFAULT_VECTOR_PASS_DEADLINE_SECONDS,
        poll_seconds: float = 1.0,
    ) -> VectorPassReport:
        """One bounded production pass, never an automatic timeout retry.

        Only a successful exact job with monotonic document progress can return
        partial-clean. A timeout, ambiguous start or missing durable evidence
        raises; the host must quarantine it. The report proves only helper-side
        cleanup and is not authorization for host resume or active switching.
        """

        nonce = _vector_options(deadline_seconds, poll_seconds, nonce)
        if (
            journal is None
            or not callable(getattr(journal, "append", None))
            or not isinstance(run_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
            or not isinstance(recipe_sha256, str)
            or SHA256_RE.fullmatch(recipe_sha256) is None
            or (
                expected_before_state_sha256 is not None
                and (
                    not isinstance(expected_before_state_sha256, str)
                    or SHA256_RE.fullmatch(expected_before_state_sha256) is None
                )
            )
        ):
            raise ConfigurationError("vector pass identity or evidence sink is invalid")
        expected_identity = {
            "schema_version": 1,
            "run_id": run_id,
            "bundle_id": self.bundle.bundle_id,
            "manifest_sha256": self.bundle.manifest_sha256,
            "recipe_sha256": recipe_sha256,
        }
        if getattr(journal, "identity", None) != expected_identity:
            raise ConfigurationError(
                "vector pass evidence identity differs from its request"
            )
        started = self.access.clock()
        deadline = Deadline.after(deadline_seconds, self.access.clock)
        token_api_deadline = Deadline(
            started + MAX_VECTOR_TOKEN_API_SECONDS, self.access.clock
        )
        _vector_event(
            journal,
            "pass-started",
            {
                "deadline_seconds": float(deadline_seconds),
                "poll_seconds": float(poll_seconds),
            },
        )
        with self.access.token(
            admin_username,
            admin_password,
            nonce,
            deadline,
            lifetime_minutes=VECTOR_TOKEN_LIFETIME_MINUTES,
            journal=journal,
        ) as values:
            session, token = values
            scheduler = self._vector_scheduler(
                session.scheduler_settings(token, deadline)
            )
            scheduler_id = str(scheduler["id"])
            checked = self._scheduler_detail(
                session.scheduler_setting(token, scheduler_id, deadline),
                scheduler_id,
                expected_available="false",
            )
            if checked["running"] is not False:
                raise VectorBuildError("vector scheduler is not disabled and stopped")
            _vector_event(
                journal,
                "scheduler-verified",
                {"scheduler_id": scheduler_id, "available": "false", "running": False},
            )
            before = self._progress_snapshot(session, token, deadline)
            _vector_event(journal, "before-state", before.as_dict())
            if (
                expected_before_state_sha256 is not None
                and before.sha256 != expected_before_state_sha256
            ):
                raise VectorBuildError(
                    "vector progress differs from the resume checkpoint"
                )
            no_op = not before.pending_entries
            job_id = None
            if not no_op:
                completed, scheduler_id = self._run_vector_job(
                    session,
                    token,
                    deadline,
                    token_api_deadline,
                    float(poll_seconds),
                    journal=journal,
                    scheduler_id=scheduler_id,
                )
                job_id = str(completed["id"])
            else:
                checked = self._scheduler_detail(
                    session.scheduler_setting(token, scheduler_id, deadline),
                    scheduler_id,
                    expected_available="false",
                )
                if checked["running"] is not False:
                    raise VectorBuildError(
                        "vector scheduler changed during no-op verification"
                    )
                _vector_event(
                    journal,
                    "scheduler-restored",
                    {"scheduler_id": scheduler_id, "job_id": None},
                )
            verification = Deadline(
                min(
                    token_api_deadline.end,
                    self.access.clock() + VECTOR_VERIFICATION_SECONDS,
                ),
                self.access.clock,
            )
            after = self._progress_snapshot(session, token, verification)
            repeated = self._progress_snapshot(session, token, verification)
            if after != repeated:
                raise VectorBuildError(
                    "post-vector progress changed between proof reads"
                )
            progress = self._verify_vector_progress(before, after, no_op=no_op)
            _vector_event(journal, "after-state", after.as_dict())
        # Exiting the context has already proved token revocation and logout.
        report = VectorPassReport(
            status="partial-clean" if after.pending_entries else "complete-clean",
            run_id=run_id,
            recipe_sha256=recipe_sha256,
            bundle_id=self.bundle.bundle_id,
            manifest_sha256=self.bundle.manifest_sha256,
            document_count=after.inventory.count,
            before_done_count=len(before.done_entries),
            done_count=len(after.done_entries),
            pending_count=len(after.pending_entries),
            progress_count=progress,
            before_state_sha256=before.sha256,
            after_state_sha256=after.sha256,
            job_id=job_id,
            scheduler_id=scheduler_id,
            elapsed_seconds=self.access.clock() - started,
            no_op=no_op,
        )
        _vector_event(journal, "pass-result", report.as_dict())
        return report
