"""Mission 004 (Issue #198) — production sub-run adapter unit tests.

Covers the deterministic productionization layer:
canonical five-phase spec construction, production argv shape, prompt
materialization (model-heavy only, bounded), the bounded shell-lexed
command split, and fail-closed input rejection.

All tests are isolated (tmp dirs under the test root) and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trajectory_os.missions import adapter as ad
from trajectory_os.missions import model as m


class TestClassification:
    def test_kind_based(self) -> None:
        assert ad.is_model_heavy(m.PH_PLAN)
        assert ad.is_model_heavy(m.PH_IMPLEMENT)
        assert ad.is_model_heavy(m.PH_REPAIR)
        assert ad.is_model_heavy(m.PH_REVIEW)
        assert not ad.is_model_heavy(m.PH_VALIDATE)
        assert not ad.is_model_heavy(m.PH_CONSOLIDATE)

    def test_unknown_kind_fails_closed(self) -> None:
        with pytest.raises(ValueError):
            ad.is_model_heavy("nope")
        with pytest.raises(ad.AdapterError):
            ad.phase_mode("nope")
        with pytest.raises(ad.AdapterError):
            ad.phase_class("nope")

    def test_modes_and_classes_are_canonical(self) -> None:
        assert ad.phase_mode(m.PH_PLAN) == "PLAN"
        assert ad.phase_class(m.PH_PLAN) == "smoke"
        assert ad.phase_mode(m.PH_IMPLEMENT) == "IMPLEMENT"
        assert ad.phase_mode(m.PH_VALIDATE) == "VERIFY"
        assert ad.phase_mode(m.PH_REVIEW) == "REVIEW"
        assert ad.phase_mode(m.PH_REPAIR) == "REPAIR"
        assert ad.phase_class(m.PH_PLAN) == "smoke"
        assert ad.phase_class(m.PH_IMPLEMENT) == "feature"
        assert ad.phase_class(m.PH_REVIEW) == "feature"
        assert ad.phase_class(m.PH_REPAIR) == "repair"
        assert ad.phase_class(m.PH_VALIDATE) == "smoke"


class TestRenderPhasePrompt:
    def test_prompt_shape(self) -> None:
        p = ad.render_phase_prompt("m1", "implement", m.PH_IMPLEMENT,
                                   "do the thing")
        lines = p.splitlines()
        assert "mission   : m1" in p
        assert "phase     : implement" in p
        assert "kind      : IMPLEMENT" in p
        assert "mode      : IMPLEMENT" in p
        assert "[phase:IMPLEMENT]" in lines
        assert "objective :" in p
        assert "do the thing" in p
        assert "instructions" in p
        # bounded: the operator objective appears exactly once (verbatim)
        assert p.count("do the thing") == 1

    def test_objective_bounds(self) -> None:
        with pytest.raises(ad.AdapterError):
            ad.render_phase_prompt("m1", "plan", m.PH_PLAN, "")
        with pytest.raises(ad.AdapterError):
            ad.render_phase_prompt("m1", "plan", m.PH_PLAN,
                                   (m.MAX_OBJECTIVE_LEN + 1) * "x")

    def test_unknown_kind(self) -> None:
        with pytest.raises(ad.AdapterError):
            ad.render_phase_prompt("m1", "x", "NOPE", "o")


class TestMaterializePrompts:
    def test_only_model_heavy_get_prompt_files(self, tmp_path: Path) -> None:
        sequence = [(m.KIND_TO_PHASE_ID[k], k)
                    for k in m.CANONICAL_SEQUENCE]
        manifest = ad.materialize_phase_prompts(
            tmp_path, "m1", "objective text", sequence)
        assert set(manifest) == {"plan", "implement", "review"}
        for phase_id, path in manifest.items():
            p = Path(path)
            assert p.is_file()
            first = p.read_text(encoding="utf-8").splitlines()
            assert f"[phase:{phase_id.upper()}]" in first

    def test_prompt_file_marker(self, tmp_path: Path) -> None:
        ad.materialize_phase_prompts(tmp_path, "mx", "o",
                                     [("plan", m.PH_PLAN)])
        p = tmp_path / "prompts" / "plan.txt"
        assert p.is_file()
        body = p.read_text(encoding="utf-8")
        assert "[phase:PLAN]" in body.splitlines()

    def test_deterministic_same_bytes(self, tmp_path: Path) -> None:
        a = ad.materialize_phase_prompts(tmp_path, "m1", "o",
                                         [("plan", m.PH_PLAN)])
        b = ad.materialize_phase_prompts(tmp_path, "m1", "o",
                                         [("plan", m.PH_PLAN)])
        assert a == b
        first = a["plan"]
        rendered = ad.render_phase_prompt("m1", "plan", m.PH_PLAN, "o")
        assert Path(first).read_text(encoding="utf-8") == rendered

    def test_empty_phase_id_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(ad.AdapterError):
            ad.materialize_phase_prompts(tmp_path, "m1", "o",
                                         [("", m.PH_PLAN)])


class TestPhaseCommand:
    def test_argv_shape(self) -> None:
        cmd = ad.phase_command(m.PH_IMPLEMENT, pi_wrapper="pi",
                               prompt_file="/p/implement.txt",
                               model_name="big", phase_id="implement",
                               objective="o" * 10)
        assert cmd == [
            "pi",
            "--no-notify", "--dirty-ok", "--require-changes",
            "--class", "feature",
            "--mode", "IMPLEMENT",
            "--model", "big",
            "--prompt-file", "/p/implement.txt",
            "--", "TrajectoryOS implement: oooooooooo",
        ]


class TestRequireChanges:
    """M006: only canonical IMPLEMENT sub-runs carry --require-changes."""

    def test_implement_carries_require_changes(self) -> None:
        cmd = ad.phase_command(m.PH_IMPLEMENT, pi_wrapper="pi",
                               prompt_file="/p/implement.txt",
                               model_name="big", phase_id="implement",
                               objective="o")
        assert cmd.count("--require-changes") == 1
        # positioned with the other run-contract flags, before the
        # deterministic class/mode/model/prompt block
        assert cmd[:4] == ["pi", "--no-notify", "--dirty-ok",
                           "--require-changes"]
        assert cmd[4:8] == ["--class", "feature", "--mode", "IMPLEMENT"]
        assert cmd[8:] == ["--model", "big", "--prompt-file",
                           "/p/implement.txt", "--",
                           "TrajectoryOS implement: o"]

    def test_plan_review_and_repair_do_not_carry_it(self) -> None:
        for kind, pid in ((m.PH_PLAN, "plan"),
                          (m.PH_REVIEW, "review"),
                          (m.PH_REPAIR, "repair")):
            cmd = ad.phase_command(kind, pi_wrapper="pi",
                                   prompt_file=f"/p/{pid}.txt",
                                   model_name="big", phase_id=pid,
                                   objective="o")
            assert "--require-changes" not in cmd, kind

    def test_canonical_specs_only_implement(self, tmp_path: Path) -> None:
        specs = ad.build_canonical_specs(
            root=str(tmp_path), mission_id="mi", objective="obj",
            pi_wrapper="pi", model_name="big")
        by_id = dict((s.phase_id, s) for s in specs)
        assert "--require-changes" in list(by_id["implement"].command)
        for pid in ("plan", "review"):
            assert "--require-changes" not in list(by_id[pid].command)
        # deterministic phases are untouched (operator command verbatim)
        assert by_id["validate"].command == ("bash", "scripts/quality.sh")
        assert by_id["consolidate"].command == ("bash", "scripts/quality.sh")

    def test_deterministic(self) -> None:
        a = ad.phase_command(m.PH_PLAN, pi_wrapper="pi",
                             prompt_file="/p/plan.txt", model_name="m1",
                             phase_id="plan", objective="obj")
        b = ad.phase_command(m.PH_PLAN, pi_wrapper="pi",
                             prompt_file="/p/plan.txt", model_name="m1",
                             phase_id="plan", objective="obj")
        assert a == b

    def test_model_heavy_prompt_has_explicit_completion_contract(
            self) -> None:
        for kind in (m.PH_PLAN, m.PH_IMPLEMENT, m.PH_REPAIR, m.PH_REVIEW):
            text = ad.render_phase_prompt("mi", kind.lower(), kind, "obj")
            assert "HANDOFF" in text
            assert f"TRAJECTORY_{kind}_COMPLETE" in text
            assert (
                "The exact final non-blank line of your response MUST be:"
                in text
            )

    def test_rejects_non_heavy_kind(self) -> None:
        with pytest.raises(ad.AdapterError):
            ad.phase_command(m.PH_VALIDATE, pi_wrapper="pi",
                             prompt_file="/p/x.txt", model_name="m",
                             phase_id="validate", objective="o")

    def test_rejects_empty_inputs(self) -> None:
        with pytest.raises(ad.AdapterError):
            ad.phase_command(m.PH_PLAN, pi_wrapper="", prompt_file="p",
                             model_name="m", phase_id="k", objective="o")
        with pytest.raises(ad.AdapterError):
            ad.phase_command(m.PH_PLAN, pi_wrapper="pi", prompt_file="",
                             model_name="m", phase_id="k", objective="o")
        with pytest.raises(ad.AdapterError):
            ad.phase_command(m.PH_PLAN, pi_wrapper="pi", prompt_file="p",
                             model_name="", phase_id="k", objective="o")
        with pytest.raises(ad.AdapterError):
            ad.phase_command(m.PH_PLAN, pi_wrapper="pi", prompt_file="p",
                             model_name="m", phase_id="k", objective="")


class TestCanonicalSpecs:
    def test_five_phase_order_and_kinds(self, tmp_path: Path) -> None:
        specs = ad.build_canonical_specs(
            root=str(tmp_path), mission_id="mi", objective="o",
            pi_wrapper="pi", model_name="m")
        assert [(s.phase_id, s.kind) for s in specs] == [
            ("plan", m.PH_PLAN),
            ("implement", m.PH_IMPLEMENT),
            ("validate", m.PH_VALIDATE),
            ("review", m.PH_REVIEW),
            ("consolidate", m.PH_CONSOLIDATE),
        ]

    def test_model_heavy_command_shape(self, tmp_path: Path) -> None:
        specs = ad.build_canonical_specs(
            root=str(tmp_path), mission_id="mi", objective="obj",
            pi_wrapper="pi", model_name="big",
            validate_command=("check", "on"))
        by_id = dict((s.phase_id, s) for s in specs)
        plan = by_id["plan"]
        assert plan.command[0] == "pi"
        assert "--prompt-file" in plan.command
        pf = plan.command[plan.command.index("--prompt-file") + 1]
        assert Path(pf).is_file()
        # deterministic phase: operator command verbatim, no model
        val = by_id["validate"]
        assert val.command == ("check", "on")
        cons = by_id["consolidate"]
        assert cons.command == ("check", "on")

    def test_depends_on_linear_chain(self, tmp_path: Path) -> None:
        specs = ad.build_canonical_specs(root=str(tmp_path), mission_id="mi",
                                         objective="o",
                                         pi_wrapper="pi", model_name="m")
        assert specs[0].depends_on == ()
        for prev, cur in zip(specs, specs[1:], strict=False):
            assert cur.depends_on == (prev.phase_id,), cur.depends_on

    def test_gpu_policy_attaches_heavy_only(self, tmp_path: Path) -> None:
        specs = ad.build_canonical_specs(
            root=str(tmp_path), mission_id="mi", objective="o",
            pi_wrapper="pi", model_name="m",
            gpu=True, gpu_mem_bytes=25 * 2 ** 30)
        by_id = dict((s.phase_id, s) for s in specs)
        for pid in ("plan", "implement", "review"):
            r = by_id[pid].resources
            assert r is not None and r["gpu"] is True
            assert r["gpu_mem_bytes"] == 25 * 2 ** 30
        for pid in ("validate", "consolidate"):
            assert by_id[pid].resources is None

    def test_bad_objective_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(ad.AdapterError):
            ad.build_canonical_specs(root=str(tmp_path), mission_id="mi",
                                     objective="", pi_wrapper="pi",
                                     model_name="m")

    def test_bad_validate_command_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(ad.AdapterError):
            ad.build_canonical_specs(root=str(tmp_path), mission_id="mi",
                                     objective="o", pi_wrapper="pi",
                                     model_name="m", validate_command=())


class TestSplitCommand:
    def test_single_token(self) -> None:
        assert ad.split_command("true") == ("true",)

    def test_none_passes_through(self) -> None:
        assert ad.split_command(None) is None

    def test_shlex_split(self) -> None:
        assert ad.split_command('grep -q "phase" f.txt') == \
            ("grep", "-q", "phase", "f.txt")

    def test_empty_fails_closed(self) -> None:
        with pytest.raises(ad.AdapterError):
            ad.split_command("   ")

    def test_unbalanced_quote_fails_closed(self) -> None:
        with pytest.raises(ad.AdapterError):
            ad.split_command('echo "unterminated')

    def test_too_many_parts(self) -> None:
        with pytest.raises(ad.AdapterError):
            ad.split_command("x " * (m.MAX_COMMAND_PARTS + 1))

    def test_oversized_part(self) -> None:
        with pytest.raises(ad.AdapterError):
            ad.split_command("ok " + "y" * (m.MAX_COMMAND_PART_LEN + 1))

def test_canonical_specs_materialize_repair_prompt(tmp_path: Path) -> None:
    ad.build_canonical_specs(
        root=str(tmp_path),
        mission_id="mi",
        objective="obj",
        pi_wrapper="pi",
        model_name="big",
    )

    repair_prompt = tmp_path / "missions" / "mi" / "prompts" / "repair.txt"
    assert repair_prompt.is_file()

    text = repair_prompt.read_text(encoding="utf-8")
    assert "kind      : REPAIR" in text
    assert "mode      : REPAIR" in text
    assert "[phase:REPAIR]" in text
