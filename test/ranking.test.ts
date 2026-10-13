import { describe, expect, it } from "vitest";
import { bradleyTerry, deriveChampionBaseline, tournamentPairs } from "../src/mass/ranking.ts";

describe("ranking", () => {
	it("Bradley-Terry orders a consistent winner first", () => {
		const m = new Map<string, Record<string, string>>([
			[JSON.stringify(["a", "b"]), { self: "a" }],
			[JSON.stringify(["b", "c"]), { self: "b" }],
			[JSON.stringify(["a", "c"]), { self: "a" }],
		]);
		const s = bradleyTerry(["a", "b", "c"], m);
		expect(s.a).toBeGreaterThan(s.b);
		expect(s.b).toBeGreaterThan(s.c);
	});
	it("tournament pairs are unique and connected", () => {
		const names = ["e1", "e2", "e3", "e4", "e5"];
		const pairs = tournamentPairs(names, 2, 7);
		const keys = pairs.map((p) => [...p].sort().join("|"));
		expect(new Set(keys).size).toBe(keys.length);
		expect(new Set(pairs.flat()).size).toBe(5);
		expect(() => tournamentPairs(names, 3, 1)).toThrow();
	});
	it("champion follows MASS baseline-anchored rules", () => {
		const h = (k: number, w: string) => ({ key: `v${k}_vs_no_harness`, winner: w });
		expect(deriveChampionBaseline([h(0, "A"), h(1, "A")], [], 1)).toEqual({ champion: 0, hasWin: false, needsArbitration: false });
		expect(deriveChampionBaseline([h(0, "A"), h(1, "B")], [], 1)).toEqual({ champion: 1, hasWin: true, needsArbitration: false });
		expect(deriveChampionBaseline([h(0, "B"), h(1, "B")], [], 1).needsArbitration).toBe(true);
		expect(deriveChampionBaseline([h(0, "B"), h(1, "B")], [{ key: "champion_v0_vs_v1", winner: "B" }], 1)).toEqual({ champion: 1, hasWin: true, needsArbitration: false });
	});
});
