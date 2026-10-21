"""Read-only admission to an existing private conversation backend.

Inspect only bounded Docker metadata. Never start a container, contact the API,
read a credential, or return Docker output in operator diagnostics.
"""

import os
import re
import subprocess

from search.serialization import strict_json
from .. import image_build


MAX_PEERS = 32
MAX_OUTPUT_BYTES = 256 * 1024
COMMAND_TIMEOUT = 15
IDENTIFIER = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
PROJECT = re.compile(r"depositar-ai-release-[0-9a-f]{12}-[0-9a-f]{12}\Z")
ERROR_CODES = frozenset(
    "public_entry_backend_" + code
    for code in (
        "plan_invalid",
        "inspection_failed",
        "output_limit",
        "network_invalid",
        "ambiguous",
        "unhealthy",
        "image_changed",
        "contract_changed",
        "ports_exposed",
        "key_changed",
    )
)
NETWORK_FORMAT = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},"Driver":{{json .Driver}},'
    '"Scope":{{json .Scope}},"Internal":{{json .Internal}},'
    '"Labels":{{json .Labels}},"Containers":{{json .Containers}}}'
)
CONTAINER_FORMAT = (
    '{"Id":{{json .Id}},"Image":{{json .Image}},"Labels":{{json .Config.Labels}},'
    '"State":{"Running":{{json .State.Running}},"Paused":{{json .State.Paused}},'
    '"Restarting":{{json .State.Restarting}},"Dead":{{json .State.Dead}},'
    '"Health":{{if .State.Health}}{"Status":{{json .State.Health.Status}}}{{else}}null{{end}}},'
    '"Mounts":{{json .Mounts}},"Networks":{{json .NetworkSettings.Networks}},'
    '"Ports":{{json .NetworkSettings.Ports}},'
    '"PortBindings":{{json .HostConfig.PortBindings}}}'
)


class PublicEntryPreflightError(ValueError):
    """Fixed diagnostics only; no secrets, paths, or daemon output."""


def _require(condition, code):
    if not condition:
        raise PublicEntryPreflightError("public_entry_backend_" + code)


