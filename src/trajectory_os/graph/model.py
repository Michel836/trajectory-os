"""Mission 012 — goal decomposition graph model (pure, bounded, fail closed).

This module owns the canonical, deterministic representation of one
strategic goal as a directed acyclic graph of explicit dependent mission
nodes. It performs no I/O, no clock reads and no mission-evidence access:
normalization, validation, edge derivation and topological ordering are
pure functions of their inputs.

Canonical invariants (ADR-010):

* stable goal/node identity — ids are canonical slugs and uniqueness is
  enforced, never inferred;
* explicit dependency edges — derived only from a node's explicit
  ``depends_on`` list, never from prose;
* explicit persisted acceptance criteria — structured, bounded, exactly
  preserved through persistence/reconstruction;
* deterministic priority with explicit, stable tie-breaking;
* bounded resource requirements and execution budgets;
* optional, explicit canonical mission identity/reference;
* fail closed on cycles, self dependencies, missing dependencies,
  duplicate goal/node/edge identities, malformed schema, unsupported
  schema version, oversized graphs/fields, invalid priorities, invalid
  resource/budget declarations, duplicate/contradictory references.

Topological order: Kahn's algorithm with the explicit, stable key
``(priority DESC, node_id ASC)``. Higher priority values are emitted first
and node id is the tie-breaker, so the order never depends on dictionary
insertion order, input order or filesystem order.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from trajectory_os.graph import identity as graph_identity
from trajectory_os.missions import model as mission_model
from trajectory_os.runs import resources as runs_resources

#: Schema version of the durable goal-graph document (strict, fail closed).
SCHEMA_VERSION = 1

# --- hard bounded limits (no configuration may exceed these) -----------------

MAX_NODES = 64
MAX_EDGES = 256
MAX_DEPENDENCIES_PER_NODE = 16
MAX_ACCEPTANCE_CRITERIA_PER_NODE = 16
MAX_TITLE_LEN = 512
MAX_OBJECTIVE_LEN = mission_model.MAX_OBJECTIVE_LEN
MAX_CRITERION_LEN = 1024
MAX_REVISION_LEN = mission_model.MAX_REVISION_LEN
MAX_TIMESTAMP_LEN = 64
MAX_CREATED_BY_LEN = 128

#: Declarative spec hard byte cap, checked before JSON parsing.
MAX_SPEC_BYTES = 262144

#: Deterministic priority domain (inclusive); higher executes first.
MIN_PRIORITY = 0
MAX_PRIORITY = 100

#: Stable lowercase slug grammar (same as mission ids: 2..64 chars).
ID_RE = mission_model.ID_RE

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_GRAPH"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
E_INVALID_ID = "INVALID_ID"
E_DUPLICATE_NODE = "DUPLICATE_NODE_ID"
E_DUPLICATE_EDGE = "DUPLICATE_EDGE"
E_SELF_DEPENDENCY = "SELF_DEPENDENCY"
E_MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
E_CYCLE = "DEPENDENCY_CYCLE"
E_OVERSIZED = "GRAPH_OVERSIZED"
E_FIELD_OVERSIZED = "FIELD_OVERSIZED"
E_INVALID_PRIORITY = "INVALID_PRIORITY"
E_INVALID_RESOURCE = "INVALID_RESOURCE"
E_INVALID_BUDGET = "INVALID_BUDGET"
E_INVALID_ACCEPTANCE = "INVALID_ACCEPTANCE_CRITERIA"
E_DUPLICATE_CRITERION = "DUPLICATE_CRITERION_ID"
E_DUPLICATE_MISSION_REF = "DUPLICATE_MISSION_REFERENCE"
E_UNRESOLVED_REFERENCE = "UNRESOLVED_REFERENCE"
E_REFERENCE_INVALID = "REFERENCE_INVALID"
E_CONTRADICTORY_REFERENCE = "CONTRADICTORY_REFERENCE"
E_EMPTY_GRAPH = "EMPTY_GRAPH"
E_IDENTITY_MISMATCH = "GRAPH_IDENTITY_MISMATCH"

#: Input spec top-level keys.
SPEC_KEYS = ("schema_version", "goal_id", "objective", "nodes")

#: Normalized node keys.
NODE_KEYS = (
    "node_id", "title", "priority", "depends_on", "acceptance_criteria",
    "mission_ref", "resources", "budgets",
)

#: Mission reference keys.
MISSION_REF_KEYS = ("mission_id", "required")

#: Acceptance criterion keys.
CRITERION_KEYS = ("criterion_id", "statement", "verification")

#: Node resource keys.
RESOURCE_KEYS = ("cpu_slots", "gpu", "gpu_mem_bytes", "exclusive",
                 "model_heavy")

#: Node budget keys.
BUDGET_KEYS = ("subruns", "time_budget_s", "repair_budget", "max_attempts")


class GraphValidationError(Exception):
    """The goal graph violates the canonical bounded schema (fail closed)."""

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code}: {path}" + (f" ({detail})" if detail else ""))
        self.code = code
        self.path = path
        self.detail = detail


# --- strict primitive helpers -------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fail(code: str, path: str, detail: str = "") -> NoReturn:
    raise GraphValidationError(code, path, detail)


def _known_keys(doc: Mapping[str, Any], allowed: Sequence[str],
                path: str) -> None:
    unknown = set(doc) - set(allowed)
    if unknown:
        _fail(E_MALFORMED, path, f"unknown field(s): {sorted(unknown)}")


def _require_keys(doc: Mapping[str, Any], required: Sequence[str],
                  path: str) -> None:
    missing = [key for key in required if key not in doc]
    if missing:
        _fail(E_MALFORMED, path, f"missing field(s): {missing}")


def _require_mapping(value: object, path: str, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(E_MALFORMED, path, detail)
    return value


def _require_str(value: object, path: str, detail: str,
                 *, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        _fail(E_MALFORMED, path, detail)
    if len(value) > maximum:
        _fail(E_FIELD_OVERSIZED, path, f"{detail} exceeds {maximum} chars")
    return value


def _require_id(value: object, path: str, detail: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        _fail(E_INVALID_ID, path, f"{detail}={value!r}")
    return value


def _require_bool(value: object, path: str, detail: str) -> bool:
    if not isinstance(value, bool):
        _fail(E_MALFORMED, path, detail)
    return value


def _require_int(value: object, path: str, detail: str, *,
                 minimum: int, maximum: int | None = None,
                 code: str = E_MALFORMED) -> int:
    if not _is_int(value):
        _fail(code, path, detail)
    assert isinstance(value, int)
    if value < minimum or (maximum is not None and value > maximum):
        _fail(code, path, f"{detail} out of bounds: {value}")
    return value


def _optional_bool(value: object, path: str, detail: str) -> bool | None:
    if value is None:
        return None
    return _require_bool(value, path, detail)


def _optional_str(value: object, path: str, detail: str, *,
                  maximum: int) -> str | None:
    if value is None:
        return None
    return _require_str(value, path, detail, maximum=maximum)


# --- normalized value objects -------------------------------------------------


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One explicit, persisted acceptance criterion."""

    criterion_id: str
    statement: str
    verification: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "statement": self.statement,
            "verification": self.verification,
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any], path: str) -> AcceptanceCriterion:
        _known_keys(doc, CRITERION_KEYS, path)
        _require_keys(doc, ("criterion_id", "statement"), path)
        return AcceptanceCriterion(
            criterion_id=_require_id(doc["criterion_id"], path,
                                     "criterion_id"),
            statement=_require_str(doc["statement"], path, "statement",
                                   maximum=MAX_CRITERION_LEN),
            verification=_optional_str(doc.get("verification"), path,
                                       "verification",
                                       maximum=MAX_CRITERION_LEN),
        )


