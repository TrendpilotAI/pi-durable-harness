/** Ports of MASS mass/ranking.py (Bradley-Terry) and pipeline.tournament_pairs. */

/** Mulberry32: small seeded PRNG (Python's random.Random is not reproducible in JS; schedules differ from MASS). */
export function rng(seed: number): () => number {
	let a = seed >>> 0;
	return () => {
		a = (a + 0x6d2b79f5) >>> 0;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

export function shuffle<T>(xs: T[], seed: number): T[] {
	const r = rng(seed);
	const a = [...xs];
	for (let i = a.length - 1; i > 0; i--) {
		const j = Math.floor(r() * (i + 1));
		[a[i], a[j]] = [a[j], a[i]];
	}
	return a;
}

/** Connected balanced ring schedule; unique unordered pairs, seeded order. */
export function tournamentPairs(names: string[], opponents: number, seed: number): [string, string][] {
	if (opponents < 2 || opponents % 2) throw new Error("Use an even opponents_per_episode >= 2");
	const ring = shuffle([...names].sort(), seed);
	const set = new Set<string>();
	const n = ring.length;
	for (let i = 0; i < n; i++)
		for (let d = 1; d <= Math.min(opponents / 2, n - 1); d++) {
			const pair = [ring[i], ring[(i + d) % n]].sort();
			set.add(JSON.stringify(pair));
		}
	const pairs = [...set].sort().map((s) => JSON.parse(s) as [string, string]);
	const r = rng(seed + 1);
	return pairs.map(([a, b]) => (r() < 0.5 ? [b, a] : [a, b]));
}

/** MM algorithm with a weak prior (each episode gets `prior` virtual wins/losses vs the mean). Returns log-strengths. */
export function bradleyTerry(episodes: string[], matches: Map<string, Record<string, string>>, iters = 500, prior = 0.5): Record<string, number> {
	const idx = new Map(episodes.map((e, i) => [e, i]));
	const n = episodes.length;
	const wins = Array.from({ length: n }, () => new Array(n).fill(0));
	for (const [key, per] of matches) {
		const [a, b] = JSON.parse(key) as [string, string];
		if (!idx.has(a) || !idx.has(b)) continue;
		const ia = idx.get(a)!;
		const ib = idx.get(b)!;
		for (const lab of Object.values(per)) {
			if (lab === a) wins[ia][ib] += 1;
			else if (lab === b) wins[ib][ia] += 1;
			else {
				wins[ia][ib] += 0.5;
				wins[ib][ia] += 0.5;
			}
		}
	}
	let p = new Array(n).fill(1);
	for (let it = 0; it < iters; it++) {
		const np: number[] = [];
		for (let i = 0; i < n; i++) {
			const W = wins[i].reduce((s, x) => s + x, 0) + prior;
			let denom = prior / (p[i] + 1);
			for (let j = 0; j < n; j++) {
				if (i === j) continue;
				const nij = wins[i][j] + wins[j][i];
				if (nij) denom += nij / (p[i] + p[j]);
			}
			np.push(denom > 0 ? W / denom : p[i]);
		}
		const s = np.reduce((x, y) => x + y, 0) / n;
		p = np.map((x) => x / s);
	}
	return Object.fromEntries(episodes.map((e) => [e, Math.log(p[idx.get(e)!])]));
}

/** MASS _derive_champion_baseline: rows keyed v{k}_vs_no_harness (A = reference, B = version) and champion_v{a}_vs_v{b}. */
export function deriveChampionBaseline(historyRows: { key: string; winner?: string }[], championRows: { key: string; winner?: string }[], current: number): { champion: number; hasWin: boolean; needsArbitration: boolean } {
	const wins = new Map<number, string | undefined>();
	for (const r of historyRows) {
		const m = /^v(\d+)_vs_no_harness$/.exec(r.key);
		if (m) wins.set(Number(m[1]), r.winner);
	}
	const arb = new Map<string, string | undefined>();
	for (const r of championRows) {
		const m = /^champion_v(\d+)_vs_v(\d+)$/.exec(r.key);
		if (m) arb.set(`${m[1]},${m[2]}`, r.winner);
	}
	let champ = 0;
	let hasWin = wins.get(0) === "B";
	let needs = false;
	for (let k = 1; k <= current; k++) {
		if (wins.get(k) !== "B") continue;
		if (!hasWin) {
			champ = k;
			hasWin = true;
		} else if (arb.has(`${champ},${k}`)) {
			if (arb.get(`${champ},${k}`) === "B") champ = k;
		} else if (k === current) needs = true;
	}
	return { champion: champ, hasWin, needsArbitration: needs };
}
