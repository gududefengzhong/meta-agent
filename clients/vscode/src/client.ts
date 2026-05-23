// HTTP + SSE client for the meta-agent task API.
//
// Mirrors the Python `meta_agent.cli.client` surface so both clients
// share one wire contract:
//
//   submitTask                -> POST /v1/tasks
//   streamLlmChunks           -> GET  /v1/tasks/{id}/llm-stream  (SSE)
//   streamEvents              -> GET  /v1/tasks/{id}/events       (SSE)
//   streamPermissionPrompts   -> GET  /v1/tasks/{id}/permissions/stream (SSE)
//   decidePermission          -> POST /v1/tasks/{id}/permissions/{prompt_id}/decide
//
// SSE parsing is intentionally minimal: we read the response body as
// a stream, decode UTF-8, split on blank-line boundaries, and emit
// the JSON payload after every `data:` line. Non-data lines
// (heartbeats, `event:` framing) are skipped.
//
// Cancellation: each streaming call accepts an `AbortSignal` so the
// caller can cut the connection when the user closes the panel or
// the task reaches a terminal state.

export type PermissionMode =
    | "auto"
    | "approve_before_push"
    | "approve_each_tool"
    | "plan";

export interface SubmitTaskOptions {
    taskType: string;
    inputPayload: Record<string, unknown>;
    permissionMode?: PermissionMode;
    sessionId?: string;
    idempotencyKey?: string;
}

export interface TaskResponse {
    task_id: string;
    tenant_id: string;
    state: string;
    task_type: string;
    trace_id: string;
    session_id: string | null;
    permission_mode: string;
    budget_policy: string;
    created_at: string;
    updated_at: string;
}

export interface LlmStreamChunk {
    content_delta?: string;
    finish_reason?: string | null;
    [k: string]: unknown;
}

export interface LifecycleEvent {
    event_id?: string;
    action?: string;
    state?: string;
    [k: string]: unknown;
}

export interface PermissionPrompt {
    prompt_id: string;
    tenant_id: string;
    task_id: string;
    tool_name: string;
    summary: string;
    payload: Record<string, unknown>;
    created_at: string;
}

export class MetaAgentApiError extends Error {
    constructor(
        public readonly status: number,
        message: string,
        public readonly detail?: string,
    ) {
        super(message);
        this.name = "MetaAgentApiError";
    }
}

export class MetaAgentClient {
    constructor(
        private readonly apiUrl: string,
        private readonly token: string,
    ) {
        if (!token) {
            throw new Error("MetaAgentClient: bearer token is required");
        }
    }

    private headers(extra: Record<string, string> = {}): Record<string, string> {
        return {
            Authorization: `Bearer ${this.token}`,
            "Content-Type": "application/json",
            ...extra,
        };
    }

    private url(path: string): string {
        return `${this.apiUrl.replace(/\/$/, "")}${path}`;
    }

    async submitTask(opts: SubmitTaskOptions): Promise<TaskResponse> {
        const body: Record<string, unknown> = {
            task_type: opts.taskType,
            input_payload: opts.inputPayload,
        };
        if (opts.permissionMode) {
            body.permission_mode = opts.permissionMode;
        }
        if (opts.sessionId) {
            body.session_id = opts.sessionId;
        }
        if (opts.idempotencyKey) {
            body.idempotency_key = opts.idempotencyKey;
        }
        const response = await fetch(this.url("/v1/tasks"), {
            method: "POST",
            headers: this.headers(),
            body: JSON.stringify(body),
        });
        if (!response.ok) {
            throw await apiErrorFrom(response);
        }
        return (await response.json()) as TaskResponse;
    }