@dataclass(frozen=True)
class MissionRef:
    """Optional explicit canonical mission reference (never trust evidence)."""

    mission_id: str
    required: bool

    def to_dict(self) -> dict[str, Any]:
        return {"mission_id": self.mission_id, "required": self.required}

    @staticmethod
    def from_dict(doc: Mapping[str, Any], path: str) -> MissionRef:
        _known_keys(doc, MISSION_REF_KEYS, path)
        _require_keys(doc, ("mission_id",), path)
        required = doc.get("required", True)
        return MissionRef(
            mission_id=_require_id(doc["mission_id"], path, "mission_id"),
            required=_require_bool(required, path, "required"),
        )


@dataclass(frozen=True)
class NodeResources:
    """Bounded structured resource requirement suitable for M013 arbitration.

    CPU/GPU/GPU-memory declaration validity reuses the canonical
    ``runs.resources.ResourceRequirement`` validator so the resource
    contract never drifts between packages. ``exclusive`` and
    ``model_heavy`` are graph-level scheduling hints only.
    """

    cpu_slots: int | None = None
    gpu: bool | None = None
    gpu_mem_bytes: int | None = None
    exclusive: bool | None = None
    model_heavy: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_slots": self.cpu_slots,
            "gpu": self.gpu,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "exclusive": self.exclusive,
            "model_heavy": self.model_heavy,
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any] | None, path: str) -> NodeResources:
        if doc is None:
            return NodeResources()
        unknown = set(doc) - set(RESOURCE_KEYS)
        if unknown:
            _fail(E_INVALID_RESOURCE, path,
                  f"unknown field(s): {sorted(unknown)}")
        try:
            runs_resources.ResourceRequirement(
                cpu_slots=doc.get("cpu_slots"),
                gpu=doc.get("gpu"),
                gpu_mem_bytes=doc.get("gpu_mem_bytes"),
            ).validate()
        except runs_resources.ResourcePolicyError as exc:
            _fail(E_INVALID_RESOURCE, path, str(exc))
        return NodeResources(
            cpu_slots=doc.get("cpu_slots"),
            gpu=doc.get("gpu"),
            gpu_mem_bytes=doc.get("gpu_mem_bytes"),
            exclusive=_optional_bool(doc.get("exclusive"), path, "exclusive"),
            model_heavy=_optional_bool(doc.get("model_heavy"), path,
                                       "model_heavy"),
        )


