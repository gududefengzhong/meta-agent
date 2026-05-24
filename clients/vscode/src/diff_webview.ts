// Webview panel for reviewing the agent's proposed file edits.
//
// Edit-shaped tool prompts (``edit_write``, ``edit_patch_apply``)
// carry a payload large enough that the native modal would truncate
// or scroll badly — and the user usually wants to diff the change
// against the current file, which a modal can't render. This module
// owns a single, reusable webview panel that renders a side-by-side
// diff for ``edit_write`` (or the raw patch text for
// ``edit_patch_apply``) and posts the Allow / Deny decision back
// through ``postMessage``.
//
// The panel is reused across prompts to avoid spawning a fresh tab
// per edit on a long task. We never run two prompts in parallel —
// the caller awaits each ``show`` before issuing the next one — so a
// single ``pending`` slot is enough.
//
// HTML is generated inline; we don't pull in a framework. Theming
// uses VS Code's CSS variables so the diff blends with the
// surrounding editor regardless of light/dark theme.

import * as vscode from "vscode";

import type { PermissionPrompt } from "./client";
import { computeDiff, summarizeDiff, type DiffRow } from "./diff";

const EDIT_TOOL_NAMES = new Set(["edit_write", "edit_patch_apply"]);

export function isEditTool(toolName: string): boolean {
    return EDIT_TOOL_NAMES.has(toolName);
}

export interface ReviewDecision {
    allow: boolean;
    reason?: string;
}

type Resolver = (decision: ReviewDecision) => void;

export class EditReviewPanel {
    private panel: vscode.WebviewPanel | undefined;
    private pending: Resolver | undefined;

    async show(prompt: PermissionPrompt): Promise<ReviewDecision> {
        const view = await buildEditView(prompt);
        const panel = this.ensurePanel();
        panel.title = `meta-agent: ${prompt.tool_name}`;
        panel.webview.html = renderHtml(panel.webview, view);
        panel.reveal(vscode.ViewColumn.Beside, true);

        // Resolve any prompt that's still waiting — concurrent shows
        // shouldn't happen (the prompt loop awaits each decide), but
        // if they do, the prior one being superseded should not hang.
        if (this.pending) {
            this.pending({ allow: false, reason: "Superseded by another prompt." });
            this.pending = undefined;
        }
        return new Promise<ReviewDecision>((resolve) => {
            this.pending = resolve;
        });
    }

    dispose(): void {
        if (this.pending) {
            this.pending({ allow: false, reason: "Extension deactivated." });
            this.pending = undefined;
        }
        this.panel?.dispose();
        this.panel = undefined;
    }

