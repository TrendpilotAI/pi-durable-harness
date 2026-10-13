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
  pdh mass train --config ...      (deferred: prints the LoRA plan, runs nothing)

Env: PDH_HOME (default ~/.pi-durable-harness), PDH_BASE_URL, PDH_MODEL, PDH_MAX_TOKENS, PDH_PI_MODELS_JSON`;

const ev = (s: string) => console.error(`${new Date().toISOString()} ${s}`);

async function main() {
	const [cmd, ...rest] = process.argv.slice(2);
	const { values, positionals } = parseArgs({
		args: rest,
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
	const timeoutMs = values["timeout-ms"] ? Number(values["timeout-ms"]) : undefined;
	const sampling = { temperature: values.temperature !== undefined ? Number(values.temperature) : undefined, seed: values.seed !== undefined ? Number(values.seed) : undefined };
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
			const x = makeCtx(values.config!, (s) => ev(s));
			x.retryFailed = values["retry-failed"];
			const tasks = values.tasks ? values.tasks.split(",").map(Number) : undefined;
			if (stage === "search") for (const q of tasks ?? x.c.tasks.train) await searchTask(x, q);
			else if (stage === "collect") await collect(x, tasks ?? x.c.tasks.train);
			else if (stage === "rank") await rank(x, tasks ?? x.c.tasks.train);
			else if (stage === "select") select(x);
			else if (stage === "render") render(x);
			else if (stage === "cycle") await cycle(x, tasks);
			else if (stage === "train") console.log("Deferred: weight post-training is not run on this Mac. See docs/MASS.md#post-training-deferred for the mlx_lm.lora plan over runs/*/data/{train,val}.jsonl.");
			else throw new Error(`unknown mass stage ${stage}`);
			break;
		}
		default:
			console.log(USAGE);
			process.exitCode = cmd ? 2 : 0;
	}
}

main().catch((e) => {
	console.error(`error: ${e?.message ?? e}`);
	process.exit(1);
});
