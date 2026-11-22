# ADR 0005: One commercial alias through the user's ChatGPT subscription

Status: accepted (user decision). Amends the roadmap hard constraint
"no commercial API calls".

Context: the user wants the Claude Code and Codex models usable through the
gateway, with their Claude Pro and ChatGPT Plus subscriptions rather than
pay-as-you-go API keys. Anthropic does not permit Free/Pro/Max sign-ins outside
Claude Code and claude.ai, so Claude cannot be routed through the gateway on
Pro; it stays available by running Claude Code itself. ChatGPT Plus can be used
through LiteLLM's `chatgpt/` provider (device-code sign-in to the Codex
backend). OpenAI officially supports that sign-in only in Codex; proxying it is
not documented as allowed. The user accepted that risk for a single-user,
loopback-only gateway.

Decision: one alias `cloud-gpt-sol` → `chatgpt/gpt-6.1-sol` (the Codex CLI
default model), `model_info.mode: responses` so Chat Completions and Anthropic
Messages are bridged to the Responses backend.

- A separate LiteLLM process serves it (`agentiac-chatgpt`,
  `gateway/chatgpt.yaml`, 127.0.0.1:14001); the main gateway forwards the alias
  there as a plain OpenAI-compatible backend. LiteLLM fetches a ChatGPT token
  when it loads a `chatgpt/` deployment, and without one it waits for an
  interactive device-code login or fails the whole router. In the main gateway
  that stops every local alias, so the main config must never contain `chatgpt/`
  (tested).
- Sign-in is an explicit operator step: `make chatgpt-login` (device code,
  tokens in `secrets/chatgpt/`, 0700, separate from Codex's own `~/.codex`
  sign-in so refresh-token rotation in one cannot log out the other). It stops
  the unit, signs in or refreshes, and starts it. `ARGS=--status` reports,
  `ARGS=--logout` stops the unit and deletes the tokens.
- Only one process refreshes tokens at a time: the unit's preflight
  (`chatgpt-login.py --preflight`) before LiteLLM starts, then the unit's
  pre-call check (`gateway/chatgpt_auth.py`), which refreshes an expired token
  or refuses with 503 and a reason. The main gateway only checks that a sign-in
  exists. A refused preflight leaves the unit failed until the next login (no
  retry loop).
- The alias is explicit only: never a `local-coder` target, no fallback to or
  from it (ADR 0002). Metadata-only logging is unchanged.

Consequences:
- Prompts, code and tool output sent to `cloud-gpt-sol` leave the control host
  and are processed by OpenAI under the ChatGPT terms. Use it only for
  repositories whose content may be sent there; the local aliases remain the
  default. Traces kept by this repository still stay control-local.
- Usage counts against the Plus plan's Codex limits, shared with Codex CLI; a
  limit or a revoked sign-in is an error, not a route change.
- Protocols (smoke-checked): Chat Completions, streamed tool calls, Anthropic
  Messages and a pi repair pass. The main gateway forwards with the
  `litellm_proxy` provider because for `openai` backends LiteLLM turns Messages
  into non-streamed Responses, which this provider answers with SSE only.
  `/v1/responses` works only streamed with list `input` (the Codex CLI shape);
  a string `input` or a non-streamed request fails (ChatGPT backend / LiteLLM
  1.105.1), so the smoke `responses` check fails on this alias. Both
  deployments set `cooldown_time: 0` so one upstream error does not turn later
  requests into router 429s.
- LiteLLM prepends the Codex base instructions to every request on this
  provider; results are not comparable with local models without that noted.
- No API keys are stored. Adding Claude or OpenAI API-key aliases later needs a
  new decision (billing, spend limits).
- (User decision) The rule-based pi router (`--alias router`) may
  choose this alias automatically, but only in the repositories listed in
  `agents/pi/router.json` (open-genie-server, agentiac) and after a
  per-session confirmation unless started with `--allow-cloud`. It never
  switches model on an error.
- (User decision, ADR 0009) This exception does not widen: agent
  adapters, scheduled jobs and fallbacks never use this alias. The interactive
  router rule above is unchanged.