@dataclass(frozen=True)
class NodeBudget:
    """Bounded execution/resource budgets for one node (M013 facing)."""

    subruns: int | None = None
    time_budget_s: int | None = None
    repair_budget: int | None = None
    max_attempts: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subruns": self.subruns,
            "time_budget_s": self.time_budget_s,
            "repair_budget": self.repair_budget,
            "max_attempts": self.max_attempts,
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any] | None, path: str) -> NodeBudget:
        if doc is None:
            return NodeBudget()
        unknown = set(doc) - set(BUDGET_KEYS)
        if unknown:
            _fail(E_INVALID_BUDGET, path,
                  f"unknown field(s): {sorted(unknown)}")
        subruns = doc.get("subruns")
        if subruns is not None:
            subruns = _require_int(subruns, path, "subruns", minimum=1,
                                   maximum=mission_model.MAX_SUBRUNS,
                                   code=E_INVALID_BUDGET)
        time_budget = doc.get("time_budget_s")
        if time_budget is not None:
            time_budget = _require_int(
                time_budget, path, "time_budget_s",
                minimum=mission_model.MIN_TIME_BUDGET_S,
                maximum=mission_model.MAX_TIME_BUDGET_S,
                code=E_INVALID_BUDGET)
        repair_budget = doc.get("repair_budget")
        if repair_budget is not None:
            repair_budget = _require_int(
                repair_budget, path, "repair_budget", minimum=0,
                maximum=mission_model.MAX_REPAIR_ROUNDS,
                code=E_INVALID_BUDGET)
        max_attempts = doc.get("max_attempts")
        if max_attempts is not None:
            max_attempts = _require_int(
                max_attempts, path, "max_attempts", minimum=1,
                maximum=mission_model.MAX_ATTEMPTS_PER_PHASE,
                code=E_INVALID_BUDGET)
        return NodeBudget(
            subruns=subruns,
            time_budget_s=time_budget,
            repair_budget=repair_budget,
            max_attempts=max_attempts,
        )


