# TrajectoryOS AI Stack Audit — 2026-08-26

Tracked by Issue #36 and draft PR #37.

## Purpose

Record the first measured baseline of the local AI development stack before changing runtime or agent configuration.

This document contains only non-sensitive summarized technical findings. Raw local logs remain local and are not committed.

Status vocabulary follows `docs/development/AI_DEVELOPMENT_STACK.md`:

- **CURRENT** — directly observed;
- **PROPOSED** — hypothesis/change to test;
- **VALIDATED** — demonstrated by repeatable project evidence.

---

## CURRENT — runtime baseline

### Ollama

- version: `0.32.14`;
- local inference service: systemd-managed `ollama serve`;
- no explicit Ollama tuning variables were observed in the service environment beyond `PATH`;
- active Qwen3.8 server command used an effective context of `32768` tokens (`-c 32768`);
- active server used one parallel slot (`-np 1`);
- Flash Attention was left at runtime auto-selection (`--flash-attn auto`);
- context shifting was enabled;
- speculative MTP draft decoding was enabled by the current Ollama runner.

### Active `qwen3.8:27b` placement

Measured while the model was loaded:

- Ollama-reported model residency: approximately `18 GB`;
- processor placement: approximately `93% GPU / 7% CPU`;
- effective Ollama context: `32768` tokens;
- llama-server GPU memory: approximately `19.5 GiB`;
- total GPU memory use including desktop applications: approximately `22.8 GiB / 24 GiB`.

Interpretation:

- the current 32k configuration is already close to the practical VRAM ceiling once the desktop session is included;
- increasing context without reducing KV-cache memory pressure is likely to increase CPU offload or cause memory pressure;
- 64k should therefore be tested with memory optimizations rather than applied blindly.

---

## CURRENT — Pi baseline

- Pi version: `0.84.3`;
- provider: Ollama through `http://localhost:11434/v1` using `openai-completions`;
- default model: `qwen3.8:27b`;
- default thinking level: `medium`;
- `qwen3.8:27b` is declared to Pi with:
  - `contextWindow: 262144`;
  - `maxTokens: 32768`;
  - `reasoning: true`;
- provider compatibility currently declares:
  - `supportsDeveloperRole: false`;
  - `supportsReasoningEffort: false`.

Pi settings do not explicitly override compaction, so the documented Pi defaults apply unless changed elsewhere:

- auto-compaction enabled;
- response reserve: `16384` tokens;
- recent tokens retained verbatim: `20000` tokens.

---

## Finding F1 — Pi/Ollama context metadata is materially misaligned

**Severity: HIGH operational risk.**

Pi believes `qwen3.8:27b` has a `262144`-token context, while the actual Ollama runner is loaded with only `32768` tokens.

This is not a cosmetic discrepancy.

Pi's context management and auto-compaction decisions are based on its configured `contextWindow`, while Ollama enforces the actual runtime context. With the current values, Pi can continue accumulating history long after Ollama has reached its real limit.

Likely consequences include:

- Ollama context shifting/truncation before Pi believes compaction is necessary;
- loss of older user/tool context during long agentic sessions;
- reduced continuity on repository-scale work;
- difficult-to-diagnose tool-message failures;
- misleading UI expectations about available context.

This finding is strongly consistent with the V1.10 long-session failures and must be corrected before judging Qwen3.8's coding suitability.

---

## Finding F2 — Pi output budget currently equals the entire effective Ollama context

Pi advertises `maxTokens: 32768` for Qwen3.8 while the actual Ollama context is also `32768`.

A non-trivial agent request cannot simultaneously contain substantial repository/tool history and reserve the entire 32k window for new output.

This configuration therefore does not represent a coherent prompt/output budget at the actual runtime context size.

The V1.10 `maximum output token limit` incident demonstrates that output budgeting is operationally relevant, but the durable fix is not simply to raise `maxTokens`: prompt history, reasoning, tool messages, output and compaction must fit the same effective runtime context.

---

## Finding F3 — Pi thinking-level control is not currently wired to Ollama reasoning effort

Pi marks the model as reasoning-capable and sets a default thinking level of `medium`, but provider compatibility currently declares `supportsReasoningEffort: false`.

