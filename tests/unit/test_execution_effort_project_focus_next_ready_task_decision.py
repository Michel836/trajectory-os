"""Unit tests for V1.42 — explicit human-accepted next READY TASK decision.

Covers:

  - genuine / hostile input discipline (genuine V1.41 + exact accepted
    pair accepted; None, dict, string, foreign model, hostile top-level
    ``model_construct`` scalars — including a hostile selected project
    ID, hostile selected task ID, and hostile ``ready_task_count`` —
    rejected; and proof that no semantic reads are served from the
    caller-owned V1.41 object after fresh re-validation);
  - the zero-candidate policy (zero READY / no selected IDs rejected;
    hostile constructed partial selected states rejected; no decision
    object can represent an accepted empty selection);
  - exact human acceptance (exact project + task accepted; wrong
    project, wrong task, both wrong rejected; task-only and project-
    only acceptance unavailable; no index / rank / title selector
    available; non-UUID accepted arguments rejected);
  - provenance / counts (decision_id, decided_at, portfolio_id,
    selected_project_count, ready_task_count preserved EXACTLY;
    accepted pair exact);
  - model invariants (strict / frozen / extra-forbid;
    ready_task_count >= 1; accepted IDs non-null UUIDs; invalid direct
    construction rejected);
  - behavior (repeated identical accepted calls value-identical; input
    not mutated; no generated identity / time);
  - public surface / architecture discipline (exact exports; function
    takes only selection + explicit pair; no Portfolio / WBS /
    repository / persistence / provider / AI / runtime / clock /
    UUID-generation surfaces; no ranking / scoring / recommendation /
    execution semantics).
"""

from __future__ import annotations

import inspect
import types
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import trajectory_os.application as app
import trajectory_os.application.execution_effort_project_focus_next_ready_task_decision as v142_mod  # noqa: E501
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskDecision,
    PortfolioProjectFocusNextReadyTaskDecisionError,
    PortfolioProjectFocusNextReadyTaskSelection,
    accept_next_ready_task_selection,
)

PORTFOLIO_ID = uuid.UUID("81616161-6161-4161-8161-616161616161")
DECISION_ID = uuid.UUID("83636363-6363-4363-8363-836363636363")
DECIDED_AT = datetime(2025, 7, 3, 10, 0, 30, tzinfo=UTC)

# Identities deliberately INVERTED against natural UUID order so any
# UUID-sorted substitution is detectable.
P_A = uuid.UUID("b0000000-0000-4000-8000-00000000000b")
P_B = uuid.UUID("c0000000-0000-4000-8000-00000000000c")
T_A = uuid.UUID("d1d1d1d1-0000-4000-8000-000000000011")
T_B = uuid.UUID("f4f4f4f4-0000-4000-8000-000000000044")


# ---------------------------------------------------------------------------
# Fixture helpers.
# ---------------------------------------------------------------------------


def _v141(**overrides: object) -> PortfolioProjectFocusNextReadyTaskSelection:
    """One GENUINE (fully validated) V1.41 selection."""
    base: dict[str, object] = {
        "decision_id": DECISION_ID,
        "decided_at": DECIDED_AT,
        "portfolio_id": PORTFOLIO_ID,
        "selected_project_count": 3,
        "ready_task_count": 5,
        "selected_project_id": P_A,
        "selected_task_id": T_A,
    }
    base.update(overrides)
    return PortfolioProjectFocusNextReadyTaskSelection(**base)  # type: ignore[arg-type]


def _hostile_v141(**overrides: object) -> PortfolioProjectFocusNextReadyTaskSelection:
    """A validation-BYPASSED V1.41 instance; only the V1.42 fresh strict
    re-validation can reject its contents."""
    base: dict[str, object] = {
        "decision_id": DECISION_ID,
        "decided_at": DECIDED_AT,
        "portfolio_id": PORTFOLIO_ID,
        "selected_project_count": 3,
        "ready_task_count": 5,
        "selected_project_id": P_A,
        "selected_task_id": T_A,
    }
    base.update(overrides)
    return PortfolioProjectFocusNextReadyTaskSelection.model_construct(
        **base  # type: ignore[arg-type]
    )


class _ForeignModel(BaseModel):
    """A different model; must never be accepted as a V1.41 payload."""

    model_config = {"frozen": True}

    field: str = "foreign"


