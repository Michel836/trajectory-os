from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "local-ai-profile"


def write_fake_nvidia_smi(tmp_path: Path, *, name: str, vram_mib: int) -> Path:
    path = tmp_path / "fake-nvidia-smi"
    path.write_text(f"#!/usr/bin/env bash\necho '{name}, {vram_mib}'\nexit 0\n")
    path.chmod(0o755)
    return path


def write_failing_nvidia_smi(tmp_path: Path, stderr: str) -> Path:
    path = tmp_path / "fake-nvidia-smi-fail"
    # The single-quoted body is echoed to stderr verbatim.
    path.write_text(
        f"#!/usr/bin/env bash\necho '{stderr}' >&2\nexit 9\n"
    )
    path.chmod(0o755)
    return path


def write_malformed_nvidia_smi(tmp_path: Path, line: str) -> Path:
    path = tmp_path / "fake-nvidia-smi-bad"
    path.write_text(f"#!/usr/bin/env bash\necho '{line}'\nexit 0\n")
    path.chmod(0o755)
    return path


def write_multi_gpu_smi(
    tmp_path: Path,
    lines: list[str],
    *,
    name: str = "fake-nvidia-smi-multi",
) -> Path:
    """Fake ``nvidia-smi`` emitting one CSV row per line (multi-GPU host).

    ``name`` selects the fixture filename so tests that create multiple
    fixtures keep each one in its own file (no silent overwrite).
    """
    path = tmp_path / name
    body = "\n".join(f"echo '{line}'" for line in lines)
    path.write_text(f"#!/usr/bin/env bash\n{body}\nexit 0\n")
    path.chmod(0o755)
    return path


def write_meminfo(tmp_path: Path, total_kb: int | None) -> Path:
    path = tmp_path / "meminfo"
    if total_kb is None:
        path.write_text("MemFree:            1024 kB\nBuffers:                     8 kB\n")
    else:
        path.write_text(f"MemTotal:       {total_kb:16d} kB\nMemFree:           1024 kB\n")
    return path


def run_profile(
    nvidia_smi: str,
    meminfo: Path,
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["TRAJECTORY_NVIDIA_SMI"] = nvidia_smi
    env["TRAJECTORY_MEMINFO_FILE"] = str(meminfo)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd) if cwd is not None else None,
    )


def list_tree(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
    )


def test_recommends_validated_rtx3090_class_with_sufficient_ram(tmp_path: Path) -> None:
    smi = write_fake_nvidia_smi(tmp_path, name="NVIDIA GeForce RTX 3090", vram_mib=24576)
    meminfo = write_meminfo(tmp_path, total_kb=64 * 1024 * 1024)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Recommendation: qwen3.8-dev3090" in result.stdout
    assert "GPU: NVIDIA GeForce RTX 3090" in result.stdout
    assert "VRAM: 24.0 GiB" in result.stdout
    assert "System RAM: 64.0 GiB" in result.stdout


def test_rejects_nvidia_host_below_required_vram(tmp_path: Path) -> None:
    smi = write_fake_nvidia_smi(tmp_path, name="NVIDIA GeForce RTX 3060", vram_mib=12288)
    meminfo = write_meminfo(tmp_path, total_kb=64 * 1024 * 1024)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0
    assert "Recommendation: no validated recommendation" in result.stdout
    assert "VRAM 12.0 GiB is below the 24 GiB" in result.stdout


def test_no_nvidia_smi_reports_unavailable_gpu(tmp_path: Path) -> None:
    meminfo = write_meminfo(tmp_path, total_kb=64 * 1024 * 1024)

    result = run_profile("/nonexistent/nvidia-smi", meminfo)

    assert result.returncode == 0
    assert "GPU: unavailable" in result.stdout
    assert "Recommendation: no validated recommendation" in result.stdout
    assert "nvidia-smi unavailable" in result.stdout


def test_failing_nvidia_smi_is_handled_gracefully(tmp_path: Path) -> None:
    smi = write_failing_nvidia_smi(tmp_path, stderr="driver error")
    meminfo = write_meminfo(tmp_path, total_kb=64 * 1024 * 1024)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0
    assert "GPU: unavailable" in result.stdout
    assert "Recommendation: no validated recommendation" in result.stdout
    assert "driver error" in result.stdout


