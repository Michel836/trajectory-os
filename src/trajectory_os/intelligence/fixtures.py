"""M063 — deterministic synthetic canonical runs for acceptance testing.

These fixtures are **not** real Trajectory_OS history. They are generated in a
temporary directory with the same canonical run-directory layout
(``meta.txt`` / ``lifecycle.json`` / ``status.log`` / ``worktree.patch`` /
``worktree-numstat.txt``) so the M056 extractor and the M057/M058 pipeline can
be exercised deterministically in CI, where the real ``.trajectory-pi/runs``
history is not available.

Every observation produced from these fixtures is labelled ``FIXTURE`` by the
dataset builder; they are never presented as measured production history.

The generated signal is intentionally obvious (one route succeeds much more
often than another, duration grows with task class) so the acceptance matrix
can assert that the learning machinery actually learns, without claiming any
real-world effect size.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from trajectory_os.intelligence import model

#: Models used by the synthetic history.
FIXTURE_MODELS: tuple[tuple[str, float], ...] = (
    ("qwen3.8-dev3090", 0.35),
    ("deepseek/deepseek-flash", 0.70),
)

FIXTURE_CLASSES: tuple[str, ...] = ("smoke", "feature", "complex")

#: Base duration by task class (seconds) — the learnable duration signal.
_DURATION_BASE: dict[str, float] = {
    "smoke": 120.0, "feature": 900.0, "complex": 2400.0,
}


def _draw(*parts: object) -> float:
    material = "|".join(str(part) for part in parts).encode("utf-8")
    value = int(hashlib.sha256(material).hexdigest()[:12], 16)
    return (value % 10_000) / 10_000.0


def _write_run(directory: Path, index: int, *, model: str,
               success_threshold: float, run_class: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    run_id = directory.name
    branch = f"fixture/branch-{index % 17}"
    draw = _draw(run_id, model, index)
    success = draw < success_threshold
    readiness = "READY_FOR_COMMIT" if success else (
        "BLOCKED" if _draw(run_id, "blocked") < 0.2 else "NEEDS_REVIEW")
    validation = "PASS" if success else (
        "FAIL" if _draw(run_id, "validation") < 0.35 else "PASS")
    review_status = "PASS" if success else "REJECTED"
    repairs = 0 if success else (1 if _draw(run_id, "repair") < 0.6 else 2)
    base = _DURATION_BASE[run_class]
    duration = base + _draw(run_id, "duration") * base * 0.5
    changed_files = 1 + int(_draw(run_id, "files") * 12)
    patch_bytes = changed_files * 400 + int(_draw(run_id, "bytes") * 2_000)
    lines = changed_files * 20 + int(_draw(run_id, "lines") * 200)
    gpu_pct = int(_draw(run_id, "gpu") * 99)
    vram_mib = 2_400 + int(_draw(run_id, "vram") * 6_000)
    review_wait = 30.0 + _draw(run_id, "wait") * 600.0

    meta = "\n".join([
        "trajectory_pi_version=fixture-1.0.0",
        f"run_id={run_id}",
        "started_at=2026-01-01T00:00:00+00:00",
        "workspace=/workspace/demo",
        f"branch={branch}",
        f"run_class={run_class}",
        "run_mode=IMPLEMENT",
        "agent_backend=pi",
        f"model={model}",
        "reviewer_model=qwen3.8:27b-q4_K_M",
        f"elapsed_seconds={int(duration)}",
        f"changed_files={changed_files}",
        f"repository_readiness={readiness}",
        "readiness_reason=" + (
            "fixture synthesized outcome" if not success
            else "fixture synthesized success gate"),
        f"validation={validation}",
        f"review_status={review_status}",
        f"repair_attempts_used={repairs}",
        f"final_patch_sha256={_draw(run_id, 'patch')}",
        "",
    ])
    (directory / "meta.txt").write_text(meta, encoding="utf-8")
    lifecycle = (
        '{"ts":"2026-01-01T00:00:00+00:00","mode":"IMPLEMENT",'
        f'"class":"{run_class}","final_state":"COMPLETE",'
        f'"readiness":"{readiness}","exit_code":0,"transitions":5,'
        '"evidence":"lifecycle.jsonl"}\n')
    (directory / "lifecycle.json").write_text(lifecycle, encoding="utf-8")
    states = [
        ("RUNNING", 0.0), ("VALIDATING", base * 0.5),
        ("REVIEWING", base * 0.6),
        ("REPAIRING" if not success else "COMPLETE",
         base * 0.6 + review_wait),
    ]
    lifecycle_lines = []
    for state, offset in states:
        seconds = int(offset)
        stamp = f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}+00:00"
        lifecycle_lines.append(
            f'{{"ts":"{stamp}","state":"{state}","class":"{run_class}",'
            '"mode":"IMPLEMENT"}')
    (directory / "lifecycle.jsonl").write_text(
        "\n".join(lifecycle_lines) + "\n", encoding="utf-8")
    status = (
        f"[00:00:01] elapsed=00:00:01 | phase=IMPLEMENT | "
        f"agent=pi remote active | reviewer=ollama local idle | "
        f"local-gpu=idle {gpu_pct}% {vram_mib}/24576MiB | files=1\n")
    (directory / "status.log").write_text(status, encoding="utf-8")
    (directory / "worktree.patch").write_text(
        "fixture patch bytes\n" * max(1, patch_bytes // 20), encoding="utf-8")
    (directory / "worktree-numstat.txt").write_text(
        f"{lines}\t0\tmodule.py\n", encoding="utf-8")


def generate_fixture_runs(
    root: str | Path, *, count: int = 120,
) -> list[Path]:
    """Generate ``count`` deterministic canonical run directories."""
    if count <= 0 or count > model.MAX_ROWS:
        raise ValueError("count out of range")
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    directories: list[Path] = []
    for index in range(count):
        model_name, threshold = FIXTURE_MODELS[index % len(FIXTURE_MODELS)]
        run_class = FIXTURE_CLASSES[index % len(FIXTURE_CLASSES)]
        directory = base / f"fixture-{index:04d}"
        _write_run(directory, index, model=model_name,
                   success_threshold=threshold, run_class=run_class)
        directories.append(directory)
    return directories


__all__ = [
    "FIXTURE_CLASSES",
    "FIXTURE_MODELS",
    "generate_fixture_runs",
]
