import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const cliPath = join(__dirname, "../src/cli.ts");
function pdh(...args: string[]) {
	const home = mkdtempSync(join(tmpdir(), "pdh-cli-"));
	const r = spawnSync(process.execPath, [cliPath, ...args], { encoding: "utf8", cwd: home, env: { ...process.env, PDH_HOME: home, PDH_PI_MODELS_JSON: join(home, "none.json") } });
	return { code: r.status, out: r.stdout, err: r.stderr };
}

describe("cli: mass", () => {
	it("train is deferred: prints DEFERRED, runs nothing, exits 0", () => {
		const r = pdh("mass", "train", "--config", "/nonexistent.json");
		expect(r.code).toBe(0);
		expect(r.out).toMatch(/^DEFERRED: LoRA post-training is not implemented/);
	});
	it("unknown or missing stage prints usage and exits 2", () => {
		for (const args of [["mass", "bogus"], ["mass"]]) {
			const r = pdh(...args);
			expect(r.code).toBe(2);
			expect(r.err).toMatch(/unknown mass stage 'bogus'|mass needs a stage/);
			expect(r.out).toContain("pdh mass search|collect|rank|select|render|cycle");
		}
	});
	it("cycle never runs train", () => {
		const src = readFileSync(join(__dirname, "../src/mass/pipeline.ts"), "utf8");
		const body = src.slice(src.indexOf("export async function cycle"));
		expect(body).not.toMatch(/train\s*\(/);
	});
});

describe("cli: option validation", () => {
	it.each([
		[["start", "--timeout-ms", "abc", "x"], /--timeout-ms must be a positive integer, got "abc"/],
		[["start", "--timeout-ms", "0", "x"], /--timeout-ms must be a positive integer/],
		[["resume", "s1", "--temperature", "hot"], /--temperature must be a finite number >= 0, got "hot"/],
		[["resume", "s1", "--temperature", "Infinity"], /--temperature must be a finite number >= 0/],
		[["start", "--seed", "1.5", "x"], /--seed must be an integer, got "1.5"/],
		[["mass", "rank", "--tasks", "1,x"], /--tasks must be a comma-separated list of task ids/],
		[["start", "--bogus", "x"], /unknown option '--bogus'\. Run 'pdh' with no arguments for usage\./],
		[["start", "--timeout-ms"], /Option '--timeout-ms <value>' argument missing/],
	])("%j", (args, msg) => {
		const r = pdh(...args);
		expect(r.code).toBe(2);
		expect(r.err).toMatch(msg);
		expect(r.err).not.toMatch(/\n\s+at /); // no stack trace
	});
});