def test_malformed_nvidia_smi_is_handled_gracefully(tmp_path: Path) -> None:
    smi = write_malformed_nvidia_smi(tmp_path, line="garbage without fields")
    meminfo = write_meminfo(tmp_path, total_kb=64 * 1024 * 1024)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0
    assert "GPU: unavailable" in result.stdout
    assert "malformed nvidia-smi output" in result.stdout
    assert "Recommendation: no validated recommendation" in result.stdout


def test_rejects_insufficient_system_ram(tmp_path: Path) -> None:
    smi = write_fake_nvidia_smi(tmp_path, name="NVIDIA GeForce RTX 3090", vram_mib=24576)
    # 32 GiB is a separate workstation class, below the conservative
    # 60 GiB Linux-visible floor, and has not been validated.
    meminfo = write_meminfo(tmp_path, total_kb=32 * 1024 * 1024)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0
    assert "Recommendation: no validated recommendation" in result.stdout
    assert "system RAM 32.0 GiB is below the 60 GiB Linux-visible MemTotal floor" in result.stdout
    assert "64 GiB installed" in result.stdout


def test_recommends_real_reference_host_memtotal(tmp_path: Path) -> None:
    """Regression: the real validated host reports 65660776 kB (~62.62 GiB)
    visible in /proc/meminfo, not an artificial exact 64.00 GiB."""
    smi = write_fake_nvidia_smi(tmp_path, name="NVIDIA GeForce RTX 3090", vram_mib=24576)
    observed_memtotal_kb = 65660776  # real reference host, 64 GiB installed class
    meminfo = write_meminfo(tmp_path, total_kb=observed_memtotal_kb)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Recommendation: qwen3.8-dev3090" in result.stdout
    assert "System RAM: 62.62 GiB" in result.stdout
    assert "64 GiB installed" in result.stdout


def test_multi_gpu_smaller_first_selects_validated_24gib_gpu(tmp_path: Path) -> None:
    """A validated >=24 GiB GPU appearing after a smaller GPU must not be ignored."""
    smi = write_multi_gpu_smi(
        tmp_path,
        [
            "NVIDIA GeForce RTX 3060, 12288",
            "NVIDIA GeForce RTX 3090, 24576",
        ],
    )
    meminfo = write_meminfo(tmp_path, total_kb=65660776)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Recommendation: qwen3.8-dev3090" in result.stdout
    assert "GPU: NVIDIA GeForce RTX 3090" in result.stdout
    assert "VRAM: 24.0 GiB" in result.stdout


def test_multi_gpu_detection_is_deterministic_across_gpu_order(tmp_path: Path) -> None:
    smaller = "NVIDIA GeForce RTX 3060, 12288"
    larger = "NVIDIA GeForce RTX 3090, 24576"
    first = write_multi_gpu_smi(tmp_path, [smaller, larger], name="fake-nvidia-smi-multi-a")
    second = write_multi_gpu_smi(tmp_path, [larger, smaller], name="fake-nvidia-smi-multi-b")
    meminfo = write_meminfo(tmp_path, total_kb=65660776)

    # Regression guard: the two fixtures must be distinct files with distinct
    # contents, so the order-independence assertion below is not vacuous.
    assert first != second
    first_text = first.read_text()
    second_text = second.read_text()
    assert first_text != second_text
    # Each fixture lists the two GPUs in opposite order.
    assert [line for line in first_text.splitlines() if "echo" in line] == [
        f"echo '{smaller}'",
        f"echo '{larger}'",
    ]
    assert [line for line in second_text.splitlines() if "echo" in line] == [
        f"echo '{larger}'",
        f"echo '{smaller}'",
    ]

    result_first = run_profile(str(first), meminfo)
    result_second = run_profile(str(second), meminfo)

    assert result_first.returncode == 0
    assert result_second.returncode == 0
    # Same selected GPU and byte-for-byte identical advisory output regardless
    # of the order in which the GPUs are reported.
    assert "GPU: NVIDIA GeForce RTX 3090" in result_first.stdout
    assert result_first.stdout == result_second.stdout
    assert result_first.stderr == result_second.stderr


def test_multi_gpu_prefers_greatest_vram_among_valid_rows(tmp_path: Path) -> None:
    """Ties/other valid rows: the greatest total VRAM deterministically wins."""
    smi = write_multi_gpu_smi(
        tmp_path,
        [
            "NVIDIA T4, 15360",
            "NVIDIA A100, 81920",
            "NVIDIA GeForce RTX 3090, 24576",
        ],
    )
    meminfo = write_meminfo(tmp_path, total_kb=65660776)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GPU: NVIDIA A100" in result.stdout
    assert "VRAM: 80.0 GiB" in result.stdout
    assert "Recommendation: qwen3.8-dev3090" in result.stdout


