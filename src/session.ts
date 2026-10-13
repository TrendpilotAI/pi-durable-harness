import { BACKGROUND_CONTEXT as ctx } from "@earendil-works/chord/context";
import type { Models } from "@earendil-works/pi-ai";
import { AssistantEntry, createRegistry, Harness, type Extension } from "@earendil-works/pi-durable";
import { NodeExecutionEnv } from "@earendil-works/pi-durable/env/node";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { CodingTools } from "@earendil-works/pi-durable/tools";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { harnessHome, loadMlxConfig, type MlxConfig } from "./config.ts";
import { mlxModels } from "./provider.ts";
import { assistantText, TeamExtension } from "./subagent.ts";

export type SessionStatus = "created" | "running" | "done" | "unanswered" | "aborted" | "interrupted";

export type SessionMeta = {
	id: string;
	createdAt: string;
	updatedAt: string;
	cwd: string;
	model: string;
	thinkingLevel: string;
	team: boolean;
	instructions?: string;
	prompts: { requestId: string; content: string; submissionId?: number; status?: string; answerText?: string }[];
	status: SessionStatus;
	pid?: number;
	runs: { pid: number; startedAt: string; endedAt?: string; outcome?: string }[];
};

export type RunOptions = {
	/** Base URL override (e.g. a trajectory-logging proxy in front of mlx-serve). */
	baseUrl?: string;
	/** Abort the conversation after this many ms (keeps demos bounded). */
	timeoutMs?: number;
	onEvent?: (line: string) => void;
	models?: Models;
	extraExtensions?: Extension[];
	/** When a run ends `unanswered` with a model error (e.g. a server-side decode failure), queue up to this many durable "continue" inputs. Default 2. */
	autoContinue?: number;
};

export const CONTINUE_PROMPT =
	"The previous model request failed with a server-side error before you finished. Continue the original task from where you left off; check the files on disk first.";

export function sessionsRoot(root = harnessHome()): string {
	return join(root, "sessions");
}

function isAlive(pid: number | undefined): boolean {
	if (!pid) return false;
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
}

/** Durable coding-agent sessions: one pi-durable SQLite store per session id. */
export class SessionManager {
	readonly root: string;
	readonly cfg: MlxConfig;
	constructor(root = sessionsRoot(), cfg = loadMlxConfig()) {
		this.root = root;
		this.cfg = cfg;
		mkdirSync(root, { recursive: true });
	}

	dir(id: string): string {
		if (!/^[A-Za-z0-9._-]+$/.test(id)) throw new Error(`invalid session id: ${id}`);
		return join(this.root, id);
	}

	readMeta(id: string): SessionMeta {
		const p = join(this.dir(id), "meta.json");
		if (!existsSync(p)) throw new Error(`unknown session: ${id}`);
		return JSON.parse(readFileSync(p, "utf8"));
	}

	writeMeta(meta: SessionMeta): void {
		meta.updatedAt = new Date().toISOString();
		const p = join(this.dir(meta.id), "meta.json");
		writeFileSync(p + ".tmp", JSON.stringify(meta, null, 2) + "\n");
		renameSync(p + ".tmp", p); // atomic replace
	}

	create(opts: { prompt: string; cwd: string; id?: string; thinkingLevel?: string; team?: boolean; instructions?: string }): SessionMeta {
		const id = opts.id ?? `s-${new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14)}-${randomUUID().slice(0, 8)}`;
		const d = this.dir(id);
		if (existsSync(join(d, "meta.json"))) throw new Error(`session exists: ${id}`);
		mkdirSync(d, { recursive: true });
		const cwd = resolve(opts.cwd);
		mkdirSync(cwd, { recursive: true });
		const now = new Date().toISOString();
		const meta: SessionMeta = {
			id,
			createdAt: now,
			updatedAt: now,
			cwd,
			model: this.cfg.model.id,
			thinkingLevel: opts.thinkingLevel ?? "off",
			team: opts.team ?? false,
			instructions: opts.instructions,
			prompts: [{ requestId: `start:${id}`, content: opts.prompt }],
			status: "created",
			runs: [],
		};
		this.writeMeta(meta);
		return meta;
	}

