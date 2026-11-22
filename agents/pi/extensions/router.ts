/**
 * Rule-based router: virtual model `router/auto` that sends ordinary work to the
 * local coder and escalates hard work to the ChatGPT-subscription alias.
 *
 * - Decides only on a message the user wrote. Tool follow-ups and retries stay on
 *   the model that handled the turn, so a failure never moves to another model
 *   (ADR 0002) and prompt caches stay valid.
 * - Escalation only: once a session is on the cloud model it stays there, because
 *   moving a long conversation back makes the Orin prefill it cold for minutes.
 *   `/route low|high|auto` overrides.
 * - The cloud model is used only in repositories listed in `cloud_repos` and,
 *   unless started with --allow-cloud, after a confirmation (ADR 0005: code
 *   leaves the host). Without a dialog (print mode) the session stays local.
 * - Each user-turn decision is logged as metadata (scores and rule names, never
 *   prompt text) for tuning the rules.
 *
 * scripts/agent.py writes the configuration and passes it in AGENTIAC_ROUTER:
 * agentiac code REPO -m router
 */

import { appendFileSync, readFileSync } from "node:fs";
import type { ExtensionAPI, ExtensionContext, ModelRoute, ModelRouteRequest } from "@earendil-works/pi-coding-agent";

export type Tier = "low" | "high";

export interface RouterConfig {
	provider: string;
	low: string;
	high: string;
	repo: string;
	cloudAllowed: boolean;
	confirm: boolean;
	threshold: number;
	contextWindow: number;
	maxTokens: number;
	log?: string;
}

export interface RouterState {
	tier: Tier;
	/** The user approved (or --allow-cloud) or declined the cloud model in this session. */
	cloud?: "approved" | "declined";
	/** Manual override from /route. */
	lock?: Tier;
}

const HARD = [
	/\b(re)?design|architect/i, /refactor/i, /\brace\b|deadlock|concurren|thread.?safe/i, /root cause|investigat|debug/i,
	/performance|optimi[sz]/i, /security|vulnerab/i, /migrat/i, /protocol|algorithm/i, /across (the|all|multiple)|end.to.end/i,
	/flaky|intermittent/i, /設計|アーキテクチャ/, /リファクタ/, /競合|デッドロック|並行|並列|スレッド/, /原因|調査|デバッグ/,
	/性能|最適化|高速化/, /セキュリティ|脆弱/, /移行/, /プロトコル|アルゴリズム/, /全体|横断|複数の(ファイル|モジュール)/,
	/再現しない|間欠/,
];
const EASY = [
	/typo|rename|docstring|\bcomment/i, /\bformat|\blint/i, /explain|summari[sz]e|what does/i, /\bsimple\b|\bsmall\b|one.line/i,
	/誤字|タイポ|名前変更|リネーム|コメント/, /整形|フォーマット/, /説明|要約|とは/, /簡単|小さな|一行/,
];
const FILE = /[\w./-]+\.(py|ts|tsx|js|c|cc|cpp|h|hpp|rs|go|java|kt|sh|md|json|ya?ml|toml)\b/g;
const TRACE = /Traceback \(most recent call last\)|panic:|Exception\b|error\[E\d+\]|\bat \S+:\d+/;

/** Difficulty score of one user message and the rules that fired. */
export function score(text: string): { score: number; reasons: string[] } {
	const reasons: string[] = [];
	const hard = HARD.filter((rule) => rule.test(text)).length;
	const easy = EASY.filter((rule) => rule.test(text)).length;
	let total = Math.min(hard * 2, 6) - Math.min(easy * 2, 4);
	if (hard) reasons.push(`hard:${hard}`);
	if (easy) reasons.push(`easy:${easy}`);
	const files = new Set(text.match(FILE) ?? []).size;
	if (files >= 3) {
		total += 2;
		reasons.push(`files:${files}`);
	}
	if (text.length > 2000) {
		total += 2;
		reasons.push("chars>2000");
	} else if (text.length > 600) {
		total += 1;
		reasons.push("chars>600");
	}
	if (TRACE.test(text)) {
		total += 1;
		reasons.push("trace");
	}
	return { score: total, reasons };
}

export interface Decision {
	tier: Tier;
	/** The cloud model was chosen but needs the user's confirmation first. */
	ask: boolean;
	why: string;
	score?: number;
	reasons?: string[];
}

/** Tier for a message the user wrote; pure so the rules are testable without pi. */
export function decide(text: string, state: RouterState | undefined, config: RouterConfig): Decision {
	if (state?.lock) return { tier: state.lock, ask: false, why: "manual" };
	if (state?.tier === "high") return { tier: "high", ask: false, why: "sticky" };
	const { score: value, reasons } = score(text);
	const base = { score: value, reasons };
	if (value < config.threshold) return { ...base, tier: "low", ask: false, why: "rules" };
	if (!config.cloudAllowed) return { ...base, tier: "low", ask: false, why: "cloud not allowed for this repository" };
	if (state?.cloud === "declined") return { ...base, tier: "low", ask: false, why: "cloud declined" };
	const ask = config.confirm && state?.cloud !== "approved";
	return { ...base, tier: "high", ask, why: "rules" };
}

