"""M064–M071 — real-world operating system and portfolio proof.

This package is an additive, *practical* layer above the existing M017–M063
platform. It does not rebuild or compete with any existing system
(execution, mission/release lifecycle, observability, trust, scheduler,
routing, RAG, intelligence, decision workspace, LifeOS projection). It adds
the missing real-world loop:

    real inputs (M064) -> practical intelligence (M065/M066)
    -> operational view (M067) -> outcome tracking (M068)
    -> guarded learning (M069) -> human cockpit (M070)
    -> external proof (M071)

Trust invariants (unchanged):

* no module here performs a release Git write (commit, push, merge, reset,
  restore, clean, stash, rebase, checkout/switch);
* intelligence/cockpit/model-refresh layers never mutate policy, the
  canonical scheduler or any release gate;
* a value is never detached from how it was obtained: every claim is typed
  ``FACT`` / ``OBSERVATION`` / ``EVIDENCE`` / ``INFERENCE`` / ``HYPOTHESIS``
  and ``UNKNOWN`` stays ``UNKNOWN``;
* real inputs are context/provenance, never runtime truth;
* the human remains authoritative for every consequential decision.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Human/machine version string for the M064–M071 bundle.
__version__ = "m064-m071.1"