def _dict_payload() -> dict[str, object]:
    return {
        "decision_id": DECISION_ID,
        "decided_at": DECIDED_AT,
        "portfolio_id": PORTFOLIO_ID,
        "selected_project_count": 3,
        "ready_task_count": 5,
        "selected_project_id": P_A,
        "selected_task_id": T_A,
    }


# ---------------------------------------------------------------------------
# Decision model invariants.
# ---------------------------------------------------------------------------


class TestDecisionModel:
    def _base_kwargs(self) -> dict[str, object]:
        return {
            "decision_id": DECISION_ID,
            "decided_at": DECIDED_AT,
            "portfolio_id": PORTFOLIO_ID,
            "selected_project_count": 3,
            "ready_task_count": 5,
            "accepted_project_id": P_A,
            "accepted_task_id": T_A,
        }

    def test_exactly_seven_fields(self) -> None:
        expected = [
            "decision_id",
            "decided_at",
            "portfolio_id",
            "selected_project_count",
            "ready_task_count",
            "accepted_project_id",
            "accepted_task_id",
        ]
        assert list(PortfolioProjectFocusNextReadyTaskDecision.model_fields) == (  # noqa: E501
            expected
        )

    def test_strict_frozen_extra_forbid(self) -> None:
        decision = PortfolioProjectFocusNextReadyTaskDecision(
            **self._base_kwargs()
        )

        with pytest.raises(ValidationError, match="decision_id"):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "decision_id": str(DECISION_ID),
                }
            )

        with pytest.raises(
            ValidationError,
            match="accepted_project_id",
        ):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "accepted_project_id": "not-a-uuid",
                }
            )

        with pytest.raises(ValidationError, match="Extra inputs"):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[call-arg]
                    **self._base_kwargs(),
                    "score": 1.0,
                }
            )

        with pytest.raises((ValidationError, AttributeError, TypeError)):
            decision.decision_id = DECISION_ID  # type: ignore[misc]

    def test_ready_task_count_must_be_at_least_one(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "ready_task_count": 0,
                }
            )
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "ready_task_count": -1,
                }
            )

    def test_accepted_ids_must_be_non_null_uuids(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "accepted_project_id": None,
                }
            )
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "accepted_task_id": None,
                }
            )

    def test_counts_reject_bools(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "selected_project_count": True,
                }
            )
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "ready_task_count": True,
                }
            )

    def test_genuine_construction_is_valid(self) -> None:
        decision = PortfolioProjectFocusNextReadyTaskDecision(
            **self._base_kwargs()
        )
        assert decision.accepted_project_id is P_A
        assert decision.accepted_task_id is T_A


# ---------------------------------------------------------------------------
# Genuine / hostile input discipline.
# ---------------------------------------------------------------------------


