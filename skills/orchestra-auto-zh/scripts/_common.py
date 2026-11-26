#!/usr/bin/env python3
"""Shared standard-library helpers for the orchestra-auto scripts.

Keeping CLI resolution, sibling loading, UTC timestamp parsing, directory fsyncs,
subprocess execution, and JSON output in one place avoids drifting copies across the
standalone scripts without turning the directory into a Python package.
"""

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

#: Python 3.10 is the supported floor and has no ``datetime.UTC`` alias.
UTC = timezone.utc  # noqa: UP017
RFC3339_UTC = "%Y-%m-%dT%H:%M:%SZ"

CLI_ENV = "ORCA_CLI_COMMAND"
DEFAULT_CLI = "orca"
CLI_TIMEOUT_SECONDS = 30
REMOTE_ENVIRONMENT_ENVS = ("ORCA_ENVIRONMENT", "ORCA_PAIRING_CODE")

_SIBLING_CACHE = {}


def resolve_orca_cli():
    """Follow the version-matched Orca discovery stub without fallback."""
    if CLI_ENV in os.environ:
        return os.environ[CLI_ENV]
    if os.environ.get("ORCA_DEV_REPO_ROOT"):
        return "orca-dev"
    if sys.platform.startswith("linux") and not (
            os.environ.get("ORCA_TERMINAL_HANDLE") or os.environ.get("ORCA_AGENT_SESSION_ID")):
        return "orca-ide"
    return DEFAULT_CLI


def has_remote_environment_override(env=None):
    """Return True when ``ORCA_ENVIRONMENT`` or ``ORCA_PAIRING_CODE`` is non-empty."""
    source = os.environ if env is None else env
    return any(isinstance(source.get(key), str) and bool(source.get(key, "").strip())
               for key in REMOTE_ENVIRONMENT_ENVS)


def _has_environment_or_pairing_flag(args):
    return any(
        arg == "--environment" or arg.startswith("--environment=")
        or arg == "--pairing-code" or arg.startswith("--pairing-code=")
        for arg in args
    )


def _is_non_retargetable_orca_command(subargs):
    if not subargs:
        return True
    if any(arg in ("--version", "-v", "--help", "-h") for arg in subargs):
        return True
    return tuple(subargs[:2]) == ("agent", "hooks")


def with_local_environment(argv, *, has_executable=True, env=None):
    """Append ``--environment local`` when remote env vars would retarget local Orca."""
    args = list(argv)
    if not has_remote_environment_override(env):
        return args
    subargs = args[1:] if has_executable else args
    if _has_environment_or_pairing_flag(subargs) or _is_non_retargetable_orca_command(subargs):
        return args
    return [*args, "--environment", "local"]


def utc_now():
    """Return the current UTC instant in RFC3339 ``Z`` form."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value):
    """Return an aware UTC ``datetime`` for an RFC3339 string, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def timestamp_text(moment):
    """Format an aware ``datetime`` as RFC3339 UTC ``Z`` text."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_sibling(base_file, filename, alias=None):
    """Import a sibling script by path without modifying ``sys.path``."""
    path = Path(base_file).resolve().with_name(filename)
    key = (str(path), alias or filename)
    cached = _SIBLING_CACHE.get(key)
    if cached is not None:
        return cached
    module_name = alias or ("orca_sibling_" + path.stem.replace("-", "_"))
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SIBLING_CACHE[key] = module
    return module


def fsync_directory(directory):
    """Flush directory metadata so atomic renames and creates survive crashes."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_command(argv, *, timeout=CLI_TIMEOUT_SECONDS, cwd=None):
    """Run one subprocess without shell interpolation and return normalized output."""
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"returncode": 127, "stdout": "", "stderr": str(error)}
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def json_object(text):
    """Parse ``text`` as a JSON dict, returning ``None`` on error or non-dict values."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def emit_json(payload, *, compact=False, indent=None):
    """Print JSON to stdout; ``compact=True`` minimizes whitespace for token savings."""
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    elif indent is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=indent))
    else:
        print(json.dumps(payload, ensure_ascii=False))