@dataclass(frozen=True)
class GraphNode:
    """One normalized mission node of the goal graph."""

    node_id: str
    title: str
    priority: int
    depends_on: tuple[str, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    mission_ref: MissionRef | None
    resources: NodeResources
    budgets: NodeBudget

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "priority": self.priority,
            "depends_on": list(self.depends_on),
            "acceptance_criteria": [c.to_dict()
                                    for c in self.acceptance_criteria],
            "mission_ref": (self.mission_ref.to_dict()
                            if self.mission_ref is not None else None),
            "resources": self.resources.to_dict(),
            "budgets": self.budgets.to_dict(),
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any], path: str, *,
                  require_all: bool = False) -> GraphNode:
        _known_keys(doc, NODE_KEYS, path)
        required = (NODE_KEYS if require_all else
                    ("node_id", "title", "priority", "depends_on",
                     "acceptance_criteria"))
        _require_keys(doc, required, path)
        node_id = _require_id(doc["node_id"], path, "node_id")
        title = _require_str(doc["title"], path, "title",
                             maximum=MAX_TITLE_LEN)
        priority = _require_int(doc["priority"], path, "priority",
                                minimum=MIN_PRIORITY, maximum=MAX_PRIORITY,
                                code=E_INVALID_PRIORITY)
        raw_deps = doc["depends_on"]
        if not isinstance(raw_deps, list) or not all(
                isinstance(dep, str) for dep in raw_deps):
            _fail(E_MALFORMED, path, "depends_on must be a list of ids")
        if len(raw_deps) > MAX_DEPENDENCIES_PER_NODE:
            _fail(E_OVERSIZED, path, "too many dependencies")
        deps: list[str] = []
        seen_deps: set[str] = set()
        for dep in raw_deps:
            dep_id = _require_id(dep, path, "depends_on")
            if dep_id == node_id:
                _fail(E_SELF_DEPENDENCY, path, node_id)
            if dep_id in seen_deps:
                _fail(E_DUPLICATE_EDGE, path, dep_id)
            seen_deps.add(dep_id)
            deps.append(dep_id)
        raw_criteria = doc["acceptance_criteria"]
        if not isinstance(raw_criteria, list) or not raw_criteria:
            _fail(E_INVALID_ACCEPTANCE, path, "acceptance_criteria required")
        if len(raw_criteria) > MAX_ACCEPTANCE_CRITERIA_PER_NODE:
            _fail(E_OVERSIZED, path, "too many acceptance criteria")
        criteria: list[AcceptanceCriterion] = []
        seen_criteria: set[str] = set()
        for index, raw in enumerate(raw_criteria):
            cpath = f"{path}[acceptance_criteria/{index}]"
            if not isinstance(raw, dict):
                _fail(E_INVALID_ACCEPTANCE, cpath, "object required")
            criterion = AcceptanceCriterion.from_dict(raw, cpath)
            if criterion.criterion_id in seen_criteria:
                _fail(E_DUPLICATE_CRITERION, cpath, criterion.criterion_id)
            seen_criteria.add(criterion.criterion_id)
            criteria.append(criterion)
        raw_ref = doc.get("mission_ref")
        mission_ref: MissionRef | None = None
        if raw_ref is not None:
            if not isinstance(raw_ref, dict):
                _fail(E_MALFORMED, path, "mission_ref must be an object")
            mission_ref = MissionRef.from_dict(raw_ref, f"{path}[mission_ref]")
        raw_resources = doc.get("resources")
        if raw_resources is not None and not isinstance(raw_resources, dict):
            _fail(E_INVALID_RESOURCE, path, "resources must be an object")
        raw_budgets = doc.get("budgets")
        if raw_budgets is not None and not isinstance(raw_budgets, dict):
            _fail(E_INVALID_BUDGET, path, "budgets must be an object")
        return GraphNode(
            node_id=node_id,
            title=title,
            priority=priority,
            depends_on=tuple(sorted(deps)),
            acceptance_criteria=tuple(
                sorted(criteria, key=lambda c: c.criterion_id)),
            mission_ref=mission_ref,
            resources=NodeResources.from_dict(
                raw_resources, f"{path}[resources]"),
            budgets=NodeBudget.from_dict(raw_budgets, f"{path}[budgets]"),
        )


@dataclass(frozen=True)
class GraphEdge:
    """Explicit dependency edge: ``source`` must complete before ``target``."""

    source: str
    target: str

    def to_dict(self) -> dict[str, str]:
        return {"from": self.source, "to": self.target}


