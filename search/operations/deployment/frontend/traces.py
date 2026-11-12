"""Permit one explicitly configured private trace output bind."""

from pathlib import PurePosixPath

from search.trace.limits import TraceLimits


TARGET = "/var/lib/depositar/traces"
LIMIT_NAMES = {
    "TTL_SECONDS",
    "MAX_TURNS",
    "MAX_EVENT_BYTES",
    "MAX_SESSION_BYTES",
    "MAX_TOTAL_BYTES",
    "QUEUE_ITEMS",
    "QUEUE_BYTES",
    "MAX_SESSIONS",
    "SHUTDOWN_SECONDS",
}
ENVIRONMENT_KEYS = {"WEB_TRACE_ROOT"} | {
    "WEB_TRACE_" + name for name in LIMIT_NAMES
}


def absolute_directory(value):
    """Reject ambiguous paths before Compose can normalize their source."""
    return (
        type(value) is str
        and bool(value)
        and value != "/"
        and not value.startswith("//")
        and PurePosixPath(value).is_absolute()
        and str(PurePosixPath(value)) == value
        and ".." not in PurePosixPath(value).parts
        and not any(character in value for character in "\0\r\n")
    )


def checked_limits(environment):
    values = {}
    try:
        for suffix in LIMIT_NAMES:
            name = "WEB_TRACE_" + suffix
            if name in environment:
                parser = float if suffix == "SHUTDOWN_SECONDS" else int
                value = environment[name]
                if type(value) is not str:
                    raise ValueError("trace_limit_invalid")
                values[suffix.lower()] = parser(value)
        return TraceLimits(**values)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            "trace limits must be finite positive budgets"
        ) from None


def enabled(environment):
    checked_limits(environment)
    root = environment.get("WEB_TRACE_ROOT", "")
    if type(root) is not str:
        raise ValueError("trace output requires its fixed private mount")
    if root and root != TARGET:
        raise ValueError("trace output requires its fixed private mount")
    if not root and (set(environment) & ENVIRONMENT_KEYS) - {"WEB_TRACE_ROOT"}:
        raise ValueError("trace limits require an enabled private output")
    return bool(root)


def output_mount(service, services):
    """Select the only permitted writable output, preserving input rules."""
    active = enabled(service["environment"])
    matches = [
        mount
        for mount in service.get("volumes", [])
        if mount.get("target") == TARGET
    ]
    if not active and not matches:
        return None
    if not active or len(matches) != 1:
        raise ValueError("trace output requires exactly one configured mount")
    mount = matches[0]
    if (
        mount.get("type") != "bind"
        or mount.get("read_only", False) is not False
        or mount.get("bind", {}).get("create_host_path") is not False
        or not absolute_directory(mount.get("source"))
    ):
        raise ValueError("trace output requires one existing writable bind")
    if "docker.sock" in mount["source"]:
        raise ValueError("no Docker socket in website")
    _separate_inputs(mount, services)
    return mount


def _separate_inputs(mount, services):
    root = PurePosixPath(mount["source"])
    for service in services.values():
        for other in service.get("volumes", []):
            if other is mount:
                continue
            source = PurePosixPath(other.get("source", ""))
            if (
                root == source
                or root in source.parents
                or source in root.parents
            ):
                raise ValueError("trace output overlaps a readonly input")
