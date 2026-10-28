"""Compile a reflection-only probe and run the pinned Fess jars in isolation."""

import argparse
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid


IMAGE = "ghcr.io/codelibs/fess:15.8.0@sha256:442a58831e87aae6ae22ef60e0536ececbda553bd5f5e8e5dc98cc022578dc75"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if not shutil.which("javac"):
        parser.error(
            "the experiment requires an existing host javac; it never installs one"
        )
    inputs, output, scratch = [
        path.resolve() for path in (args.inputs, args.output, args.scratch)
    ]
    for left, right in itertools.combinations((inputs, output, scratch), 2):
        if left == right or left in right.parents or right in left.parents:
            parser.error("input, output and scratch directories must not overlap")
    if not (inputs / "manifest.json").is_file():
        parser.error("inputs must contain the shared experiment manifest")
    for directory in (output, scratch):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (inputs, output, scratch):
        in_git = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if in_git.returncode == 0 and in_git.stdout.strip() == "true":
            parser.error("inputs, output and scratch must be outside Git worktrees")
    if (output / "report.json").exists():
        parser.error("output already contains a report; choose a new directory")
    source = Path(__file__).resolve().parent
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
        text=True,
        timeout=15,
    ).strip()
    subprocess.run(
        ["javac", "-d", str(scratch), str(source / "ExtractProbe.java")],
        check=True,
        timeout=30,
    )
    name = "depositarai-fess-extract-" + uuid.uuid4().hex[:12]
    command = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        name,
        "--runtime",
        "runc",
        "--network",
        "none",
        "--read-only",
        "--hostname",
        "fess-extract",
        "--add-host",
        "fess-extract:127.0.0.1",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--pids-limit",
        "96",
        "--label",
        "depositarai.experiment=document-extraction",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
        "--env",
        "CUDA_VISIBLE_DEVICES=-1",
        "--env",
        "NVIDIA_VISIBLE_DEVICES=void",
        "--mount",
        f"type=bind,src={scratch},dst=/probe,readonly",
        "--mount",
        f"type=bind,src={inputs},dst=/inputs,readonly",
        "--mount",
        f"type=bind,src={output},dst=/output",
        "--entrypoint",
        "/opt/java/openjdk/bin/java",
        IMAGE,
        "-Xmx1280m",
        "-XX:ActiveProcessorCount=2",
        "-Djava.awt.headless=true",
        "-Duser.home=/tmp",
        "-Dlog4j2.disable.jmx=true",
        "-cp",
        "/probe:/usr/share/fess/app/WEB-INF/lib/*",
        "ExtractProbe",
        "/inputs",
        "/output",
    ]
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            capture_output=True,
            check=False,
            timeout=10,
        )
    (output / "process.log").write_text(result.stdout + result.stderr)
    execution = {
        "image": IMAGE,
        "image_id": image_id,
        "docker_wall_ms": round((time.monotonic() - started) * 1000, 2),
        "exit_code": result.returncode,
        "limits": {
            "cpus": 2,
            "memory_bytes": 2 * 1024**3,
            "network": "none",
            "gpu": False,
        },
    }
    (output / "execution.json").write_text(json.dumps(execution, indent=2) + "\n")
    for path in output.iterdir():
        if path.is_file():
            path.chmod(0o600)
    print(json.dumps(execution))
    if result.returncode:
        raise SystemExit(result.returncode)
    if not (output / "report.json").is_file():
        raise SystemExit("Fess extraction process did not produce its report")


if __name__ == "__main__":
    main()