def test_multi_gpu_malformed_row_does_not_hide_valid_row(tmp_path: Path) -> None:
    """Malformed rows are skipped safely; a valid row is still selected."""
    smi = write_multi_gpu_smi(
        tmp_path,
        [
            "garbage without fields",
            "NVIDIA GeForce RTX 3090, 24576",
        ],
    )
    meminfo = write_meminfo(tmp_path, total_kb=65660776)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GPU: NVIDIA GeForce RTX 3090" in result.stdout
    assert "Recommendation: qwen3.8-dev3090" in result.stdout


def test_multi_gpu_all_rows_invalid_reports_no_recommendation(tmp_path: Path) -> None:
    """No valid GPU rows at all => handled safely, no crash, no validation claim."""
    smi = write_multi_gpu_smi(
        tmp_path,
        [
            "garbage without fields",
            "NVIDIA GeForce RTX 3090, not-a-number",
        ],
    )
    meminfo = write_meminfo(tmp_path, total_kb=65660776)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0
    assert "GPU: unavailable" in result.stdout
    assert "malformed nvidia-smi output" in result.stdout
    assert "Recommendation: no validated recommendation" in result.stdout


def test_rejects_missing_meminfo_file(tmp_path: Path) -> None:
    smi = write_fake_nvidia_smi(tmp_path, name="NVIDIA GeForce RTX 3090", vram_mib=24576)
    missing = tmp_path / "does-not-exist"

    result = run_profile(str(smi), missing)

    assert result.returncode == 0
    assert "System RAM: unavailable" in result.stdout
    assert "Recommendation: no validated recommendation" in result.stdout
    assert "cannot read memory summary" in result.stdout


def test_rejects_malformed_meminfo_content(tmp_path: Path) -> None:
    smi = write_fake_nvidia_smi(tmp_path, name="NVIDIA GeForce RTX 3090", vram_mib=24576)
    meminfo = write_meminfo(tmp_path, total_kb=None)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0
    assert "System RAM: unavailable" in result.stdout
    assert "memory summary missing MemTotal" in result.stdout
    assert "Recommendation: no validated recommendation" in result.stdout


def test_output_is_deterministic(tmp_path: Path) -> None:
    smi = write_fake_nvidia_smi(tmp_path, name="NVIDIA GeForce RTX 3090", vram_mib=24576)
    meminfo = write_meminfo(tmp_path, total_kb=64 * 1024 * 1024)

    first = run_profile(str(smi), meminfo)
    second = run_profile(str(smi), meminfo)

    assert first.returncode == 0
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr


def test_is_read_only_and_modifies_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".pi").mkdir()
    (home / ".pi" / "existing").write_text("pre-existing\n")

    cwd = tmp_path / "cwd"
    cwd.mkdir()

    smi = write_fake_nvidia_smi(
        tmp_path, name="NVIDIA GeForce RTX 3090", vram_mib=24576
    )
    meminfo = write_meminfo(tmp_path, total_kb=64 * 1024 * 1024)

    home_before = list_tree(home)

    result = run_profile(
        str(smi),
        meminfo,
        cwd=cwd,
        extra_env={"HOME": str(home)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # HOME (including ~/.pi) and the working directory are byte-for-byte untouched
    # in terms of file membership, and the pre-existing file is unchanged.
    assert list_tree(home) == home_before
    assert (home / ".pi" / "existing").read_text() == "pre-existing\n"
    assert list_tree(cwd) == []
    # stdout is purely advisory and states it changes nothing.
    assert "read-only" in result.stdout
    assert "does not change any configuration." in result.stdout


def test_reason_mentions_all_detected_components(tmp_path: Path) -> None:
    smi = write_fake_nvidia_smi(tmp_path, name="NVIDIA GeForce RTX 3090", vram_mib=24576)
    meminfo = write_meminfo(tmp_path, total_kb=64 * 1024 * 1024)

    result = run_profile(str(smi), meminfo)

    assert result.returncode == 0
    # The output explicitly explains GPU, VRAM, RAM, recommendation and reason.
    for token in ("GPU:", "VRAM:", "System RAM:", "Recommendation:", "Reason:"):
        assert token in result.stdout
