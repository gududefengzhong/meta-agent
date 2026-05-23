// VS Code surface for the meta-agent extension: output channel + the
// permission-prompt UX that maps the agent's prompts into native VS
// Code notifications. Kept separate from `extension.ts` so the
// command bodies stay readable.

import * as vscode from "vscode";

import type {
    LifecycleEvent,
    LlmStreamChunk,
    MetaAgentClient,
    PermissionPrompt,
} from "./client";

const PLAN_TOOL_SENTINEL = "<plan>";

export function createOutputChannel(): vscode.OutputChannel {
    return vscode.window.createOutputChannel("meta-agent");
}

export function logChunk(channel: vscode.OutputChannel, chunk: LlmStreamChunk): void {
    if (typeof chunk.content_delta === "string" && chunk.content_delta) {
        // `append` (not `appendLine`) so tokens stream as the agent
        // emits them, without a forced newline per chunk.
        channel.append(chunk.content_delta);
    }
    if (chunk.finish_reason) {
        channel.appendLine("");
    }
}

export function logEvent(channel: vscode.OutputChannel, event: LifecycleEvent): string | null {
    const action = typeof event.action === "string" ? event.action : null;
    const state = typeof event.state === "string" ? event.state : null;
    if (action) {
        channel.appendLine(`[${action}${state ? ` state=${state}` : ""}]`);
    }
    return state;
}

export async function decidePermissionViaUi(
    client: MetaAgentClient,
    prompt: PermissionPrompt,
): Promise<void> {
    const isPlan = prompt.tool_name === PLAN_TOOL_SENTINEL;
    let allow: boolean;
    if (isPlan) {
        allow = await showPlanPrompt(prompt);
    } else {
        allow = await showToolPrompt(prompt);
    }
    let reason: string | undefined;
    if (!allow) {
        reason = await vscode.window.showInputBox({
            prompt: `Reason for denying ${prompt.tool_name} (optional)`,
            placeHolder: "Leave blank to skip",
        });
        if (reason === undefined) {
            reason = undefined;
        } else if (reason.trim() === "") {
            reason = undefined;
        }
    }
    try {
        await client.decidePermission(prompt.task_id, prompt.prompt_id, allow, reason);
    } catch (err) {
        vscode.window.showErrorMessage(
            `meta-agent: failed to deliver permission decision: ${String(err)}`,
        );
    }
}

async function showToolPrompt(prompt: PermissionPrompt): Promise<boolean> {
    const detail = prompt.summary || `Run tool '${prompt.tool_name}'`;
    const payload = renderJson(prompt.payload);
    const message = `meta-agent: ${detail}`;
    const choice = await vscode.window.showWarningMessage(
        message,
        { modal: true, detail: payload ? `payload:\n${payload}` : undefined },
        "Allow",
        "Deny",
    );
    return choice === "Allow";
}

async function showPlanPrompt(prompt: PermissionPrompt): Promise<boolean> {
    const planText = prompt.summary || "(no plan text)";
    const toolCalls = extractPlanToolCalls(prompt);
    const actions = toolCalls
        .map((tc, idx) => `${idx + 1}. ${tc.name}(${renderJson(tc.arguments)})`)
        .join("\n");
    const detail =
        actions.length > 0
            ? `${planText}\n\nProposed actions (${toolCalls.length}):\n${actions}`
            : `${planText}\n\n(no tool calls in this plan)`;
    const choice = await vscode.window.showInformationMessage(
        "meta-agent: approve plan?",
        { modal: true, detail },
        "Allow plan",
        "Deny",
    );
    return choice === "Allow plan";
}

interface PlanToolCall {
    name: string;
    arguments: Record<string, unknown>;
}

function extractPlanToolCalls(prompt: PermissionPrompt): PlanToolCall[] {
    const payload = prompt.payload;
    if (!payload || typeof payload !== "object") {
        return [];
    }
    const raw = (payload as Record<string, unknown>).tool_calls;
    if (!Array.isArray(raw)) {
        return [];
    }
    const out: PlanToolCall[] = [];
    for (const entry of raw) {
        if (!entry || typeof entry !== "object") {
            continue;
        }
        const name = (entry as Record<string, unknown>).name;
        const args = (entry as Record<string, unknown>).arguments;
        if (typeof name !== "string") {
            continue;
        }
        out.push({
            name,
            arguments:
                args && typeof args === "object"
                    ? (args as Record<string, unknown>)
                    : {},
        });
    }
    return out;
}

function renderJson(value: unknown): string {
    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}
