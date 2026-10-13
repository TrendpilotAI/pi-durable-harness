#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { loadMlxConfig } from "./config.ts";
import { collect, cycle, makeCtx, rank, render, searchTask, select } from "./mass/pipeline.ts";
import { SessionManager, sessionsRoot } from "./session.ts";

const USAGE = `pdh — durable pi coding-agent sessions on local mlx-serve, plus a local MASS loop

Sessions:
  pdh start  [--cwd DIR] [--id ID] [--team] [--thinking off|low|medium|high] [--timeout-ms N] [--temperature T] [--seed N] (PROMPT | --prompt-file F)
  pdh resume ID [--prompt TEXT] [--timeout-ms N] [--temperature T] [--seed N]
  pdh list   [--json]
  pdh status ID
  pdh transcript ID
  pdh config
MASS:
  pdh mass search|collect|rank|select|render|cycle --config configs/local.json [--tasks 1,2] [--retry-failed]
  pdh mass train                   (DEFERRED: prints a note, runs nothing, exits 0)

Env: PDH_HOME (default ~/.pi-durable-harness), PDH_BASE_URL, PDH_MODEL, PDH_MAX_TOKENS, PDH_API_KEY, PDH_PI_MODELS_JSON,
     PDH_MAX_CONCURRENT (MASS episodes: max in-flight requests to mlx-serve; 0 = unlimited)`;

const MASS_STAGES = ["search", "collect", "rank", "select", "render", "cycle"];

const TRAIN_DEFERRED = `DEFERRED: LoRA post-training is not implemented in this harness; nothing was run.
pdh mass cycle stops after render. The SFT hand-off data is runs/<run>/data/{train,val}.jsonl.
See docs/MASS.md#post-training-deferred for the (unverified) mlx_lm.lora plan.`;

/** Bad command-line input: reported without a stack, exit code 2. */
class UsageError extends Error {}

/** Parse a numeric option; undefined when absent. */
function numberOption(name: string, raw: string | undefined, rule: "positive-int" | "int" | "non-negative"): number | undefined {
	if (raw === undefined) return undefined;
	const n = raw.trim() === "" ? NaN : Number(raw);
	const ok = rule === "positive-int" ? Number.isSafeInteger(n) && n > 0 : rule === "int" ? Number.isSafeInteger(n) : Number.isFinite(n) && n >= 0;
	const want = rule === "positive-int" ? "a positive integer" : rule === "int" ? "an integer" : "a finite number >= 0";
	if (!ok) throw new UsageError(`--${name} must be ${want}, got ${JSON.stringify(raw)}`);
	return n;
}

function tasksOption(raw: string | undefined): number[] | undefined {
	if (raw === undefined) return undefined;
	const parts = raw.split(",").map((t) => t.trim());
	if (parts.some((t) => !/^\d+$/.test(t))) throw new UsageError(`--tasks must be a comma-separated list of task ids (e.g. 1,2), got ${JSON.stringify(raw)}`);
	return parts.map(Number);
}

/** Turn node:util parseArgs errors (strict mode) into one-line messages. */
function friendlyParseError(e: any): Error {
	const hint = "Run 'pdh' with no arguments for usage.";
	if (e?.code === "ERR_PARSE_ARGS_UNKNOWN_OPTION") return new UsageError(`unknown option '${/'([^']+)'/.exec(String(e.message))?.[1]}'. ${hint}`);
	if (e?.code === "ERR_PARSE_ARGS_INVALID_OPTION_VALUE") return new UsageError(`${e.message}. ${hint}`);
	return e;
}

const ev = (s: string) => console.error(`${new Date().toISOString()} ${s}`);

const parse = (args: string[]) =>
	parseArgs({
		args,
		allowPositionals: true,
		options: {
			cwd: { type: "string" },
			id: { type: "string" },
			team: { type: "boolean" },
			thinking: { type: "string" },
			"timeout-ms": { type: "string" },
			temperature: { type: "string" },
			seed: { type: "string" },
			prompt: { type: "string" },
			"prompt-file": { type: "string" },
			json: { type: "boolean" },
			config: { type: "string", default: "configs/local.json" },
			tasks: { type: "string" },
			"retry-failed": { type: "boolean" },
		},
	});