	list(): SessionMeta[] {
		if (!existsSync(this.root)) return [];
		return readdirSync(this.root)
			.filter((n) => existsSync(join(this.root, n, "meta.json")))
			.map((n) => this.readMeta(n))
			.map((m) => (m.status === "running" && !isAlive(m.pid) ? { ...m, status: "interrupted" as const } : m))
			.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
	}

	private async open(meta: SessionMeta, opts: RunOptions) {
		const registry = createRegistry();
		registry.install(CodingTools);
		if (meta.team) registry.install(TeamExtension);
		for (const e of opts.extraExtensions ?? []) registry.install(e);
		const models = opts.models ?? mlxModels(this.cfg, opts.baseUrl);
		const storage = await openNodeSqliteStorage(join(this.dir(meta.id), "session.sqlite"));
		const harness = await Harness.open(
			storage,
			{
				models,
				registry,
				settings: { stream: { timeoutMs: 600_000 }, retry: { maxRetries: 2 } },
				env: ({ cwd }) => new NodeExecutionEnv({ cwd: cwd ?? meta.cwd }),
				onReport: (e) => opts.onEvent?.(`[report] ${String((e as any)?.message ?? e)}`),
			},
			ctx,
		);
		const root = await harness.root(ctx, {
			agent: {
				model: { provider: this.cfg.provider, modelId: meta.model },
				thinkingLevel: meta.thinkingLevel as any,
				cwd: meta.cwd,
				...(meta.instructions ? { instructions: meta.instructions } : {}),
			},
		});
		return { harness, root };
	}

	/** Run (or continue) every prompt of the session to settlement. Safe to call again after a crash. */
	async run(id: string, opts: RunOptions = {}): Promise<SessionMeta> {
		let meta = this.readMeta(id);
		if (meta.status === "running" && isAlive(meta.pid) && meta.pid !== process.pid) {
			throw new Error(`session ${id} is owned by live pid ${meta.pid}; one process per store`);
		}
		const lock = join(this.dir(id), "owner.pid");
		writeFileSync(lock, String(process.pid));
		meta.status = "running";
		meta.pid = process.pid;
		meta.runs.push({ pid: process.pid, startedAt: new Date().toISOString() });
		this.writeMeta(meta);
		const { harness, root } = await this.open(meta, opts);
		let timer: NodeJS.Timeout | undefined;
		let timedOut = false;
		if (opts.timeoutMs) {
			timer = setTimeout(() => {
				timedOut = true;
				opts.onEvent?.(`[timeout] aborting after ${opts.timeoutMs} ms`);
				void root.abort(ctx).catch(() => {});
			}, opts.timeoutMs);
		}
		try {
			harness.resume(); // continue any run the previous process left unfinished
			for (const p of meta.prompts) {
				if (p.status === "done" || p.status === "unanswered") continue;
				// requestId makes this idempotent: after a crash it returns the already-admitted submission.
				const sub = await root.submit({ type: "input", content: p.content, requestId: p.requestId }, ctx);
				p.submissionId = sub.id as unknown as number;
				p.status = "submitted";
				this.writeMeta(meta);
				opts.onEvent?.(`[submit] ${p.requestId} -> submission ${sub.id}`);
				const settled = await sub.wait(ctx);
				p.status = settled.status;
				if (settled.status === "done") {
					const entry = await root.commit((tx) => tx.entry(AssistantEntry, settled.answer!), ctx);
					p.answerText = assistantText(entry);
				} else {
					p.answerText = `unanswered: ${(settled as any).reason ?? ""} ${(settled as any).detail ?? ""}`.trim();
				}
				this.writeMeta(meta);
				opts.onEvent?.(`[settled] ${p.requestId} ${settled.status} ${settled.status === "done" ? "" : p.answerText}`);
				if (settled.status !== "done") {
					const retries = meta.prompts.filter((x) => x.requestId.includes(":continue")).length;
					if ((settled as any).reason === "model_error" && !timedOut && retries < (opts.autoContinue ?? 2)) {
						meta.prompts.push({ requestId: `${meta.prompts[0].requestId}:continue${retries + 1}`, content: CONTINUE_PROMPT });
						this.writeMeta(meta);
						opts.onEvent?.(`[auto-continue] queued continue${retries + 1} after model_error`);
						continue;
					}
					break;
				}
			}
			const last = meta.prompts[meta.prompts.length - 1];
			meta.status = timedOut ? "aborted" : last.status === "done" ? "done" : "unanswered";
		} finally {
			if (timer) clearTimeout(timer);
			await harness.close(ctx).catch(() => {});
			meta.pid = undefined;
			const run = meta.runs[meta.runs.length - 1];
			run.endedAt = new Date().toISOString();
			run.outcome = meta.status;
			this.writeMeta(meta);
			rmSync(lock, { force: true });
		}
		return meta;
	}