@dataclass(frozen=True)
class GraphProvenance:
    """Graph creation provenance (which spec + repository baseline created it)."""

    created_at: str
    created_by: str
    repo_root: str | None
    baseline_revision: str | None
    domain: str = graph_identity.GRAPH_DOMAIN
    spec_domain: str = graph_identity.SPEC_DOMAIN

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "spec_domain": self.spec_domain,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "repository": {
                "repo_root": self.repo_root,
                "baseline_revision": self.baseline_revision,
            },
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any], path: str) -> GraphProvenance:
        _known_keys(doc, ("domain", "spec_domain", "created_at", "created_by",
                          "repository"), path)
        _require_keys(doc, ("domain", "spec_domain", "created_at",
                            "created_by", "repository"), path)
        domain = doc["domain"]
        if domain != graph_identity.GRAPH_DOMAIN:
            _fail(E_MALFORMED, path, f"unsupported graph domain: {domain!r}")
        spec_domain = doc["spec_domain"]
        if spec_domain != graph_identity.SPEC_DOMAIN:
            _fail(E_MALFORMED, path,
                  f"unsupported spec domain: {spec_domain!r}")
        repository = _require_mapping(doc["repository"], path, "repository")
        _known_keys(repository, ("repo_root", "baseline_revision"),
                    f"{path}[repository]")
        return GraphProvenance(
            created_at=_require_str(doc["created_at"], path, "created_at",
                                    maximum=MAX_TIMESTAMP_LEN),
            created_by=_require_str(doc["created_by"], path, "created_by",
                                    maximum=MAX_CREATED_BY_LEN),
            repo_root=_optional_str(
                repository.get("repo_root"), f"{path}[repository]",
                "repo_root", maximum=mission_model.MAX_COMMAND_PART_LEN),
            baseline_revision=_optional_str(
                repository.get("baseline_revision"),
                f"{path}[repository]", "baseline_revision",
                maximum=MAX_REVISION_LEN),
        )


def _spec_payload(goal_id: str, objective: str,
                  nodes: tuple[GraphNode, ...]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "goal_id": goal_id,
        "objective": objective,
        "nodes": [node.to_dict() for node in nodes],
    }


def _graph_payload(goal_id: str, objective: str,
                   nodes: tuple[GraphNode, ...],
                   edges: tuple[GraphEdge, ...]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "goal_id": goal_id,
        "objective": objective,
        "nodes": [node.to_dict() for node in nodes],
        "edges": [edge.to_dict() for edge in edges],
    }


@dataclass(frozen=True)
class NormalizedSpec:
    """A validated, normalized declarative goal spec (no provenance yet)."""

    goal_id: str
    objective: str
    nodes: tuple[GraphNode, ...]


