"""Deterministic wrapper tests for scripts/trajectory-pi (Issue #76).

These tests exercise the wrapper in isolated temporary Git repositories
with a fake ``pi`` executable on PATH. They require no real Ollama
server and no real Pi provider, and add no dependencies.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "trajectory-pi"

PROVIDER_PHRASE = "no user query found in messages"
FALLBACK_QUERY = (
    "Execute the task described in the supplied prompt file and return the requested handoff."
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "trajectory-pi-test",
    "GIT_AUTHOR_EMAIL": "tp-test@example.invalid",
    "GIT_COMMITTER_NAME": "trajectory-pi-test",
    "GIT_COMMITTER_EMAIL": "tp-test@example.invalid",
}

# Fake pi reads its scenario (rc / stdout / optionally a file to create)
# from runtime data files so a single script serves every test case.
FAKE_PI_TEMPLATE = """\
#!/usr/bin/env bash
set -u
ctx={ctx}
# The wrapper must invoke this fake pi ONLY as the agent. Every
# invocation records one line so tests can prove no second Pi process
# is ever spawned (the reviewer is a direct Ollama call, not Pi).
printf 'AGENT\\n' >> "$ctx/agent_invocations.log"
printf '%s\\n' "$@" > "$ctx/args.log"
touch_file="$ctx/touch_file"
if [[ -s "$touch_file" ]]; then
    printf 'agent work\\n' > "$(cat "$touch_file")"
fi
if [[ -f "$ctx/pi_output.txt" ]]; then
    cat "$ctx/pi_output.txt"
fi
rc_file="$ctx/rc"
if [[ -f "$rc_file" ]]; then
    rc="$(cat "$rc_file")"
else
    rc=1
