import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SessionManager } from "../src/session.ts";
import { testConfig } from "./fixtures/util.ts";

function runChild(args: string[], killWhen?: RegExp, killAfterMs = 1500): Promise<{ out: string; code: number | null; signal: string | null }> {
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
});
