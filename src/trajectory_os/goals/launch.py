"""M017 — provision the canonical missions a goal graph references.

A goal graph declares, per node, an explicit ``mission_ref`` identity. The
graph store deliberately refuses to create a graph whose *required* mission
references do not already resolve, so an operator launch has to provision the
mission state first.

This module is a thin composition seam over the existing production mission
surface (:mod:`trajectory_os.missions.adapter` +
:mod:`trajectory_os.missions.orchestrator`). It introduces no second mission
engine, no second phase plan and no second store: every mission is the
canonical five-phase bounded plan, and provisioning is idempotent — an
existing mission is never overwritten or resumed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trajectory_os.graph import model as graph_model
from trajectory_os.missions import adapter, orchestrator
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import store as mission_store


@dataclass(frozen=True)
class MissionLaunchDefaults:
    """Bounded default launch configuration for provisioned missions."""

    repo_root: str | None = None
    baseline_revision: str | None = None
    model: str = adapter.DEFAULT_MODEL
    pi_wrapper: str = adapter.DEFAULT_PI_WRAPPER
    validate_command: tuple[str, ...] = adapter.DEFAULT_VALIDATE_COMMAND
    consolidate_command: tuple[str, ...] | None = None
    gpu: bool = False
    gpu_mem_bytes: int = 0
    time_budget_s: int = mission_model.DEFAULT_TIME_BUDGET_S
    repair_budget: int = mission_model.MAX_REPAIR_ROUNDS
    subrun_budget: int | None = None


def mission_objective(node: graph_model.GraphNode,
                      goal_objective: str) -> str:
    """Deterministic bounded objective for one provisioned mission."""
    objective = f"{node.title} — {goal_objective}"
    if len(objective) > mission_model.MAX_OBJECTIVE_LEN:
        objective = objective[: mission_model.MAX_OBJECTIVE_LEN]
    return objective


def _build_specs(defaults: MissionLaunchDefaults, *,
                 root: str, mission_id: str, objective: str,
                 node: graph_model.GraphNode,
                 ) -> tuple[orchestrator.PhaseSpec, ...]:
    budget = node.budgets
    gpu = defaults.gpu
    gpu_mem = defaults.gpu_mem_bytes
    if node.resources is not None and node.resources.gpu:
        gpu = True
        if node.resources.gpu_mem_bytes:
            gpu_mem = node.resources.gpu_mem_bytes
    return adapter.build_canonical_specs(
        root=root,
        mission_id=mission_id,
        objective=objective,
        pi_wrapper=defaults.pi_wrapper,
        model_name=defaults.model,
        validate_command=defaults.validate_command,
        consolidate_command=defaults.consolidate_command,
        repair_budget=(
            budget.repair_budget
            if budget is not None and budget.repair_budget is not None
            else defaults.repair_budget),
        gpu=gpu,
        gpu_mem_bytes=gpu_mem,
    )


def _create(root: str, defaults: MissionLaunchDefaults, *,
            mission_id: str, objective: str, node: graph_model.GraphNode,
            ) -> None:
    specs = _build_specs(defaults, root=root, mission_id=mission_id,
                         objective=objective, node=node)
    budget = node.budgets
    config = orchestrator.MissionConfig(
        mission_id=mission_id,
        objective=objective,
        phase_specs=specs,
        repo_root=defaults.repo_root,
        cwd=defaults.repo_root,
        baseline_revision=defaults.baseline_revision,
        time_budget_s=(
            budget.time_budget_s
            if budget is not None and budget.time_budget_s is not None
            else defaults.time_budget_s),
        repair_budget=(
            budget.repair_budget
            if budget is not None and budget.repair_budget is not None
            else defaults.repair_budget),
        subrun_budget=(
            budget.subruns
            if (defaults.subrun_budget is None and budget is not None
                and budget.subruns is not None)
            else defaults.subrun_budget),
    )
    try:
        orchestrator.create_mission(root, config)
    except orchestrator.MissionExists:
        # Race with another launch: an existing mission is authoritative and
        # is never overwritten.
        return


def ensure_missions(root: str,
                    nodes: Sequence[graph_model.GraphNode],
                    objective: str,
                    defaults: MissionLaunchDefaults,
                    ) -> dict[str, str]:
    """Provision every referenced mission that does not exist yet.

    Returns a stable ``{mission_id: "created" | "existing"}`` map. Missing
    missions are created with the canonical bounded plan; existing missions
    are left untouched (idempotent, never resumed by provisioning).
    """
    outcomes: dict[str, str] = {}
    for node in nodes:
        ref = node.mission_ref
        if ref is None:
            continue
        mission_id = ref.mission_id
        if mission_store.mission_paths(root, mission_id)["mission"].is_file():
            outcomes[mission_id] = "existing"
            continue
        mission_objective_text = mission_objective(node, objective)
        _create(root, defaults, mission_id=mission_id,
                objective=mission_objective_text, node=node)
        outcomes[mission_id] = "created"
    return outcomes


def provision_from_spec(root: str, spec: object,
                        defaults: MissionLaunchDefaults,
                        ) -> tuple[graph_model.NormalizedSpec, dict[str, str]]:
    """Validate a declarative spec (pure) and provision its missions.

    The spec is normalized first, so a malformed spec is rejected before any
    mission state is written.
    """
    normalized = graph_model.normalize_spec(spec)
    outcomes = ensure_missions(root, normalized.nodes, normalized.objective,
                               defaults)
    return normalized, outcomes
