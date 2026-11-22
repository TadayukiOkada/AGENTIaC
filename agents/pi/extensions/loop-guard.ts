/**
 * Loop guard: stops pi from rerunning one experiment in bash over and over.
 *
 * Every wall-limit run of the Strata 128K suite (docs/results/strata-128k-2026-10.md)
 * ended in such a loop: the same `python3 - <<EOF` script, identical or almost so,
 * run 44-274 times in a row. No passing run had more than 6 similar bash calls in a row.
 *
 * - Two bash commands are similar when difflib's quick ratio of their characters
 *   is >= 0.9. A command repeats when it is similar to one of the last `window`
 *   bash commands, so a cycle of a few alternating commands counts too (even when
 *   two immediate neighbours are dissimilar). A successful edit or write starts a
 *   new count, so edit-and-rerun cycles are never counted.
 * - At `nudge` repeating calls in a row the call is blocked with a note that asks the
 *   model to change approach; at `stop` it is blocked and the agent stops (the
 *   count starts again for the next prompt).
 *
 * Optional (off in suite v1): agentiac code --loop-guard, agentiac eval suite
 * run --loop-guard. scripts/agent.py passes a JSONL log path in AGENTIAC_LOOP_GUARD_LOG.
 */

import { appendFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export const LIMITS = { nudge: 10, stop: 20, similarity: 0.9, window: 3 };

/** difflib.SequenceMatcher.quick_ratio: shared characters (as multisets) over the total length. */
export function similarity(a: string, b: string): number {
	if (!a.length && !b.length) return 1;
	const counts = new Map<string, number>();
	for (const ch of a) counts.set(ch, (counts.get(ch) ?? 0) + 1);
	let matches = 0;
	for (const ch of b) {
		const left = counts.get(ch) ?? 0;
		if (left > 0) {
			counts.set(ch, left - 1);
			matches++;
		}
	}
	return (2 * matches) / ([...a].length + [...b].length);
}

export interface GuardState {
	/** The last `window` bash commands of the current count, oldest first. */
	recent: string[];
	streak: number;
}

export type Verdict = "run" | "nudge" | "stop";

/** Count one bash command; the verdict for running it. */
export function observe(state: GuardState, command: string, limits = LIMITS): Verdict {
	const repeats = state.recent.some((earlier) => similarity(earlier, command) >= limits.similarity);
	state.streak = repeats ? state.streak + 1 : 1;
	state.recent = [...state.recent, command].slice(-limits.window);
	if (state.streak >= limits.stop) return "stop";
	if (state.streak >= limits.nudge) return "nudge";
	return "run";
}

const NUDGE =
	"Not run: this is the {n}th bash command in a row that repeats one of the last few, almost identically. " +
	"Rerunning the same experiment will not give a new answer. Read the last output, change the code or " +
	"the approach, or stop and report what you found and what is still unknown.";
const STOP =
	"Not run, and the agent stops here: {n} bash commands in a row repeated one of the last few, almost " +
	"identically. Report what you found and what is still unknown.";

export default function (pi: ExtensionAPI) {
	const state: GuardState = { recent: [], streak: 0 };
	const reset = () => {
		state.recent = [];
		state.streak = 0;
	};
	const log = (entry: object) => {
		const path = process.env.AGENTIAC_LOOP_GUARD_LOG;
		if (!path) return;
		try {
			appendFileSync(path, JSON.stringify({ time: new Date().toISOString(), ...entry }) + "\n");
		} catch {
			// The log is for tuning only; never fail a tool call over it.
		}
	};

	pi.on("tool_call", async (event, ctx) => {
		if (event.toolName !== "bash") return;
		const verdict = observe(state, String((event.input as { command?: unknown }).command ?? ""));
		if (verdict === "run") return;
		log({ verdict, streak: state.streak });
		if (ctx.hasUI) ctx.ui.notify(`loop guard: ${state.streak} repeated bash commands in a row (${verdict})`, "warning");
		const reason = (verdict === "stop" ? STOP : NUDGE).replace("{n}", String(state.streak));
		if (verdict === "stop") reset(); // The next prompt starts a new count.
		return { block: true, reason, terminate: verdict === "stop" };
	});

	pi.on("tool_result", async (event) => {
		if ((event.toolName === "edit" || event.toolName === "write") && !event.isError) reset();
	});
}
