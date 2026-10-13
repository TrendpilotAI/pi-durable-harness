import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SessionManager } from "../src/session.ts";
import { testConfig } from "./fixtures/util.ts";

function runChild(args: string[], killWhen?: RegExp, killAfterMs = 1500, onSpawn?: (kill: () => void) => void): Promise<{ out: string; code: number | null; signal: string | null }> {
	return new Promise((resolve) => {
		const child = spawn(process.execPath, [join(__dirname, "fixtures/faux-session.ts"), ...args], { stdio: ["ignore", "pipe", "pipe"] });
		let out = "";
		let armed = false;
		const onData = (d: Buffer) => {
			out += d.toString();
			if (killWhen && !armed && killWhen.test(out)) {
				armed = true;
				setTimeout(() => child.kill("SIGKILL"), killAfterMs);
			}
		};
		child.stdout.on("data", onData);
		child.stderr.on("data", onData);
		child.on("exit", (code, signal) => resolve({ out, code, signal }));
		onSpawn?.(() => child.kill("SIGKILL"));
	});
}

describe("durable sessions", () => {
	it("survives SIGKILL mid-generation and resumes by id", async () => {
		const root = mkdtempSync(join(tmpdir(), "pdh-test-"));
		const first = await runChild([root, "crash1", "slow"], /\[submit\]/);
		expect(first.signal).toBe("SIGKILL");
		const sm = new SessionManager(root, testConfig());
		expect(sm.list()[0].status).toBe("interrupted");
		const st: any = await sm.status("crash1");
		expect(st.status).toBe("interrupted");
		expect(st.store.unsettledSubmissions.length).toBe(1);
		const second = await runChild([root, "crash1", "fast"]);
		expect(second.code).toBe(0);
		const final = JSON.parse(second.out.split("FINAL ")[1]);
		expect(final.status).toBe("done");
		expect(final.runs).toBe(2);
		const after: any = await sm.status("crash1");
		expect(after.store.unsettledSubmissions.length).toBe(0);
		expect(after.prompts[0].submissionId).toBe(st.prompts[0].submissionId); // same submission, not a duplicate
	}, 60_000);

	it("owner.pid lock: refuses a live owner, recovers a stale one, admits one of two racing processes", async () => {
		const root = mkdtempSync(join(tmpdir(), "pdh-lock-"));
		const sm = new SessionManager(root, testConfig());
		sm.create({ id: "lk", prompt: "say something", cwd: join(root, "ws") });
		const lock = join(root, "lk", "owner.pid");
		// Live owner (our parent process): refused, lock untouched, meta not marked running.
		writeFileSync(lock, String(process.ppid));
		await expect(sm.run("lk")).rejects.toThrow(/owned by live pid/);
		expect(readFileSync(lock, "utf8")).toBe(String(process.ppid));
		expect(sm.readMeta("lk").status).toBe("created");
		// A follow-up on a locked session is refused before it touches meta.json.
		const metaPath = join(root, "lk", "meta.json");
		const before = readFileSync(metaPath, "utf8");
		await expect(sm.resume("lk", { prompt: "follow up" })).rejects.toThrow(/owned by live pid/);
		expect(readFileSync(metaPath, "utf8")).toBe(before);
		// Stale owner (a pid that has exited): the lock is taken over and released at the end.
		const dead = spawnSync(process.execPath, ["-e", ""]).pid!;
		writeFileSync(lock, String(dead));
		const done = await runChild([root, "lk", "fast"]);
		expect(done.code).toBe(0);
		expect(done.out).toContain('"status":"done"');
		expect(existsSync(lock)).toBe(false);
		// Two processes race for the same store: exactly one runs, the other refuses.
		sm.create({ id: "race", prompt: "say something", cwd: join(root, "ws2") });
		const kills: (() => void)[] = [];
		const a = runChild([root, "race", "hold"], undefined, 0, (k) => kills.push(k));
		const b = runChild([root, "race", "hold"], undefined, 0, (k) => kills.push(k));
		const loser = await Promise.race([a, b]);
		expect(loser.code).toBe(1);
		expect(loser.out).toMatch(/owned by (live pid|another process)/);
		for (const k of kills) k();
		const both = await Promise.all([a, b]);
		expect(both.filter((r) => r.signal === "SIGKILL").length).toBe(1);
	}, 60_000);
});