def _output(runner, argv):
    try:
        if runner is subprocess.run:
            # Reuse the existing live pipe cap instead of adding another process
            # capture implementation. It also terminates timed-out child groups.
            result = image_build._run(
                argv, timeout=COMMAND_TIMEOUT, environment=dict(os.environ)
            )
        else:
            result = runner(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        stdout, stderr = result.stdout, result.stderr
        _require(type(stdout) is bytes and type(stderr) is bytes, "inspection_failed")
        _require(len(stdout) + len(stderr) <= MAX_OUTPUT_BYTES, "output_limit")
        _require(
            type(result.returncode) is int and result.returncode == 0,
            "inspection_failed",
        )
        return stdout
    except PublicEntryPreflightError:
        raise
    except (OSError, subprocess.SubprocessError, image_build.ImageBuildError):
        raise PublicEntryPreflightError(
            "public_entry_backend_inspection_failed"
        ) from None


def _objects(raw):
    try:
        rows = [strict_json(line) for line in raw.splitlines() if line.strip()]
        _require(all(type(row) is dict for row in rows), "inspection_failed")
        return rows
    except (ValueError, UnicodeError, RecursionError):
        raise PublicEntryPreflightError(
            "public_entry_backend_inspection_failed"
        ) from None


def _plan(profile, release):
    try:
        project = release["name"]
        network = release["networks"]["website"]["name"]
        external = profile["networks"]["existing-release"]
        image = release["services"]["conversation-api"]["image"]
        mounts = [
            mount
            for mount in profile["services"]["public-web"]["volumes"]
            if mount.get("target") == "/run/secrets/api_key"
        ]
        _require(
            type(project) is str
            and PROJECT.fullmatch(project)
            and network == project + "_website"
            and external.get("external") is True
            and external.get("name") == network
            and type(image) is str
            and IMAGE.fullmatch(image)
            and len(mounts) == 1
            and mounts[0].get("type") == "bind"
            and mounts[0].get("read_only") is True
            and type(mounts[0].get("source")) is str
            and mounts[0]["source"].startswith("/"),
            "plan_invalid",
        )
        return project, network, image, mounts[0]["source"]
    except (KeyError, TypeError, AttributeError):
        raise PublicEntryPreflightError("public_entry_backend_plan_invalid") from None


def _verify_private_backend(profile, release, runner):
    project, network_name, expected_image, key_source = _plan(profile, release)
    networks = _objects(
        _output(
            runner,
            ["docker", "network", "inspect", "--format", NETWORK_FORMAT, network_name],
        )
    )
    _require(len(networks) == 1, "network_invalid")
    network = networks[0]
    labels = network.get("Labels") or {}
    members = network.get("Containers")
    _require(
        network.get("Name") == network_name
        and network.get("Driver") == "bridge"
        and network.get("Scope") == "local"
        and network.get("Internal") is True
        and type(network.get("Id")) is str
        and IDENTIFIER.fullmatch(network["Id"])
        and type(labels) is dict
        and labels.get("com.docker.compose.project") == project
        and labels.get("com.docker.compose.network") == "website"
        and type(members) is dict
        and 1 <= len(members) <= MAX_PEERS
        and all(
            type(identifier) is str and IDENTIFIER.fullmatch(identifier)
            for identifier in members
        ),
        "network_invalid",
    )
    rows = _objects(
        _output(
            runner,
            ["docker", "inspect", "--format", CONTAINER_FORMAT, *sorted(members)],
        )
    )
    _require(
        len(rows) == len(members) and {row.get("Id") for row in rows} == set(members),
        "inspection_failed",
    )
    candidates, alias_owners = [], []
    for row in rows:
        labels = row.get("Labels") or {}
        attached = (row.get("Networks") or {}).get(network_name) or {}
        aliases = attached.get("Aliases") or []
        if type(aliases) is list and "conversation-api" in aliases:
            alias_owners.append(row["Id"])
        if type(labels) is dict and (
            labels.get("com.docker.compose.project") == project
            and labels.get("com.docker.compose.service") == "conversation-api"
        ):
            candidates.append(row)
    _require(len(candidates) == 1, "ambiguous")
    api = candidates[0]
    _require(alias_owners == [api["Id"]], "ambiguous")
    attached = api["Networks"][network_name]
    _require(attached.get("NetworkID") == network["Id"], "network_invalid")
    state = api.get("State") or {}
    _require(
        type(state) is dict
        and state.get("Running") is True
        and state.get("Paused") is False
        and state.get("Restarting") is False
        and state.get("Dead") is False
        and (state.get("Health") or {}).get("Status") == "healthy",
        "unhealthy",
    )
    _require(api.get("Image") == expected_image, "image_changed")
    _require(
        api["Labels"].get("io.depositar.ai.conversation.version") == "3",
        "contract_changed",
    )
    ports = api.get("Ports")
    _require(
        api.get("PortBindings") in (None, {})
        and (
            ports is None
            or type(ports) is dict
            and all(value in (None, []) for value in ports.values())
        ),
        "ports_exposed",
    )
    key_mounts = [
        mount
        for mount in api.get("Mounts", [])
        if type(mount) is dict and mount.get("Destination") == "/run/secrets/api_key"
    ]
    _require(
        len(key_mounts) == 1
        and key_mounts[0].get("Type") == "bind"
        and key_mounts[0].get("Source") == key_source
        and key_mounts[0].get("RW") is False,
        "key_changed",
    )


def verify_private_backend(profile, release, *, runner=subprocess.run):
    """Reject a missing, ambiguous or incompatible backend before CPU startup."""
    try:
        return _verify_private_backend(profile, release, runner)
    except PublicEntryPreflightError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
        raise PublicEntryPreflightError(
            "public_entry_backend_inspection_failed"
        ) from None


def verify_bound_backend(profile, *, runner=subprocess.run, compose_file=None):
    """Verify the backend identity sealed in a prepared or versioned profile."""
    try:
        plan = profile["x-depositar-plan"]["private_backend"]
        if type(plan) is dict and plan.get("kind") == "https":
            from .remote import verify_remote_backend

            return verify_remote_backend(
                profile, runner=runner, compose_file=compose_file
            )
        _require(
            type(plan) is dict
            and set(plan)
            == {"schema_version", "release_project", "network_name", "api_image"}
            and type(plan["schema_version"]) is int
            and plan["schema_version"] == 1,
            "plan_invalid",
        )
        release = {
            "name": plan["release_project"],
            "networks": {"website": {"name": plan["network_name"]}},
            "services": {"conversation-api": {"image": plan["api_image"]}},
        }
    except (KeyError, TypeError, AttributeError):
        raise PublicEntryPreflightError("public_entry_backend_plan_invalid") from None
    return verify_private_backend(profile, release, runner=runner)
