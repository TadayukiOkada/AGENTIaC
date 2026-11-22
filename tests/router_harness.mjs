// Drives agents/pi/extensions/router.ts with a fake pi API; tests/test_router.py checks the JSON it prints.
import { readFileSync } from "node:fs";

const [routerPath, configPath, scenario] = process.argv.slice(2);
const { default: register, score, decide } = await import(routerPath);
const config = JSON.parse(readFileSync(configPath, "utf8"));
process.env.AGENTIAC_ROUTER = configPath;

let virtual, command;
register({ registerVirtualModel: (def) => (virtual = def), registerCommand: (name, def) => (command = def) });

const model = (id) => ({ provider: "local-infra", id });
const notes = [];
function ctx(confirmAnswer) {
	const asked = [];
	return {
		asked,
		hasUI: confirmAnswer !== undefined,
		modelRegistry: { find: (provider, id) => (provider === "local-infra" ? model(id) : undefined) },
		ui: {
			confirm: async (title) => (asked.push(title), confirmAnswer),
			notify: (message, type) => notes.push({ message, type }),
		},
	};
}
const user = (text) => [{ role: "user", content: text }];
const HARD = "Investigate the root cause of the deadlock and refactor the scheduler: src/a.py src/b.py src/c.py";

async function route(request, c) {
	const result = await virtual.route({ thinkingLevel: "medium", messages: user("x"), ...request }, c);
	return { model: result.model.id, state: result.state ?? null };
}

const out = { score: { hard: score(HARD), easy: score("fix typo in README.md"), plain: score("add a --verbose flag") } };
if (scenario === "rules") {
	out.decide = {
		low: decide("add a --verbose flag", undefined, config),
		hard: decide(HARD, undefined, config),
		notAllowed: decide(HARD, undefined, { ...config, cloudAllowed: false }),
		declined: decide(HARD, { tier: "low", cloud: "declined" }, config),
		sticky: decide("fix typo", { tier: "high", cloud: "approved" }, config),
		locked: decide(HARD, { tier: "low", lock: "low" }, config),
		noConfirm: decide(HARD, undefined, { ...config, confirm: false }),
	};
} else if (scenario === "route") {
	const yes = ctx(true), no = ctx(false), print = ctx(undefined);
	out.easy = await route({ reason: "user", messages: user("add a --verbose flag") }, yes);
	out.approved = await route({ reason: "user", messages: user(HARD) }, yes);
	out.askedOnce = yes.asked.length;
	out.declined = await route({ reason: "user", messages: user(HARD) }, no);
	out.noUi = await route({ reason: "user", messages: user(HARD) }, print);
	// A tool follow-up or a retry stays on the model that handled the turn, even when the state says otherwise.
	out.continuation = await route({ reason: "continuation", previous: { model: model("local-coder") }, state: { tier: "high", cloud: "approved" } }, yes);
	out.retry = await route({ reason: "retry", failed: { model: model("cloud-gpt-sol"), message: {} }, previous: { model: model("local-coder") } }, yes);
	out.direct = await route({ reason: "direct" }, yes);
	out.sticky = await route({ reason: "user", messages: user("fix typo"), state: { tier: "high", cloud: "approved" } }, yes);
	await command.handler("low", yes);
	out.lockedLow = await route({ reason: "user", messages: user(HARD), state: { tier: "high", cloud: "approved" } }, yes);
	await command.handler("auto", yes);
	out.unlocked = await route({ reason: "user", messages: user(HARD), state: { tier: "low", cloud: "approved", lock: "low" } }, yes);
	out.notes = notes.map((n) => n.type);
}
console.log(JSON.stringify(out));