class TestGenuineAndHostileInput:
    def test_genuine_v141_exact_pair_accepted(self) -> None:
        decision = accept_next_ready_task_selection(
            _v141(),
            accepted_project_id=P_A,
            accepted_task_id=T_A,
        )
        assert decision.accepted_project_id is P_A
        assert decision.accepted_task_id is T_A

    def test_none_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="genuine V1.41",
        ):
            accept_next_ready_task_selection(
                None,  # type: ignore[arg-type]
                P_A,
                T_A,
            )

    def test_dict_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="genuine V1.41",
        ):
            accept_next_ready_task_selection(
                _dict_payload(),  # type: ignore[arg-type]
                P_A,
                T_A,
            )

    def test_string_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="genuine V1.41",
        ):
            accept_next_ready_task_selection(
                "not a selection",  # type: ignore[arg-type]
                P_A,
                T_A,
            )

    def test_foreign_model_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="genuine V1.41",
        ):
            accept_next_ready_task_selection(
                _ForeignModel(),  # type: ignore[arg-type]
                P_A,
                T_A,
            )

    def test_duck_type_rejected(self) -> None:
        class _Duck:  # pragma: no cover - intentional non-model
            decision_id = DECISION_ID
            selected_project_id = P_A
            selected_task_id = T_A

        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="genuine V1.41",
        ):
            accept_next_ready_task_selection(
                _Duck(),  # type: ignore[arg-type]
                P_A,
                T_A,
            )

    def test_hostile_top_level_construct_scalar_rejected(self) -> None:
        hostile = _hostile_v141(decision_id="not-a-uuid")  # raw scalar
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="strict re-validation",
        ):
            accept_next_ready_task_selection(hostile, P_A, T_A)

    def test_hostile_selected_project_id_rejected(self) -> None:
        hostile = _hostile_v141(selected_project_id="not-a-uuid")
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="strict re-validation",
        ):
            accept_next_ready_task_selection(hostile, P_A, T_A)

    def test_hostile_selected_task_id_rejected(self) -> None:
        hostile = _hostile_v141(selected_task_id=T_B)
        # Even raw, this raw value happens to look "acceptable" against
        # T_B — the boundary still derives semantics only from the
        # freshly validated copy, and a payload whose raw selected task
        # is a non-UUID must fail re-validation.
        hostile_bad = _hostile_v141(selected_task_id="not-a-uuid")
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="strict re-validation",
        ):
            accept_next_ready_task_selection(hostile_bad, P_A, T_A)

        # a genuine-looking but mismatched raw task is rejected by the
        # exact-equality rule, not by re-validation
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="accepted_task_id does not match",
        ):
            accept_next_ready_task_selection(hostile, P_A, T_A)

    def test_hostile_ready_task_count_rejected(self) -> None:
        hostile = _hostile_v141(ready_task_count="five")  # hostile raw count
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="strict re-validation",
        ):
            accept_next_ready_task_selection(hostile, P_A, T_A)

    def test_no_semantic_reads_from_caller_owned_instance(self) -> None:
        # A raw payload whose selected pair is trivially "usable" for a
        # shortcut read (raw selected_project_id is the exact accepted
        # project, rendered as a raw STRING) must be rejected by fresh
        # strict re-validation: the boundary serves no semantic read
        # from the caller-owned instance — only the freshly validated
        # copy would be authoritative, and it does not exist here.
        hostile = _hostile_v141(selected_project_id=str(P_A))
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="strict re-validation",
        ):
            accept_next_ready_task_selection(hostile, P_A, T_A)


# ---------------------------------------------------------------------------
# Zero-candidate policy.
# ---------------------------------------------------------------------------


class TestZeroCandidatePolicy:
    def test_genuine_zero_ready_rejected(self) -> None:
        zero = _v141(
            selected_project_count=0,
            ready_task_count=0,
            selected_project_id=None,
            selected_task_id=None,
        )
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="no READY task",
        ):
            accept_next_ready_task_selection(zero, P_A, T_A)

    def test_hostile_constructed_partial_selected_state_rejected(self) -> None:
        # Partial selected state violates V1.41 invariants; fresh
        # strict re-validation must reject the payload before any
        # zero-policy check.
        zero_with_project = _hostile_v141(
            ready_task_count=0,
            selected_project_id=P_A,
            selected_task_id=None,
        )
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="strict re-validation",
        ):
            accept_next_ready_task_selection(zero_with_project, P_A, T_A)

        positive_count_no_task = _hostile_v141(
            ready_task_count=3,
            selected_project_id=P_A,
            selected_task_id=None,
        )
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="strict re-validation",
        ):
            accept_next_ready_task_selection(
                positive_count_no_task, P_A, T_A
            )

    def test_no_decision_object_can_represent_accepted_empty_selection(
        self,
    ) -> None:
        base = {
            "decision_id": DECISION_ID,
            "decided_at": DECIDED_AT,
            "portfolio_id": PORTFOLIO_ID,
            "selected_project_count": 0,
            "ready_task_count": 5,
            "accepted_project_id": P_A,
            "accepted_task_id": T_A,
        }
        # accepted identities are non-nullable
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **base,
                    "accepted_project_id": None,
                }
            )
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **base,
                    "accepted_task_id": None,
                }
            )
        # a decision is always an accepted task: zero READY count is
        # impossible on the output model itself
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecision(
                **{  # type: ignore[arg-type]
                    **base,
                    "ready_task_count": 0,
                }
            )


# ---------------------------------------------------------------------------
# Exact human acceptance.
# ---------------------------------------------------------------------------


