// The guard's own decisions, with the network out of the picture: a model with no chat-template
// switch stays on pi's path, so a check that is not skipped returns nothing and sends nothing.
// What the guard does to a real session is tests/test_compaction_e2e.py. argv: guard module.
const [guardPath] = process.argv.slice(2);
const { default: register, carryFileLists, skippedChecks, summarizesHere } = await import(guardPath);

const handlers = {};
register({ on: (name, handler) => (handlers[name] = handler) });
const notes = [];
const ctx = { hasUI: true, model: { compat: {} }, ui: { notify: (message, type) => notes.push({ message, type }) } };
const preparation = { fileOps: { read: new Set(), written: new Set(), edited: new Set() } };
const check = async (reason = "threshold") =>
	((await handlers.session_before_compact({ reason, preparation, branchEntries: [], signal: new AbortController().signal }, ctx)) ?? {}).cancel === true;
const fail = (fields = {}) => handlers.session_compact_failed({ reason: "threshold", errorMessage: "boom", aborted: false, ...fields }, ctx);
const checks = async (n) => {
	const seen = [];
	for (let i = 0; i < n; i++) seen.push(await check());
	return seen;
};

const out = { backoff: { healthy: await checks(1) } };
await fail({ aborted: true });
out.backoff.afterAbort = await checks(1);
await fail();
out.backoff.afterOne = await checks(3);
await fail();
out.backoff.overflowDuringSkip = await check("overflow");
out.backoff.manualDuringSkip = await check("manual");
out.backoff.afterTwo = await checks(5);
await fail();
await handlers.session_compact({});
out.backoff.afterSuccess = await checks(1);
out.backoff.ladder = [1, 2, 3, 4, 5, 9].map(skippedChecks);
out.notes = notes.map((note) => note.type);

out.summarizesHere = {
	chatTemplate: summarizesHere({ compat: { thinkingFormat: "chat-template" } }),
	qwenChatTemplate: summarizesHere({ compat: { thinkingFormat: "qwen-chat-template" } }),
	otherFormat: summarizesHere({ compat: { thinkingFormat: "openai" } }),
	noCompat: summarizesHere({}),
	noModel: summarizesHere(undefined),
};

const lists = (entries, fileOps = { read: new Set(["own.py"]), written: new Set(), edited: new Set() }) => {
	carryFileLists(fileOps, entries);
	return { read: [...fileOps.read].sort(), edited: [...fileOps.edited].sort() };
};
const details = { readFiles: ["a.py"], modifiedFiles: ["b.py"] };
out.carry = {
	none: lists([]),
	ours: lists([{ type: "compaction", fromHook: true, details }]),
	pisOwn: lists([{ type: "compaction", fromHook: false, details }]),
	latestWins: lists([{ type: "compaction", fromHook: true, details }, { type: "message" }, { type: "compaction", fromHook: true, details: { readFiles: ["c.py"] } }]),
	malformed: lists([{ type: "compaction", fromHook: true, details: { readFiles: "a.py" } }]),
};
console.log(JSON.stringify(out));
