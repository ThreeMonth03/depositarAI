"""Sealed catalog fixtures and scripted Fess IO; no test-suite imports."""

from search.catalog.admin import ADMIN_PERMISSION
from search.catalog.bundle import CatalogBundle, MANIFEST_NAME
from search.catalog.codec import _json_wire_copy
from search.catalog.errors import CatalogTimeout
from search.catalog.transport import HTTPResponse

import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
from search.catalog.build import builder

# Fixed Fess scheduler protocol expected by the scripted server.
VECTOR_JOB_NAME = "Content Chunk Vector Indexer"
VECTOR_SCRIPT_TYPE = "groovy"
VECTOR_SCRIPT_DATA = (
    'return container.getComponent("chunkVectorJob").logLevel("info")'
    '.sessionId("CHUNK").execute(executor);'
)
VECTOR_SCRIPT_RESULT = "Session Id: CHUNK"

PASSWORD = "admin-password-must-not-leak"

TOKEN = "temporary-token-must-not-leak-0123456789"

REMOTE_SECRET = "remote-error-body-must-not-leak"

NONCE = "0123456789ab"


def _document_sha256(document):
    canonical = {
        key: _json_wire_copy(value)
        for key, value in document.items()
        if key not in {"depositar_bundle_id", "depositar_document_sha256"}
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _different_sha256(value):
    return ("0" if value[0] != "0" else "1") + value[1:]


def _record(number, name):
    return {
        "private": False,
        "state": "active",
        "type": "dataset",
        "id": "00000000-0000-4000-8000-%012d" % number,
        "name": name,
        "title": "第 %d 筆環境資料" % number,
        "notes": "雨量、溫度與觀測站 metadata",
        "organization": {"id": "org", "name": "lab", "title": "研究室"},
        "tags": [{"name": "氣候"}],
        "groups": [{"name": "環境"}],
        "license_id": "cc-by",
        "license_title": "CC BY",
        "metadata_created": "2026-01-01T00:00:00",
        "metadata_modified": "2026-01-02T00:00:00",
        "resources": [
            {
                "id": "resource-%d" % number,
                "name": "測站 CSV",
                "description": "逐時觀測",
                "format": "CSV",
                "mimetype": "text/csv",
            }
        ],
    }


class BuiltBundle:
    def __init__(self, count=2):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        source = base / "source.jsonl"
        rows = [_record(index, "資料 集/%d" % index) for index in range(1, count + 1)]
        source.write_bytes(
            b"".join(
                json.dumps(
                    row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
                for row in rows
            )
        )
        os.chmod(source, 0o600)
        output = base / "bundles"
        output.mkdir(mode=0o700)
        result = builder.build_bundle(source, output)
        self.path = Path(result["bundle_path"])
        manifest = self.path / MANIFEST_NAME
        self.manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.bundle = CatalogBundle.load(
            self.path, expected_manifest_sha256=self.manifest_sha256
        )

    def reseal(self, *, artifact_mutations=None, manifest_mutation=None):
        """Create a cryptographically self-consistent but semantically tampered bundle."""

        artifact_mutations = artifact_mutations or {}
        os.chmod(self.path, 0o700)
        manifest_path = self.path / MANIFEST_NAME
        os.chmod(manifest_path, 0o600)
        manifest = json.loads(manifest_path.read_bytes())
        for name, mutation in artifact_mutations.items():
            artifact_path = self.path / name
            with gzip.open(artifact_path, "rt", encoding="utf-8") as stream:
                rows = [json.loads(line) for line in stream]
            mutation(rows)
            expanded = b"".join(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
                for row in rows
            )
            os.chmod(artifact_path, 0o600)
            with artifact_path.open("wb") as raw:
                with gzip.GzipFile(
                    filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0
                ) as compressed:
                    compressed.write(expanded)
            os.chmod(artifact_path, 0o400)
            compressed_bytes = artifact_path.read_bytes()
            descriptor = manifest["artifacts"][name]
            descriptor["sha256"] = hashlib.sha256(compressed_bytes).hexdigest()
            descriptor["bytes"] = len(compressed_bytes)
            descriptor["expanded_bytes"] = len(expanded)
            descriptor["records"] = len(rows)
        if manifest_mutation is not None:
            manifest_mutation(manifest)
        core = dict(manifest)
        core.pop("bundle_id")
        core_bytes = json.dumps(
            core,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest["bundle_id"] = hashlib.sha256(core_bytes).hexdigest()
        manifest_bytes = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        manifest_path.write_bytes(manifest_bytes)
        os.chmod(manifest_path, 0o400)
        destination = self.path.parent / ("sha256-" + manifest["bundle_id"])
        self.path.rename(destination)
        self.path = destination
        os.chmod(self.path, 0o500)
        self.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    def close(self):
        os.chmod(self.path, 0o700)
        for child in self.path.iterdir():
            if not child.is_symlink():
                os.chmod(child, 0o600)
        self.temporary.cleanup()


def _response(request, value, status=200, headers=()):
    body = (
        value
        if isinstance(value, bytes)
        else json.dumps(
            {"response": value}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    return HTTPResponse(status, tuple(headers), body, request.url)


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.token_active = False
        self.token_name = None
        self.token_expiry_override = None
        self.created_token_expiries = []
        self.documents = {}
        self.bulk_mode = "success"
        self.tamper_document_on_bulk = False
        self.revoke_fails = False
        self.scheduler_started = False
        self.job_log_created = False
        self.vector_job_running = False
        self.scheduler_available = False
        self.scheduler_version = -1
        self.start_failures_remaining = 0
        self.start_commits_then_times_out = False
        self.ambiguous_start_pending = False
        self.ambiguous_detail_checks = 0
        self.ambiguous_running_observed = False
        self.vector_job_status = "ok"
        self.vector_script_result = VECTOR_SCRIPT_RESULT
        self.stop_mode = "success"
        self.stop_calls = 0
        self.settings_calls = 0
        self.scheduler_updates = []
        self.token_listing_pages = None

    @staticmethod
    def _authorization(request):
        return request.header("Authorization")

    def _require_token(self, request):
        if self._authorization(request) != "Bearer " + TOKEN or not self.token_active:
            return False
        if request.header("Cookie") is not None:
            raise AssertionError("bearer API request also carried an admin cookie")
        return True

    def send(self, request, *, timeout, max_bytes):
        self.requests.append(request)
        parsed = urllib.parse.urlsplit(request.url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path.startswith("/admin/") and request.header("Accept-Language") != "en":
            raise AssertionError("admin HTML request did not pin the English locale")
        if path == "/api/v2/auth/login":
            payload = json.loads(request.body)
            if payload["password"] != PASSWORD:
                return _response(request, {"status": 3})
            return _response(
                request,
                {
                    "status": 0,
                    "user": {"admin": payload["username"] == "admin"},
                    "csrf_token": "c" * 43,
                },
                headers=(("Set-Cookie", "JSESSIONID=" + "s" * 32 + "; HttpOnly"),),
            )
        if path == "/api/v2/auth/logout":
            if (
                request.header("Cookie") is None
                or request.header("X-Fess-CSRF-Token") is None
            ):
                raise AssertionError("logout was not session authenticated")
            return _response(request, {"status": 0})
        if path == "/admin/accesstoken/createnew/":
            return _response(
                request,
                b'<form method="post" action="/admin/accesstoken/">'
                b'<input type="hidden" name="crudMode" value="1">'
                b'<input type="hidden" name="csrfNonce" value="safe"></form>',
            )
        if path.startswith("/admin/accesstoken/list/") and request.method == "GET":
            page_number = int(path.rsplit("/", 1)[1])
            if self.token_listing_pages is None:
                pages = [
                    [] if not self.token_active else [(self.token_name, "token-id")]
                ]
            else:
                pages = self.token_listing_pages
            rows = b""
            if 1 <= page_number <= len(pages):
                for name, token_id in pages[page_number - 1]:
                    rows += (
                        '<tr data-href="/admin/accesstoken/details/4/%s"><td>%s</td></tr>'
                        % (token_id, name)
                    ).encode("ascii")
            next_link = b""
            if page_number < len(pages):
                next_link = (
                    '<a href="/admin/accesstoken/list/%d">next</a>' % (page_number + 1)
                ).encode("ascii")
            return _response(request, b"<table>" + rows + b"</table>" + next_link)
        if path == "/admin/accesstoken/" and request.method == "POST":
            values = urllib.parse.parse_qs(
                request.body.decode("ascii"), strict_parsing=True
            )
            self.token_name = values["name"][0]
            self.token_active = True
            if values["permissions"] != [ADMIN_PERMISSION]:
                raise AssertionError("token was overprivileged")
            expires = values["expires"][0]
            if expires not in (
                "2026-09-09T12:15:00",
                "2026-09-09T12:35:00",
            ):
                raise AssertionError("token lifetime was not allowlisted")
            self.created_token_expiries.append(expires)
            return _response(
                request,
                b"",
                status=302,
                headers=(("Location", "/admin/accesstoken/"),),
            )
        if path == "/admin/accesstoken/details/4/token-id":
            html = (
                "<table>"
                "<tr><th>Name</th><td>%s</td></tr>"
                "<tr><th>Token</th><td>%s</td></tr>"
                "<tr><th>Permissions</th><td>%s</td></tr>"
                "<tr><th>Parameter Name</th><td></td></tr>"
                "<tr><th>Expires</th><td>%s</td></tr>"
                "<tr><th>Created Date</th><td>Sep 9, 2026, 12:00:00 PM</td></tr>"
                "</table>"
                % (
                    self.token_name,
                    TOKEN,
                    ADMIN_PERMISSION,
                    self.token_expiry_override or self.created_token_expiries[-1],
                )
            ).encode("ascii")
            return _response(request, html)
        if path == "/api/admin/accesstoken/setting/token-id":
            if not self._require_token(request):
                return _response(request, {"status": 3}, status=403)
            if self.revoke_fails:
                return _response(request, {"status": 9, "message": REMOTE_SECRET})
            self.token_active = False
            return _response(request, {"status": 0})
        if path == "/api/admin/systeminfo":
            if not self._require_token(request):
                return _response(request, REMOTE_SECRET.encode("ascii"), status=401)
            return _response(request, {"status": 0, "data": {"version": "15.8"}})
        if path == "/api/admin/searchlist/docs":
            if not self._require_token(request):
                return _response(request, {"status": 3}, status=403)
            if request.method != "PUT" or query:
                raise AssertionError(
                    "SearchList pagination was not sent as a PUT JSON body"
                )
            search = json.loads(request.body)
            if search.get("q") != "*:*" or search.get("sort") != "url.asc":
                raise AssertionError(
                    "SearchList did not use the fixed global inventory query"
                )
            start = search["start"]
            size = search["size"]
            values = [self.documents[url] for url in sorted(self.documents)]
            selected = values[start : start + size]
            return _response(
                request,
                {
                    "status": 0,
                    "docs": [
                        {
                            key: row[key]
                            for key in (
                                "url",
                                "depositar_bundle_id",
                                "depositar_document_sha256",
                                "content_chunk_status",
                            )
                            if key in row
                        }
                        for row in selected
                    ],
                    "record_count": len(values),
                    "record_count_relation": "EQUAL_TO",
                    "partial": False,
                    "page_size": size,
                    "page_number": start // size + 1,
                    "start_record_number": start + 1 if selected else 0,
                    "end_record_number": start + len(selected),
                },
            )
        if path == "/api/admin/documents/bulk":
            if not self._require_token(request):
                return _response(request, {"status": 3}, status=403)
            docs = json.loads(request.body)["documents"]
            committed = docs[:1] if self.bulk_mode == "partial" else docs
            if self.tamper_document_on_bulk and committed:
                committed[0]["title"] = "tampered after validation"
                committed[0]["depositar_document_sha256"] = _document_sha256(
                    committed[0]
                )
            self.documents.update((row["url"], row) for row in committed)
            if self.bulk_mode == "timeout":
                raise CatalogTimeout("simulated timeout without secret data")
            if self.bulk_mode == "partial":
                return _response(
                    request,
                    {
                        "status": 9,
                        "items": [{"result": "CREATED", "id": "created-1"}],
                        "message": REMOTE_SECRET,
                    },
                )
            return _response(
                request,
                {
                    "status": 0,
                    "items": [
                        {"result": "CREATED", "id": "created-%d" % index}
                        for index, _row in enumerate(docs, 1)
                    ],
                },
            )
        if path == "/api/admin/scheduler/settings":
            if not self._require_token(request):
                return _response(request, {"status": 3}, status=403)
            self.assert_json_body(request, {"size": 100, "page": 1})
            self.settings_calls += 1
            return _response(
                request,
                {
                    "status": 0,
                    "settings": [
                        {
                            "id": "vector-scheduler",
                            "version_no": self.scheduler_version,
                            "name": VECTOR_JOB_NAME,
                            "target": "all",
                            "script_type": VECTOR_SCRIPT_TYPE,
                            "script_data": VECTOR_SCRIPT_DATA,
                            "crawler": "false",
                            "job_logging": "true",
                            "available": "true"
                            if self.scheduler_available
                            else "false",
                            "sort_order": 14,
                            "running": False,
                        }
                    ],
                    "total": 1,
                },
            )
        if (
            path == "/api/admin/scheduler/setting/vector-scheduler"
            and request.method == "GET"
        ):
            if not self._require_token(request):
                return _response(request, {"status": 3}, status=403)
            running = self.vector_job_running
            if self.ambiguous_start_pending:
                self.ambiguous_detail_checks += 1
                # launchNow() was queued before the response was lost.  It is
                # not executing for the first recovery observations, then
                # starts even though recovery has already disabled the DB
                # schedule, and finally becomes quiescent again.
                if self.ambiguous_detail_checks in (3, 4):
                    running = True
                    self.ambiguous_running_observed = True
                    self.scheduler_started = True
                    for document in self.documents.values():
                        document["content_chunk_status"] = "done"
                elif self.ambiguous_detail_checks >= 5:
                    self.ambiguous_start_pending = False
            return _response(
                request,
                {
                    "status": 0,
                    "setting": {
                        "id": "vector-scheduler",
                        "version_no": self.scheduler_version,
                        "name": VECTOR_JOB_NAME,
                        "target": "all",
                        "script_type": VECTOR_SCRIPT_TYPE,
                        "script_data": VECTOR_SCRIPT_DATA,
                        "crawler": "false",
                        "job_logging": "true",
                        "available": "true" if self.scheduler_available else "false",
                        "sort_order": 14,
                        "running": running,
                    },
                },
            )
        if path == "/api/admin/scheduler/setting" and request.method == "PUT":
            if not self._require_token(request):
                return _response(request, {"status": 3}, status=403)
            payload = json.loads(request.body)
            expected = {
                "id",
                "version_no",
                "name",
                "target",
                "cron_expression",
                "script_type",
                "script_data",
                "crawler",
                "job_logging",
                "available",
                "sort_order",
            }
            if set(payload) != expected:
                raise AssertionError(
                    "scheduler update did not send the full mutable record"
                )
            if (
                payload["id"] != "vector-scheduler"
                or payload["version_no"] != self.scheduler_version
                or payload["name"] != VECTOR_JOB_NAME
                or payload["target"] != "all"
                or payload["cron_expression"] != ""
                or payload["script_type"] != VECTOR_SCRIPT_TYPE
                or payload["script_data"] != VECTOR_SCRIPT_DATA
                or payload["crawler"] != "false"
                or payload["job_logging"] != "true"
                or payload["available"] not in ("true", "false")
                or payload["sort_order"] != 14
            ):
                raise AssertionError("scheduler update changed a sealed field")
            self.scheduler_available = payload["available"] == "true"
            self.scheduler_updates.append(payload["available"])
            self.scheduler_version += 1
            return _response(
                request,
                {
                    "status": 0,
                    "version": "15.8.0",
                    "id": "vector-scheduler",
                    "created": False,
                },
            )
        if path == "/api/admin/joblog/logs":
            if not self._require_token(request):
                return _response(request, {"status": 3}, status=403)
            self.assert_json_body(request, {"id": "new-job", "size": 2, "page": 1})
            logs = []
            if self.job_log_created:
                logs = [
                    {
                        "id": "new-job",
                        "job_name": VECTOR_JOB_NAME,
                        "job_status": self.vector_job_status,
                        "target": "all",
                        "script_type": VECTOR_SCRIPT_TYPE,
                        "script_data": VECTOR_SCRIPT_DATA,
                        "script_result": self.vector_script_result,
                        "start_time": "2026-09-09T12:00:00",
                        "end_time": (
                            None if self.vector_job_running else "2026-09-09T12:00:01"
                        ),
                    }
                ]
            return _response(request, {"status": 0, "logs": logs, "total": len(logs)})
        if path == "/api/admin/scheduler/vector-scheduler/start":
            if not self._require_token(request):
                return _response(request, {"status": 3}, status=403)
            if not self.scheduler_available:
                return _response(request, {"status": 9})
            if self.start_failures_remaining:
                self.start_failures_remaining -= 1
                return _response(request, {"status": 9})
            if self.start_commits_then_times_out:
                self.ambiguous_start_pending = True
                self.ambiguous_detail_checks = 0
                raise CatalogTimeout("simulated lost start response")
            self.scheduler_started = True
            self.job_log_created = True
            self.vector_job_running = self.vector_job_status == "running"
            if (
                self.vector_job_status == "ok"
                and self.vector_script_result.strip() == VECTOR_SCRIPT_RESULT
            ):
                for document in self.documents.values():
                    document["content_chunk_status"] = "done"
            return _response(request, {"status": 0, "job_log_id": "new-job"})
        if path == "/api/admin/scheduler/vector-scheduler/stop":
            if not self._require_token(request):
                return _response(request, {"status": 3}, status=403)
            self.stop_calls += 1
            if self.stop_mode == "failure":
                return _response(request, {"status": 9})
            if self.stop_mode != "no_commit_timeout":
                self.vector_job_running = False
                self.vector_job_status = "fail"
                self.vector_script_result = "stopped"
            if self.stop_mode in ("commit_timeout", "no_commit_timeout"):
                raise CatalogTimeout("simulated lost stop response")
            if self.stop_mode == "extra_field":
                return _response(request, {"status": 0, "unexpected": True})
            return _response(request, {"status": 0})
        raise AssertionError("unexpected path %s" % path)

    @staticmethod
    def assert_json_body(request, expected):
        if request.body is None or json.loads(request.body) != expected:
            raise AssertionError("request did not carry the exact JSON body")


class ScriptedInventory:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def snapshot(self, session, token, deadline):
        del session, token, deadline
        self.calls += 1
        if not self.snapshots:
            raise AssertionError("inventory script was exhausted")
        value = self.snapshots.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ManualClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class MemoryVectorJournal:
    """Record events while preserving the real vector-pass identity contract."""

    def __init__(self, bundle):
        self.identity = {
            "schema_version": 1,
            "run_id": "1" * 32,
            "recipe_sha256": "2" * 64,
            "bundle_id": bundle.bundle_id,
            "manifest_sha256": bundle.manifest_sha256,
        }
        self.events = []

    def append(self, event, payload):
        self.events.append((event, payload))


def vector_pass_arguments(bundle):
    return {
        "journal": MemoryVectorJournal(bundle),
        "run_id": "1" * 32,
        "recipe_sha256": "2" * 64,
    }