async function main() {
	const [cmd, ...rest] = process.argv.slice(2);
	let parsed;
	try {
		parsed = parse(rest);
	} catch (e) {
		throw friendlyParseError(e);
	}
	const { values, positionals } = parsed;
	const timeoutMs = numberOption("timeout-ms", values["timeout-ms"], "positive-int");
	const sampling = { temperature: numberOption("temperature", values.temperature, "non-negative"), seed: numberOption("seed", values.seed, "int") };
	const tasks = tasksOption(values.tasks);
	const sm = () => new SessionManager(sessionsRoot());
	switch (cmd) {
		case "start": {
			const prompt = values["prompt-file"] ? readFileSync(values["prompt-file"], "utf8") : positionals.join(" ");
			if (!prompt.trim()) throw new Error("start needs a prompt");
			const m = sm();
			const meta = m.create({ prompt, cwd: values.cwd ?? process.cwd(), id: values.id, team: values.team, thinkingLevel: values.thinking, sampling });
			console.log(`session ${meta.id}`); // printed before running so a killed run can be resumed by id
			const done = await m.run(meta.id, { timeoutMs, onEvent: ev });
			console.log(JSON.stringify({ id: done.id, status: done.status, answer: done.prompts.at(-1)?.answerText }, null, 2));
			break;
		}
		case "resume": {
			const id = positionals[0];
			if (!id) throw new Error("resume needs a session id");
			const done = await sm().resume(id, { prompt: values.prompt, timeoutMs, sampling, onEvent: ev });
			console.log(JSON.stringify({ id: done.id, status: done.status, runs: done.runs.length, answer: done.prompts.at(-1)?.answerText }, null, 2));
			break;
		}
		case "list": {
			const rows = sm().list();
			if (values.json) console.log(JSON.stringify(rows, null, 2));
			else for (const r of rows) console.log(`${r.id}\t${r.status}\truns=${r.runs.length}\t${r.cwd}\t${r.prompts[0].content.slice(0, 50).replace(/\s+/g, " ")}`);
			break;
		}
		case "status": {
			const id = positionals[0];
			if (!id) throw new Error("status needs a session id");
			console.log(JSON.stringify(await sm().status(id), null, 2));
			break;
		}
		case "transcript": {
			const id = positionals[0];
			if (!id) throw new Error("transcript needs a session id");
			for (const e of await sm().transcript(id)) console.log(JSON.stringify(e));
			break;
		}
		case "config": {
			const c = loadMlxConfig();
			console.log(JSON.stringify({ ...c, apiKey: "(redacted)", sessions: sessionsRoot() }, null, 2));
			break;
		}
		case "mass": {
			const stage = positionals[0];
			if (stage === "train") {
				console.log(TRAIN_DEFERRED);
				break;
			}
			if (!MASS_STAGES.includes(stage)) {
				console.error(stage ? `error: unknown mass stage '${stage}'\n` : "error: mass needs a stage\n");
				console.log(USAGE);
				process.exitCode = 2;
				break;
			}
			const x = makeCtx(values.config!, (s) => ev(s));
			x.retryFailed = values["retry-failed"];
			if (stage === "search") for (const q of tasks ?? x.c.tasks.train) await searchTask(x, q);
			else if (stage === "collect") await collect(x, tasks ?? x.c.tasks.train);
			else if (stage === "rank") await rank(x, tasks ?? x.c.tasks.train);
			else if (stage === "select") select(x);
			else if (stage === "render") render(x);
			else if (stage === "cycle") await cycle(x, tasks);
			break;
		}
		default:
			console.log(USAGE);
			process.exitCode = cmd ? 2 : 0;
	}
}

main().catch((e) => {
	console.error(`error: ${e?.message ?? e}`);
	process.exit(e instanceof UsageError ? 2 : 1);
});
