"""Administrator session and recoverable ephemeral-token lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
import datetime as _datetime
from html.parser import HTMLParser
import re
import time
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .bundle import MAX_BULK_DOCUMENTS
from .codec import _exact_int, _json_wire_copy
from .errors import (
    AuthenticationError,
    CatalogError,
    CatalogTimeout,
    ConfigurationError,
    FessBusy,
    ImportFailed,
    ProtocolError,
    TokenCleanupError,
    _diagnosed,
    _failure_cause,
    _failure_detail,
)
from .journal import VectorJournal, _vector_event
from .policy import ID_RE, MAX_DOCUMENTS, MAX_INVENTORY_PAGE, NONCE_RE
from .session import (
    CSRF_RE,
    FessSession,
    MAX_JSON_RESPONSE_BYTES,
    TOKEN_RE,
    USERNAME_RE,
)
from .transport import Deadline, HTTPTransport, UrllibTransport

ADMIN_PERMISSION = "{role}admin-api"


ADMIN_SESSION_KEEPALIVE_SECONDS = 300.0


MAX_HTML_RESPONSE_BYTES = 2 * 1024 * 1024


MAX_BULK_RESPONSE_BYTES = 8 * 1024 * 1024


MAX_INVENTORY_RESPONSE_BYTES = 16 * 1024 * 1024


TOKEN_LIFETIME_MINUTES = 15


VECTOR_TOKEN_LIFETIME_MINUTES = 35


TOKEN_CLEANUP_SECONDS = 30.0


MAX_TOKEN_CLEANUP_SECONDS = 180.0


TOKEN_CLEANUP_LOGOUT_RESERVE_SECONDS = 5.0


MAX_CLEANUP_ATTEMPTS = 3


TOKEN_EXPIRY_MARGIN_SECONDS = 30.0


ACCESS_TOKEN_PAGE_SIZE = 25


MAX_ACCESS_TOKEN_PAGES = 400


_FIXED_ENDPOINTS = frozenset(
    (
        ("POST", "/api/v2/auth/login"),
        ("POST", "/api/v2/auth/logout"),
        ("GET", "/admin/accesstoken/"),
        ("GET", "/admin/accesstoken/createnew/"),
        ("POST", "/admin/accesstoken/"),
        ("PUT", "/api/admin/documents/bulk"),
        ("PUT", "/api/admin/searchlist/docs"),
        ("GET", "/api/admin/scheduler/settings"),
        ("PUT", "/api/admin/scheduler/setting"),
        ("GET", "/api/admin/joblog/logs"),
        ("GET", "/api/admin/systeminfo"),
    )
)


_DYNAMIC_ENDPOINTS = (
    ("GET", re.compile(r"^/admin/accesstoken/details/4/[A-Za-z0-9_-]{1,1000}$")),
    ("PUT", re.compile(r"^/api/admin/scheduler/[A-Za-z0-9_-]{1,1000}/start$")),
    ("PUT", re.compile(r"^/api/admin/scheduler/[A-Za-z0-9_-]{1,1000}/stop$")),
    ("GET", re.compile(r"^/api/admin/scheduler/setting/[A-Za-z0-9_-]{1,1000}$")),
    ("GET", re.compile(r"^/admin/accesstoken/list/[1-9][0-9]{0,2}$")),
    ("DELETE", re.compile(r"^/api/admin/accesstoken/setting/[A-Za-z0-9_-]{1,1000}$")),
)


class _AdminHTML(HTMLParser):
    """Parse only the Fess 15.8 access-token form/list/detail shapes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: List[Dict[str, Any]] = []
        self.rows: List[Dict[str, Any]] = []
        self.links: List[str] = []
        self._form: Optional[Dict[str, Any]] = None
        self._textarea: Optional[Dict[str, Any]] = None
        self._row: Optional[Dict[str, Any]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        values = dict(attrs)
        if tag == "a":
            href = values.get("href")
            if href is not None:
                self.links.append(href)
        elif tag == "form":
            if self._form is not None:
                raise ProtocolError("nested Fess admin forms are not accepted")
            self._form = {
                "action": values.get("action"),
                "method": (values.get("method") or "get").lower(),
                "controls": [],
            }
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            self._form["controls"].append(
                {
                    "name": values.get("name"),
                    "type": (values.get("type") or "text").lower(),
                    "value": values.get("value") or "",
                }
            )
        elif tag == "textarea" and self._form is not None:
            self._textarea = {
                "name": values.get("name"),
                "type": "textarea",
                "parts": [],
            }
            self._form["controls"].append(self._textarea)
        elif tag == "button" and self._form is not None:
            self._form["controls"].append(
                {
                    "name": values.get("name"),
                    "type": (values.get("type") or "submit").lower(),
                    "value": values.get("value") or "",
                }
            )
        elif tag == "tr":
            if self._row is not None:
                raise ProtocolError("nested Fess admin table rows are not accepted")
            self._row = {"href": values.get("data-href"), "cells": []}
            self.rows.append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            if self._cell is not None:
                raise ProtocolError("nested Fess admin table cells are not accepted")
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea["parts"].append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self._textarea is not None:
            self._textarea["value"] = "".join(self._textarea.pop("parts"))
            self._textarea = None
        elif tag == "form":
            self._form = None
        elif tag in ("td", "th") and self._cell is not None:
            assert self._row is not None
            self._row["cells"].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr":
            self._row = None


class AdminSession(FessSession):
    """Fixed administrator endpoints and short-lived bearer credentials."""

    @staticmethod
    def _assert_allowlisted_endpoint(method: str, path: str) -> None:
        if (method, path) in _FIXED_ENDPOINTS:
            return
        if any(
            candidate == method and pattern.fullmatch(path)
            for candidate, pattern in _DYNAMIC_ENDPOINTS
        ):
            return
        raise ConfigurationError("internal Fess endpoint is not allowlisted")

    def require_admin(self) -> None:
        # This is checked from the login payload by login_admin below.  Kept as a
        # separate method to make accidental ordinary-user token creation fail.
        if self._session_id is None:
            raise AuthenticationError("Fess administrator session is unavailable")

    def login_admin(self, username: str, password: str, deadline: Deadline) -> None:
        # Repeat the request here so the admin bit can be checked without storing
        # the response or credentials on the object.
        if USERNAME_RE.fullmatch(username or "") is None:
            raise AuthenticationError("Fess username is malformed")
        if (
            not isinstance(password, str)
            or not password
            or len(password) > 4096
            or "\x00" in password
        ):
            raise AuthenticationError("Fess password is malformed")
        if self._session_id is not None:
            raise AuthenticationError("Fess session is already authenticated")
        response = self._request(
            "POST",
            "/api/v2/auth/login",
            deadline=deadline,
            json_body={"username": username, "password": password},
            max_bytes=64 * 1024,
        )
        envelope = self._envelope(response)
        user = envelope.get("user")
        csrf = envelope.get("csrf_token")
        if (
            not isinstance(user, Mapping)
            or user.get("admin") is not True
            or not isinstance(csrf, str)
            or CSRF_RE.fullmatch(csrf) is None
        ):
            raise AuthenticationError("Fess administrator login was not confirmed")
        self._session_id = self._one_session_cookie(response)
        self._csrf_token = csrf
        self._admin_keepalive_at = self.clock() + ADMIN_SESSION_KEEPALIVE_SECONDS

    def _admin_html(self, path: str, deadline: Deadline) -> str:
        response = self._request(
            "GET",
            path,
            deadline=deadline,
            max_bytes=MAX_HTML_RESPONSE_BYTES,
            require_session=True,
        )
        try:
            return response.body.decode("utf-8", "strict")
        except UnicodeError:
            raise ProtocolError("Fess admin page is not UTF-8") from None

    @staticmethod
    def _parse_admin_html(value: str) -> _AdminHTML:
        parser = _AdminHTML()
        try:
            parser.feed(value)
            parser.close()
        except (ProtocolError, ValueError):
            raise ProtocolError("Fess admin HTML shape is invalid") from None
        if len(parser.forms) > 32 or len(parser.rows) > 128 or len(parser.links) > 4096:
            raise ProtocolError("Fess admin HTML exceeds its structural cap")
        return parser

    @staticmethod
    def _token_detail_path(href: Any) -> str:
        if not isinstance(href, str) or not 1 <= len(href) <= 4096:
            raise ProtocolError("Fess token detail link changed")
        split = urllib.parse.urlsplit(href)
        prefix = "/admin/accesstoken/details/4/"
        if (
            split.scheme
            or split.netloc
            or not split.path.startswith(prefix)
            or split.query
            or split.fragment
        ):
            raise ProtocolError("Fess token detail link changed")
        token_id = urllib.parse.unquote(split.path[len(prefix) :])
        if ID_RE.fullmatch(token_id or "") is None or "/" in token_id:
            raise ProtocolError("Fess token identifier is malformed")
        return prefix + urllib.parse.quote(token_id, safe="")

    def _access_token_snapshot(self, deadline: Deadline) -> Tuple[Tuple[str, str], ...]:
        records: List[Tuple[str, str]] = []
        seen_paths = set()
        for page_number in range(1, MAX_ACCESS_TOKEN_PAGES + 1):
            path = "/admin/accesstoken/list/%d" % page_number
            parser = self._parse_admin_html(self._admin_html(path, deadline))
            page_records: List[Tuple[str, str]] = []
            for row in parser.rows:
                href = row.get("href")
                if href is None:
                    continue
                cells = row.get("cells")
                if (
                    not isinstance(cells, list)
                    or not 1 <= len(cells) <= 16
                    or not isinstance(cells[0], str)
                    or not 1 <= len(cells[0]) <= 256
                    or any(
                        unicodedata.category(character) in ("Cc", "Cf")
                        for character in cells[0]
                    )
                ):
                    raise ProtocolError("Fess token listing row changed")
                detail_path = self._token_detail_path(href)
                if detail_path in seen_paths:
                    raise ProtocolError(
                        "Fess token listing contains a duplicate identifier"
                    )
                seen_paths.add(detail_path)
                page_records.append((cells[0], detail_path))
            if len(page_records) > ACCESS_TOKEN_PAGE_SIZE:
                raise ProtocolError("Fess token listing exceeded its pinned page size")
            records.extend(page_records)

            next_page = page_number + 1
            next_path = "/admin/accesstoken/list/%d" % next_page
            has_next = False
            for href in parser.links:
                split = urllib.parse.urlsplit(href)
                if split.scheme or split.netloc or split.query or split.fragment:
                    continue
                if split.path == next_path:
                    has_next = True
                elif split.path == "list/%d" % next_page:
                    has_next = True
            if not has_next:
                return tuple(records)
            if page_number == MAX_ACCESS_TOKEN_PAGES:
                raise ProtocolError("Fess token listing exceeded its page cap")
        raise ProtocolError("Fess token listing traversal did not terminate")

    def _listed_token_paths(self, token_name: str, deadline: Deadline) -> List[str]:
        first = self._access_token_snapshot(deadline)
        second = self._access_token_snapshot(deadline)
        if first != second:
            raise ProtocolError("Fess token listing changed during verification")
        return [path for name, path in first if name == token_name]

    def _token_from_detail(
        self,
        token_name: str,
        detail_path: str,
        deadline: Deadline,
        *,
        expected_expiry: Optional[str] = None,
    ) -> Tuple[str, str]:
        prefix = "/admin/accesstoken/details/4/"
        if not detail_path.startswith(prefix):
            raise ProtocolError("Fess token detail path is invalid")
        token_id = urllib.parse.unquote(detail_path[len(prefix) :])
        if ID_RE.fullmatch(token_id) is None or "/" in token_id:
            raise ProtocolError("Fess token identifier is malformed")
        parser = self._parse_admin_html(self._admin_html(detail_path, deadline))
        fields: Dict[str, str] = {}
        for row in parser.rows:
            cells = row.get("cells") or []
            if len(cells) != 2 or cells[0] in fields:
                raise ProtocolError("Fess token detail shape changed")
            fields[cells[0]] = cells[1]
        if set(fields) != {
            "Name",
            "Token",
            "Permissions",
            "Parameter Name",
            "Expires",
            "Created Date",
        }:
            raise ProtocolError("Fess token detail shape changed")
        token = fields["Token"]
        if (
            fields["Name"] != token_name
            or TOKEN_RE.fullmatch(token or "") is None
            or fields["Permissions"] != ADMIN_PERMISSION
            or fields["Parameter Name"] != ""
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}",
                fields["Expires"] or "",
            )
            is None
            or (expected_expiry is not None and fields["Expires"] != expected_expiry)
            or not 1 <= len(fields["Created Date"]) <= 128
        ):
            raise ProtocolError("Fess token detail is invalid")
        return token_id, token

    def create_ephemeral_admin_token(
        self,
        nonce: str,
        deadline: Deadline,
        *,
        utcnow: Callable[[], _datetime.datetime],
        lifetime_minutes: int = TOKEN_LIFETIME_MINUTES,
    ) -> Tuple[str, str, str]:
        self.require_admin()
        if NONCE_RE.fullmatch(nonce or "") is None:
            raise ConfigurationError(
                "import token nonce must be 12 lowercase hex characters"
            )
        if not _exact_int(lifetime_minutes) or lifetime_minutes not in (
            TOKEN_LIFETIME_MINUTES,
            VECTOR_TOKEN_LIFETIME_MINUTES,
        ):
            raise ConfigurationError(
                "ephemeral token lifetime is outside its allowlist"
            )
        token_name = "depositar-import-" + nonce
        if self._listed_token_paths(token_name, deadline):
            raise TokenCleanupError("an import token with this nonce already exists")
        create = self._parse_admin_html(
            self._admin_html("/admin/accesstoken/createnew/", deadline)
        )
        forms = []
        for form in create.forms:
            action = urllib.parse.urlsplit(str(form.get("action") or ""))
            if (
                action.path == "/admin/accesstoken/"
                and not action.query
                and not action.fragment
                and form.get("method") == "post"
            ):
                forms.append(form)
        if len(forms) != 1:
            raise ProtocolError("Fess access-token create form shape changed")
        hidden: Dict[str, str] = {}
        for control in forms[0]["controls"]:
            if control.get("type") != "hidden":
                continue
            name = control.get("name")
            value = control.get("value")
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) is None
                or not isinstance(value, str)
                or len(value) > 4096
                or name in hidden
            ):
                raise ProtocolError("Fess access-token hidden fields changed")
            hidden[name] = value
        if not 1 <= len(hidden) <= 8 or hidden.get("crudMode") != "1":
            raise ProtocolError("Fess access-token create state changed")
        now = utcnow()
        if not isinstance(now, _datetime.datetime) or now.tzinfo is None:
            raise ConfigurationError("utcnow must return an aware datetime")
        expiry = (
            now.astimezone(_datetime.timezone.utc)
            + _datetime.timedelta(minutes=lifetime_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        hidden.update(
            {
                "crudMode": "1",
                "name": token_name,
                "permissions": ADMIN_PERMISSION,
                "parameterName": "",
                "expires": expiry,
                "create": "Create",
            }
        )
        self._token_creation_dispatched = True
        self._request(
            "POST",
            "/admin/accesstoken/",
            deadline=deadline,
            form_body=hidden,
            max_bytes=MAX_HTML_RESPONSE_BYTES,
            allow_redirect_path="/admin/accesstoken/",
            require_session=True,
        )
        matches = self._listed_token_paths(token_name, deadline)
        if len(matches) != 1:
            raise ProtocolError("new Fess access token is not uniquely listed")
        token_id, token = self._token_from_detail(
            token_name,
            matches[0],
            deadline,
            expected_expiry=expiry,
        )
        self.system_info(token, deadline)
        return token_id, token, token_name

    def recover_ephemeral_token(
        self, token_name: str, deadline: Deadline
    ) -> Optional[Tuple[str, str]]:
        paths = self._listed_token_paths(token_name, deadline)
        if not paths:
            return None
        if len(paths) != 1:
            raise TokenCleanupError(
                "ephemeral access token is not unique during cleanup"
            )
        return self._token_from_detail(token_name, paths[0], deadline)

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        deadline: Deadline,
        query: Optional[Sequence[Tuple[str, str]]] = None,
        body: Optional[Mapping[str, Any]] = None,
        max_bytes: int = MAX_JSON_RESPONSE_BYTES,
        accepted_statuses: Sequence[int] = (0,),
        on_dispatch: Optional[Callable[[], None]] = None,
    ) -> Mapping[str, Any]:
        if (
            self._admin_keepalive_at is not None
            and self._session_id is not None
            and self.clock() >= self._admin_keepalive_at
        ):
            # One fixed, read-only HTML request refreshes cookie inactivity.
            # Never attach the cookie to a bearer API call: token revocation
            # must still be checked without an alternative authenticated route.
            # This uses the caller's deadline and never retries a write.
            self._parse_admin_html(
                self._admin_html("/admin/accesstoken/list/1", deadline)
            )
            self._admin_keepalive_at = self.clock() + ADMIN_SESSION_KEEPALIVE_SECONDS
        response = self._request(
            method,
            path,
            deadline=deadline,
            query=query,
            json_body=body,
            token=token,
            max_bytes=max_bytes,
            on_dispatch=on_dispatch,
        )
        return self._envelope(response, accepted_statuses=accepted_statuses)

    def documents_bulk(
        self,
        token: str,
        documents: Sequence[Mapping[str, Any]],
        deadline: Deadline,
    ) -> Mapping[str, Any]:
        if not 1 <= len(documents) <= MAX_BULK_DOCUMENTS:
            raise ConfigurationError("document batch size is outside its cap")
        wire_documents = [_json_wire_copy(document) for document in documents]
        dispatched = False

        def mark_dispatched() -> None:
            nonlocal dispatched
            dispatched = True
            self._document_bulk_dispatched = True

        try:
            return self._api_request(
                "PUT",
                "/api/admin/documents/bulk",
                token=token,
                deadline=deadline,
                body={"documents": wire_documents},
                max_bytes=MAX_BULK_RESPONSE_BYTES,
                accepted_statuses=(0, 1, 2, 3, 9),
                on_dispatch=mark_dispatched,
            )
        except CatalogError as error:
            if dispatched:
                raise _diagnosed(
                    ImportFailed(
                        "document bulk request failed; catalog may be partially imported"
                    ),
                    stage="bulk_dispatched",
                    cause=_failure_cause(error),
                ) from None
            raise

    def search_list(
        self, token: str, start: int, size: int, deadline: Deadline
    ) -> Mapping[str, Any]:
        if not _exact_int(start, minimum=0, maximum=MAX_DOCUMENTS) or not _exact_int(
            size, minimum=1, maximum=MAX_INVENTORY_PAGE
        ):
            raise ConfigurationError("SearchList page is outside its cap")
        return self._api_request(
            "PUT",
            "/api/admin/searchlist/docs",
            token=token,
            deadline=deadline,
            body={"q": "*:*", "start": start, "size": size, "sort": "url.asc"},
            max_bytes=MAX_INVENTORY_RESPONSE_BYTES,
        )

    def scheduler_settings(self, token: str, deadline: Deadline) -> Mapping[str, Any]:
        return self._api_request(
            "GET",
            "/api/admin/scheduler/settings",
            token=token,
            deadline=deadline,
            body={"size": 100, "page": 1},
        )

    def scheduler_setting(
        self, token: str, scheduler_id: str, deadline: Deadline
    ) -> Mapping[str, Any]:
        if ID_RE.fullmatch(scheduler_id or "") is None:
            raise ConfigurationError("scheduler identifier is malformed")
        return self._api_request(
            "GET",
            "/api/admin/scheduler/setting/" + urllib.parse.quote(scheduler_id, safe=""),
            token=token,
            deadline=deadline,
        )

    def update_scheduler(
        self,
        token: str,
        setting: Mapping[str, Any],
        deadline: Deadline,
    ) -> Mapping[str, Any]:
        return self._api_request(
            "PUT",
            "/api/admin/scheduler/setting",
            token=token,
            deadline=deadline,
            body=setting,
        )

    def job_logs(
        self, token: str, job_id: str, deadline: Deadline
    ) -> Mapping[str, Any]:
        if ID_RE.fullmatch(job_id or "") is None:
            raise ConfigurationError("job-log identifier is malformed")
        return self._api_request(
            "GET",
            "/api/admin/joblog/logs",
            token=token,
            deadline=deadline,
            body={"id": job_id, "size": 2, "page": 1},
        )

    def start_scheduler(
        self, token: str, scheduler_id: str, deadline: Deadline
    ) -> Mapping[str, Any]:
        if ID_RE.fullmatch(scheduler_id or "") is None:
            raise ConfigurationError("scheduler identifier is malformed")
        return self._api_request(
            "PUT",
            "/api/admin/scheduler/"
            + urllib.parse.quote(scheduler_id, safe="")
            + "/start",
            token=token,
            deadline=deadline,
            accepted_statuses=(0, 9),
        )

    def stop_scheduler(self, token: str, scheduler_id: str, deadline: Deadline) -> None:
        if ID_RE.fullmatch(scheduler_id or "") is None:
            raise ConfigurationError("scheduler identifier is malformed")
        response = self._api_request(
            "PUT",
            "/api/admin/scheduler/"
            + urllib.parse.quote(scheduler_id, safe="")
            + "/stop",
            token=token,
            deadline=deadline,
        )
        if set(response) != {"status"} or response.get("status") != 0:
            raise ProtocolError("scheduler stop response is invalid")

    def system_info(self, token: str, deadline: Deadline) -> Mapping[str, Any]:
        return self._api_request(
            "GET",
            "/api/admin/systeminfo",
            token=token,
            deadline=deadline,
        )

    def revoke_access_token(
        self, token_id: str, token: str, deadline: Deadline
    ) -> None:
        if ID_RE.fullmatch(token_id or "") is None:
            raise ConfigurationError("access-token identifier is malformed")
        self._api_request(
            "DELETE",
            "/api/admin/accesstoken/setting/" + urllib.parse.quote(token_id, safe=""),
            token=token,
            deadline=deadline,
        )

    def assert_token_revoked(self, token: str, deadline: Deadline) -> None:
        response = self._request(
            "GET",
            "/api/admin/systeminfo",
            deadline=deadline,
            token=token,
            max_bytes=MAX_JSON_RESPONSE_BYTES,
            accepted_http_statuses=(401, 403),
        )
        if response.status == 401:
            return
        # A generic 403 can be the IP-block filter, not invalid credentials.
        # Require Fess's explicit authentication-failure envelope otherwise.
        envelope = self._envelope(response, accepted_statuses=(0, 3))
        if envelope.get("status") == 3:
            return
        raise TokenCleanupError("revoked access token still authenticates")


class AdminAccess:
    """Own administrator credentials only for one bounded operation."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: Optional[HTTPTransport] = None,
        request_timeout: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utcnow: Callable[[], _datetime.datetime] = lambda: _datetime.datetime.now(
            _datetime.timezone.utc
        ),
    ) -> None:
        self.base_url = base_url
        self.transport = transport or UrllibTransport()
        self.request_timeout = request_timeout
        self.clock = clock
        self.sleep = sleep
        self.utcnow = utcnow

    def _session(self) -> AdminSession:
        return AdminSession(
            self.base_url,
            transport=self.transport,
            request_timeout=self.request_timeout,
            clock=self.clock,
        )

    def _cleanup_retry(self, operation: Callable[[], Any], deadline: Deadline) -> Any:
        """Cleanup-only HTTP-429 retries, never bulk/create/start retries."""
        for attempt in range(MAX_CLEANUP_ATTEMPTS):
            deadline.remaining(self.request_timeout)
            try:
                return operation()
            except FessBusy as error:
                if attempt + 1 == MAX_CLEANUP_ATTEMPTS:
                    raise
                delay = max(
                    1.0,
                    float(error.retry_after_seconds)
                    if error.retry_after_seconds is not None
                    else 2.0 ** (attempt + 1),
                )
                if delay + 1.0 >= deadline.end - self.clock():
                    raise CatalogTimeout(
                        "cleanup retry does not fit its deadline"
                    ) from None
                self.sleep(delay)
        raise TokenCleanupError("cleanup attempt cap reached")

    def _cleanup_token(
        self,
        session: AdminSession,
        token_name: str,
        token_id: str,
        token: str,
        deadline: Deadline,
    ) -> None:
        attempted = False

        def revoke() -> None:
            nonlocal attempted
            if attempted:
                found = session.recover_ephemeral_token(token_name, deadline)
                if found is None:
                    return
                if found != (token_id, token):
                    raise TokenCleanupError("cleanup token identity changed")
            attempted = True
            session.revoke_access_token(token_id, token, deadline)

        try:
            self._cleanup_retry(revoke, deadline)
        except CatalogError:
            # A failed response may follow a committed deletion. Reconcile by
            # reads; never replay an ambiguous DELETE, and never delete a peer.
            pass
        if self._cleanup_retry(
            lambda: session._listed_token_paths(token_name, deadline), deadline
        ):
            raise TokenCleanupError("revoked access token remains listed")
        self._cleanup_retry(
            lambda: session.assert_token_revoked(token, deadline), deadline
        )

    @contextmanager
    def token(
        self,
        username: str,
        password: str,
        nonce: str,
        deadline: Deadline,
        *,
        lifetime_minutes: int = TOKEN_LIFETIME_MINUTES,
        journal: Optional[VectorJournal] = None,
    ) -> Iterator[Tuple[AdminSession, str]]:
        session = self._session()
        logged_in = False
        token_id: Optional[str] = None
        token: Optional[str] = None
        token_name = "depositar-import-" + nonce
        primary: Optional[BaseException] = None
        token_cleanup_end = self.clock() + TOKEN_CLEANUP_SECONDS
        try:
            session.login_admin(username, password, deadline)
            logged_in = True
            token_cleanup_end = (
                self.clock() + lifetime_minutes * 60 - TOKEN_EXPIRY_MARGIN_SECONDS
            )
            _vector_event(journal, "token-create-intent", {"token_name": token_name})
            token_id, token, _ = session.create_ephemeral_admin_token(
                nonce,
                deadline,
                utcnow=self.utcnow,
                lifetime_minutes=lifetime_minutes,
            )
            _vector_event(
                journal,
                "token-created",
                {"token_name": token_name, "token_id": token_id},
            )
            yield session, token
        except BaseException as exc:
            primary = exc
        cleanup_failed = False
        cleanup_diagnostic = None

        def note_cleanup_failure(error=None, *, stage="token_cleanup"):
            nonlocal cleanup_failed, cleanup_diagnostic
            if not cleanup_failed:
                cleanup_diagnostic = _failure_detail(error, stage=stage)
            cleanup_failed = True

        if logged_in:
            cleanup_deadline = Deadline(
                min(self.clock() + MAX_TOKEN_CLEANUP_SECONDS, token_cleanup_end),
                self.clock,
            )
            token_deadline = Deadline(
                cleanup_deadline.end - TOKEN_CLEANUP_LOGOUT_RESERVE_SECONDS,
                self.clock,
            )
            try:
                _vector_event(
                    journal, "token-cleanup-intent", {"token_name": token_name}
                )
            except CatalogError as error:
                # The durable create intent already obliges cleanup. Evidence
                # failure must block a clean result, not strand a credential.
                note_cleanup_failure(error, stage="token_journal")
            creation_dispatched = session._token_creation_dispatched
            if creation_dispatched and (token_id is None or token is None):
                try:
                    recovered = self._cleanup_retry(
                        lambda: session.recover_ephemeral_token(
                            token_name, token_deadline
                        ),
                        token_deadline,
                    )
                    if recovered is not None:
                        token_id, token = recovered
                except CatalogError as error:
                    note_cleanup_failure(error, stage="token_recovery")
            if token_id is not None and token is not None:
                try:
                    self._cleanup_token(
                        session, token_name, token_id, token, token_deadline
                    )
                except CatalogError as error:
                    note_cleanup_failure(error)
            elif creation_dispatched and primary is not None:
                # A failed create request may have committed.  Absence was
                # established only when recovery completed successfully.
                try:
                    if self._cleanup_retry(
                        lambda: session._listed_token_paths(token_name, token_deadline),
                        token_deadline,
                    ):
                        note_cleanup_failure(
                            TokenCleanupError("cleanup token remains listed")
                        )
                except CatalogError as error:
                    note_cleanup_failure(error)
            if not cleanup_failed:
                try:
                    _vector_event(journal, "token-revoked", {"token_name": token_name})
                except CatalogError as error:
                    note_cleanup_failure(error, stage="token_journal")
            try:
                _vector_event(journal, "logout-intent", {})
            except CatalogError as error:
                note_cleanup_failure(error, stage="token_journal")
            try:
                self._cleanup_retry(
                    lambda: session.logout(cleanup_deadline), cleanup_deadline
                )
                _vector_event(journal, "session-logged-out", {})
            except CatalogError as error:
                note_cleanup_failure(error, stage="logout")
            finally:
                session._forget_session()
        if cleanup_failed:
            error = TokenCleanupError(
                "ephemeral access-token cleanup could not be verified",
                possibly_polluted=(
                    session._document_bulk_dispatched
                    or bool(getattr(primary, "possibly_polluted", False))
                ),
            )
            error.primary_diagnostic = _failure_detail(primary)
            error.cleanup_diagnostic = cleanup_diagnostic
            _diagnosed(
                error,
                stage="token_cleanup",
                cause=(
                    cleanup_diagnostic["cause"]
                    if cleanup_diagnostic is not None
                    else "cleanup_unverified"
                ),
            )
            raise error from None
        if primary is not None:
            raise primary
