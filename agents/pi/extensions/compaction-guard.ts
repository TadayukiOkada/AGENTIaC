/**
 * Compaction guard: a session summary must not think, and a failing one must not loop.
 *
 * pi asks the session's model to summarize the session, at the session's thinking level, inside
 * an answer budget of `min(floor(0.8 * compaction.reserveTokens), model.maxTokens)`. At `high`
 * thinking, the model can spend the whole budget thinking: pi reads `stopReason: "length"` as an
 * incomplete summary and throws, nothing is written, and pi retries at the next check until the
 * session hits the context window without ever compacting.
 *
 * So for a model with a chat-template thinking switch, this runs pi's own compact() itself from
 * `session_before_compact`, with thinking off. A thinking *budget* is not available on these
 * backends: the pinned template reads only `enable_thinking`, `reasoning_effort` and
 * `preserve_thinking`, and a `thinking_token_budget` on the wire changes nothing. Any other model
 * (a virtual router model, a cloud model) stays on pi's own path.
 *
 * Both switch names pi sends chat_template_kwargs under are taken: "chat-template" (the spark
 * target) and "qwen-chat-template" (the Orin and Strata targets; pi hardcodes
 * `enable_thinking` and `preserve_thinking` for it). Checking only the first lets a session using
 * the other name fall through to pi's own path with thinking on and exhaust its summary budget in
 * the same way.
 *
 * Why this and not a request patch: the first version of this file rewrote the body in
 * `before_provider_request`. That hook is wired through the agent's `onPayload`, which only agent
 * turns pass; compaction calls the stream function without it, so the patch never ran on a real
 * summary. tests/test_compaction_e2e.py runs the pinned pi binary against a backend that fails the
 * way the model did, because a test that calls a hook by hand cannot tell that a hook never fires.
 * compact() also covers both requests a compaction sends: the history summary and, when the cut
 * lands inside a turn (15 of the 23 compactions on record), the turn prefix. It discards the
 * whole result if either fails.
 *
 * pi retries a failed threshold compaction at every check with no limit, so after a failure the
 * next threshold checks are skipped: 2, 4, 8, then 16 at most, until one succeeds. Overflow
 * recovery and a manual /compact are never skipped: past those there is no later chance.
 *
 * Every summary and failure goes to AGENTIAC_COMPACTION_GUARD_LOG, because a summary that
 * silently stops appearing is the failure this exists to prevent.
 *
 * scripts/agent.py loads this on every launch and sizes the `compaction` block in settings.json.
 * The suite does not load it: a suite run is one turn per case and never compacts.
 */

import { appendFileSync } from "node:fs";
import { compact } from "@earendil-works/pi-coding-agent";
import type { CompactionEntry, ExtensionAPI, FileOperations, SessionEntry } from "@earendil-works/pi-coding-agent";

/** The most threshold checks skipped after failures in a row. */
export const MAX_SKIPPED_CHECKS = 16;

/** How many threshold checks to skip after this many failures in a row. */
export function skippedChecks(failures: number): number {
	return Math.min(2 ** failures, MAX_SKIPPED_CHECKS);
}

/** The thinking formats whose off request was measured: both send chat_template_kwargs.enable_thinking. */
const SWITCHABLE_THINKING_FORMATS = new Set(["chat-template", "qwen-chat-template"]);

/** True for a model whose thinking this can switch off: the path that was measured. */
export function summarizesHere(model: { compat?: { thinkingFormat?: string } } | undefined): boolean {
	return model?.compat?.thinkingFormat !== undefined && SWITCHABLE_THINKING_FORMATS.has(model.compat.thinkingFormat);
}

/**
 * Add the previous compaction's file lists to FILEOPS. pi drops them for a compaction an extension
 * wrote (the entry's `fromHook`), so without this each pass would forget what the last one recorded.
 */
export function carryFileLists(fileOps: FileOperations, entries: SessionEntry[]): void {
	const previous = entries.findLast((entry): entry is CompactionEntry => entry.type === "compaction");
	if (!previous?.fromHook) return; // pi carries its own
	const details = previous.details as { readFiles?: unknown; modifiedFiles?: unknown } | undefined;
	if (Array.isArray(details?.readFiles)) for (const file of details.readFiles) fileOps.read.add(file);
	if (Array.isArray(details?.modifiedFiles)) for (const file of details.modifiedFiles) fileOps.edited.add(file);
}

export default function (pi: ExtensionAPI) {
	let failures = 0;
	let skip = 0;

	const log = (entry: object) => {
		const path = process.env.AGENTIAC_COMPACTION_GUARD_LOG;
		if (!path) return;
		try {
			appendFileSync(path, JSON.stringify({ time: new Date().toISOString(), ...entry }) + "\n");
		} catch {
			// The log is for diagnosis only; never fail a summary over it.
		}
	};

	const failed = (reason: string, error: string | undefined, ui?: { notify(message: string, type: "error"): void }) => {
		failures += 1;
		skip = skippedChecks(failures);
		log({ event: "failed", reason, error: error ?? null, failures, skip });
		ui?.notify(`compaction failed; skipping the next ${skip} checks: ${error ?? "unknown error"}`, "error");
	};

	pi.on("session_before_compact", async (event, ctx) => {
		if (event.reason === "threshold" && skip > 0) {
			skip -= 1;
			log({ event: "skipped", failures, skip });
			return { cancel: true };
		}
		const model = ctx.model;
		if (!model || !summarizesHere(model)) return; // pi's own path; session_compact_failed still counts it
		try {
			const auth = await ctx.modelRegistry.getApiKeyAndHeaders(model);
			if (!auth.ok) throw new Error(auth.error);
			carryFileLists(event.preparation.fileOps, event.branchEntries);
			const target = auth.baseUrl ? { ...model, baseUrl: auth.baseUrl } : model;
			const compaction = await compact(event.preparation, target, auth.apiKey ?? "", auth.headers, event.customInstructions, event.signal, "off", undefined, auth.env);
			log({ event: "summarized", reason: event.reason, split_turn: event.preparation.isSplitTurn, tokens_before: event.preparation.tokensBefore, usage: compaction.usage ?? null });
			return { compaction };
		} catch (error) {
			// Never fall through: pi's path would ask the same model the same thing, thinking on.
			if (!event.signal.aborted) failed(event.reason, error instanceof Error ? error.message : String(error), ctx.hasUI ? ctx.ui : undefined);
			return { cancel: true };
		}
	});

	pi.on("session_compact_failed", async (event, ctx) => {
		if (event.aborted) return; // cancelled, by the user or above; counted there if it was a failure
		failed(event.reason, event.errorMessage, ctx.hasUI ? ctx.ui : undefined);
	});

	pi.on("session_compact", async () => {
		failures = 0;
		skip = 0;
	});
}
