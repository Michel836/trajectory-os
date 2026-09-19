"""M056–M063 — adaptive intelligence and practical decision workflows.

This package is the first end-to-end adaptive-intelligence loop over the
canonical Trajectory_OS execution history:

    canonical history -> learning dataset (M056) -> predictive ML (M057)
    -> advisory routing (M058) / adaptive scheduling (M059)
    -> grounded knowledge work (M060) -> practical workflows (M061)
    -> human decision support (M062) -> real outcomes -> future learning data

Every layer is **advisory**: it never performs release Git writes, never
mutates policy, and never presents an inferred value as a measurement.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "m056-m063.1"
