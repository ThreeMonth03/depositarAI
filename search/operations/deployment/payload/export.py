"""Export approved offline-payload files and images into a fresh directory."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
import tempfile

from scripts import search_model_source
from search.operations.deployment import composition, source_bundle
from search.operations.deployment.images import build as image_build
from search.operations.deployment.payload import archive, contract
from search.operations.deployment.payload import plan as package_plan
from search.operations.shared import private_io


def _file_info(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) not in {0o600, 0o644}
    ):
        contract.fail("file_unsafe")
    return info


def _copy_files(inputs, expected, destination, *, sealed_metadata=False):
    contract.keys(inputs, expected, "input_inventory_incomplete")
    identities = {}
    with search_model_source.open_private_root(destination) as target:
        for relative, record in expected.items():
            path = Path(inputs[relative])
            parts = PurePosixPath(relative).parts
            if tuple(path.parts[-len(parts) :]) != parts:
                contract.fail("input_filename_mismatch")
            parent, name = target.relative_parent(
                relative, private=True, create=True
            )
            output_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            os.close(output_fd)
            with ExitStack() as sources:
                source_root = path.parents[len(parts) - 1]
                source_relative = relative
                if sealed_metadata:
                    # CatalogBundle roots are sealed 0500. Anchor the existing
                    # copy helper at their private 0700 parent so its allocated
                    # ancestor policy stays intact; the sealed child and file
                    # are still opened no-follow and verified through its FDs.
                    sources.enter_context(
                        search_model_source.open_private_root(
                            source_root.parent
                        )
                    )
                    source_relative = source_root.name + "/" + relative
                    source_root = source_root.parent
                search_model_source.copy_verified_file(
                    source_root,
                    source_relative,
                    destination / relative,
                    record["sha256"],
                    record["bytes"],
                )
            target.verify()
            identities[relative] = private_io.identity(
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            )
            os.fsync(parent)
        target.verify()
        os.fsync(target.fd)
    return identities


def _verify_copies(destination, copies):
    for relative, identities in copies.items():
        with search_model_source.open_private_root(
            destination / relative
        ) as root:
            for name, expected in identities.items():
                parent, leaf = root.relative_parent(name, private=True)
                if (
                    private_io.identity(
                        os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                    )
                    != expected
                ):
                    contract.fail("copied_asset_changed")
            root.verify()


def _check_image(role, entry, inspected, plan):
    if (
        type(inspected) is not dict
        or inspected.get("Id") != entry["image_id"]
        or inspected.get("Os") != "linux"
        or inspected.get("Architecture") != "amd64"
    ):
        contract.fail("image_identity_mismatch")
    environment, labels = contract.image_environment(inspected.get("Config"))
    if (
        hashlib.sha256(private_io.canonical(environment)).hexdigest()
        != entry["image_environment_sha256"]
    ):
        contract.fail("image_environment_changed")
    if role == "ollama":
        digest = "ollama/ollama@" + composition.OLLAMA_IMAGE.split("@", 1)[1]
        repo_digests = inspected.get("RepoDigests")
        if (
            type(repo_digests) is not list
            or any(type(value) is not str for value in repo_digests)
            or digest not in repo_digests
        ):
            contract.fail("registry_digest_mismatch")
        return
    if labels.get("org.opencontainers.image.revision") != entry["revision"]:
        contract.fail("image_source_mismatch")
    if role in image_build.SERVICES:
        if (
            labels.get(source_bundle.LABEL_PREFIX + "sha256")
            != entry["source_sha256"]
        ):
            contract.fail("image_source_digest_mismatch")
    elif role != "opensearch-hash":
        prefix = "io.depositar.ai.catalog."
        if (
            labels.get(prefix + "project") != plan["catalog"]["project"]
            or labels.get(prefix + "source-sha256") != entry["source_sha256"]
        ):
            contract.fail("catalog_image_mismatch")


def _inputs(plan, runner, before_publish, model_files, catalog_images):
    if not callable(runner):
        contract.fail("runner_invalid")
    if before_publish is not None and not callable(before_publish):
        contract.fail("publication_callback_invalid")
    catalog_raw = package_plan.catalog_images(plan, catalog_images)
    if plan["models"]["delivery"] == "bundled":
        contract.keys(
            model_files,
            package_plan.MODEL_ROLES,
            "model_input_inventory_incomplete",
        )
    elif model_files is not None:
        contract.fail("external_model_inputs_forbidden")
    return catalog_raw


def _check_destination(destination):
    if (
        not destination.is_absolute()
        or destination.name in {"", ".", ".."}
        or ".." in destination.parts
        or destination != destination.resolve()
    ):
        contract.fail("destination_unsafe")


def _copy_payload(
    root_fd,
    destination,
    plan,
    source_files,
    metadata_files,
    model_files,
    catalog_raw,
):
    copies = {}
    if catalog_raw is not None:
        private_io.write_file(
            root_fd,
            "catalog-images.json",
            catalog_raw,
            max_bytes=contract.MAX_RECORD_BYTES,
        )
        copies[""] = {
            "catalog-images.json": private_io.identity(
                os.stat(
                    "catalog-images.json",
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            )
        }
    assets = [
        ("source", source_files, plan["source"]["files"]),
        ("metadata", metadata_files, plan["metadata"]["files"]),
    ]
    if plan["models"]["delivery"] == "bundled":
        models_fd = private_io.open_child_directory(
            root_fd, "models", create=True
        )
        os.close(models_fd)
        assets.extend(
            ("models/" + role, model_files[role], files)
            for role, files in plan["models"]["files"].items()
        )
    for name, inputs, expected in assets:
        parent = root_fd
        model_fd = None
        if name.startswith("models/"):
            model_fd = private_io.open_child_directory(root_fd, "models")
            parent = model_fd
        child = private_io.open_child_directory(
            parent, name.split("/")[-1], create=True
        )
        try:
            copies[name] = _copy_files(
                inputs,
                expected,
                destination / name,
                sealed_metadata=name == "metadata",
            )
            private_io.verify_directory(child, destination / name)
        finally:
            os.close(child)
            if model_fd is not None:
                os.close(model_fd)
    return copies


class _DockerImages:
    def __init__(self, client, runner):
        self.runner = runner
        self.prefix = [
            "docker",
            "--host",
            image_build.DOCKER_HOST,
            "--config",
            client,
        ]
        self.environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    def call(self, arguments, timeout=30):
        result = self.runner(
            self.prefix + arguments,
            timeout=timeout,
            environment=dict(self.environment),
        )
        if type(result.returncode) is not int or result.returncode != 0:
            contract.fail("docker_command_failed")
        raw = (
            result.stdout.encode()
            if type(result.stdout) is str
            else result.stdout
        )
        error = (
            result.stderr.encode()
            if type(result.stderr) is str
            else result.stderr
        )
        if (
            type(raw) is not bytes
            or type(error) is not bytes
            or len(raw) + len(error) > image_build.MAX_OUTPUT_BYTES
        ):
            contract.fail("docker_output_invalid")
        return raw

    def inspect(self, plan):
        for role, entry in plan["images"].items():
            raw = self.call(
                [
                    "image",
                    "inspect",
                    "--format",
                    "{{json .}}",
                    entry["image_id"],
                ]
            )
            _check_image(role, entry, image_build._json(raw), plan)


def _archive_digest(archive_fd, before, root_fd):
    digest = hashlib.sha256()
    os.lseek(archive_fd, 0, os.SEEK_SET)
    while part := os.read(archive_fd, 1024**2):
        digest.update(part)
    if private_io.identity(before) != private_io.identity(
        os.fstat(archive_fd)
    ) or private_io.identity(before) != private_io.identity(
        os.stat("images.tar", dir_fd=root_fd, follow_symlinks=False)
    ):
        contract.fail("archive_changed")
    return digest


def _publish(root_fd, destination, result):
    checksums = {
        "images.tar": result["image_archive"]["sha256"],
        "package.json": hashlib.sha256(
            private_io.canonical(result)
        ).hexdigest(),
    }
    if result["catalog_image_receipt"] is not None:
        checksums["catalog-images.json"] = result["catalog"][
            "image_receipt_sha256"
        ]
    for directory in ("source", "metadata"):
        checksums.update(
            {
                directory + "/" + name: record["sha256"]
                for name, record in result[directory]["files"].items()
            }
        )
    if result["models"]["delivery"] == "bundled":
        for role, files in result["models"]["files"].items():
            checksums.update(
                {
                    "models/" + role + "/" + name: record["sha256"]
                    for name, record in files.items()
                }
            )
    encoded = "".join(
        value + "  " + name + "\n" for name, value in sorted(checksums.items())
    ).encode("ascii")
    private_io.write_file(
        root_fd, "SHA256SUMS", encoded, max_bytes=contract.MAX_RECORD_BYTES
    )
    private_io.verify_directory(root_fd, destination)
    private_io.write_json(
        root_fd, "package.json", result, max_bytes=contract.MAX_RECORD_BYTES
    )


def create(
    plan,
    destination,
    *,
    source_files,
    metadata_files,
    model_files=None,
    catalog_images=None,
    runner=None,
    before_publish=None,
):
    """Copy approved payloads and Docker-save immutable IDs into a new
    directory.

    The runner follows image_build._run(argv, timeout, environment)'s contract.
    No Docker mutation besides image save occurs. Failed output is preserved;
    package.json is published only after payload checks and fresh
    post-inspection.
    The caller retains its product lock and owns durable outer operation state.
    ``before_publish`` lets that trusted caller reverify original
    source/catalog/
    build authority after copying and exporting, before any completion marker.
    ``catalog_images`` optionally includes the verified exact-project prebuilt
    receipt accepted by start-index; arbitrary receipt fields are rejected.
    """
    plan = package_plan.validate(plan)
    destination = Path(destination)
    parent_fd = root_fd = archive_fd = None
    runner = image_build._run if runner is None else runner
    catalog_raw = _inputs(
        plan, runner, before_publish, model_files, catalog_images
    )
    try:
        _check_destination(destination)
        parent_fd = private_io.open_directory(destination.parent)
        root_fd = private_io.open_child_directory(
            parent_fd, destination.name, create=True
        )
        copies = _copy_payload(
            root_fd,
            destination,
            plan,
            source_files,
            metadata_files,
            model_files,
            catalog_raw,
        )
        with tempfile.TemporaryDirectory(
            prefix="depositar-package-docker-"
        ) as client:
            docker = _DockerImages(client, runner)
            docker.inspect(plan)
            private_io.verify_directory(root_fd, destination)
            if "images.tar" in os.listdir(root_fd):
                contract.fail("archive_already_exists")
            # Docker 29 uses atomicwriter: /proc/.../fd symlinks are refused
            # and
            # the client renames a temporary inode. Save only inside this new
            # private directory, then open/recheck the resulting regular file.
            output = str(destination / "images.tar")
            docker.call(
                [
                    "image",
                    "save",
                    "--output",
                    output,
                    *sorted(
                        entry["image_id"] for entry in plan["images"].values()
                    ),
                ],
                timeout=1800,
            )
            private_io.verify_directory(root_fd, destination)
            archive_fd = os.open(
                "images.tar",
                os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root_fd,
            )
            _file_info(archive_fd)
            os.fchmod(archive_fd, 0o600)
            os.fsync(archive_fd)
            before = _file_info(archive_fd)
            if not 1 <= before.st_size <= contract.MAX_ARCHIVE_BYTES:
                contract.fail("archive_size_limit")
            archive_format = archive.check_archive(archive_fd, plan["images"])
            digest = _archive_digest(archive_fd, before, root_fd)
            docker.inspect(plan)
        private_io.verify_directory(parent_fd, destination.parent)
        private_io.verify_directory(root_fd, destination)
        _verify_copies(destination, copies)
        if before_publish is not None:
            try:
                before_publish()
            except Exception:
                contract.fail("publication_authority_failed")
        private_io.verify_directory(root_fd, destination)
        _verify_copies(destination, copies)
        if private_io.identity(before) != private_io.identity(
            os.fstat(archive_fd)
        ) or private_io.identity(before) != private_io.identity(
            os.stat("images.tar", dir_fd=root_fd, follow_symlinks=False)
        ):
            contract.fail("archive_changed")
        result = {
            **plan,
            "status": "packaged",
            "image_archive": {
                "path": "images.tar",
                "bytes": before.st_size,
                "sha256": digest.hexdigest(),
                "format": archive_format,
            },
            "catalog_image_receipt": {
                "path": "catalog-images.json",
                "sha256": plan["catalog"]["image_receipt_sha256"],
            }
            if catalog_raw is not None
            else None,
        }
        _publish(root_fd, destination, result)
        return result
    except (
        OSError,
        ValueError,
        TypeError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ) as error:
        if isinstance(error, contract.ProductPackageError):
            raise
        raise contract.ProductPackageError(
            "product_package_export_failed"
        ) from None
    finally:
        for descriptor in (archive_fd, root_fd, parent_fd):
            if descriptor is not None:
                os.close(descriptor)