function lastUserText(request: ModelRouteRequest<RouterState>): string {
	const content = request.messages.filter((message) => message.role === "user").at(-1)?.content ?? "";
	if (typeof content === "string") return content;
	return content.flatMap((block) => (block.type === "text" ? [block.text] : [])).join("\n");
}

export default function (pi: ExtensionAPI) {
	const config: RouterConfig = JSON.parse(readFileSync(process.env.AGENTIAC_ROUTER ?? "", "utf8"));
	let pendingLock: Tier | "auto" | undefined;

	const physical = (ctx: ExtensionContext, tier: Tier) => {
		const id = tier === "high" ? config.high : config.low;
		const model = ctx.modelRegistry.find(config.provider, id);
		if (!model) throw new Error(`router: ${config.provider}/${id} is not configured`);
		return model;
	};
	const log = (entry: object) => {
		if (!config.log) return;
		try {
			appendFileSync(config.log, JSON.stringify({ time: new Date().toISOString(), repo: config.repo, ...entry }) + "\n");
		} catch {
			// Logging is for tuning only; never fail a request over it.
		}
	};

	pi.registerVirtualModel<RouterState>({
		provider: "router",
		id: "auto",
		name: `Auto (${config.low} / ${config.high})`,
		thinkingLevels: ["medium", "low", "high"],
		// The local coder's limits: a session starts there.
		contextWindow: config.contextWindow,
		maxTokens: config.maxTokens,
		async route(request, ctx): Promise<ModelRoute<RouterState>> {
			const level = request.thinkingLevel;
			if (request.reason === "direct") {
				return { model: request.previous?.model ?? physical(ctx, "low"), thinkingLevel: level };
			}
			const sticky = request.failed ?? request.previous;
			if (request.reason !== "user" && sticky) {
				return { model: sticky.model, thinkingLevel: sticky.thinkingLevel ?? level };
			}
			let state = request.state;
			if (pendingLock && request.reason === "user") {
				const lock = pendingLock === "auto" ? undefined : pendingLock;
				state = { tier: lock ?? state?.tier ?? "low", cloud: state?.cloud, lock };
				pendingLock = undefined;
			}
			const text = lastUserText(request);
			const decision = decide(text, state, config);
			let tier = decision.tier;
			let cloud = state?.cloud ?? (decision.tier === "high" && !decision.ask ? "approved" : undefined);
			if (decision.ask) {
				const approved = ctx.hasUI
					? await ctx.ui.confirm(
							"Use the cloud model?",
							`This task scored ${decision.score} (${decision.reasons?.join(", ")}). Send it and the rest of this ` +
								`session in ${config.repo} to ${config.high} (OpenAI)? Declining keeps the session local.`,
						)
					: false;
				cloud = approved ? "approved" : ctx.hasUI ? "declined" : undefined;
				tier = approved ? "high" : "low";
			}
			const next: RouterState = { tier, cloud, lock: state?.lock };
			const changed = JSON.stringify(next) !== JSON.stringify(request.state ?? null);
			if (request.reason === "user") {
				log({ tier, model: tier === "high" ? config.high : config.low, why: decision.ask ? `asked:${cloud ?? "no-ui"}` : decision.why,
					score: decision.score, reasons: decision.reasons, chars: text.length });
			}
			return { model: physical(ctx, tier), thinkingLevel: level, state: changed ? next : undefined };
		},
	});

	pi.registerCommand("route", {
		description: "Router override: low, high, auto (rules) or status",
		getArgumentCompletions: (prefix) =>
			["low", "high", "auto", "status"].filter((v) => v.startsWith(prefix)).map((v) => ({ value: v, label: v })),
		handler: async (args, ctx) => {
			const choice = args.trim() || "status";
			if (choice === "status") {
				const where = config.cloudAllowed ? "allowed" : "not allowed";
				ctx.ui.notify(`router: ${config.low} / ${config.high}; cloud ${where} in ${config.repo}; threshold ${config.threshold}`, "info");
				return;
			}
			if (choice !== "low" && choice !== "high" && choice !== "auto") {
				ctx.ui.notify("usage: /route low|high|auto|status", "warning");
				return;
			}
			if (choice === "high" && !config.cloudAllowed) {
				ctx.ui.notify(`cloud model not allowed in ${config.repo} (agents/pi/router.json)`, "error");
				return;
			}
			pendingLock = choice;
			log({ tier: choice, why: "command" });
			ctx.ui.notify(`router: ${choice === "auto" ? "rules" : choice} from the next message`, "info");
		},
	});
}