fi
exit "$rc"
"""


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass
class TPContext:
    work: Path
    ctx: Path
    args_log: Path
    touched: Path = field(init=False)
    output: Path = field(init=False)
    rc_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.ctx.mkdir(parents=True, exist_ok=True)
        self.rc_file = self.ctx / "rc"
        self.output = self.ctx / "pi_output.txt"
        self.touched = self.ctx / "touch_file"
        self.args_log = self.ctx / "args.log"

    def scenario(self, rc: int, output: str, touch: Path | str | None = None) -> None:
        for stale in (self.args_log, self.touched):
            stale.unlink(missing_ok=True)
        self.rc_file.write_text(str(rc))
        self.output.write_text(output)
        if touch is not None:
            self.touched.write_text(str(touch))

    def reset_scenario_files(self) -> None:
        for stale in (self.args_log, self.rc_file, self.output, self.touched):
            stale.unlink(missing_ok=True)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Do not inherit wrapper-internal bootstrap guard state from the
        # ambient environment (issue #152): it would put the wrapper on
        # the recursion-guard fast path and skip the bootstrap hop.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in (
                "TRAJECTORY_PI_BOOTSTRAP",
                "TRAJECTORY_PI_BOOTSTRAP_COPY",
            )
        }
        env["PATH"] = f"{self.ctx.parent / 'bin'}{os.pathsep}{os.environ['PATH']}"
        return subprocess.run(
            ["bash", str(WRAPPER), *args],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
        )

    def pi_args(self) -> list[str]:
        assert self.args_log.exists(), "fake pi was not invoked"
        lines = self.args_log.read_text().splitlines()
        lines = [line for line in lines if line != ""]
        assert lines, "fake pi argv log is empty"
        return lines

    def latest_meta(self) -> str:
        run_dirs = sorted((self.work / ".trajectory-pi" / "runs").iterdir())
        assert run_dirs, "no run directory was created"
        meta = run_dirs[-1] / "meta.txt"
        assert meta.is_file()
        return meta.read_text()


@pytest.fixture()
def tp(tmp_path: Path) -> TPContext:
    work = tmp_path / "work"
    binary = tmp_path / "bin"
    ctx = tmp_path / "ctx"
    binary.mkdir(parents=True)

    work.mkdir()
    (work / "README.md").write_text("test repository\n")
    # Mirror the repository convention: wrapper run artifacts are ignored.
    (work / ".gitignore").write_text(".trajectory-pi/\n")
    _git(work, "init", "-b", "main")
    _git(work, "add", "README.md", ".gitignore")
    _git(work, "commit", "-m", "initial commit")
    _git(work, "checkout", "-b", "feature/tp-test")

    script = binary / "pi"
    script.write_text(FAKE_PI_TEMPLATE.format(ctx=shlex.quote(str(ctx))))
    script.chmod(0o755)

    return TPContext(work=work, ctx=ctx, args_log=ctx / "args.log")


SMOKE_ARGS = ("--class", "smoke", "--interval", "1", "--no-notify")


# A. Explicit final user query is passed to Pi verbatim.
def test_explicit_final_user_query_is_passed(tp: TPContext) -> None:
    tp.scenario(rc=0, output="")

    result = tp.run(*SMOKE_ARGS, "--", "Implement the requested change.")
    assert result.returncode == 0
    assert tp.pi_args()[-1] == "Implement the requested change."
    meta = tp.latest_meta()
    assert "final_query_source=explicit" in meta


# B. --prompt-file + no explicit query uses the deterministic fallback.
def test_prompt_file_without_query_uses_fallback(tp: TPContext) -> None:
    prompt_file = tp.ctx.parent / "prompt.md"
    prompt_file.write_text("# Task\nDo the thing.\n")
    tp.scenario(rc=0, output="")

    result = tp.run(*(SMOKE_ARGS + ("--prompt-file", str(prompt_file))))
    assert result.returncode == 0
    args = tp.pi_args()
    assert f"@{prompt_file}" in args
    assert args[-1] == FALLBACK_QUERY
    meta = tp.latest_meta()
    assert "final_query_source=fallback" in meta
    # No query text is recorded in metadata.
    assert "Execute the task" not in meta


# C. No prompt-file + no query fails locally before invoking Pi.
def test_no_prompt_file_and_no_query_fails_before_pi(tp: TPContext) -> None:
    tp.scenario(rc=0, output="")

    result = tp.run(*SMOKE_ARGS)
    assert result.returncode == 2
    assert "no usable final user query" in result.stderr
    assert not tp.args_log.exists(), "Pi must not be invoked"
    assert not (tp.work / ".trajectory-pi" / "runs").exists(), "no run artifacts"


# D. Whitespace-only query behaves as missing.
def test_whitespace_only_query_is_treated_as_missing(tp: TPContext) -> None:
    prompt_file = tp.ctx.parent / "prompt.md"
    prompt_file.write_text("# Task\nDo the thing.\n")
    tp.scenario(rc=0, output="")

    # With a prompt file: whitespace-only falls back deterministically.
    result = tp.run(*(SMOKE_ARGS + ("--prompt-file", str(prompt_file), "--", "   \t  ")))
    assert result.returncode == 0
    assert tp.pi_args()[-1] == FALLBACK_QUERY
    assert "final_query_source=fallback" in tp.latest_meta()

    # Without a prompt file: fails locally before invoking Pi.
    tp.reset_scenario_files()
    tp.scenario(rc=0, output="")
    result = tp.run(*SMOKE_ARGS, "--", "   ")
    assert result.returncode == 2
    assert not tp.args_log.exists()


# E. Nonzero Pi + exact provider phrase => explicit upstream classification,
# preserving Pi's exit code and any agent work.
def test_recoverable_provider_failure(tp: TPContext) -> None:
    tp.scenario(rc=7, output=f"500 internal error: {PROVIDER_PHRASE}\n", touch="agent_work.txt")

    result = tp.run(*SMOKE_ARGS, "--", "Build the feature.")
    # Preserve Pi's actual non-zero exit code.
    assert result.returncode == 7
    assert "UPSTREAM_PROVIDER_MISSING_QUERY" in result.stdout
    assert "UPSTREAM PROVIDER MISSING-QUERY FAILURE" in result.stdout
    assert "inspected and resumed" in result.stdout
    assert "classification=UPSTREAM_PROVIDER_MISSING_QUERY" in tp.latest_meta()


# F. Same provider phrase with no changed work remains explicitly upstream.
def test_provider_phrase_without_work_is_agent_failed(tp: TPContext) -> None:
    tp.scenario(rc=5, output=f"500 internal error: {PROVIDER_PHRASE}\n")

    result = tp.run(*SMOKE_ARGS, "--", "Build the feature.")
    assert result.returncode == 5
    assert "UPSTREAM_PROVIDER_MISSING_QUERY" in result.stdout
    assert "classification=UPSTREAM_PROVIDER_MISSING_QUERY" in tp.latest_meta()


# G. RC=0 with handoff + explicit completion marker => AGENT_COMPLETED.
def test_agent_completed(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output=(
            "Handoff\n"
            "TASK: build the thing\n"
            "RESULT: done\n"
            "TRAJECTORY_OS_V0_TEST_COMPLETE\n"
        ),
    )

    result = tp.run(*SMOKE_ARGS, "--", "Build the thing.")
    assert result.returncode == 0
    assert "AGENT_COMPLETED" in result.stdout
    assert "classification=AGENT_COMPLETED" in tp.latest_meta()


# H. RC=0 without completion evidence => INCOMPLETE_AGENT_RUN.
def test_incomplete_agent_run(tp: TPContext) -> None:
    tp.scenario(rc=0, output="I thought about the task thoroughly.\n")

    result = tp.run(*SMOKE_ARGS, "--", "Build the thing.")
    assert result.returncode == 0
    assert "INCOMPLETE_AGENT_RUN" in result.stdout
    assert "AGENT_COMPLETED" not in result.stdout
    assert "classification=INCOMPLETE_AGENT_RUN" in tp.latest_meta()


# I. Unrelated nonzero error => AGENT_FAILED (RC preserved).
def test_unrelated_nonzero_error(tp: TPContext) -> None:
    tp.scenario(rc=3, output="boom: unrelated failure\n")

    result = tp.run(*SMOKE_ARGS, "--", "Build the thing.")
    assert result.returncode == 3
    assert "AGENT_FAILED" in result.stdout
    assert "RECOVERABLE" not in result.stdout
    assert "classification=AGENT_FAILED" in tp.latest_meta()


# J. Recoverable failure leaves modified files untouched.
def test_recoverable_failure_preserves_work(tp: TPContext) -> None:
    head_before = _git(tp.work, "rev-parse", "HEAD")
    tp.scenario(
        rc=9,
        output=f"500 internal error: {PROVIDER_PHRASE}\n",
        touch="agent_work.txt",
    )

    result = tp.run(*SMOKE_ARGS, "--", "Build the feature.")
    assert result.returncode == 9

    preserved = tp.work / "agent_work.txt"
    assert preserved.exists()
    assert preserved.read_text() == "agent work\n"

    # No commit/reset/clean: HEAD unchanged, agent work still untracked.
    assert _git(tp.work, "rev-parse", "HEAD") == head_before
    status = _git(tp.work, "status", "--porcelain")
    assert "agent_work.txt" in status
    assert "??" in status


def _write_fake_executable(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\nset -u\n" + body)
    path.chmod(0o755)
    return path


def _latest_status_log(tp: TPContext) -> str:
    run_dirs = sorted((tp.work / ".trajectory-pi" / "runs").iterdir())
    assert run_dirs, "no run directory was created"
    status = run_dirs[-1] / "status.log"
    assert status.is_file()
    return status.read_text()


def _install_telemetry_fakes(
    tp: TPContext,
    *,
    ollama_active: bool,
    journal_body: str,
    gpu_line: str = "99, 350.00, 22800, 24576",
) -> None:
    binary = tp.ctx.parent / "bin"

    ollama_output = (
        "NAME ID SIZE PROCESSOR CONTEXT UNTIL\n"
        "qwen3.8-dev3090:latest fake 17GB 100% GPU 65536 4 minutes\n"
        if ollama_active
        else "NAME ID SIZE PROCESSOR CONTEXT UNTIL\n"
    )

    _write_fake_executable(
        binary,
        "ollama",
        f"""\
