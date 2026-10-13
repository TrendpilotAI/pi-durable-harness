import { Type } from "@earendil-works/pi-ai";
import { AssistantEntry, configure, defineExtension, defineTool, type Extension } from "@earendil-works/pi-durable";

export function assistantText(entry: any): string {
	const msgs = entry?.model ?? [];
	const last = msgs[msgs.length - 1];
	return (last?.content ?? [])
		.filter((c: any) => c.type === "text")
		.map((c: any) => c.text)
		.join("")
		.trim();
}

/**
 * The `agent` tool: delegate one self-contained task to a worker that is a copy of the same model
 * (a MASS "model copy"). The worker is a durable child conversation owned by this tool call, so it
 * survives a crash: the tool is replay-safe and a rerun finds the same child and submission.
 * Workers cannot spawn workers (the team extension is removed from the child).
 */
export const TeamExtension: Extension = defineExtension({
	name: "team",
	tools: [
		defineTool({
			name: "agent",
			description:
				"Run a sub-agent (a copy of yourself with the same tools and working directory) on one self-contained task and return its final report. The sub-agent sees only `role_instruction` and `task` plus the filesystem.",
			parameters: Type.Object({
				agent_id: Type.String({ description: "Team member id from the workflow, e.g. agent_tester" }),
				role_instruction: Type.String({ description: "The role's instruction from the team definition" }),
				task: Type.String({ description: "Self-contained instructions: files, interfaces, verification command, expected report" }),
			}),
			replay: "safe",
			execute: async (args, api, context) => {
				const child = await api.commit(async (tx) => {
					const existing = (await tx.scanConversations({ ownerTaskId: api.taskId }, 1)).items[0];
					if (existing !== undefined) return existing.id;
					const created = await tx.createConversation({ ownership: { kind: "task", taskId: api.taskId } });
					await configure(tx, created.id, {
						extensions: { remove: [TeamExtension] },
						instructions: `You are team member ${args.agent_id}. ${args.role_instruction}\nWork only through your tools. Finish with a concise report: files written, commands run with results, known problems.`,
					});
					return created.id;
				}, context);
				await api.details({ conversationId: child, agentId: args.agent_id }, context);
				const handle = await api.conversation(child, context);
				if (handle === undefined) throw new Error("sub-agent conversation missing");
				const sub = await handle.submit({ type: "input", content: args.task, requestId: `agent:${api.taskId}` }, context);
				const settled = await sub.wait(context);
				if (settled.status !== "done") {
					return { output: [{ type: "text", text: `[${args.agent_id}] unanswered: ${settled.reason ?? "unknown"} ${settled.detail ?? ""}` }], isError: true };
				}
				const answer = await api.commit((tx) => tx.entry(AssistantEntry, settled.answer!), context);
				return { output: [{ type: "text", text: `[${args.agent_id} report]\n${assistantText(answer) || "(empty report)"}` }] };
			},
		}),
	],
});
