// Entry point for the meta-agent VS Code extension.
//
// The extension exposes two commands:
//
// * `metaAgent.run` — prompts for a natural-language task, submits it
//   to the meta-agent API, and tails the resulting streams.
// * `metaAgent.tail` — attaches to an existing task by id.
//
// Each command runs at most one "active task watcher" at a time per
// task — the watcher multiplexes three SSE streams (LLM chunks,
// lifecycle events, permission prompts) and lives until the
// lifecycle stream observes a terminal state. An `AbortController`
// owns the streams so deactivation / cancellation tears them down
// cleanly.

import * as vscode from "vscode";

import {
    LifecycleEvent,
    LlmStreamChunk,
    MetaAgentApiError,
    MetaAgentClient,
    PermissionPrompt,
    isTerminalState,
} from "./client";
import { readConfig } from "./config";
import { EditReviewPanel } from "./diff_webview";
import {
    createOutputChannel,
    decidePermissionViaUi,
    logChunk,
    logEvent,
} from "./ui";

let outputChannel: vscode.OutputChannel | undefined;
let editPanel: EditReviewPanel | undefined;
const watchers = new Map<string, AbortController>();

export function activate(context: vscode.ExtensionContext): void {
    outputChannel = createOutputChannel();
    editPanel = new EditReviewPanel();
    context.subscriptions.push(outputChannel);
    context.subscriptions.push({ dispose: () => editPanel?.dispose() });

    context.subscriptions.push(
        vscode.commands.registerCommand("metaAgent.run", async () => {
            await commandRun();
        }),
        vscode.commands.registerCommand("metaAgent.tail", async () => {
            await commandTail();
        }),
    );
}

export function deactivate(): void {
    for (const controller of watchers.values()) {
        controller.abort();
    }
    watchers.clear();
    outputChannel?.dispose();
    editPanel?.dispose();
    editPanel = undefined;
}

async function commandRun(): Promise<void> {
    const client = buildClientOrWarn();
    if (!client) {
        return;
    }
    const prompt = await vscode.window.showInputBox({
        prompt: "meta-agent: task prompt",
        placeHolder: "e.g. add a logger to src/auth/login.py",
        ignoreFocusOut: true,
    });
    if (!prompt) {
        return;
    }
    const channel = ensureOutputChannel();
    channel.show(true);
    channel.appendLine(`> ${prompt}`);

    let task;
    try {
        const cfg = readConfig();
        task = await client.submitTask({
            taskType: "system_shell_agent",
            inputPayload: { user_prompt: prompt },
            permissionMode: cfg.permissionMode,
        });
    } catch (err) {
        showError("submit task", err);
        return;
    }
    channel.appendLine(`[task ${task.task_id} state=${task.state}]`);
    await watchTask(client, task.task_id, channel);
}

async function commandTail(): Promise<void> {
    const client = buildClientOrWarn();
    if (!client) {
        return;
    }
    const taskId = await vscode.window.showInputBox({
        prompt: "meta-agent: task id to tail",
        placeHolder: "task-uuid",
        ignoreFocusOut: true,
    });
    if (!taskId) {
        return;
    }
    const channel = ensureOutputChannel();
    channel.show(true);
    channel.appendLine(`[attaching to ${taskId}]`);
    await watchTask(client, taskId, channel);
}

async function watchTask(
    client: MetaAgentClient,
    taskId: string,
    channel: vscode.OutputChannel,
): Promise<void> {
    if (watchers.has(taskId)) {
        // Surface a non-modal warning + bail — concurrent watchers
        // for the same task would duplicate output and double-
        // respond to permission prompts.
        vscode.window.showWarningMessage(
            `meta-agent: task ${taskId} is already being watched.`,
        );
        return;
    }
    const controller = new AbortController();
    watchers.set(taskId, controller);

    const chunkLoop = (async () => {
        try {
            for await (const chunk of client.streamLlmChunks(taskId, controller.signal)) {
                logChunk(channel, chunk as LlmStreamChunk);
            }
        } catch (err) {
            if (!isAbort(err)) {
                showError("llm stream", err);
            }
        }
    })();

    const promptLoop = (async () => {
        try {
            for await (const prompt of client.streamPermissionPrompts(
                taskId,
                controller.signal,
            )) {
                // Render + decide. We deliberately await the user's
                // decision: a fast model emitting back-to-back
                // prompts is rare in v0, and serialising keeps the
                // UX comprehensible.
                const panel = editPanel ?? new EditReviewPanel();
                await decidePermissionViaUi(client, prompt as PermissionPrompt, panel);
            }
        } catch (err) {
            if (!isAbort(err)) {
                showError("permission stream", err);
            }
        }
    })();

    const eventLoop = (async () => {
        try {
            for await (const event of client.streamEvents(taskId, controller.signal)) {
                const state = logEvent(channel, event as LifecycleEvent);
                if (isTerminalState(state)) {
                    controller.abort();
                    return;
                }
            }
        } catch (err) {
            if (!isAbort(err)) {
                showError("events stream", err);
            }
        }
    })();

    try {
        // Lifecycle stream is the authoritative completion signal;
        // others get cancelled via the controller when it returns.
        await eventLoop;
    } finally {
        controller.abort();
        await Promise.allSettled([chunkLoop, promptLoop]);
        watchers.delete(taskId);
        channel.appendLine(`[task ${taskId} closed]`);
    }
}

function buildClientOrWarn(): MetaAgentClient | null {
    const cfg = readConfig();
    if (!cfg.token) {
        vscode.window.showWarningMessage(
            "meta-agent: bearer token missing. Set metaAgent.token in settings.",
        );
        return null;
    }
    try {
        return new MetaAgentClient(cfg.apiUrl, cfg.token);
    } catch (err) {
        showError("client init", err);
        return null;
    }
}

function ensureOutputChannel(): vscode.OutputChannel {
    if (!outputChannel) {
        outputChannel = createOutputChannel();
    }
    return outputChannel;
}

function isAbort(err: unknown): boolean {
    if (typeof err !== "object" || err === null) {
        return false;
    }
    const name = (err as { name?: unknown }).name;
    return name === "AbortError";
}

function showError(action: string, err: unknown): void {
    let message = `meta-agent: ${action} failed`;
    if (err instanceof MetaAgentApiError) {
        message = `meta-agent: ${action} → HTTP ${err.status}`;
        if (err.detail) {
            message += `: ${err.detail}`;
        }
    } else if (err instanceof Error) {
        message = `meta-agent: ${action} → ${err.message}`;
    } else {
        message = `meta-agent: ${action} → ${String(err)}`;
    }
    vscode.window.showErrorMessage(message);
    outputChannel?.appendLine(message);
}
