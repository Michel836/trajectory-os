"""Focused unit test: the PLAN phase kind maps to the PLAN run mode.

The canonical ``KIND_TO_MODE`` table maps every phase kind to the exact
Mission 002 execution mode its sub-run uses; PLAN must map to ``"PLAN"``
(read-only, model-heavy), never to a writable mode.
"""

from trajectory_os.missions import model as m


def test_ph_plan_maps_to_plan_mode() -> None:
    assert m.KIND_TO_MODE[m.PH_PLAN] == "PLAN"
