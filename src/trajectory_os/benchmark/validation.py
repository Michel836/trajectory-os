"""M029 — deterministic validation gate execution (no Git, bounded).

The validation gate is the operator-owned command declared by the workload.
It runs in the isolated workspace with a bounded timeout. The result is
recorded verbatim (exit code, wall time, output digest); an unavailable or
timed-out gate never counts as a pass.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

from trajectory_os.benchmark import model


def substitute(command: Sequence[str], *,
               python: str | None = None) -> tuple[str, ...]:
    """Replace the ``{python}`` placeholder (same interpreter as the gate)."""
    interpreter = python or sys.executable
    return tuple(part.replace("{python}", interpreter) for part in command)


ParserRunner = Callable[
    [Sequence[str], str, int], tuple[int | None, str, str, bool]]


def _default_runner(
    argv: Sequence[str], cwd: str, timeout_s: int,
) -> tuple[int | None, str, str, bool]:
    try:
        proc = subprocess.run(  # noqa: S603 - command is operator-fixed
            list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            timeout=max(1, timeout_s), check=False)
    except subprocess.TimeoutExpired:
        return (None, "", "", True)
    except (OSError, subprocess.SubprocessError) as exc:
        return (None, "", f"{type(exc).__name__}: {exc}", False)
    return (int(proc.returncode), proc.stdout, proc.stderr, False)


def run_validation(
    command: Sequence[str],
    workspace: str,
    *,
    timeout_s: int = 300,
    python: str | None = None,
    runner: ParserRunner | None = None,
) -> model.ValidationOutcome:
    """Run one bounded validation gate and classify it (fail closed)."""
    argv = substitute(command, python=python)
    started = time.monotonic()
    execute = runner or _default_runner
    exit_code, stdout, stderr, timed_out = execute(argv, workspace, timeout_s)
    duration_ms = int((time.monotonic() - started) * 1000)
    combined = (stdout + stderr).encode("utf-8", errors="replace")
    output_sha256 = hashlib.sha256(combined).hexdigest() if combined else None
    passed = exit_code == 0 and not timed_out
    if timed_out:
        reason = model.R_TIMEOUT
    elif passed:
        reason = model.R_OK
    else:
        reason = model.R_VALIDATION_FAILED
    return model.ValidationOutcome(
        command=argv, passed=passed, exit_code=exit_code,
        duration_ms=duration_ms, timed_out=timed_out,
        output_sha256=output_sha256, reason=reason)
