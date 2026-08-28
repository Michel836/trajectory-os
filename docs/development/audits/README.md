# AI Development Stack Audits

This directory contains summarized, non-sensitive engineering audits and benchmark evidence used to validate the local/cloud development stack tracked by Issue #36.

Audits preserve measured state before configuration changes; benchmark reports record what happened after controlled experiments. Both distinguish observation from proposal and validation.

- `AI_STACK_AUDIT_2026-08-26.md` — initial Ollama/Pi/Qwen runtime and context-alignment audit.
- `AI_STACK_BENCHMARK_2026-08-26.md` — post-audit V1.10 runtime, harness, model, efficiency, and reviewer benchmark evidence.
- `AI_STACK_BOUNDED_EDIT_AB_2026-08-27.md` — controlled same-harness A/B comparison of Qwen3.8 27B and Qwen3-Coder 30B on an identical bounded semantic repair.
- `AI_STACK_AIDER_QWEN3CODER_2026-08-27.md` — controlled harness-isolation run showing Aider + Qwen3-Coder on the same bounded repair, with timing, token, quality, and cache-hygiene evidence.
- `AI_STACK_AIDER_QWEN38_2026-08-27.md` — Aider + Qwen3.8 run completing the 2x2 Pi/Aider × Qwen3.8/Qwen3-Coder bounded-edit matrix and recording the Qwen3.8 summarizer failure signal.
- `AI_STACK_MULTIFILE_HARNESS_2026-08-27.md` — controlled two-file Qwen3-Coder benchmark documenting unanchored Aider failure, explicitly file-scoped Aider success, and Pi success under the same anchored task contract.
- `AI_STACK_ADDITIVE_FEATURE_2026-08-27.md` — first controlled substantive additive-feature benchmark: Qwen3-Coder failed under OpenCode, Aider, and Pi in distinct ways, while Pi + Qwen3.8 completed the same frozen contract with hidden acceptance, 726 tests, Ruff, mypy, and diff-check all passing.
- `AI_STACK_FEATURE_HARNESS_AB_2026-08-27.md` — follow-up Qwen3.8 feature harness A/B: both Pi and explicitly file-scoped Aider passed the same frozen feature; Aider completed it in 534.29 s versus Pi's 736.39 s, supporting Aider + Qwen3.8 when edit surfaces are known and Pi + Qwen3.8 when broader autonomous scope discovery/navigation is needed.
- `AI_STACK_SCOPE_DISCOVERY_2026-08-28.md` — frozen unknown-scope substantive-feature benchmark: Pi passed cleanly; Aider demonstrated genuine repo-map scope discovery and disposable-index Git isolation, but only 1/3 isolated-index replications passed the full canonical gate, so Pi + Qwen3.8 remains the conservative default for unknown-scope substantive work.

Do not commit raw local logs, hostnames, tokens, credentials, or personal/client data here.
