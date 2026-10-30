"""Run the shared loopback smoke probe in an authenticated web image.

The caller supplies a bounded ``runtime_docker.Docker``, holds the product/catalog
locks, authenticates the immutable web build, and checks the gateway profile/CAS
before and after. This adapter has no source, gateway, lease or route authority.
It needs no host Pydantic: the existing web image supplies the real wire models.
Only fixed code and public identities enter argv; no host mounts or secrets do.
"""

from __future__ import annotations

import hashlib
import os
import re
import time

from search.operations.catalog import policy
from search.operations.deployment.images import build as image_build
from search.operations.deployment.release import probe as release_probe
from search.operations.deployment import source_bundle
from search.operations.shared import private_io


TOTAL_SECONDS = 360
MAX_COMMANDS = 12
LABEL = "io.depositar.ai.gateway-probe."
USER = "1000:1000"
TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=700"
SCRIPT = (
    release_probe._COMMON_SCRIPT
    + '\nBASE_URL = ORIGIN = "http://127.0.0.1:18083"\n'
    + "DEADLINE = started + 280\ncheck_website = lambda: None\n"
    + release_probe._STATIC_ACCEPTANCE_SCRIPT
    + release_probe._WEB_ACCEPTANCE_SCRIPT
)


class GatewayProbeRuntimeError(ValueError):
    """Fixed diagnostics, never Docker output, Env or HTTP content."""


def _require(condition):
    if not condition:
        raise GatewayProbeRuntimeError("gateway_probe_runtime_failed")


def _equal(actual, expected):
    _require(private_io.canonical(actual) == private_io.canonical(expected))


def _profile(row, *, image, identity, container_id, exited):
    """Prove the actual helper before start and before removing an exited ID."""
    _require(type(row) is dict)
    config, host, state, network = (
        row.get(key) for key in ("Config", "HostConfig", "State", "NetworkSettings")
    )
    _require(all(type(value) is dict for value in (config, host, state, network)))
    baseline = image["Config"]
    command = [
        "-B",
        "-c",
        SCRIPT,
        identity["revision"],
        identity["bundle_id"],
        identity["manifest_sha256"],
    ]
    labels = {
        **(baseline.get("Labels") or {}),
        **{
            LABEL + "run": identity["run_id"],
            LABEL + "revision": identity["revision"],
        },
    }
    environment = source_bundle.parse_image_environment(baseline.get("Env"))
    environment.update(NVIDIA_VISIBLE_DEVICES="void", CUDA_VISIBLE_DEVICES="")
    _require(
        row.get("Id") == container_id
        and row.get("Name") == "/" + identity["container_name"]
        and row.get("Image") == identity["image_id"]
        and config.get("Image") == identity["image_id"]
        and config.get("User") == USER
        and config.get("Entrypoint") == ["python"]
        and config.get("Cmd") == command
        and row.get("Path") == "python"
        and row.get("Args") == command
        and config.get("WorkingDir") == baseline.get("WorkingDir")
        and config.get("StopSignal") == baseline.get("StopSignal")
        and config.get("Healthcheck") == {"Test": ["NONE"]}
        and config.get("OpenStdin") is False
        and config.get("Tty") is False
        and not config.get("Volumes")
        and not host.get("Binds")
        and not host.get("Mounts")
        and host.get("NetworkMode") == "host"
        and host.get("ReadonlyRootfs") is True
        and host.get("Privileged") is False
        and host.get("CapDrop") == ["ALL"]
        and not host.get("CapAdd")
        and host.get("SecurityOpt") == ["no-new-privileges:true"]
        and host.get("Init") is True
        and host.get("AutoRemove") is False
        and not host.get("VolumesFrom")
        and not host.get("Devices")
        and not host.get("DeviceRequests")
        and not host.get("DeviceCgroupRules")
        and not host.get("ExtraHosts")
        and not host.get("Links")
        and not host.get("Dns")
        and not host.get("DnsOptions")
        and not host.get("DnsSearch")
        and not host.get("Sysctls")
        and host.get("Annotations") in (None, {})
        and host.get("CgroupnsMode", "") in {"", "private"}
        and policy._private_namespaces(host)
        and host.get("NanoCpus") == 10**9
        and host.get("Memory") == 1024**3
        and host.get("MemorySwap") == 1024**3
        and host.get("PidsLimit") == 64
        and host.get("RestartPolicy") == {"Name": "no", "MaximumRetryCount": 0}
        and host.get("LogConfig") == {"Type": "none", "Config": {}}
        and policy._ulimits(host.get("Ulimits")) == {"nofile": (1024, 1024)}
        and policy._tmpfs(host.get("Tmpfs"))
        == {"/tmp": frozenset(TMPFS.split(":", 1)[1].split(","))}
        and not host.get("PortBindings")
        and not any((network.get("Ports") or {}).values())
        and type(network.get("Networks")) is dict
        and set(network["Networks"]) == {"host"}
        and type(network["Networks"]["host"]) is dict
        and state.get("Status") == ("exited" if exited else "created")
        and state.get("Running") is False
        and state.get("Paused") is False
        and state.get("OOMKilled") is False
        and type(state.get("ExitCode")) is int
    )
    _equal(config.get("Labels"), labels)
    _equal(host.get("RestartPolicy"), {"Name": "no", "MaximumRetryCount": 0})
    _equal(source_bundle.parse_image_environment(config.get("Env")), environment)
    _equal(config.get("ExposedPorts"), baseline.get("ExposedPorts"))
    mounts = row.get("Mounts")
    _require(type(mounts) is list and len(mounts) <= 1)
    for mount in mounts:
        _require(
            type(mount) is dict
            and mount.get("Type") == "tmpfs"
            and mount.get("Destination") == "/tmp"
            and mount.get("RW") is True
        )
    return state["ExitCode"]


