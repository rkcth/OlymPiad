"""Pinned, user-local Pi installation; no global npm or system changes."""

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

PI_PACKAGE = "@earendil-works/pi-coding-agent"
PI_VERSION = "1.4.0"
MIN_NODE = (22, 19, 0)
ASSETS = Path(__file__).resolve().parents[1]


def data_directory():
    override = os.environ.get("CODING_BENCHMARK_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodingBenchmark"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/CodingBenchmark"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "coding-benchmark"


def probe(command, timeout=15):
    return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def node_command():
    node = shutil.which("node")
    if not node:
        raise ValueError("Node.js is missing. Install Node.js 22.19+ from https://nodejs.org/ and reopen the terminal.")
    result = probe([node, "--version"])
    try:
        version = tuple(int(x) for x in result.stdout.strip().lstrip("v").split(".")[:3])
    except ValueError:
        raise ValueError("Could not determine Node.js version; install Node.js 22.19+.") from None
    if result.returncode or version < MIN_NODE:
        raise ValueError("Pi requires Node.js 22.19 or newer. Update Node.js from https://nodejs.org/.")
    return node


def npm_command(node):
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("npm is missing. Reinstall Node.js with npm enabled: https://nodejs.org/.")
    # Avoid cmd.exe shims entirely on Windows; all paths remain literal argv.
    path = Path(npm).resolve()
    candidates = [path, Path(node).resolve().parent / "node_modules/npm/bin/npm-cli.js",
                  path.parent / "node_modules/npm/bin/npm-cli.js",
                  path.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"]
    for candidate in candidates:
        if candidate.is_file() and candidate.name == "npm-cli.js":
            return [node, str(candidate)]
    if os.name != "nt":
        return [npm]
    raise ValueError("Cannot find npm-cli.js beside Node.js. Repair the official Node.js/npm installation.")


def pi_command():
    node = node_command()
    root = data_directory() / "pi"
    package = root / "node_modules" / PI_PACKAGE
    try:
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
        original_lock = (ASSETS / "pi-runtime/package-lock.json").read_bytes()
        if metadata.get("name") != PI_PACKAGE or metadata.get("version") != PI_VERSION:
            raise ValueError("Wrong managed Pi version")
        if (root / "package-lock.json").read_bytes() != original_lock:
            raise ValueError("Managed Pi lock differs from the packaged lock")
        cli = package / "dist/bundle/cli.js"
        if not cli.is_file():
            raise ValueError("Managed Pi entrypoint is missing")
    except (OSError, ValueError):
        raise ValueError(f"Managed Pi {PI_VERSION} is missing or changed. Run: coding-benchmark setup --install-pi") from None
    return [node, str(cli)]


def install_pi():
    node = node_command()
    npm = npm_command(node)
    root = data_directory() / "pi"
    root.mkdir(parents=True, exist_ok=True)
    # npm ci verifies the shipped lock integrity, including transitive versions.
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(ASSETS / "pi-runtime" / name, root / name)
    result = probe(npm + ["ci", "--prefix", str(root), "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund"], timeout=600)
    if result.returncode:
        raise ValueError("Pi installation failed. Check npm registry/network access and free disk space. " + result.stdout[-1500:])
    command = pi_command()
    result = probe(command + ["--version"], timeout=30)
    if result.returncode or result.stdout.strip() != PI_VERSION:
        raise ValueError("Installed Pi failed its version check; run doctor for prerequisites.")
    return {"version": PI_VERSION, "directory": str(root)}


def bash_command():
    if os.name == "nt":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(key)
            if base:
                candidate = Path(base) / "Git/bin/bash.exe"
                if candidate.is_file():
                    return str(candidate)
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parent.parent / "bin/bash.exe"
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash")


def evaluator_python(requested=None):
    candidates = [requested] if requested else [shutil.which("python3.11"), sys.executable]
    if not requested and os.name == "nt" and shutil.which("py"):
        try:
            candidate = probe([shutil.which("py"), "-3.11", "-c", "import sys;print(sys.executable)"])
            if candidate.returncode == 0:
                candidates.insert(0, candidate.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    for candidate in candidates:
        if not candidate:
            continue
        try:
            result = probe([str(candidate), "-c", "import json,sys,venv;print(json.dumps(list(sys.version_info[:2])))"])
            version = json.loads(result.stdout)
            if result.returncode == 0 and tuple(version) == (3, 11):
                return str(candidate)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    raise ValueError("The locked Django environment requires Python 3.11 with venv. Install Python 3.11; pass --python /path/to/python3.11 if not detected. On Debian/Ubuntu also install python3.11-venv.")


def python_lock_files():
    root = ASSETS / "python-runtime"
    try:
        metadata = json.loads((root / "sources.json").read_text(encoding="utf-8"))
        paths = [root / "bootstrap.txt", root / "requirements.txt"]
        for path in paths:
            if hashlib.sha256(path.read_bytes()).hexdigest() != metadata["files"][path.name]:
                raise ValueError("Changed Python dependency lock")
    except (OSError, ValueError, KeyError):
        raise ValueError("Packaged Python dependency lock is missing or changed. Reinstall the benchmark package.") from None
    return paths