if [[ "${{1:-}}" == "ps" ]]; then
    cat <<'EOF'
{ollama_output}EOF
    exit 0
fi
exit 1
""",
    )

    _write_fake_executable(
        binary,
        "nvidia-smi",
        f"""\
printf '%s\\n' {shlex.quote(gpu_line)}
""",
    )

    _write_fake_executable(
        binary,
        "journalctl",
        f"""\
cat <<'EOF'
{journal_body}EOF
""",
    )


def test_wrapper_version_is_v031(tp: TPContext) -> None:
    result = tp.run("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == "trajectory-pi 0.3.1"


def test_heartbeat_reports_recent_native_generation_rate(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output="Handoff\nTRAJECTORY_PI_TEST_COMPLETE\n",
    )

    # Use a timestamp far in the future so the fake journal line can be
    # rewritten immediately before the wrapper starts.
    now = int(
        subprocess.run(
            ["date", "+%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    _install_telemetry_fakes(
        tp,
        ollama_active=True,
        journal_body=(
            f"{now}.000000 host ollama[1]: "
            "slot print_timing: id 0 | task 0 | "
            "n_gen = 346, tg = 38.05 t/s, tg_3s = 36.37 t/s\n"
        ),
    )

    result = tp.run(*SMOKE_ARGS, "--", "Telemetry test.")
    assert result.returncode == 0

    status = _latest_status_log(tp)

    assert "ollama=active" in status
    assert "GPU 99%" in status
    assert "350.00 W" in status
    assert "VRAM 22800/24576 MiB" in status
    assert "gen_3s=36.37 tok/s" in status


def test_heartbeat_rejects_stale_generation_rate(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output="Handoff\nTRAJECTORY_PI_TEST_COMPLETE\n",
    )

    now = int(
        subprocess.run(
            ["date", "+%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    _install_telemetry_fakes(
        tp,
        ollama_active=True,
        journal_body=(
            f"{now - 30}.000000 host ollama[1]: "
            "slot print_timing: id 0 | task 0 | "
            "n_gen = 346, tg = 38.05 t/s, tg_3s = 36.37 t/s\n"
        ),
    )

    result = tp.run(*SMOKE_ARGS, "--", "Telemetry stale test.")
    assert result.returncode == 0

    status = _latest_status_log(tp)

    assert "ollama=active" in status
    assert "gen_3s=unavailable" in status
    assert "last_gen=36.37 tok/s" in status

    age_fragment = status.split("age=", 1)[1].split("s", 1)[0]
    age = int(age_fragment)
    assert 30 <= age <= 35


def test_heartbeat_rejects_malformed_generation_rate(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output="Handoff\nTRAJECTORY_PI_TEST_COMPLETE\n",
    )

    now = int(
        subprocess.run(
            ["date", "+%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    _install_telemetry_fakes(
        tp,
        ollama_active=True,
        journal_body=(
            f"{now}.000000 host ollama[1]: "
            "slot print_timing: id 0 | task 0 | "
            "tg_3s = definitely-not-a-number t/s\n"
        ),
    )

    result = tp.run(*SMOKE_ARGS, "--", "Malformed telemetry test.")
    assert result.returncode == 0

    status = _latest_status_log(tp)
    assert "gen_3s=unavailable" in status


def test_heartbeat_does_not_report_rate_when_model_inactive(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output="Handoff\nTRAJECTORY_PI_TEST_COMPLETE\n",
    )

    now = int(
        subprocess.run(
            ["date", "+%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    _install_telemetry_fakes(
        tp,
        ollama_active=False,
        journal_body=(
            f"{now}.000000 host ollama[1]: "
            "slot print_timing: id 0 | task 0 | "
            "tg_3s = 99.99 t/s\n"
        ),
    )

    result = tp.run(*SMOKE_ARGS, "--", "Inactive telemetry test.")
    assert result.returncode == 0

    status = _latest_status_log(tp)

    assert "ollama=idle" in status
    assert "gen_3s=unavailable" in status
    assert "99.99 tok/s" not in status


def test_heartbeat_survives_journalctl_failure(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output="Handoff\nTRAJECTORY_PI_TEST_COMPLETE\n",
    )

    binary = tp.ctx.parent / "bin"

    _write_fake_executable(
        binary,
        "ollama",
        """\
