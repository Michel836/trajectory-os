"""V1.97–V2.06 final review-fix regressions (surgical, behavior-pinning).

Regression coverage for the five review findings fixed in this pass:

* BLOCKER — the supervisor no longer carries its own ``started_by_job``
  traversal; it uses the canonical ``orchestration.started_by_jobs``
  (asserted for exact equality with the canonical function, on a deep
  chain that would exhaust the interpreter recursion limit, and on a
  cycle — no RecursionError, no infinite loop, deterministic output);
* MAJOR — the supervisor stop-reason control flow selects exactly ONE
  authoritative stop per cycle (duplicate identity / admission reject /
  launch bound each halt with exactly one stop code and one stop
  reason; a single cycle can never record two conflicting stops);
* MAJOR — resource REQUIREMENT declaration validity is type/domain only
  (unbounded-magnitude positive integers serialize, round-trip and are
  evaluated against capacity evidence), while capacity ADMISSIBILITY is
  decided by ``resources.evaluate`` against authoritative evidence;
  invalid declaration values still fail closed with
  ``ResourcePolicyError``, and the bounded slot domain is preserved;
* MINOR — ``dependencies._dfs_from`` keeps an honest docstring of its
  actual bounds (iterative; O(1) position lookup; O(depth) path storage;
  O(nodes + edges) steps), proven by deep-chain and cycle behavior;
* MINOR — ``observability._as_mapping`` fails CLOSED (deterministic
  ``TypeError``) on non-Mapping input instead of silently degrading to
  a plausible empty mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_os.runs import (
    dependencies,
    model,
    observability,
    resources,
    spec,
    store,
    supervisor,
)
from trajectory_os.runs import orchestration as orch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paths(tmp_path, name: str) -> tuple[object, object]:
    state_root = tmp_path / f"state-{name}"
    runs_root = tmp_path / f"runs-{name}"
    state_root.mkdir()
    runs_root.mkdir()
    return state_root, runs_root


def _closed_record(job_id: str, terminal: str, attempts: int = 1) -> store.ClosedRecord:
    return store.ClosedRecord(
        job_id=job_id,
        seq=1,
        observed_at=store.utc_now_iso(),
        terminal=terminal,
        attempts=attempts,
        exit_code=0 if terminal == model.TERMINAL_DONE else None,
    )


def _seed_closed(state_root: object, records: list[store.ClosedRecord]) -> None:
    paths = store.state_paths(state_root)
    store.save_closed_records(paths["closed"], records)


def _enqueue(state_root: object, job_id: str, depends_on: tuple[str, ...] = ()) -> None:
    paths = store.state_paths(state_root)
    doc = store.QueueDoc.load(paths["queue"])
    sp = spec.build_spec(job_id, ["true"], spec.EXEC_AD_HOC, depends_on=tuple(depends_on))
    doc.enqueue_spec(sp)
    doc.save(paths["queue"])


# ---------------------------------------------------------------------------
# Finding 1 (BLOCKER) — canonical started-by: no duplicated traversal
# ---------------------------------------------------------------------------


def test_supervisor_uses_canonical_started_by(tmp_path):
    """Supervisor summary must equal the canonical orchestration closure.

    The removed local traversal lived in ``supervisor`` itself; the module
    may no longer define it, and the summary must be byte-identical to
    what ``orchestration.started_by_jobs`` returns for the same state.
    """
    state_root, runs_root = _paths(tmp_path, "canon")
    # C (failed) blocks B; A (done) and C (failed) are its prerequisites.
    _seed_closed(
        state_root,
        [
            _closed_record("A", model.TERMINAL_DONE),
            _closed_record("C", model.TERMINAL_FAILED),
        ],
    )
    _enqueue(state_root, "B", ("A", "C"))

    summary = supervisor.run_session(
        state_root, runs_root, config=supervisor.SupervisorConfig(cycles=1, capacity=1)
    )

    canonical = orch.started_by_jobs(orch.rebuild_state(state_root))
    expected = {job: list(deps) for job, deps in canonical.items()}
    assert summary["started_by_job"] == expected == {"B": ["A", "C"]}
    # The session launched nothing (blocked by the failed prerequisite C)
    # and recorded exactly one authoritative stop with no stop codes.
    assert summary["totals"]["launched"] == 0
    assert summary["stop"]["reason"] == model.STOP_NO_ELIGIBLE_WORK
    assert summary["stop"]["codes"] == []


def test_supervisor_module_has_no_local_started_by_traversal():
    """The duplicated traversal (the BLOCKER) must be gone from supervisor."""
    assert not hasattr(supervisor, "started_by_job"), (
        "supervisor.started_by_job is the removed local duplicate; "
        "use orchestration.started_by_jobs"
    )


def _in_memory_bundle(queue_edges: dict[str, tuple[str, ...]], known: set[str]):
    """Build a StateBundle in memory (no I/O) for the canonical started-by walk.

    ``started_by_jobs`` closes over a node ONLY when the node is both
    *known* (active or closed -> the ``ids`` filter) and *in the graph*
    (a queue entry carrying ``depends_on`` -> a graph edge). So to exercise a
    deep chain or a cycle, every node on the path must be placed in BOTH
    ``known`` (as a closed record) and ``queue_edges`` (as a queued spec).
    This is a legitimate state (a job re-queued after closing) and the walk
    only reads ``queue``/``active``/``closed`` — no file I/O is required.
    """
    queue_entries: list[store.QueueEntry] = []
    for seq, (job_id, deps) in enumerate(sorted(queue_edges.items()), start=1):
        sp = spec.build_spec(job_id, ["true"], spec.EXEC_AD_HOC, depends_on=tuple(deps))
        queue_entries.append(
            store.QueueEntry(
                seq=seq,
                job_id=job_id,
                enqueued_at=store.utc_now_iso(),
                attempts=0,
                command=["true"],
                max_attempts=1,
                retry_wait=0,
                spec=sp,
            )
        )
    closed = [
        store.ClosedRecord(
            job_id=job_id,
            seq=seq,
            observed_at=store.utc_now_iso(),
            terminal=model.TERMINAL_DONE,
            attempts=1,
            exit_code=0,
        )
        for seq, job_id in enumerate(sorted(known), start=1)
    ]
    return orch.StateBundle(
        base=Path("/tmp/tor") / f"bundle-{id(queue_edges)}",
        queue_path="/tmp/tor-q",
        active_path="/tmp/tor-a",
        closed_path="/tmp/tor-c",
        workspaces="/tmp/tor-w",
        queue=store.QueueDoc(seq=len(queue_edges), entries=queue_entries),
        active=[],
        closed=closed,
        active_proven={},
    )


def test_canonical_started_by_deep_chain_no_recursion_error():
    """Deep but narrow chains must not hit the interpreter recursion limit.

    Default Python recursion limit is 1000; a 1500-deep transitive closure
    (j1500 -> j1499 -> ... -> j0) would RecursionError a naive recursive
    implementation. The canonical walk is iterative: O(nodes + edges) work,
    each job visited at most once. Every chain node is both known (closed)
    and in the graph (queued), so the closure is genuinely deep.
    """
    n = 1500
    known = {f"j{i}" for i in range(0, n + 1)}
    queue_edges = {f"j{i}": (f"j{i - 1}",) for i in range(1, n + 1)}
    bundle = _in_memory_bundle(queue_edges, known)

    result = orch.started_by_jobs(bundle)

    # Keys are the graph jobs (those carrying depends_on).
    assert set(result) == set(queue_edges)
    # j_n's transitive prerequisites are exactly j_0 .. j_{n-1}.
    assert set(result[f"j{n}"]) == {f"j{i}" for i in range(0, n)}
    assert len(result[f"j{n}"]) == n
    # Deterministic sorted output; the root's own id is never a prerequisite.
    assert result[f"j{n}"] == tuple(sorted(f"j{i}" for i in range(0, n)))
    assert f"j{n}" not in result[f"j{n}"]
    # Two-level closure sanity (O(depth), not O(depth^2)).
    assert result["j3"] == ("j0", "j1", "j2")


def test_canonical_started_by_cycle_terminates():
    """A dependency cycle must terminate with deterministic output.

    Both nodes are known (closed) and in the graph (queued); a->b->a forms a
    2-cycle. The iterative walk must terminate (no infinite loop) and produce
    each prerequisite at most once (no duplicated entries), including both
    endpoints (a's closure reaches b, and b's edge brings a back in).
    """
    bundle = _in_memory_bundle({"a": ("b",), "b": ("a",)}, known={"a", "b"})
    result = orch.started_by_jobs(bundle)
    # Both jobs' closures contain the other; cycle-safe, deterministic, no dupes.
    assert result["a"] == ("a", "b")
    assert result["b"] == ("a", "b")
    list(result["a"])  # tuple of unique sorted ids (no repetitions possible)
    assert result["a"] == tuple(sorted(set(result["a"])))


# ---------------------------------------------------------------------------
# Finding 2 (MAJOR) — exactly ONE authoritative stop per supervisor cycle
# ---------------------------------------------------------------------------


def test_supervisor_duplicate_identity_halts_with_single_stop(tmp_path):
    state_root, runs_root = _paths(tmp_path, "dup")
    _seed_closed(state_root, [_closed_record("dup-1", model.TERMINAL_DONE)])
    _enqueue(state_root, "dup-1")

    summary = supervisor.run_session(
        state_root, runs_root, config=supervisor.SupervisorConfig(cycles=3, capacity=2)
    )

    # Exactly one stop code and exactly one stop reason — the halt branch
    # must not be overwritten or contradicted by termination logic.
    assert summary["stop"]["codes"] == ["DUPLICATE_IDENTITY:dup-1"]
    assert summary["stop"]["reason"] == model.STOP_ADMISSION_BLOCKED
    assert summary["stop"]["reason"] is not None
    assert summary["stop"]["reason"] in {
        model.STOP_ADMISSION_BLOCKED,
        model.STOP_CYCLE_BOUND_EXHAUSTED,
        model.STOP_NO_ELIGIBLE_WORK,
        model.STOP_WORK_SETTLED,
    }
    assert summary["totals"]["launched"] == 0
    # The offending queue entry must be preserved (no silent mutation).
    paths = store.state_paths(state_root)
    doc = store.QueueDoc.load(paths["queue"])
    assert [entry.job_id for entry in doc.entries] == ["dup-1"]


def test_supervisor_launch_reject_halts_with_single_stop(tmp_path):
    """Admission rejection surfaces as exactly one authoritative stop."""
    state_root, runs_root = _paths(tmp_path, "rej")
    for i in range(model.MAX_QUEUE_ENTRIES):
        _enqueue(state_root, f"q{i:02d}")

    summary = supervisor.run_session(
        state_root, runs_root, config=supervisor.SupervisorConfig(cycles=3, capacity=1)
    )

    assert summary["stop"]["codes"] == ["QUEUE_FULL"]
    assert summary["stop"]["reason"] == model.STOP_ADMISSION_BLOCKED
    assert summary["totals"]["launched"] == 0
    paths = store.state_paths(state_root)
    doc = store.QueueDoc.load(paths["queue"])
    assert len(doc.entries) == model.MAX_QUEUE_ENTRIES


def test_supervisor_launch_bound_halts_with_single_stop(tmp_path):
    state_root, runs_root = _paths(tmp_path, "bound")
    _enqueue(state_root, "one")
    _enqueue(state_root, "two")

    summary = supervisor.run_session(
        state_root,
        runs_root,
        config=supervisor.SupervisorConfig(
            cycles=3, capacity=2, launches_bound=1
        ),
    )

    # Cycle 1 launches "one"; cycle 2 must halt on the launch bound BEFORE
    # any further launch — exactly one stop code and one stop reason.
    assert summary["stop"]["codes"] == ["SESSION_LAUNCH_BOUND_EXHAUSTED"]
    assert summary["stop"]["reason"] == model.STOP_CYCLE_BOUND_EXHAUSTED
    assert summary["totals"]["launched"] == 1
    assert [item["job_id"] for item in summary["started"]] == ["one"]


def _sentinel_active() -> list[object]:
    # ``_resource_gate`` only checks ``not active`` (truthiness); a stable,
    # non-empty sentinel stands in for a live active record without spawning
    # a real child process (which would make the test liveness-fragile on CI).
    return [object()]


def test_resource_gate_fails_closed_and_is_single_value():
    """The fail-closed resource gate is a deterministic, single stop value.

    With a capacity policy in force AND any active work, active usage is
    unproven, so the gate withholds the launch and returns exactly ONE
    canonical stop code (the supervisor selects it as the first-priority
    stop, before duplicate-identity / eligibility). With no capacity policy
    or no active work the gate defers (None) so other stops may apply.
    """
    cap = resources.ResourceCapacity(cpu_slots=1)
    gate = supervisor._resource_gate(cap, _sentinel_active())
    assert gate == f"{model.REASON_RESOURCE_UNKNOWN}:active_usage_unproven"
    # Deterministic, single, typed stop code (not a list of alternatives).
    assert isinstance(gate, str) and ":" in gate and len(gate.split(":")) == 2

    # Defers (no authoritative stop) when either precondition is absent.
    assert supervisor._resource_gate(None, _sentinel_active()) is None
    assert supervisor._resource_gate(cap, []) is None


# ---------------------------------------------------------------------------
# Finding 3 (MAJOR) — declaration validity vs capacity admissibility
# ---------------------------------------------------------------------------


def test_large_ram_declaration_serializes_and_round_trips():
    """Unbounded-magnitude ram_bytes are valid declarations (no MAX_BYTES).

    1<<60 is 1 EiB — beyond any single node; the declaration itself is a
    well-formed JSON-serializable positive integer and must pass.
    """
    big = 1 << 60
    req = resources.ResourceRequirement(ram_bytes=big).validate()
    assert req.declared
    d = req.to_dict()
    assert d["ram_bytes"] == big
    # JSON round-trip (the actual persistence boundary) is exact.
    rt = json.loads(json.dumps(d))
    assert rt["ram_bytes"] == big
    assert resources.ResourceRequirement.from_dict(rt).to_dict() == d
    assert req.dimensions()["ram_bytes"] == big


def test_capacity_evidence_accepts_large_values_verbatim():
    """Capacity parsing must not impose a hidden cap on byte dimensions."""
    cap = resources.policy_from_evidence({"ram_bytes": 1 << 50})
    assert cap.ram_bytes == 1 << 50


def test_unknown_capacity_evidence_defers_large_requirement():
    req = resources.ResourceRequirement(ram_bytes=1 << 60).validate()
    cap = resources.policy_from_evidence({"ram_bytes": None})  # unknown evidence
    decision = resources.evaluate(req, cap)
    assert not decision.allowed
    assert f"{model.REASON_RESOURCE_UNKNOWN}:ram_bytes" in decision.reasons


def test_insufficient_capacity_evidence_defers_large_requirement():
    req = resources.ResourceRequirement(ram_bytes=1 << 60).validate()
    cap = resources.policy_from_evidence({"ram_bytes": 1 << 40})
    decision = resources.evaluate(req, cap)
    assert not decision.allowed
    assert f"{model.REASON_RESOURCE_EXHAUSTED}:ram_bytes" in decision.reasons


def test_sufficient_capacity_evidence_allows_large_requirement():
    req = resources.ResourceRequirement(ram_bytes=1 << 50).validate()
    cap = resources.policy_from_evidence({"ram_bytes": 1 << 55})
    decision = resources.evaluate(req, cap)
    assert decision.allowed
    assert decision.reasons == ()


def test_no_arbitrary_magitude_cap_remains_in_resources():
    """The removed MAX_BYTES constant must not reappear under any name."""
    assert not hasattr(resources, "MAX_BYTES"), (
        "resources.MAX_BYTES is the removed arbitrary byte cap"
    )


@pytest.mark.parametrize(
    "bad",
    [
        -1,  # negative
        0,  # zero (declared dimensions must be positive)
        True,  # bool is not an integer here
        "8388608",  # string
        8.5,  # float
        ["1"],  # list
        {"ram_bytes": 1},  # dict
    ],
)
def test_invalid_declaration_values_fail_closed(bad):
    """Malformed declaration values must raise, never coerce."""
    with pytest.raises(resources.ResourcePolicyError):
        resources.ResourceRequirement(ram_bytes=bad).validate()
    with pytest.raises(resources.ResourcePolicyError):
        resources.ResourceRequirement.from_dict({"ram_bytes": bad})


@pytest.mark.parametrize("value", [True, "8", 8.5, ["1"], {"ram_bytes": 1}, -1, -100])
def test_invalid_capacity_values_fail_closed(value):
    """Malformed capacity types (bool/str/float/list/dict) and negative values
    fail closed (never coerced)."""
    with pytest.raises(resources.ResourcePolicyError):
        resources.policy_from_evidence({"ram_bytes": value})


@pytest.mark.parametrize("value", [None, "unknown"])
def test_capacity_not_provided_is_unknown(value):
    """For CAPACITY, ``None``/``"unknown"`` mean not-provided -> UNKNOWN evidence
    (deferred, never invented)."""
    cap = resources.policy_from_evidence({"ram_bytes": value})
    assert cap.ram_bytes is None


def test_capacity_zero_is_valid_evidence():
    """``0`` is a legitimate capacity value (0 available), distinct from
    not-provided; it must round-trip verbatim, not collapse to None."""
    cap = resources.policy_from_evidence({"ram_bytes": 0})
    assert cap.ram_bytes == 0


def test_slot_domain_remains_bounded_for_cpu():
    """The bounded slot domain (scheduling units) is preserved.

    Only byte dimensions are unbounded; cpu_slots stays within
    MAX_CPU_SLOTS (a scheduling-unit count, not a byte count).
    """
    # Boundary is still accepted.
    ok = resources.ResourceRequirement(cpu_slots=resources.MAX_CPU_SLOTS).validate()
    assert ok.cpu_slots == resources.MAX_CPU_SLOTS
    # Over the bound is still rejected (bounded domain).
    with pytest.raises(resources.ResourcePolicyError):
        resources.ResourceRequirement(cpu_slots=resources.MAX_CPU_SLOTS + 1).validate()


def test_gpu_mem_requires_gpu_true_still_enforced():
    """Inconsistent declaration (gpu_mem without gpu=true) still fails."""
    with pytest.raises(resources.ResourcePolicyError):
        resources.ResourceRequirement(
            gpu=False, gpu_mem_bytes=1 << 30
        ).validate()
    # Consistent declaration passes (unbounded bytes).
    ok = resources.ResourceRequirement(gpu=True, gpu_mem_bytes=1 << 40).validate()
    assert ok.gpu is True
    assert ok.gpu_mem_bytes == 1 << 40


# ---------------------------------------------------------------------------
# Finding 4 (MINOR) — honest _dfs_from docs, proven by behavior
# ---------------------------------------------------------------------------


def test_dfs_from_docstring_states_actual_complexity():
    doc = dependencies._dfs_from.__doc__
    assert doc is not None
    # The docstring must state the implementation is iterative and bound.
    lowered = doc.lower()
    assert "iterative" in lowered or "explicit stack" in lowered
    # It must NOT claim a worst-case it does not have (node x edges).
    assert "n * e" not in lowered and "n*e" not in lowered
    # Honest bounds: O(depth) path storage, O(1) position lookup,
    # O(nodes + edges) total steps for a single walk.
    assert "o(depth)" in lowered or "depth" in lowered
    assert "o(nodes + edges)" in lowered or "o(n + e)" in lowered


def test_dfs_from_terminates_on_deep_diamond_chain():
    """A long chain with a back-edge must not loop and must report the cycle.

    Depth 2000 exceeds the default recursion limit; an iterative walker
    returns the closed cycle deterministically.
    """
    n = 2000
    g: dict[str, tuple[str, ...]] = {}
    for i in range(1, n):
        g[f"n{i}"] = (f"n{i + 1}",)
    # Close the cycle: n_n -> n_2 (not n_1), forming one back-edge.
    g[f"n{n}"] = ("n2",)
    cycle = dependencies.find_dependency_cycle("n1", g)
    assert cycle is not None
    # The cycle must be a closed path (first id appears at the end).
    assert cycle[0] == cycle[-1]
    assert "n2" in cycle and f"n{n}" in cycle


def test_dfs_from_rejects_self_reference_deterministically():
    # A self-referential node is a length-1 cycle in its own graph slot.
    # (Note: validate_depends_on rejects self-references at the spec level;
    # the walker itself must still be safe given such a graph.)
    cycle = dependencies._dfs_from("a", {"a": ("a",)}, {})
    assert cycle == ("a", "a")


# ---------------------------------------------------------------------------
# Finding 5 (MINOR) — observability _as_mapping fails closed
# ---------------------------------------------------------------------------


def test_as_mapping_accepts_mappings_identity():
    m: dict[str, object] = {"active": 1, "queued": 0, "closed": 3}
    assert observability._as_mapping(m) is m


@pytest.mark.parametrize(
    "bad", ["nope", 3, ["x"], 1.5, object(), ("a", "b")]
)
def test_as_mapping_rejects_non_mappings(bad):
    with pytest.raises(TypeError) as ei:
        observability._as_mapping(bad)
    # Deterministic, typed, and diagnostic (names the offending type).
    assert "OBSERVABILITY_MALFORMED" in str(ei.value)
    assert type(bad).__name__ in str(ei.value)


def test_as_mapping_rejects_none():
    with pytest.raises(TypeError):
        observability._as_mapping(None)


def test_render_human_rejects_malformed_counts():
    with pytest.raises(TypeError):
        observability.render_human({"counts": "active=1"})


def test_render_human_rejects_malformed_active_ownership():
    doc = {
        "counts": {},
        "active": [{"job_id": "x", "ownership": "dead"}],
    }
    with pytest.raises(TypeError):
        observability.render_human(doc)


def test_render_human_still_renders_valid_documents(tmp_path):
    state_root, runs_root = _paths(tmp_path, "render")
    _seed_closed(state_root, [_closed_record("done-job", model.TERMINAL_DONE)])
    _enqueue(state_root, "queued-job")
    doc = observability.build_ops_document(state_root, runs_root)
    text = observability.render_human(doc)
    assert text.startswith("ops:")
    # The header reports the stable counts (one queued, one closed, none active).
    assert "queued=1" in text
    assert "closed=1" in text
    assert "active=0" in text
    # The closed section renders its job id (stable doc shape).
    assert "done-job" in text
