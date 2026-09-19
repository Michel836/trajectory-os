"""M028 — explicit, disableable LifeOS integration adapters.

Obsidian, Super Productivity and a generic JSON manifest are supported in V0,
with a structural extension point (the closed ``ADAPTER_KINDS`` registry) for
future LifeOS consumers. See ``docs/development/LIFEOS_INTEGRATION.md``.
"""

from __future__ import annotations

__all__ = ["adapters", "engine", "identity", "model", "store", "summary"]
