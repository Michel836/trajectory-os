"""M020 — pure multi-goal portfolio model/identity unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from trajectory_os.goals import cli
from trajectory_os.portfolio import engine as portfolio_engine
from trajectory_os.portfolio import identity, model
from trajectory_os.portfolio import summary as portfolio_summary


def _entry(goal_id: str, *, decision_id: str | None = None) -> model.PortfolioEntry:
    return model.PortfolioEntry(
        goal_id=goal_id,
        graph_id=f"graph-{goal_id}",
        generation_id=None,
        priority=10,
        outcome=model.O_SELECTED,
        reason=model.R_SELECTED,
        ready_nodes=(f"n-{goal_id}",),
        active_reservations=0,
        scheduler_decision_id=decision_id,
    )


def test_policy_validation_bounds() -> None:
    model.DEFAULT_POLICY.validate()
    with pytest.raises(model.PortfolioError):
        model.PortfolioPolicy(cpu_slots=0).validate()
    with pytest.raises(model.PortfolioError):
        model.PortfolioPolicy(global_concurrency=2, max_active_goals=3).validate()
    with pytest.raises(model.PortfolioError):
        model.PortfolioPolicy(max_active_goals=0).validate()


def test_policy_identity_is_content_addressed() -> None:
    policy = model.PortfolioPolicy(max_active_goals=2, global_concurrency=4)
    again = model.PortfolioPolicy.from_dict(policy.to_dict())
    assert again.policy_id == policy.policy_id
    other = model.PortfolioPolicy(max_active_goals=1, global_concurrency=4)
    assert other.policy_id != policy.policy_id


def test_decision_identity_excludes_timestamp() -> None:
    policy = model.DEFAULT_POLICY
    a = model.PortfolioDecision.build(
        portfolio_id="p-1", goals=["g-a", "g-b"], policy=policy,
        created_at="2026-01-01T00:00:00Z",
        entries=[_entry("g-a"), _entry("g-b")])
    b = model.PortfolioDecision.build(
        portfolio_id="p-1", goals=["g-b", "g-a"], policy=policy,
        created_at="2026-06-01T09:30:00Z",
        entries=[_entry("g-b"), _entry("g-a")])
    assert a.decision_id == b.decision_id
    assert a.compute_decision_id() == a.decision_id


def test_decision_roundtrip_and_tamper_detection() -> None:
    decision = model.PortfolioDecision.build(
        portfolio_id="p-1", goals=["g-a"], policy=model.DEFAULT_POLICY,
        created_at="2026-01-01T00:00:00Z", entries=[_entry("g-a")])
    document = decision.to_dict()
    assert model.PortfolioDecision.from_dict(document).decision_id == \
        decision.decision_id
    document["decision_id"] = "0" * 64
    with pytest.raises(model.PortfolioError):
        model.PortfolioDecision.from_dict(document)


def test_assert_isolation_rejects_duplicates() -> None:
    model.assert_isolation([_entry("g-a", decision_id="a" * 64),
                            _entry("g-b", decision_id="b" * 64)])
    with pytest.raises(model.PortfolioError):
        model.assert_isolation([_entry("g-a"), _entry("g-a")])
    with pytest.raises(model.PortfolioError):
        model.assert_isolation([
            _entry("g-a", decision_id="c" * 64),
            _entry("g-b", decision_id="c" * 64)])


def test_state_rejects_untracked_last_decision() -> None:
    state = model.PortfolioState.initial(
        portfolio_id="p-1", policy=model.DEFAULT_POLICY,
        updated_at="2026-01-01T00:00:00Z")
    document = state.to_dict()
    document["last_decision_id"] = "a" * 64
    document["decision_ids"] = []
    with pytest.raises(model.PortfolioError):
        model.PortfolioState.from_dict(document)


def test_dependencies_normalize_and_cycle_detection() -> None:
    deps = model.PortfolioDependencies.normalize({"g-b": ["g-a"]})
    assert deps.for_goal("g-b") == ("g-a",)
    model.validate_dependencies(deps, ["g-a", "g-b"])
    with pytest.raises(model.PortfolioError):
        model.validate_dependencies(
            model.PortfolioDependencies.normalize({"g-b": ["missing"]}),
            ["g-a", "g-b"])
    cyclic = model.PortfolioDependencies.normalize(
        {"g-a": ["g-b"], "g-b": ["g-a"]})
    with pytest.raises(model.PortfolioError):
        model.validate_dependencies(cyclic, ["g-a", "g-b"])


def test_identity_domains_are_distinct() -> None:
    payload = {"a": 1}
    digests = {identity.digest(d, payload) for d in identity.DOMAIN_IDS}
    assert len(digests) == len(identity.DOMAIN_IDS)
    with pytest.raises(ValueError):
        identity.digest("nope", payload)


def test_cli_exposes_portfolio_and_daemon_commands() -> None:
    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions if action.dest == "command")
    commands = set(subparsers.choices)
    for required in ("portfolio", "portfolio-run", "daemon", "daemon-start",
                     "daemon-resume", "daemon-stop"):
        assert required in commands, required


def test_portfolio_status_absent_is_safe(tmp_path: Path) -> None:
    document = portfolio_summary.status_document(str(tmp_path / "root"))
    assert document["present"] is False
    assert "portfolio :" in portfolio_summary.render_status(document)


def _view(goal_id: str, *, active: int, ready: bool = False) -> model.PortfolioGoalView:
    return model.PortfolioGoalView(
        goal_id=goal_id, graph_id=f"graph-{goal_id}", generation_id=None,
        priority=10, complete=False, final_state="INCOMPLETE",
        final_reason="SCHEDULER_MISSING",
        ready_nodes=("n-1",) if ready else (),
        blocked_nodes=(), in_progress_nodes=("n-1",) if active else (),
        active_reservations=active, reserved_cpu_slots=active,
        reserved_gpu_slots=0, reserved_gpu_mem_bytes=0,
        active_missions=("m-1",) if active else (),
        scheduler_policy_id=None, stale_generation=False, stalled=False)


def test_active_goal_is_not_starved_by_the_global_budget() -> None:
    policy = model.PortfolioPolicy(cpu_slots=4, gpu_slots=1,
                                   gpu_mem_bytes=1 << 30,
                                   global_concurrency=1, max_active_goals=1)
    view = _view("g-a", active=1)
    totals = {"reservations": 1, "cpu_slots": 1, "gpu_slots": 0,
              "gpu_mem_bytes": 0}
    outcome, reason = portfolio_engine._classify(
        view, model.PortfolioDependencies(), {"g-a": view}, policy,
        safe_stop=False, active_goal_count=1, totals=totals)
    assert (outcome, reason) == (model.O_SELECTED, model.R_SELECTED)


def test_new_goal_is_budget_blocked_at_capacity() -> None:
    policy = model.PortfolioPolicy(cpu_slots=4, gpu_slots=1,
                                   gpu_mem_bytes=1 << 30,
                                   global_concurrency=2, max_active_goals=2)
    view = _view("g-b", active=0, ready=True)
    totals = {"reservations": 2, "cpu_slots": 2, "gpu_slots": 0,
              "gpu_mem_bytes": 0}
    outcome, reason = portfolio_engine._classify(
        view, model.PortfolioDependencies(), {"g-b": view}, policy,
        safe_stop=False, active_goal_count=1, totals=totals)
    assert (outcome, reason) == (model.O_EXCLUDED,
                                 model.R_BUDGET_EXHAUSTED)


def test_reserved_totals_reads_canonical_status() -> None:
    status = {"reservation_totals": {
        "cpu_slots": 2, "gpu_slots": 1, "gpu_mem_bytes": 1024, "count": 3}}
    assert portfolio_engine._reserved_totals(status) == (2, 1, 1024)


def test_reserved_totals_fail_closed_on_malformed_status() -> None:
    # The canonical status always emits every total, so absence or malformed
    # content is an invariant violation that must not be defaulted to zero.
    for status in (
            {},
            {"reservation_totals": None},
            {"reservation_totals": {"gpu_slots": 0, "gpu_mem_bytes": 0}},
            {"reservation_totals": {
                "cpu_slots": 0, "gpu_slots": "1", "gpu_mem_bytes": 0}},
            {"reservation_totals": {
                "cpu_slots": -1, "gpu_slots": 0, "gpu_mem_bytes": 0}},
            {"reservation_totals": {
                "cpu_slots": True, "gpu_slots": 0, "gpu_mem_bytes": 0}},
    ):
        with pytest.raises(model.PortfolioError) as excinfo:
            portfolio_engine._reserved_totals(status)
        assert excinfo.value.code == model.E_MALFORMED