    async decidePermission(
        taskId: string,
        promptId: string,
        allow: boolean,
        reason?: string,
    ): Promise<void> {
        const body: Record<string, unknown> = { allow };
        if (reason) {
            body.reason = reason;
        }
        const response = await fetch(
            this.url(
                `/v1/tasks/${encodeURIComponent(taskId)}/permissions/${encodeURIComponent(
                    promptId,
                )}/decide`,
            ),
            {
                method: "POST",
                headers: this.headers(),
                body: JSON.stringify(body),
            },
        );
        if (!response.ok) {
            throw await apiErrorFrom(response);
        }
    }

    streamLlmChunks(
        taskId: string,
        signal: AbortSignal,
    ): AsyncGenerator<LlmStreamChunk, void, undefined> {
        return this.iterSse<LlmStreamChunk>(
            `/v1/tasks/${encodeURIComponent(taskId)}/llm-stream`,
            signal,
        );
    }

    streamEvents(
        taskId: string,
        signal: AbortSignal,
    ): AsyncGenerator<LifecycleEvent, void, undefined> {
        return this.iterSse<LifecycleEvent>(
            `/v1/tasks/${encodeURIComponent(taskId)}/events`,
            signal,
        );
    }

    streamPermissionPrompts(
        taskId: string,
        signal: AbortSignal,
    ): AsyncGenerator<PermissionPrompt, void, undefined> {
        return this.iterSse<PermissionPrompt>(
            `/v1/tasks/${encodeURIComponent(taskId)}/permissions/stream`,
            signal,
        );
    }

    private async *iterSse<T>(
        path: string,
        signal: AbortSignal,
    ): AsyncGenerator<T, void, undefined> {
        const response = await fetch(this.url(path), {
            method: "GET",
            headers: { Authorization: `Bearer ${this.token}` },
            signal,
        });
        if (!response.ok) {
            throw await apiErrorFrom(response);
        }
        if (!response.body) {
            return;
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) {
                    break;
                }
                buffer += decoder.decode(value, { stream: true });
                // SSE event boundary is a blank line ("\n\n"). Drain
                // every complete event currently in the buffer.
                let sepIdx = buffer.indexOf("\n\n");
                while (sepIdx !== -1) {
                    const eventText = buffer.slice(0, sepIdx);
                    buffer = buffer.slice(sepIdx + 2);
                    const payload = extractDataPayload(eventText);
                    if (payload !== null) {
                        const parsed = tryParseJson<T>(payload);
                        if (parsed !== null) {
                            yield parsed;
                        }
                    }
                    sepIdx = buffer.indexOf("\n\n");
                }
            }
        } finally {
            try {
                reader.releaseLock();
            } catch {
                /* already released */
            }
        }
    }
}

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled", "expired"]);

export function isTerminalState(state: string | undefined | null): boolean {
    return typeof state === "string" && TERMINAL_STATES.has(state);
}

function extractDataPayload(eventText: string): string | null {
    // An SSE event may contain multiple lines; we only care about
    // `data:` lines and concatenate them per the spec. `[DONE]` is
    // the streaming sentinel some providers emit; treat it as the
    // end-of-stream signal (no payload to yield).
    const lines = eventText.split("\n");
    const dataLines: string[] = [];
    for (const line of lines) {
        if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trimStart());
        }
    }
    if (dataLines.length === 0) {
        return null;
    }
    const joined = dataLines.join("\n");
    if (joined === "[DONE]") {
        return null;
    }
    return joined;
}

function tryParseJson<T>(payload: string): T | null {
    try {
        return JSON.parse(payload) as T;
    } catch {
        return null;
    }
}

async function apiErrorFrom(response: Response): Promise<MetaAgentApiError> {
    let detail: string | undefined;
    try {
        const body = await response.json();
        if (body && typeof body === "object" && "detail" in body) {
            detail = String((body as { detail: unknown }).detail);
        }
    } catch {
        try {
            const text = await response.text();
            if (text) {
                detail = text.slice(0, 200);
            }
        } catch {
            /* nothing else to surface */
        }
    }
    return new MetaAgentApiError(
        response.status,
        `meta-agent API HTTP ${response.status}${detail ? `: ${detail}` : ""}`,
        detail,
    );
}