def verify_gateway_from_image(
    docker,
    *,
    source_plan,
    image_inspection,
    image_proof,
    revision,
    bundle_id,
    manifest_sha256,
    directory_fd,
    directory_path,
    run_id,
):
    """Return closed HTTP evidence after exact exited-helper removal is proven.

    The dedicated retained directory must be empty and private. Its no-clobber
    intent/container/failure records quarantine uncertain daemon work: this
    function never adopts a prior run, retries, kills or removes running work.
    A failed smoke probe also raises, after safe exited-helper cleanup if possible.
    ``manifest_sha256`` is the catalog manifest, not the source-bundle digest.
    Normal success uses exactly twelve bounded native calls; the caller reserves
    that capacity in its shared Docker budget and overall workflow deadline.
    """
    if (
        type(revision) is not str
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
        or any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (bundle_id, manifest_sha256)
        )
        or type(run_id) is not str
        or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
        or type(image_proof) is not dict
        or type(source_plan) is not dict
        or source_plan.get("revision") != revision
    ):
        raise GatewayProbeRuntimeError("gateway_probe_runtime_identity_invalid")
    try:
        expected = source_bundle.validate_built_image(
            source_plan,
            "web",
            image_proof.get("image_id"),
            image_inspection,
        )
        _equal(image_proof, expected)
        private_io.verify_directory(directory_fd, directory_path)
        if os.listdir(directory_fd):
            raise GatewayProbeRuntimeError("gateway_probe_runtime_requires_recovery")
    except GatewayProbeRuntimeError:
        raise
    except Exception:
        raise GatewayProbeRuntimeError(
            "gateway_probe_runtime_identity_invalid"
        ) from None

    identity = {
        "schema_version": 1,
        "revision": revision,
        "bundle_id": bundle_id,
        "manifest_sha256": manifest_sha256,
        "run_id": run_id,
        "image_id": image_proof["image_id"],
        "container_name": "depositar-ai-gateway-probe-" + run_id,
        "probe_sha256": hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest(),
        "confers_runtime_authority": False,
        "active_changed": False,
    }
    records = {}
    deadline, calls = time.monotonic() + TOTAL_SECONDS, 0
    container_id, removed = None, False

    def verify_records():
        private_io.verify_directory(directory_fd, directory_path)
        _require(set(os.listdir(directory_fd)) == set(records))
        for name, raw in records.items():
            _require(
                private_io.read_file(
                    directory_fd,
                    name,
                    mode=0o600,
                    max_bytes=16384,
                )
                == raw
            )

    def publish(name, value):
        verify_records()
        private_io.write_json(directory_fd, name, value, max_bytes=16384)
        records[name] = private_io.canonical(value)
        verify_records()

    def call(arguments, timeout=15):
        nonlocal calls
        verify_records()
        calls += 1
        remaining = deadline - time.monotonic()
        _require(calls <= MAX_COMMANDS and remaining > 0)
        result = docker.call(arguments, timeout=min(timeout, remaining))
        _require(time.monotonic() <= deadline)
        verify_records()
        return result

    def verify_image():
        actual = image_build._json(
            call(
                [
                    "image",
                    "inspect",
                    "--format",
                    "{{json .}}",
                    image_proof["image_id"],
                ]
            )
        )
        _equal(
            source_bundle.validate_built_image(
                source_plan,
                "web",
                image_proof["image_id"],
                actual,
            ),
            image_proof,
        )
        _equal(actual.get("Config"), image_inspection["Config"])

    def absence(*, by_id=False):
        filters = (
            ["id=" + container_id]
            if by_id
            else [
                "name=" + identity["container_name"],
                LABEL + "run=" + run_id,
            ]
        )
        for index, value in enumerate(filters):
            argument = value if by_id or index == 0 else "label=" + value
            _require(
                call(
                    [
                        "ps",
                        "--all",
                        "--no-trunc",
                        "--filter",
                        argument,
                        "--format",
                        "{{.ID}}",
                    ]
                ).strip()
                == b""
            )

    def inspect(exited):
        value = image_build._json(
            call(
                [
                    "inspect",
                    "--format",
                    "{{json .}}",
                    container_id,
                ]
            )
        )
        return _profile(
            value,
            image=image_inspection,
            identity=identity,
            container_id=container_id,
            exited=exited,
        )

    try:
        publish("intent.json", {**identity, "status": "probe_started"})
        verify_image()
        absence()
        command = [
            "create",
            "--pull",
            "never",
            "--name",
            identity["container_name"],
            "--label",
            LABEL + "run=" + run_id,
            "--label",
            LABEL + "revision=" + revision,
            "--entrypoint",
            "python",
            "--user",
            USER,
            "--runtime",
            "runc",
            "--env",
            "NVIDIA_VISIBLE_DEVICES=void",
            "--env",
            "CUDA_VISIBLE_DEVICES=",
            "--network",
            "host",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--init",
            "--pids-limit",
            "64",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--cpus",
            "1",
            "--ulimit",
            "nofile=1024:1024",
            "--restart",
            "no",
            "--log-driver",
            "none",
            "--no-healthcheck",
            "--tmpfs",
            TMPFS,
            image_proof["image_id"],
            "-B",
            "-c",
            SCRIPT,
            revision,
            bundle_id,
            manifest_sha256,
        ]
        raw_id = call(command)
        _require(
            type(raw_id) is bytes
            and re.fullmatch(rb"[0-9a-f]{64}\n?", raw_id) is not None
        )
        container_id = raw_id.decode("ascii").strip()
        publish(
            "container.json",
            {**identity, "status": "probe_created", "container_id": container_id},
        )
        inspect(False)
        output, start_failed = None, False
        try:
            output = call(["start", "--attach", container_id], timeout=285)
        except Exception:
            start_failed = True
        # A timed-out client can leave daemon work running. Only an independently
        # inspected, exact exited helper may be removed; uncertainty is retained.
        exit_code = inspect(True)
        website = None
        if not start_failed and exit_code == 0:
            try:
                website = release_probe._web(
                    release_probe._json(output),
                    revision,
                    bundle_id,
                    manifest_sha256,
                )
            except Exception:
                pass
        _require(call(["rm", container_id]).strip() == container_id.encode("ascii"))
        absence(by_id=True)
        absence()
        removed = True
        verify_image()
        _require(website is not None and website["code"] == "ready")
        report = {
            "schema_version": 1,
            "revision": revision,
            "bundle_id": bundle_id,
            "manifest_sha256": manifest_sha256,
            "status": "ready",
            "code": "ready",
            "gateway_http_verified": True,
            "website": website,
            "model_warmup_verified": False,
            "readiness_verified": False,
            "private_release_acceptance_required": True,
            "gateway_runtime_validation_required": True,
            "confers_runtime_authority": False,
            "active_changed": False,
            "search_quality_verified": False,
            "probe_runtime": {
                "container_id": container_id,
                "run_id": run_id,
                "helper_removed_verified": True,
                "host_network_verified": True,
            },
        }
        _require(time.monotonic() <= deadline)
        publish(
            "complete.json", {**identity, "status": "probe_complete", "report": report}
        )
        _require(time.monotonic() <= deadline)
        return report
    except Exception:
        try:
            publish(
                "failure.json",
                {
                    **identity,
                    "status": "probe_failed",
                    "code": "gateway_probe_runtime_failed",
                    "container_id": container_id,
                    "helper_removed_verified": removed,
                },
            )
        except Exception:
            pass
        raise GatewayProbeRuntimeError("gateway_probe_runtime_failed") from None
