// Drives agents/pi/extensions/loop-guard.ts with a fake pi API; tests/test_loop_guard.py checks the JSON it prints.
const [guardPath] = process.argv.slice(2);
const { default: register, similarity, observe } = await import(guardPath);

const handlers = {};
register({ on: (name, handler) => (handlers[name] = handler) });
const notes = [];
const ctx = { hasUI: true, ui: { notify: (message, type) => notes.push({ message, type }) } };
const bash = (command) => handlers.tool_call({ toolName: "bash", input: { command } }, ctx);
const result = (toolName, isError = false) => handlers.tool_result({ toolName, isError, input: {}, content: [] }, ctx);

const script = (n) => `cd /work && python3 - <<'EOF'\nimport json\nprint(json.dumps({'case': ${n % 10}}))\nEOF`;
const out = {
	similarity: { same: similarity("abc", "abc"), near: similarity(script(1), script(2)), apart: similarity("ls -la", "pytest -q tests") },
	verdicts: [],
};
for (let i = 0; i < 21; i++) out.verdicts.push((await bash(script(i))) ?? null);

// Edit-and-rerun: the same test command after each successful edit is never counted.
const cycle = [];
for (let i = 0; i < 30; i++) {
	cycle.push((await bash("pytest -q tests/test_app.py")) ?? null);
	await result("edit");
}
out.cycle = cycle.filter(Boolean).length;

// A failed edit does not reset the count.
const failed = [];
for (let i = 0; i < 10; i++) {
	failed.push((await bash("pytest -q tests/test_other.py")) ?? null);
	await result("edit", true);
}
out.failedEdit = failed.map((v) => (v ? (v.terminate ? "stop" : "nudge") : "run"));
out.state = { a: observe({ recent: [], streak: 0 }, "x"), unchanged: observe({ recent: ["x"], streak: 9 }, "x") };

// A cycle of two different commands (a long run, then a check) repeats; three is the window.
const run = (dir) => `cd /work && timeout 5400 python3 -m ladder --out build/${dir} --upto lint 2>&1 | tail -3`;
const check = (dir) => `python3 -c "import json; print(json.load(open('build/${dir}/summary.json')))"`;
const pair = [];
for (let i = 0; i < 6; i++) {
	pair.push((await bash(run(`d${i}`))) ?? null);
	pair.push((await bash(check(`d${i}`))) ?? null);
}
out.pair = pair.map((v) => (v ? (v.terminate ? "stop" : "nudge") : "run"));
out.pairSimilarity = similarity(run("d0"), check("d0"));
const outside = { recent: [], streak: 0 };
out.outsideWindow = ["a1", "bbbb", "cccccc", "dddddddd", "a1"].map((c) => observe(outside, c, { nudge: 2, stop: 9, similarity: 0.9, window: 3 }));
out.notes = notes.length;
console.log(JSON.stringify(out));