@dataclass(frozen=True)
class GoalGraph:
    """Normalized, bounded, deterministic goal decomposition graph."""

    schema_version: int
    goal_id: str
    objective: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    provenance: GraphProvenance
    spec_sha256: str
    graph_id: str

    def node_map(self) -> dict[str, GraphNode]:
        return {node.node_id: node for node in self.nodes}

    def dependencies(self, node_id: str) -> tuple[str, ...]:
        return self.node_map()[node_id].depends_on

    def topological_order(self) -> tuple[str, ...]:
        """Deterministic topological order (priority DESC, node_id ASC)."""
        node_map = self.node_map()
        indegree = {nid: len(node.depends_on) for nid, node in node_map.items()}
        dependents: dict[str, list[str]] = {nid: [] for nid in node_map}
        for edge in self.edges:
            dependents[edge.source].append(edge.target)
        for targets in dependents.values():
            targets.sort()
        ready: list[tuple[int, str]] = [
            (-node_map[nid].priority, nid)
            for nid, degree in indegree.items() if degree == 0
        ]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            _, nid = heapq.heappop(ready)
            order.append(nid)
            for target in dependents[nid]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    heapq.heappush(
                        ready, (-node_map[target].priority, target))
        if len(order) != len(node_map):  # pragma: no cover - validated earlier
            _fail(E_CYCLE, "graph", "topological order incomplete")
        return tuple(order)

    @staticmethod
    def build(
        *,
        goal_id: str,
        objective: str,
        nodes: Sequence[GraphNode],
        provenance: GraphProvenance,
        spec_sha256: str | None = None,
        graph_id: str | None = None,
    ) -> GoalGraph:
        node_ids = [node.node_id for node in nodes]
        if len(set(node_ids)) != len(node_ids):
            _fail(E_DUPLICATE_NODE, "graph", "duplicate node id")
        _validate_references(nodes, "graph")
        _detect_cycle(nodes, "graph")
        normalized = tuple(sorted(nodes, key=lambda n: n.node_id))
        edges = derive_edges(normalized)
        spec_digest = graph_identity.digest(
            graph_identity.SPEC_DOMAIN,
            _spec_payload(goal_id, objective, normalized))
        graph_digest = graph_identity.digest(
            graph_identity.GRAPH_DOMAIN,
            _graph_payload(goal_id, objective, normalized, edges))
        if spec_sha256 is not None and spec_sha256 != spec_digest:
            _fail(E_IDENTITY_MISMATCH, "spec_sha256", spec_sha256)
        if graph_id is not None and graph_id != graph_digest:
            _fail(E_IDENTITY_MISMATCH, "graph_id", graph_id)
        return GoalGraph(
            schema_version=SCHEMA_VERSION,
            goal_id=goal_id,
            objective=objective,
            nodes=normalized,
            edges=edges,
            provenance=provenance,
            spec_sha256=spec_digest,
            graph_id=graph_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal_id": self.goal_id,
            "objective": self.objective,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "created_at": self.provenance.created_at,
            "graph_id": self.graph_id,
            "spec_sha256": self.spec_sha256,
            "provenance": self.provenance.to_dict(),
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any], path: str) -> GoalGraph:
        _known_keys(
            doc,
            ("schema_version", "goal_id", "objective", "nodes", "edges",
             "created_at", "graph_id", "spec_sha256", "provenance"),
            path)
        _require_keys(
            doc,
            ("schema_version", "goal_id", "objective", "nodes", "edges",
             "created_at", "graph_id", "spec_sha256", "provenance"),
            path)
        version = doc["schema_version"]
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        goal_id = _require_id(doc["goal_id"], path, "goal_id")
        objective = _require_str(doc["objective"], path, "objective",
                                 maximum=MAX_OBJECTIVE_LEN)
        raw_nodes = doc["nodes"]
        if not isinstance(raw_nodes, list):
            _fail(E_MALFORMED, path, "nodes must be a list")
        if not raw_nodes:
            _fail(E_EMPTY_GRAPH, path, "nodes must be a non-empty list")
        if len(raw_nodes) > MAX_NODES:
            _fail(E_OVERSIZED, path, "too many nodes")
        nodes: list[GraphNode] = []
        seen_nodes: set[str] = set()
        for index, raw in enumerate(raw_nodes):
            npath = f"{path}[nodes/{index}]"
            if not isinstance(raw, dict):
                _fail(E_MALFORMED, npath, "node object required")
            node = GraphNode.from_dict(raw, npath, require_all=True)
            if node.node_id in seen_nodes:
                _fail(E_DUPLICATE_NODE, npath, node.node_id)
            seen_nodes.add(node.node_id)
            nodes.append(node)
        edges = _normalize_edges(doc["edges"], path)
        provenance = GraphProvenance.from_dict(
            _require_mapping(doc["provenance"], path, "provenance"),
            f"{path}[provenance]")
        if doc["created_at"] != provenance.created_at:
            _fail(E_MALFORMED, path, "created_at/provenance mismatch")
        spec_sha256 = _require_str(doc["spec_sha256"], path, "spec_sha256",
                                   maximum=64)
        graph_id = _require_str(doc["graph_id"], path, "graph_id",
                                maximum=64)
        _validate_references(nodes, path)
        _detect_cycle(nodes, path)
        expected_edges = derive_edges(tuple(nodes))
        if edges != expected_edges:
            _fail(E_MALFORMED, path, "edge set does not match depends_on")
        return GoalGraph.build(
            goal_id=goal_id,
            objective=objective,
            nodes=tuple(nodes),
            provenance=provenance,
            spec_sha256=spec_sha256,
            graph_id=graph_id,
        )


def derive_edges(nodes: Sequence[GraphNode]) -> tuple[GraphEdge, ...]:
    """Explicit, deterministic edge set derived from ``depends_on`` only."""
    edges = [
        GraphEdge(source=dep, target=node.node_id)
        for node in nodes
        for dep in node.depends_on
    ]
    edges.sort(key=lambda e: (e.source, e.target))
    if len(edges) > MAX_EDGES:
        _fail(E_OVERSIZED, "edges", f"more than {MAX_EDGES} edges")
    return tuple(edges)


