"""Run the cached Docling CPU image against private local experiment files."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


IMAGE = (
    "ghcr.io/docling-project/docling-serve-cpu@sha256:"
    "56f12f30f672272a557beb191be6855ba76478e91bac94e8cdedd52d2c8d9da2"
)
TOTAL_DEADLINE_SECONDS = 300


def existing_ancestor(path):
    while not path.exists():
        path = path.parent
    return path


def require_external_data(path, parser):
    ancestor = existing_ancestor(path)
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ancestor if ancestor.is_dir() else ancestor.parent),
            "rev-parse",
            "--is-inside-work-tree",
            "--is-inside-git-dir",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if "true" in result.stdout.splitlines():
        parser.error("experiment inputs and outputs must be outside Git directories")


def text_output(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--format", choices=("csv", "pdf", "all"), default="all")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--table-mode", choices=("fast", "accurate"), default="fast")
    args = parser.parse_args()
    os.umask(0o077)
    inputs, output = (path.resolve() for path in (args.inputs, args.output))
    for path in (inputs, output):
        require_external_data(path, parser)
    if inputs == output or inputs in output.parents or output in inputs.parents:
        parser.error("input and output directories must be separate")
    manifest_path = inputs / "manifest.json"
    if not manifest_path.is_file():
        parser.error("inputs must contain the shared experiment manifest")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("output must be empty; choose a new directory")
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if inspected.returncode:
        parser.error("the pinned CPU image must already exist locally")
    source = Path(__file__).resolve().parent / "convert.py"
    source_bytes = source.read_bytes()
    source_snapshot = output / "converter-source.py"
    source_snapshot.write_bytes(source_bytes)
    source_snapshot.chmod(0o600)
    launcher_bytes = Path(__file__).read_bytes()
    launcher_snapshot = output / "launcher-source.py"
    launcher_snapshot.write_bytes(launcher_bytes)
    launcher_snapshot.chmod(0o600)
    name = "depositarai-docling-extract-" + uuid.uuid4().hex[:12]
    command = [
        "docker",
        "run",
        "--pull",
        "never",
        "--rm",
        "--name",
        name,
        "--runtime",
        "runc",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--pids-limit",
        "128",
        "--label",
        "depositarai.experiment=document-extraction",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
    ]
    for key, value in {
        "CUDA_VISIBLE_DEVICES": "-1",
        "NVIDIA_VISIBLE_DEVICES": "void",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
        "PYTHONDONTWRITEBYTECODE": "1",
    }.items():
        command.extend(["--env", f"{key}={value}"])
    command.extend(
        [
            "--mount",
            f"type=bind,src={source_snapshot},dst=/experiment/convert.py,readonly",
            "--mount",
            f"type=bind,src={inputs},dst=/inputs,readonly",
            "--mount",
            f"type=bind,src={output},dst=/output",
            "--entrypoint",
            "python",
            IMAGE,
            "-B",
            "/experiment/convert.py",
            "--inputs",
            "/inputs",
            "--output",
            "/output",
            "--format",
            args.format,
            "--table-mode",
            args.table_mode,
        ]
    )
    if args.ocr:
        command.append("--ocr")
    for case in args.case:
        command.extend(["--case", case])
    started = time.monotonic()
    timed_out = False
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=TOTAL_DEADLINE_SECONDS
        )
        exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        exit_code = 124
        stdout, stderr = text_output(error.stdout), text_output(error.stderr)
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            capture_output=True,
            check=False,
            timeout=15,
        )
    execution = {
        "image": IMAGE,
        "image_id": inspected.stdout.strip(),
        "convert_script_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "launcher_script_sha256": hashlib.sha256(launcher_bytes).hexdigest(),
        "input_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "docker_wall_seconds": round(time.monotonic() - started, 6),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "total_deadline_seconds": TOTAL_DEADLINE_SECONDS,
        "limits": {
            "cpus": 2,
            "memory_bytes": 4 * 1024**3,
            "network": "none",
            "gpu": False,
        },
    }
    (output / "process.log").write_text(stdout + stderr, encoding="utf-8")
    (output / "execution.json").write_text(
        json.dumps(execution, indent=2) + "\n", encoding="utf-8"
    )
    for path in output.iterdir():
        if path.is_file():
            path.chmod(0o600)
    print(json.dumps(execution))
    if exit_code:
        raise SystemExit(exit_code)
    if not (output / "report.json").is_file():
        raise SystemExit("Docling conversion process did not produce its report")


if __name__ == "__main__":
    main()