class TestExactHumanAcceptance:
    def test_exact_project_and_task_accepted(self) -> None:
        selection = _v141()
        decision = accept_next_ready_task_selection(
            selection,
            accepted_project_id=P_A,
            accepted_task_id=T_A,
        )
        assert decision.accepted_project_id == selection.selected_project_id
        assert decision.accepted_task_id == selection.selected_task_id

    def test_wrong_project_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="accepted_project_id does not match",
        ):
            accept_next_ready_task_selection(
                _v141(),
                accepted_project_id=P_B,
                accepted_task_id=T_A,
            )

    def test_wrong_task_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="accepted_task_id does not match",
        ):
            accept_next_ready_task_selection(
                _v141(),
                accepted_project_id=P_A,
                accepted_task_id=T_B,
            )

    def test_both_wrong_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="accepted_project_id does not match",
        ):
            accept_next_ready_task_selection(
                _v141(),
                accepted_project_id=P_B,
                accepted_task_id=T_B,
            )

    def test_cross_pair_rejected(self) -> None:
        selection = _v141()
        # a valid V1.41 pair with project/task crossed must not be
        # accepted as equivalent
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="accepted_task_id does not match",
        ):
            accept_next_ready_task_selection(
                selection,
                accepted_project_id=selection.selected_project_id,  # type: ignore[arg-type]
                accepted_task_id=selection.selected_project_id,  # type: ignore[arg-type]
            )

    def test_non_uuid_accepted_arguments_rejected(self) -> None:
        selection = _v141()
        bad_values = (
            None,
            str(P_A),
            "b0000000-0000-4000-8000-00000000000b",
            42,
            True,
            [P_A],
            (P_A,),
            {"uuid": P_A},
            types.SimpleNamespace(value=P_A),
        )
        for bad in bad_values:
            with pytest.raises(
                PortfolioProjectFocusNextReadyTaskDecisionError,
                match="accepted_project_id must be a genuine UUID",
            ):
                accept_next_ready_task_selection(
                    selection,
                    accepted_project_id=bad,  # type: ignore[arg-type]
                    accepted_task_id=T_A,
                )

        for bad in bad_values:
            with pytest.raises(
                PortfolioProjectFocusNextReadyTaskDecisionError,
                match="accepted_task_id must be a genuine UUID",
            ):
                accept_next_ready_task_selection(
                    selection,
                    accepted_project_id=P_A,
                    accepted_task_id=bad,  # type: ignore[arg-type]
                )

    def test_no_weaker_selector_in_public_signature(self) -> None:
        # The ONLY accepted selectors are the two explicit UUIDs: no
        # index, rank, title, tuple position, requested limit, task-
        # without-project selector, or project-without-task selector
        # exists in the boundary at all.
        parameters = list(
            inspect.signature(accept_next_ready_task_selection).parameters
        )
        assert parameters == [
            "selection",
            "accepted_project_id",
            "accepted_task_id",
        ]

    def test_task_only_acceptance_unavailable(self) -> None:
        # Supplying the task as the project selector is a mismatch, not
        # a task-only acceptance.
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="accepted_project_id does not match",
        ):
            accept_next_ready_task_selection(
                _v141(),
                accepted_project_id=T_A,
                accepted_task_id=T_A,
            )

    def test_project_only_acceptance_unavailable(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskDecisionError,
            match="accepted_task_id does not match",
        ):
            accept_next_ready_task_selection(
                _v141(),
                accepted_project_id=P_A,
                accepted_task_id=P_B,
            )


# ---------------------------------------------------------------------------
# Provenance / counts.
# ---------------------------------------------------------------------------


class TestProvenanceAndCounts:
    def test_full_provenance_preserved_exactly(self) -> None:
        selection = _v141(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            portfolio_id=PORTFOLIO_ID,
            selected_project_count=7,
            ready_task_count=12,
        )
        decision = accept_next_ready_task_selection(
            selection,
            accepted_project_id=P_A,
            accepted_task_id=T_A,
        )
        assert decision.decision_id == selection.decision_id
        assert decision.decided_at == selection.decided_at
        assert decision.portfolio_id == selection.portfolio_id
        assert decision.selected_project_count == 7
        assert decision.ready_task_count == 12
        assert decision.accepted_project_id == P_A
        assert decision.accepted_task_id == T_A

    def test_no_recomputation_of_counts(self) -> None:
        selection = _v141(selected_project_count=1, ready_task_count=99)
        decision = accept_next_ready_task_selection(
            selection,
            accepted_project_id=P_A,
            accepted_task_id=T_A,
        )
        assert decision.selected_project_count == 1
        assert decision.ready_task_count == 99