Current Ollama OpenAI-compatible `/v1/chat/completions` supports `reasoning_effort` controls for thinking-capable models.

Therefore the present Pi configuration prevents Pi's normal reasoning-effort mechanism from being transmitted through this provider path.

Implication:

> Switching Pi from `medium` to `high` or `low` must not be treated as a valid benchmark until the provider integration is configured and verified to pass reasoning effort correctly.

A controlled API/log test is required before marking reasoning-level control VALIDATED.

---

## Finding F4 — current 32k Pi compaction defaults are not a sensible local-context pairing

If Pi were corrected immediately to truthfully advertise a `32768` context while retaining its documented default compaction settings, the default `16384` response reserve and `20000` recent-token retention would together consume more than the whole context budget before accounting for system/tool instructions.

Therefore a truthful 32k Pi profile also requires smaller compaction/reserve settings, or the runtime context should be increased first.

This reinforces the need to tune context and compaction as one system rather than isolated numbers.

---

## Finding F5 — 64k remains a plausible target, but only as an experiment

Ollama documentation recommends at least ~64k context for coding agents and warns that larger context increases memory use.

The measured 32k Qwen3.8 configuration already uses approximately 93% GPU model placement and nearly fills the 24 GiB GPU when desktop usage is included.

Therefore the candidate path is:

1. explicitly enable Flash Attention;
2. evaluate `q8_0` KV-cache quantization;
3. keep one large model / one parallel request;
4. test a 64k Qwen3.8 development profile;
5. measure `ollama ps`, VRAM, CPU offload, throughput and task completion;
6. retain 64k only if interactive performance remains acceptable.

Do not globally force 64k for every installed 23–24 GiB model without separate evidence.

---

## PROPOSED — staged correction plan

### Stage A — establish a truthful 32k control profile

Before memory tuning, create a control configuration where Pi metadata matches the actual Ollama runtime:

- Pi `contextWindow = 32768` for the control profile;
- choose a smaller output/compaction budget appropriate to 32k;
- enable/verify actual reasoning-effort forwarding;
- run one bounded repository task and confirm Pi compacts before Ollama context shifting silently discards important history.

Purpose: isolate harness correctness from memory/context expansion.

### Stage B — test an optimized 64k Qwen3.8 profile

Candidate Ollama service settings to test:

```text
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
```

Then test a Qwen3.8 profile with `num_ctx = 65536` and align Pi's `contextWindow` to the measured effective value.

Do not declare this configuration VALIDATED until `ollama ps` confirms the context and processor placement and a real TrajectoryOS task completes successfully.

### Stage C — tune Pi compaction/output after 64k is measured

Candidate starting point for a 64k profile:

- output cap around 24–32k only if the harness/runtime supports it coherently;
- compaction reserve around 16k;
- recent-token retention around 16–20k;
- avoid narration-heavy prompts for implementation agents.

Exact values remain PROPOSED and must be benchmarked.

### Stage D — compare harnesses/models on the same task

After the runtime baseline is coherent, compare at least:

- Pi + Qwen3.8;
- OpenCode + Qwen3.8;
- OpenCode or Pi + Qwen3-Coder 30B;
- Qwen3.6 35B as read-only reviewer.

Use one bounded TrajectoryOS task with the same acceptance contract and record completion, quality gate, corrections and flow interruptions.

---

## Decision

**VALIDATED:** Do not fine-tune or replace Qwen3.8 based on the V1.10 incidents.

The audit has identified concrete harness/runtime mismatches that are sufficient to invalidate any conclusion that Qwen3.8 itself is unsuitable for local coding.

Optimization priority remains:

```text
runtime truth
→ agent metadata alignment
→ context / KV memory
→ reasoning control
→ compaction / output budget
→ harness benchmark
→ model routing benchmark
→ fine-tuning only if repeated model deficiencies remain
```

---

## Next evidence required

1. verify `reasoning_effort` forwarding from Pi to Ollama;
2. establish a truthful 32k control profile;
3. enable Flash Attention + q8 KV cache and test Qwen3.8 at 64k;
4. record `ollama ps`, VRAM and processor placement for both profiles;
5. rerun a bounded coding task;
6. compare with Qwen3-Coder 30B and at least one alternate harness;
7. promote only measured winners from PROPOSED to VALIDATED.