	async start(opts: Parameters<SessionManager["create"]>[0] & RunOptions): Promise<SessionMeta> {
		const meta = this.create(opts);
		return this.run(meta.id, opts);
	}

	/** Resume by id; optionally queue a follow-up prompt in the same durable conversation. */
	async resume(id: string, opts: RunOptions & { prompt?: string } = {}): Promise<SessionMeta> {
		const meta = this.readMeta(id);
		if (opts.prompt) {
			meta.prompts.push({ requestId: `follow:${id}:${meta.prompts.length}`, content: opts.prompt });
			this.writeMeta(meta);
		}
		return this.run(id, opts);
	}

	/** Compact transcript of the root conversation (read-only; the scheduler is not started). */
	async transcript(id: string): Promise<Record<string, unknown>[]> {
		const meta = this.readMeta(id);
		if (meta.status === "running" && isAlive(meta.pid)) throw new Error(`session ${id} is owned by live pid ${meta.pid}`);
		const { harness, root } = await this.open(meta, {});
		try {
			const page = await root.entries({}, 1000, undefined, ctx);
			return page.items.map((e: any) => {
				const msgs = e.model ?? [];
				const parts = msgs.flatMap((m: any) => (Array.isArray(m.content) ? m.content : [{ type: "text", text: String(m.content ?? "") }]));
				const summary = parts
					.map((c: any) => (c.type === "text" ? c.text : c.type === "toolCall" ? `toolCall ${c.name} ${JSON.stringify(c.arguments).slice(0, 160)}` : c.type))
					.join(" | ")
					.slice(0, 400);
				const m0 = msgs[0] ?? {};
				return { id: e.id, kind: e.kind, stopReason: m0.stopReason, error: m0.errorMessage, summary };
			});
		} finally {
			await harness.close(ctx).catch(() => {});
		}
	}

	/** Read-only inspection: opens the store without starting the scheduler (no task code runs). */
	async status(id: string): Promise<Record<string, unknown>> {
		const meta = this.readMeta(id);
		const live = meta.status === "running" && isAlive(meta.pid);
		const out: Record<string, unknown> = { ...meta, status: meta.status === "running" && !live ? "interrupted" : meta.status };
		if (live) {
			out.note = `owned by live pid ${meta.pid}; store not opened (one process per store)`;
			return out;
		}
		const { harness, root } = await this.open(meta, {});
		try {
			const ins = await harness.inspect(ctx);
			const page = await root.entries({}, 1000, undefined, ctx);
			out.store = {
				scheduling: ins.scheduling,
				liveTasks: ins.tasks.map((t: any) => ({ id: t.record?.id, task: t.record?.kind ?? t.record?.name, committed: t.record?.state?.status, scheduler: t.state?.kind })),
				unsettledSubmissions: ins.submissions.map((s) => ({ id: s.id, status: s.status, requestId: (s as any).requestId })),
				entries: page.items.length,
				entryKinds: page.items.reduce((acc: Record<string, number>, e: any) => ((acc[e.kind] = (acc[e.kind] ?? 0) + 1), acc), {}),
				usage: await harness.usage(ctx),
			};
		} finally {
			await harness.close(ctx).catch(() => {});
		}
		return out;
	}
}