if [[ "${1:-}" == "ps" ]]; then
    cat <<'EOF'
NAME ID SIZE PROCESSOR CONTEXT UNTIL
qwen3.8-dev3090:latest fake 17GB 100% GPU 65536 4 minutes
EOF
    exit 0
fi
exit 1
""",
    )

    _write_fake_executable(
        binary,
        "nvidia-smi",
        """\
printf '%s\n' '99, 350.00, 22800, 24576'
""",
    )

    _write_fake_executable(
        binary,
        "journalctl",
        """\
exit 9
""",
    )

    result = tp.run(*SMOKE_ARGS, "--", "Journal failure telemetry test.")

    assert result.returncode == 0
    assert "AGENT_COMPLETED" in result.stdout

    status = _latest_status_log(tp)
    assert "ollama=active" in status
    assert "gen_3s=unavailable" in status


def test_heartbeat_is_written_to_status_log(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output="Handoff\nTRAJECTORY_PI_TEST_COMPLETE\n",
    )

    _install_telemetry_fakes(
        tp,
        ollama_active=False,
        journal_body="",
    )

    result = tp.run(*SMOKE_ARGS, "--", "Status log telemetry test.")

    assert result.returncode == 0

    status = _latest_status_log(tp)
    heartbeat_lines = [
        line for line in status.splitlines()
        if line.startswith("[")
    ]

    assert heartbeat_lines
    assert all("elapsed=" in line for line in heartbeat_lines)
    assert all("files=" in line for line in heartbeat_lines)
    assert all("ollama=" in line for line in heartbeat_lines)
    assert all("GPU " in line for line in heartbeat_lines)
    assert all("VRAM " in line for line in heartbeat_lines)
    assert all("gen_3s=" in line for line in heartbeat_lines)


def test_completion_semantics_unchanged_with_telemetry(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output=(
            "Handoff\n"
            "RESULT: done\n"
            "TRAJECTORY_OS_V021_COMPLETE\n"
        ),
    )

    _install_telemetry_fakes(
        tp,
        ollama_active=False,
        journal_body="",
    )

    result = tp.run(*SMOKE_ARGS, "--", "Completion regression test.")

    assert result.returncode == 0
    assert "AGENT_COMPLETED" in result.stdout
    assert "classification=AGENT_COMPLETED" in tp.latest_meta()


def test_incomplete_semantics_unchanged_with_telemetry(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output="No handoff or completion marker here.\n",
    )

    _install_telemetry_fakes(
        tp,
        ollama_active=False,
        journal_body="",
    )

    result = tp.run(*SMOKE_ARGS, "--", "Incomplete regression test.")

    assert result.returncode == 0
    assert "INCOMPLETE_AGENT_RUN" in result.stdout
    assert "classification=INCOMPLETE_AGENT_RUN" in tp.latest_meta()


def test_dirty_tree_safety_still_prevents_pi_invocation(tp: TPContext) -> None:
    dirty = tp.work / "README.md"
    dirty.write_text("dirty before wrapper\n")

    tp.scenario(
        rc=0,
        output="Handoff\nTRAJECTORY_PI_TEST_COMPLETE\n",
    )

    result = tp.run(*SMOKE_ARGS, "--", "Safety regression test.")

    assert result.returncode == 4
    assert "Working tree is already dirty" in result.stdout
    assert not tp.args_log.exists()


def test_context_capacity_is_not_mislabeled_as_used_context(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output="Handoff\nTRAJECTORY_PI_TEST_COMPLETE\n",
    )

    _install_telemetry_fakes(
        tp,
        ollama_active=True,
        journal_body="",
    )

    result = tp.run(*SMOKE_ARGS, "--", "Context label regression test.")

    assert result.returncode == 0

    status = _latest_status_log(tp)

    # V0.2.1 deliberately does not expose context usage yet.
    assert "ctx=" not in status
    assert "ctx_used=" not in status
    assert "ctx_cap=" not in status


def test_fresh_generation_rate_is_not_duplicated_as_last_gen(tp: TPContext) -> None:
    tp.scenario(
        rc=0,
        output="Handoff\nTRAJECTORY_PI_TEST_COMPLETE\n",
    )

    now = int(
        subprocess.run(
            ["date", "+%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    _install_telemetry_fakes(
        tp,
        ollama_active=True,
        journal_body=(
            f"{now}.000000 host ollama[1]: "
            "slot print_timing: id 0 | task 0 | "
            "n_gen = 346, tg = 38.05 t/s, tg_3s = 36.37 t/s\n"
        ),
    )

    result = tp.run(*SMOKE_ARGS, "--", "Fresh telemetry distinction test.")
    assert result.returncode == 0

    status = _latest_status_log(tp)

    assert "gen_3s=36.37 tok/s" in status
    assert "last_gen=" not in status
    assert "age=" not in status

# ------------------------------------------------------------------
# Strict reviewer verdict parser consistency (Issue #148)
# ------------------------------------------------------------------
#
# The parser under test is extracted verbatim from the shipped wrapper
# script so the test always exercises the exact code that runs in
# production. PASS is permitted ONLY for an unambiguous,
# contradiction-free response: exactly one VERDICT: PASS, exactly one
# FINAL RECOMMENDATION: GO COMMIT, and BLOCKERS and MAJORS sections
# that each appear exactly once and are semantically empty
# (canonical "- None").

_REVIEW_PARSER_MARKER = "parse_review_verdict() {"

_CANONICAL_PASS = (
    "VERDICT: PASS\n"
    "\n"
    "BLOCKERS:\n"
    "- None\n"
    "\n"
    "MAJORS:\n"
    "- None\n"
    "\n"
    "MINORS:\n"
    "- None\n"
    "\n"
    "FINAL RECOMMENDATION: GO COMMIT\n"
)


def _run_parse_review_verdict(tmp_path: Path, response: str) -> dict[str, str]:
    """Run the shipped parse_review_verdict() on `response` text and
    return its key=value output as a dict."""
    script = WRAPPER.read_text()
    start = script.index(_REVIEW_PARSER_MARKER)
    end = script.index("\n}", start)
    fn_text = script[start : end + 2]

    review_file = tmp_path / "review.txt"
    review_file.write_text(response, encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        + fn_text
        + "\n"
        + "parse_review_verdict " + shlex.quote(str(review_file)) + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["bash", str(driver)],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ["PATH"]},
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = {
        key: value
        for key, sep, value in (
            line.partition("=") for line in proc.stdout.splitlines()
        )
        if sep
    }
    assert "RESULT" in out, proc.stdout
    return out


def _assert_never_pass(tmp_path: Path, response: str) -> None:
    out = _run_parse_review_verdict(tmp_path, response)
    assert out["RESULT"] != "PASS", (
        f"contradictory/malformed reviewer response was parsed as PASS: {out}"
    )


def test_parser_pass_canonical_none_blocks_majors_passes(tmp_path: Path) -> None:
    # 1. PASS + BLOCKERS "- None" + MAJORS "- None" + GO COMMIT -> PASS
    out = _run_parse_review_verdict(tmp_path, _CANONICAL_PASS)
    assert out == {
        "RESULT": "PASS",
        "VERDICT": "PASS",
        "RECOMMENDATION": "GO COMMIT",
    }


def test_parser_canonical_lowercase_still_passes(tmp_path: Path) -> None:
    # The canonical empty marker is spelling-insensitive ("none" / "None");
    # this is what the fake reviewer corpus and real providers emit.
    text = _CANONICAL_PASS.replace("- None", "- none")
    assert _run_parse_review_verdict(tmp_path, text)["RESULT"] == "PASS"


def test_parser_pass_with_real_blocker_never_passes(tmp_path: Path) -> None:
    # 2. PASS + real BLOCKER + GO COMMIT -> never PASS
    text = _CANONICAL_PASS.replace(
        "BLOCKERS:\n- None",
        "BLOCKERS:\n- concrete release-blocking defect",
    )
    _assert_never_pass(tmp_path, text)


def test_parser_pass_with_real_major_never_passes(tmp_path: Path) -> None:
    # 3. PASS + real MAJOR + GO COMMIT -> never PASS
    text = _CANONICAL_PASS.replace(
        "MAJORS:\n- None",
        "MAJORS:\n- concrete release-blocking defect",
    )
    _assert_never_pass(tmp_path, text)


def test_parser_pass_with_blocker_and_major_never_passes(tmp_path: Path) -> None:
    # 4. PASS + both blocker and major -> never PASS
    text = (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- blocker finding\n"
        "\n"
        "MAJORS:\n"
        "- major finding\n"
        "\n"
        "MINORS:\n"
        "- None\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    )
    _assert_never_pass(tmp_path, text)


def test_parser_missing_blockers_section_never_passes(tmp_path: Path) -> None:
    # 5. missing BLOCKERS section -> never PASS
    text = (
        "VERDICT: PASS\n"
        "\n"
        "MAJORS:\n"
        "- None\n"
        "\n"
        "MINORS:\n"
        "- None\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    )
    _assert_never_pass(tmp_path, text)


def test_parser_missing_majors_section_never_passes(tmp_path: Path) -> None:
    # 6. missing MAJORS section -> never PASS
    text = (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- None\n"
        "\n"
        "MINORS:\n"
        "- None\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    )
    _assert_never_pass(tmp_path, text)


def test_parser_duplicated_required_section_never_passes(tmp_path: Path) -> None:
    # 7. duplicated required section -> never PASS
    text = (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- None\n"
        "\n"
        "BLOCKERS:\n"
        "- None\n"
        "\n"
        "MAJORS:\n"
        "- None\n"
        "\n"
        "MINORS:\n"
        "- None\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    )
    _assert_never_pass(tmp_path, text)


def test_parser_fail_with_findings_and_fix_is_valid_fail(tmp_path: Path) -> None:
    # 8. FAIL + actual finding + FIX -> valid FAIL (never PASS either)
    text = (
        "VERDICT: FAIL\n"
        "\n"
        "BLOCKERS:\n"
        "- tracked.py changes behavior without tests\n"
        "\n"
        "MAJORS:\n"
        "- None\n"
        "\n"
        "MINORS:\n"
        "- None\n"
        "\n"
        "FINAL RECOMMENDATION: FIX\n"
    )
    out = _run_parse_review_verdict(tmp_path, text)
    assert out["RESULT"] == "FAIL"
    assert out["VERDICT"] == "FAIL"
    assert out["RECOMMENDATION"] == "FIX"


def test_parser_none_marker_mixed_with_finding_never_passes(tmp_path: Path) -> None:
    # 9. "- None" mixed with another finding -> never PASS
    text = _CANONICAL_PASS.replace(
        "BLOCKERS:\n- None",
        "BLOCKERS:\n- None\n- secret committed in patch",
    )
    _assert_never_pass(tmp_path, text)


# --- Strict parser structure completion (issue #148) -------------------
#
# "- None" is an empty marker ONLY when the complete section body (blank
# lines trimmed) is exactly that one line. Prose before or after the
# marker, bullets, comments or any other text coexisting with it means
# the section has findings and the response can NEVER PASS. Canonical
# headings must each appear exactly once, in canonical order.


def test_parser_prose_before_none_in_majors_never_passes(
    tmp_path: Path,
) -> None:
    # 1. prose before "- None" in MAJORS -> never PASS (the prose finding
    #    must not be ignored while the empty marker is counted).
    text = _CANONICAL_PASS.replace(
        "MAJORS:\n- None",
        "MAJORS:\n"
        "There is a concrete release-blocking defect.\n"
        "- None",
    )
    out = _run_parse_review_verdict(tmp_path, text)
    assert out["RESULT"] != "PASS", (
        f"prose before '- None' in MAJORS was parsed as PASS: {out}"
    )


def test_parser_prose_after_none_in_majors_never_passes(
    tmp_path: Path,
) -> None:
    # 2. prose after "- None" in MAJORS -> never PASS
    text = _CANONICAL_PASS.replace(
        "MAJORS:\n- None",
        "MAJORS:\n"
        "- None\n"
        "extra prose finding",
    )
    out = _run_parse_review_verdict(tmp_path, text)
    assert out["RESULT"] != "PASS", (
        f"prose after '- None' in MAJORS was parsed as PASS: {out}"
    )


def test_parser_prose_before_none_in_blockers_never_passes(
    tmp_path: Path,
) -> None:
    # 3. prose before "- None" in BLOCKERS -> never PASS
    text = _CANONICAL_PASS.replace(
        "BLOCKERS:\n- None",
        "BLOCKERS:\n"
        "Real release-blocking defect.\n"
        "- None",
    )
    out = _run_parse_review_verdict(tmp_path, text)
    assert out["RESULT"] != "PASS", (
        f"prose before '- None' in BLOCKERS was parsed as PASS: {out}"
    )


def test_parser_canonical_headings_wrong_order_never_passes(
    tmp_path: Path,
) -> None:
    # 4. canonical headings in wrong order (MAJORS before BLOCKERS) ->
    #    never PASS: the required structural sequence is exactly
    #    VERDICT, BLOCKERS, MAJORS, MINORS, FINAL RECOMMENDATION.
    text = (
        "VERDICT: PASS\n"
        "\n"
        "MAJORS:\n"
        "- None\n"
        "\n"
        "BLOCKERS:\n"
        "- None\n"
        "\n"
        "MINORS:\n"
        "- None\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    )
    out = _run_parse_review_verdict(tmp_path, text)
    assert out["RESULT"] != "PASS", (
        f"wrong canonical heading order was parsed as PASS: {out}"
    )


def test_parser_canonical_valid_pass_still_passes(
    tmp_path: Path,
) -> None:
    # 5. regression guard: the canonical valid PASS (exact structural
    #    sequence, BLOCKERS/MAJORS each exactly one line '- None') must
    #    still PASS after the strict structure completion.
    out = _run_parse_review_verdict(tmp_path, _CANONICAL_PASS)
    assert out == {
        "RESULT": "PASS",
        "VERDICT": "PASS",
        "RECOMMENDATION": "GO COMMIT",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        # every contradictory shape from tests 2-7 and 9, plus extras
        lambda t: t.replace("BLOCKERS:\n- None",
                            "BLOCKERS:\n- concrete release-blocking defect"),
        lambda t: t.replace("MAJORS:\n- None",
                            "MAJORS:\n- concrete release-blocking defect"),
        lambda t: t.replace("BLOCKERS:\n- None",
                            "BLOCKERS:\n- blocker finding")
                   .replace("MAJORS:\n- None", "MAJORS:\n- major finding"),
        lambda t: t.replace(
            "MAJORS:\n- None\n"
            "\n"
            "MINORS:",
            "MINORS:",
        ),
        lambda t: t.replace(
            "BLOCKERS:\n"
            "- None\n"
            "\n",
            "",
        ),
        lambda t: t.replace(
            "BLOCKERS:\n- None\n",
            "BLOCKERS:\n- None\n\nBLOCKERS:\n- None\n",
        ),
        lambda t: t.replace(
            "BLOCKERS:\n- None",
            "BLOCKERS:\n- None\n- another finding",
        ),
        lambda t: t.replace("FINAL RECOMMENDATION: GO COMMIT",
                            "FINAL RECOMMENDATION: FIX"),
        lambda t: t + "\nFINAL RECOMMENDATION: GO COMMIT\n",
        lambda t: t.replace("VERDICT: PASS", "VERDICT: PASS\nVERDICT: PASS"),
        # issue #148 strict structure: prose coexisting with the empty
        # marker, and wrong canonical heading order
        lambda t: t.replace(
            "MAJORS:\n- None",
            "MAJORS:\n"
            "There is a concrete release-blocking defect.\n"
            "- None",
        ),
        lambda t: t.replace(
            "MAJORS:\n- None",
            "MAJORS:\n- None\nextra prose finding",
        ),
        lambda t: t.replace(
            "BLOCKERS:\n- None",
            "BLOCKERS:\nReal release-blocking defect.\n- None",
        ),
        lambda t: t.replace(
            "BLOCKERS:\n- None\n\nMAJORS:\n- None\n",
            "MAJORS:\n- None\n\nBLOCKERS:\n- None\n",
        ),
    ],
    ids=[
        "pass-with-blocker",
        "pass-with-major",
        "pass-with-both",
        "missing-majors",
        "missing-blockers",
        "duplicated-blockers",
        "none-mixed-with-finding",
        "pass-with-fix-recommendation",
        "duplicated-recommendation",
        "duplicated-verdict",
        "prose-before-none-majors",
        "prose-after-none-majors",
        "prose-before-none-blockers",
        "wrong-heading-order",
    ],
)
def test_parser_contradictory_responses_never_allow_commit(
    tmp_path: Path, mutate  # noqa: ANN001
) -> None:
    # 10. READY_FOR_COMMIT is impossible for every contradictory
    #     reviewer response: the parser can only ever emit PASS for the
    #     canonical contradiction-free shape, and the README/report
    #     contract maps READY_FOR_COMMIT exclusively onto a PASS
    #     verdict, so `RESULT != PASS` proves commit is impossible.
    _assert_never_pass(tmp_path, mutate(_CANONICAL_PASS))