def _normalize_edges(raw_edges: object, path: str) -> tuple[GraphEdge, ...]:
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        _fail(E_MALFORMED, path, "edges must be a list")
    if len(raw_edges) > MAX_EDGES:
        _fail(E_OVERSIZED, path, "too many edges")
    edges: list[GraphEdge] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_edges):
        epath = f"{path}[edges/{index}]"
        if not isinstance(raw, dict):
            _fail(E_MALFORMED, epath, "edge object required")
        _known_keys(raw, ("from", "to"), epath)
        _require_keys(raw, ("from", "to"), epath)
        source = _require_id(raw["from"], epath, "from")
        target = _require_id(raw["to"], epath, "to")
        key = (source, target)
        if key in seen:
            _fail(E_DUPLICATE_EDGE, epath, f"{source}->{target}")
        seen.add(key)
        edges.append(GraphEdge(source=source, target=target))
    return tuple(sorted(edges, key=lambda e: (e.source, e.target)))


def _validate_references(nodes: Sequence[GraphNode], path: str) -> None:
    ids = {node.node_id for node in nodes}
    mission_ids: dict[str, str] = {}
    for node in nodes:
        for dep in node.depends_on:
            if dep not in ids:
                _fail(E_MISSING_DEPENDENCY, path,
                      f"{node.node_id} -> {dep}")
        ref = node.mission_ref
        if ref is None:
            continue
        previous = mission_ids.get(ref.mission_id)
        if previous is not None:
            _fail(E_DUPLICATE_MISSION_REF, path,
                  f"mission {ref.mission_id!r} referenced by {previous!r} "
                  f"and {node.node_id!r}")
        mission_ids[ref.mission_id] = node.node_id


def normalize_spec(doc: object, *, path: str = "spec") -> NormalizedSpec:
    """Strictly normalize a declarative spec (pure; no mission access).

    Mission references are *not* resolved here; reference resolution and
    the required-reference fail-closed check are performed by the store at
    creation/validation time.
    """
    if not isinstance(doc, dict):
        _fail(E_MALFORMED, path, "spec must be a JSON object")
    _known_keys(doc, SPEC_KEYS, path)
    _require_keys(doc, ("schema_version", "goal_id", "objective", "nodes"),
                  path)
    version = doc["schema_version"]
    if version != SCHEMA_VERSION:
        _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
    goal_id = _require_id(doc["goal_id"], path, "goal_id")
    objective = _require_str(doc["objective"], path, "objective",
                             maximum=MAX_OBJECTIVE_LEN)
    raw_nodes = doc["nodes"]
    if not isinstance(raw_nodes, list):
        _fail(E_MALFORMED, path, "nodes must be a list")
    if not raw_nodes:
        _fail(E_EMPTY_GRAPH, path, "nodes must be a non-empty list")
    if len(raw_nodes) > MAX_NODES:
        _fail(E_OVERSIZED, path, "too many nodes")
    nodes: list[GraphNode] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_nodes):
        npath = f"{path}[nodes/{index}]"
        if not isinstance(raw, dict):
            _fail(E_MALFORMED, npath, "node object required")
        node = GraphNode.from_dict(raw, npath)
        if node.node_id in seen:
            _fail(E_DUPLICATE_NODE, npath, node.node_id)
        seen.add(node.node_id)
        nodes.append(node)
    _validate_references(nodes, path)
    _detect_cycle(nodes, path)
    derive_edges(tuple(nodes))  # bounded edge-count check
    return NormalizedSpec(
        goal_id=goal_id,
        objective=objective,
        nodes=tuple(sorted(nodes, key=lambda n: n.node_id)),
    )


def _detect_cycle(nodes: Sequence[GraphNode], path: str) -> None:
    """Bounded deterministic DFS cycle detection with an explicit path.

    Depth is bounded by ``MAX_NODES`` (the graph is validated to hold no
    more than 64 nodes), so recursion can never run away.
    """
    graph: dict[str, tuple[str, ...]] = {
        node.node_id: node.depends_on for node in nodes
    }
    white, grey, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    stack: list[str] = []

    def visit(node_id: str) -> None:
        color[node_id] = grey
        stack.append(node_id)
        for dep in graph[node_id]:
            if color[dep] == grey:
                cycle_start = stack.index(dep)
                cycle = [*stack[cycle_start:], dep]
                _fail(E_CYCLE, path, " -> ".join(cycle))
            if color[dep] == white:
                visit(dep)
        stack.pop()
        color[node_id] = black

    for node_id in sorted(graph):
        if color[node_id] == white:
            visit(node_id)