    private ensurePanel(): vscode.WebviewPanel {
        if (this.panel) {
            return this.panel;
        }
        const panel = vscode.window.createWebviewPanel(
            "metaAgent.editReview",
            "meta-agent: review edit",
            { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
            { enableScripts: true, retainContextWhenHidden: true },
        );
        panel.onDidDispose(() => {
            this.panel = undefined;
            if (this.pending) {
                // Closing the panel without clicking Allow/Deny is a
                // deny by default — leaving the agent hanging would
                // freeze the task indefinitely.
                this.pending({ allow: false, reason: "Review panel closed without a decision." });
                this.pending = undefined;
            }
        });
        panel.webview.onDidReceiveMessage((message: unknown) => {
            const decision = parseDecisionMessage(message);
            if (!decision) {
                return;
            }
            if (this.pending) {
                this.pending(decision);
                this.pending = undefined;
            }
        });
        this.panel = panel;
        return panel;
    }
}

interface EditView {
    toolName: string;
    summary: string;
    filePath: string | null;
    diff: DiffRow[] | null;
    patchText: string | null;
    baselineSource: "workspace" | "empty" | "unavailable";
    payloadJson: string;
}

async function buildEditView(prompt: PermissionPrompt): Promise<EditView> {
    const payload = (prompt.payload ?? {}) as Record<string, unknown>;
    const filePath = typeof payload.path === "string" ? payload.path : null;
    const newContent = typeof payload.content === "string" ? payload.content : null;
    const patchText = typeof payload.patch === "string" ? payload.patch : null;

    let diff: DiffRow[] | null = null;
    let baselineSource: EditView["baselineSource"] = "unavailable";
    if (prompt.tool_name === "edit_write" && newContent !== null) {
        const workspaceContent = await readWorkspaceFile(filePath);
        if (workspaceContent !== null) {
            baselineSource = "workspace";
            diff = computeDiff(workspaceContent, newContent);
        } else {
            baselineSource = "empty";
            diff = computeDiff("", newContent);
        }
    }
    return {
        toolName: prompt.tool_name,
        summary: prompt.summary || `Approve ${prompt.tool_name}?`,
        filePath,
        diff,
        patchText,
        baselineSource,
        payloadJson: safeJson(payload),
    };
}

async function readWorkspaceFile(relPath: string | null): Promise<string | null> {
    if (!relPath) {
        return null;
    }
    const folders = vscode.workspace.workspaceFolders;
    if (!folders || folders.length === 0) {
        return null;
    }
    const uri = vscode.Uri.joinPath(folders[0].uri, relPath);
    try {
        const bytes = await vscode.workspace.fs.readFile(uri);
        return new TextDecoder("utf-8").decode(bytes);
    } catch {
        return null;
    }
}

function parseDecisionMessage(message: unknown): ReviewDecision | null {
    if (!message || typeof message !== "object") {
        return null;
    }
    const m = message as { type?: unknown; allow?: unknown; reason?: unknown };
    if (m.type !== "decide") {
        return null;
    }
    const allow = m.allow === true;
    let reason: string | undefined;
    if (typeof m.reason === "string") {
        const trimmed = m.reason.trim();
        if (trimmed) {
            reason = trimmed;
        }
    }
    return { allow, reason };
}

function safeJson(value: unknown): string {
    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}

function renderHtml(webview: vscode.Webview, view: EditView): string {
    const nonce = generateNonce();
    const csp = [
        `default-src 'none'`,
        `style-src ${webview.cspSource} 'unsafe-inline'`,
        `script-src 'nonce-${nonce}'`,
    ].join("; ");

    const body = view.diff
        ? renderDiffBody(view)
        : view.patchText !== null
            ? renderPatchBody(view)
            : renderFallbackBody(view);
    const headerHtml = renderHeader(view);

    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta http-equiv="Content-Security-Policy" content="${csp}" />
<title>meta-agent: review edit</title>
<style>${STYLES}</style>
</head>
<body>
<div class="container">
  ${headerHtml}
  ${body}
  <div class="actions">
    <label class="reason-label">
      <span>Reason (optional, recorded with the decision)</span>
      <textarea id="reason" rows="2" placeholder="e.g. wrong file, prefer different approach"></textarea>
    </label>
    <div class="buttons">
      <button id="allow" class="primary">Allow</button>
      <button id="deny" class="danger">Deny</button>
    </div>
  </div>
</div>
<script nonce="${nonce}">${CLIENT_SCRIPT}</script>
</body>
</html>`;
}

function renderHeader(view: EditView): string {
    const target = view.filePath ? escapeHtml(view.filePath) : "(no path)";
    const baselineNote =
        view.baselineSource === "empty"
            ? `<div class="note">No file at <code>${target}</code> in the current workspace — diff shown against an empty baseline.</div>`
            : view.baselineSource === "unavailable" && view.diff !== null
                ? `<div class="note">No workspace folder open — baseline unavailable.</div>`
                : "";
    const stats = view.diff ? summarizeDiff(view.diff) : null;
    const statsHtml = stats
        ? `<div class="stats">
              <span class="stat-ins">+${stats.inserted + stats.modified}</span>
              <span class="stat-del">-${stats.deleted + stats.modified}</span>
              <span class="stat-unchanged">${stats.unchanged} unchanged</span>
           </div>`
        : "";
    return `
<header>
  <div class="title">
    <span class="tool-name">${escapeHtml(view.toolName)}</span>
    <span class="file-path">${target}</span>
  </div>
  <div class="summary">${escapeHtml(view.summary)}</div>
  ${baselineNote}
  ${statsHtml}
</header>`;
}

function renderDiffBody(view: EditView): string {
    const rows = view.diff ?? [];
    const rowHtml = rows
        .map((row) => {
            const oldNum = row.oldLineNumber === null ? "" : String(row.oldLineNumber);
            const newNum = row.newLineNumber === null ? "" : String(row.newLineNumber);
            const oldText = escapeHtml(row.oldText);
            const newText = escapeHtml(row.newText);
            return `<tr class="row-${row.kind}">
                <td class="ln">${oldNum}</td>
                <td class="code">${oldText || "&nbsp;"}</td>
                <td class="ln">${newNum}</td>
                <td class="code">${newText || "&nbsp;"}</td>
              </tr>`;
        })
        .join("");
    return `<table class="diff-table">
  <thead>
    <tr>
      <th colspan="2">Before</th>
      <th colspan="2">After</th>
    </tr>
  </thead>
  <tbody>${rowHtml}</tbody>
</table>`;
}

function renderPatchBody(view: EditView): string {
    return `<section class="patch">
  <h3>Proposed patch</h3>
  <pre class="patch-text">${escapeHtml(view.patchText ?? "")}</pre>
</section>`;
}

function renderFallbackBody(view: EditView): string {
    return `<section class="patch">
  <h3>Payload</h3>
  <pre class="patch-text">${escapeHtml(view.payloadJson)}</pre>
</section>`;
}

function escapeHtml(value: string): string {
    return value
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function generateNonce(): string {
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let out = "";
    for (let i = 0; i < 32; i++) {
        out += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return out;
}

const STYLES = `
body {
  font-family: var(--vscode-font-family);
  font-size: var(--vscode-font-size);
  color: var(--vscode-foreground);
  background: var(--vscode-editor-background);
  margin: 0;
  padding: 0;
}
.container {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}
header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--vscode-panel-border);
}
.title { display: flex; gap: 12px; align-items: baseline; }
.tool-name {
  font-weight: 600;
  color: var(--vscode-textLink-foreground);
}
.file-path {
  font-family: var(--vscode-editor-font-family, monospace);
  color: var(--vscode-descriptionForeground);
}
.summary { margin-top: 6px; }
.note {
  margin-top: 6px;
  padding: 6px 8px;
  background: var(--vscode-inputValidation-warningBackground, rgba(255,200,0,0.1));
  border-left: 3px solid var(--vscode-inputValidation-warningBorder, #c8a900);
  font-size: 0.9em;
}
.stats {
  margin-top: 8px;
  display: flex;
  gap: 12px;
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: 0.9em;
}
.stat-ins { color: var(--vscode-gitDecoration-addedResourceForeground, #587c0c); }
.stat-del { color: var(--vscode-gitDecoration-deletedResourceForeground, #ad0707); }
.stat-unchanged { color: var(--vscode-descriptionForeground); }

.diff-table {
  width: 100%;
  border-collapse: collapse;
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: var(--vscode-editor-font-size, 12px);
  table-layout: fixed;
}
.diff-table th {
  background: var(--vscode-editor-inactiveSelectionBackground);
  text-align: left;
  padding: 4px 8px;
  border-bottom: 1px solid var(--vscode-panel-border);
}
.diff-table td {
  padding: 1px 6px;
  white-space: pre-wrap;
  word-break: break-word;
  vertical-align: top;
}
.diff-table td.ln {
  width: 3em;
  text-align: right;
  color: var(--vscode-editorLineNumber-foreground);
  user-select: none;
  border-right: 1px solid var(--vscode-panel-border);
}
.row-unchanged td.code {
  background: transparent;
}
.row-inserted td:nth-child(3),
.row-inserted td:nth-child(4),
.row-modified td:nth-child(3),
.row-modified td:nth-child(4) {
  background: var(--vscode-diffEditor-insertedTextBackground, rgba(155,185,85,0.2));
}
.row-deleted td:nth-child(1),
.row-deleted td:nth-child(2),
.row-modified td:nth-child(1),
.row-modified td:nth-child(2) {
  background: var(--vscode-diffEditor-removedTextBackground, rgba(255,0,0,0.2));
}

.patch {
  padding: 12px 16px;
}
.patch-text {
  background: var(--vscode-textCodeBlock-background, rgba(0,0,0,0.1));
  padding: 12px;
  border-radius: 4px;
  overflow-x: auto;
  white-space: pre;
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: var(--vscode-editor-font-size, 12px);
}

.actions {
  position: sticky;
  bottom: 0;
  padding: 12px 16px;
  background: var(--vscode-editor-background);
  border-top: 1px solid var(--vscode-panel-border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.reason-label { display: flex; flex-direction: column; gap: 4px; }
.reason-label span { font-size: 0.9em; color: var(--vscode-descriptionForeground); }
.reason-label textarea {
  font-family: var(--vscode-font-family);
  background: var(--vscode-input-background);
  color: var(--vscode-input-foreground);
  border: 1px solid var(--vscode-input-border, transparent);
  padding: 6px;
  resize: vertical;
}
.buttons { display: flex; gap: 8px; justify-content: flex-end; }
button {
  font-family: var(--vscode-font-family);
  font-size: var(--vscode-font-size);
  padding: 6px 14px;
  border: none;
  cursor: pointer;
  border-radius: 2px;
}
button.primary {
  background: var(--vscode-button-background);
  color: var(--vscode-button-foreground);
}
button.primary:hover { background: var(--vscode-button-hoverBackground); }
button.danger {
  background: var(--vscode-errorForeground, #c72c2c);
  color: white;
}
button.danger:hover { opacity: 0.9; }
button:disabled { opacity: 0.5; cursor: default; }
`;

const CLIENT_SCRIPT = `
(function () {
  const vscode = acquireVsCodeApi();
  const allow = document.getElementById('allow');
  const deny = document.getElementById('deny');
  const reason = document.getElementById('reason');
  function send(allowed) {
    allow.disabled = true;
    deny.disabled = true;
    vscode.postMessage({
      type: 'decide',
      allow: allowed,
      reason: reason ? reason.value : '',
    });
  }
  allow.addEventListener('click', function () { send(true); });
  deny.addEventListener('click', function () { send(false); });
})();
`;
