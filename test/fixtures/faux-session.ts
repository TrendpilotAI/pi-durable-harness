// Child process for the crash and lock tests: runs a session whose model streams slowly, so the parent can kill -9 it
// mid-turn. Modes: slow (create + slow run), hold (slow run of an existing session), fast (quick run).
import { createModels, fauxAssistantMessage, fauxProvider } from "@earendil-works/pi-ai";
import { SessionManager } from "../../src/session.ts";
import { testConfig } from "./util.ts";

const [root, id, mode] = process.argv.slice(2);
const cfg = testConfig();
const faux = fauxProvider({ provider: "mlx", models: [{ id: cfg.model.id }], tokensPerSecond: mode !== "fast" ? 2 : 100000 } as any);
faux.setResponses([fauxAssistantMessage(mode !== "fast" ? "this answer streams very slowly and will be interrupted ".repeat(20) : "recovered answer")]);
const models = createModels();
models.setProvider(faux.provider);
const sm = new SessionManager(root, cfg);
if (mode === "slow") sm.create({ id, prompt: "say something", cwd: root + "/ws" });
const meta = await sm.run(id, { models, onEvent: (s) => console.log(s) });
console.log("FINAL " + JSON.stringify({ status: meta.status, answer: meta.prompts.at(-1)?.answerText, runs: meta.runs.length }));
