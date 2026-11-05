"""Read-only first-run diagnostics, also enforced before task preflight."""

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

try:
    from .runtime import bash_command, evaluator_python, node_command, pi_command, probe, PI_VERSION, python_lock_files
except ImportError:
    from runtime import bash_command, evaluator_python, node_command, pi_command, probe, PI_VERSION, python_lock_files


def inspect_environment(output_root, python=None, need_pi=True):
    checks = []

    def add(name, status, message):
        checks.append({"name": name, "status": status, "message": message})

    add("host", "ok", f"{platform.system()} {platform.release()} / {platform.machine()} / Python {platform.python_version()}")
    for name, command in [("Git", shutil.which("git")), ("Bash", bash_command())]:
        if not command:
            add(name, "error", f"{name} is missing. " + ("Install Git for Windows including Git Bash: https://git-scm.com/downloads/win" if os.name == "nt" else "Install Git and Bash using your OS package manager."))
            continue
        try:
            result = probe([command, "--version"])
            add(name, "ok" if result.returncode == 0 else "error", result.stdout.splitlines()[0] if result.stdout else f"{name} cannot run")
        except (OSError, subprocess.SubprocessError):
            add(name, "error", f"{name} could not run. Repair its installation and reopen the terminal.")
    if sys.version_info < (3, 11):
        add("Controller Python", "error", "Python 3.11 or newer is required; install this package in a Python 3.11+ virtualenv.")
    try:
        python_lock_files()
        add("Python dependency lock", "ok", "Packaged bootstrap and runtime requirement hashes match")
    except ValueError as exc:
        add("Python dependency lock", "error", str(exc))
    resolved_python = None
    try:
        resolved_python = evaluator_python(python)
        add("Evaluator Python", "ok", resolved_python)
    except ValueError as exc:
        add("Evaluator Python", "error", str(exc))
    docker = shutil.which("docker")
    if not docker:
        add("Docker", "error", "Docker is not installed or is not on PATH. macOS/Windows: install and start Docker Desktop (Linux containers). Linux: install/start Docker Engine. https://docs.docker.com/get-started/get-docker/")
    else:
        try:
            result = probe([docker, "info", "--format", "{{json .}}"], timeout=20)
            if result.returncode:
                hint = "Your user cannot access the Docker socket; follow Docker's Linux post-install permissions instructions." if "permission denied" in result.stdout.lower() else "Start Docker Desktop / Docker Engine and check your selected Docker context."
                add("Docker", "error", "Docker is installed but the engine is unavailable. " + hint)
            else:
                info = json.loads(result.stdout)
                if info.get("OSType") != "linux":
                    add("Docker", "error", "Switch Docker Desktop to Linux containers; Windows-container mode cannot run this suite.")
                else:
                    add("Docker", "ok", "Linux engine " + str(info.get("ServerVersion", "unknown")))
                    if info.get("MemTotal", 0) and info["MemTotal"] < 8 * 1024**3:
                        add("Docker memory", "warning", "Less than 8 GiB available to Docker; reduce --agents or increase Docker memory if evaluation fails.")
        except (OSError, ValueError, subprocess.SubprocessError):
            add("Docker", "error", "Docker engine check failed/timed out. Start Docker and run docker info to diagnose it.")
    if need_pi:
        try:
            node = node_command()
            add("Node.js", "ok", probe([node, "--version"]).stdout.strip())
            command = pi_command()
            result = probe(command + ["--version"], timeout=30)
            if result.returncode or result.stdout.strip() != PI_VERSION:
                raise ValueError("Managed Pi version check failed. Run olympiad setup --install-pi.")
            add("Pi", "ok", "Pinned managed Pi " + PI_VERSION)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            add("Pi/Node.js", "error", str(exc))
    root = Path(output_root).expanduser().resolve()
    try:
        parent = root
        while not parent.exists():
            parent = parent.parent
        with tempfile.TemporaryFile(dir=parent):
            pass
        if root.exists() and not root.is_dir():
            raise OSError("output root is a file")
        add("Output", "ok", str(root))
        if shutil.disk_usage(parent).free < 30 * 1024**3:
            add("Disk space", "warning", "Less than 30 GiB free near output; evaluator images/clones/builds need substantial extra space, including Docker's separate disk allocation.")
    except OSError:
        add("Output", "error", "Output location is not writable. Choose a writable --output-root directory.")
    if platform.machine().lower() in {"arm64", "aarch64"}:
        add("Architecture", "warning", "Evaluator images are linux/amd64. Docker needs x86-64 emulation; performance differs and native ARM execution is not certified.")
    if os.name == "nt":
        add("Windows paths", "warning", "Use a short writable output path such as C:\\bench\\results. Git Bash is required; WSL2 is also available as a Linux workflow.")
    return {"ready": not any(c["status"] == "error" for c in checks), "checks": checks,
            "python": resolved_python, "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()}}
