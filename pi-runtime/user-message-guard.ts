/**
 * user-message-guard — deterministic fix for TrajectoryOS pi-runner failure
 * (run 20260913-202257, UPSTREAM_PROVIDER_MISSING_QUERY).
 *
 * Root cause (verified):
 *   - pi 0.84.3 auto-compaction can rebuild the context on a "split turn":
 *     the kept span starts at an assistant message, so the original user
 *     message is folded into the summary block. The follow-up request to
 *     continue the aborted turn then carries NO user-role message.
 *   - The local Ollama 0.34 core (chatml path) hard-rejects any
 *     /v1/chat/completions request whose message array contains no
 *     user-role message:
 *        HTTP 500  {"error":"no user query found in messages"}
 *     Deterministically reproduced; every identical request with a user
 *     message present succeeds.
 *
 * Fix:
 *   Inject one neutral synthetic user message (right after the system
 *   message) whenever an outgoing chat payload has no user message.
 *   Deterministic, local, provider-agnostic, and a no-op for every
 *   normal request (which already contains the original user message).
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type ChatMessage = { role?: string; content?: unknown };
type ChatPayload = { messages?: ChatMessage[] };

// Intentionally non-empty, generic and deterministic.
// Ollama rejects provider requests with no user-role message; this marker
// restores the missing role without introducing task-specific instructions.
// Keep this value stable so bootstrap/runtime behavior remains reproducible.
const SYNTHETIC_USER: ChatMessage = {
  role: "user",
  content: "(session continuation)",
};

export default function (pi: ExtensionAPI) {
  pi.on("before_provider_request", (event, _ctx) => {
    const payload = event?.payload as ChatPayload | undefined;
    const messages = payload?.messages;
    if (!Array.isArray(messages) || messages.length === 0) return undefined;
    if (messages.some((m) => m && m.role === "user")) return undefined;

    const systemIndex = messages.findIndex((m) => m && m.role === "system");
    const insertAt = systemIndex === -1 ? 0 : systemIndex + 1;
    messages.splice(insertAt, 0, { ...SYNTHETIC_USER });
    return event.payload;
  });
}