# ---------------------------------------------------------------------------
# Behavior.
# ---------------------------------------------------------------------------


class TestBehavior:
    def test_repeated_identical_accepted_calls_value_identical(self) -> None:
        selection = _v141()
        first = accept_next_ready_task_selection(
            selection,
            accepted_project_id=P_A,
            accepted_task_id=T_A,
        )
        second = accept_next_ready_task_selection(
            selection,
            accepted_project_id=P_A,
            accepted_task_id=T_A,
        )
        assert first == second
        assert first.model_dump(mode="json") == second.model_dump(mode="json")

    def test_input_not_mutated(self) -> None:
        selection = _v141()
        before = selection.model_dump(mode="json")
        accept_next_ready_task_selection(
            selection,
            accepted_project_id=P_A,
            accepted_task_id=T_A,
        )
        assert selection.model_dump(mode="json") == before

    def test_no_generated_identity_or_time(self) -> None:
        # The decision provenance is EXACTLY the V1.41 provenance;
        # nothing is generated.
        decision = accept_next_ready_task_selection(
            _v141(),
            accepted_project_id=P_A,
            accepted_task_id=T_A,
        )
        assert decision.decision_id == DECISION_ID
        assert decision.decided_at == DECIDED_AT

    def test_no_side_effects(self) -> None:
        source = Path(v142_mod.__file__).read_text()
        assert "open(" not in source
        assert "sqlite" not in source.lower()
        assert "subprocess" not in source


# ---------------------------------------------------------------------------
# Public surface and architecture discipline.
# ---------------------------------------------------------------------------


class TestDiscipline:
    def test_signature_takes_selection_plus_explicit_pair_only(self) -> None:
        parameters = list(
            inspect.signature(accept_next_ready_task_selection).parameters
        )
        assert parameters == [
            "selection",
            "accepted_project_id",
            "accepted_task_id",
        ]
        assert not any("portfolio" in name.lower() for name in parameters)

    def test_no_work_breakdown_construction_for_semantics(self) -> None:
        module_vars = vars(v142_mod)
        assert "build_work_breakdown" not in module_vars
        assert "WorkBreakdownError" not in module_vars
        assert not any(
            "workbreakdown" in name.lower() or "build_work" in name.lower()
            for name in module_vars
            if not name.startswith("__")
        )

    def test_no_repository_persistence_or_provider_surface(self) -> None:
        public_names = [
            name for name in vars(v142_mod) if not name.startswith("_")
        ]
        assert not any(
            "repository" in name.lower()
            or "persist" in name.lower()
            or "provider" in name.lower()
            or "runtime" in name.lower()
            for name in public_names
        )

        source = Path(v142_mod.__file__).read_text()
        assert "datetime.now" not in source
        assert "datetime.utcnow" not in source
        assert "uuid4(" not in source
        assert "uuid5(" not in source
        assert "random()" not in source
        assert "import os" not in source
        assert "requests" not in source

    def test_no_ranking_scoring_priority_or_recommendation_semantics(
        self,
    ) -> None:
        public_names = [
            name for name in vars(v142_mod) if not name.startswith("_")
        ]
        for name in public_names:
            lowered = name.lower()
            for forbidden in (
                "rank",
                "score",
                "priorit",
                "recommend",
                "urgent",
                "best",
            ):
                assert forbidden not in lowered

    def test_no_v140_recomputation_surface(self) -> None:
        source = Path(v142_mod.__file__).read_text()
        source_lower = source.lower()
        for marker in (
            "project_ready_task_candidates",
            "select_first_ready_task_candidate",
        ):
            assert marker not in source_lower

    def test_public_exports_correct(self) -> None:
        required = [
            "PortfolioProjectFocusNextReadyTaskDecision",
            "PortfolioProjectFocusNextReadyTaskDecisionError",
            "accept_next_ready_task_selection",
        ]
        for name in required:
            assert name in app.__all__
            assert getattr(app, name) is not None

        module_all = set(v142_mod.__all__)
        for name in required:
            assert name in module_all

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(
            PortfolioProjectFocusNextReadyTaskDecisionError, ValueError
        )
