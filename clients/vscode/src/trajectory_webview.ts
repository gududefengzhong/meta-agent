// Webview that renders a task's live trajectory: the user's
// prompt, assistant streaming text, lifecycle events, and
// permission prompts (with their decisions) interleaved on a
// single vertical timeline.
//
// The raw output channel still gets every byte for debugging, but
// the trajectory view is what users actually read — the channel
// flattens four streams into one column of text with no grouping;
// the webview gives each entry an icon, a timestamp, and a class
// so theming + structure carry the meaning.
//
// Lifecycle:
//   * One panel per ``activate``; ``attach(taskId, prompt)`` opens
//     it for the new task and resets the timeline.
//   * The extension feeds events via ``pushChunk`` / ``pushEvent``
//     / ``pushPrompt`` / ``pushDecision`` — each posts a message
//     to the webview JS, which appends a DOM entry.
//   * Closing the panel does not abort the task watcher; the
//     output channel keeps running.
//
// We deliberately let the webview-side JS own the small amount of
// state needed to merge consecutive assistant chunks into a single
// turn entry. Posting per-chunk messages keeps the extension side
// stateless and means cancellation / reopens don't lose tokens.

import * as vscode from "vscode";

import type { LifecycleEvent, LlmStreamChunk, PermissionPrompt } from "./client";

export class TrajectoryPanel {
    private panel: vscode.WebviewPanel | undefined;
    private currentTaskId: string | undefined;

    /** Open or refocus the panel for a new task and reset its body. */
    attach(taskId: string, userPrompt: string | null): void {
        const panel = this.ensurePanel();
        this.currentTaskId = taskId;
        panel.title = `meta-agent: ${taskId.slice(0, 8)}`;
        panel.reveal(vscode.ViewColumn.Beside, true);
        panel.webview.postMessage({
            type: "init",
            taskId,
            userPrompt,
            timestamp: Date.now(),
        });
    }

    pushChunk(taskId: string, chunk: LlmStreamChunk): void {
        if (!this.matches(taskId)) {
            return;
        }
        this.panel?.webview.postMessage({
            type: "chunk",
            contentDelta: typeof chunk.content_delta === "string" ? chunk.content_delta : "",
            finishReason: chunk.finish_reason ?? null,
            timestamp: Date.now(),
        });
    }

    pushEvent(taskId: string, event: LifecycleEvent): void {
        if (!this.matches(taskId)) {
            return;
        }
        this.panel?.webview.postMessage({
            type: "event",
            action: typeof event.action === "string" ? event.action : "(event)",
            state: typeof event.state === "string" ? event.state : null,
            timestamp: Date.now(),
        });
    }

    pushPrompt(taskId: string, prompt: PermissionPrompt): void {
        if (!this.matches(taskId)) {
            return;
        }
        this.panel?.webview.postMessage({
            type: "prompt",
            promptId: prompt.prompt_id,
            toolName: prompt.tool_name,
            summary: prompt.summary,
            timestamp: Date.now(),
        });
    }

    pushDecision(
        taskId: string,
        promptId: string,
        allow: boolean,
        reason: string | null,
    ): void {
        if (!this.matches(taskId)) {
            return;
        }
        this.panel?.webview.postMessage({
            type: "decision",
            promptId,
            allow,
            reason,
            timestamp: Date.now(),
        });
    }

    dispose(): void {
        this.panel?.dispose();
        this.panel = undefined;
        this.currentTaskId = undefined;
    }

    private matches(taskId: string): boolean {
        return this.currentTaskId === taskId && this.panel !== undefined;
    }

    private ensurePanel(): vscode.WebviewPanel {
        if (this.panel) {
            return this.panel;
        }
        const panel = vscode.window.createWebviewPanel(
            "metaAgent.trajectory",
            "meta-agent: trajectory",
            { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
            { enableScripts: true, retainContextWhenHidden: true },
        );
        panel.onDidDispose(() => {
            this.panel = undefined;
            this.currentTaskId = undefined;
        });
        panel.webview.html = renderHtml(panel.webview);
        this.panel = panel;
        return panel;
    }
}

function renderHtml(webview: vscode.Webview): string {
    const nonce = generateNonce();
    const csp = [
        `default-src 'none'`,
        `style-src ${webview.cspSource} 'unsafe-inline'`,
        `script-src 'nonce-${nonce}'`,
    ].join("; ");
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta http-equiv="Content-Security-Policy" content="${csp}" />
<title>meta-agent: trajectory</title>
<style>${STYLES}</style>
</head>
<body>
<header>
  <span class="title">meta-agent</span>
  <span id="task-id" class="task-id">(no task)</span>
</header>
<main id="timeline" aria-live="polite"></main>
<script nonce="${nonce}">${CLIENT_SCRIPT}</script>
</body>
</html>`;
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
header {
  position: sticky;
  top: 0;
  display: flex;
  gap: 12px;
  align-items: baseline;
  padding: 10px 16px;
  background: var(--vscode-editor-background);
  border-bottom: 1px solid var(--vscode-panel-border);
  z-index: 1;
}
header .title { font-weight: 600; color: var(--vscode-textLink-foreground); }
header .task-id {
  font-family: var(--vscode-editor-font-family, monospace);
  color: var(--vscode-descriptionForeground);
  font-size: 0.9em;
}
main { padding: 12px 16px 32px; }
.entry {
  display: grid;
  grid-template-columns: 14px 1fr;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px solid var(--vscode-panel-border);
}
.entry .marker {
  margin-top: 4px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--vscode-descriptionForeground);
}
.entry.user .marker { background: var(--vscode-textLink-foreground); }
.entry.assistant .marker { background: var(--vscode-charts-green, #5e9c5e); }
.entry.event .marker { background: var(--vscode-charts-gray, #8a8a8a); }
.entry.prompt .marker { background: var(--vscode-charts-orange, #d18616); }
.entry.prompt.allow .marker { background: var(--vscode-charts-green, #5e9c5e); }
.entry.prompt.deny .marker { background: var(--vscode-errorForeground, #c72c2c); }
.entry .body { min-width: 0; }
.entry .meta {
  display: flex;
  gap: 8px;
  font-size: 0.85em;
  color: var(--vscode-descriptionForeground);
  margin-bottom: 4px;
}
.entry .kind { text-transform: uppercase; letter-spacing: 0.04em; }
.entry .content {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: var(--vscode-editor-font-size, inherit);
}
.entry.event .content {
  color: var(--vscode-descriptionForeground);
  font-style: italic;
}
.entry .decision {
  margin-top: 6px;
  padding: 4px 8px;
  border-left: 3px solid var(--vscode-panel-border);
  font-size: 0.9em;
}
.entry .decision.allow { border-color: var(--vscode-charts-green, #5e9c5e); }
.entry .decision.deny { border-color: var(--vscode-errorForeground, #c72c2c); }
.entry .decision .reason {
  color: var(--vscode-descriptionForeground);
  margin-left: 6px;
}
`;

// The client script keeps its own minimal state machine: append
// chunks into the trailing assistant entry until ``finishReason``
// closes it; everything else creates a fresh entry. Permission
// prompts get tagged with ``data-prompt-id`` so the matching
// ``decision`` message can patch the same entry.
const CLIENT_SCRIPT = `
(function () {
  const timeline = document.getElementById('timeline');
  const taskIdEl = document.getElementById('task-id');
  let openAssistant = null;

  function fmtTime(ts) {
    const d = new Date(ts);
    return d.toLocaleTimeString();
  }

  function makeEntry(kind, time) {
    const entry = document.createElement('div');
    entry.className = 'entry ' + kind;
    entry.innerHTML = ''
      + '<div class="marker"></div>'
      + '<div class="body">'
      + '  <div class="meta"><span class="kind"></span><span class="time"></span></div>'
      + '  <div class="content"></div>'
      + '</div>';
    entry.querySelector('.kind').textContent = kind;
    entry.querySelector('.time').textContent = fmtTime(time);
    timeline.appendChild(entry);
    return entry;
  }

  function appendText(entry, text) {
    entry.querySelector('.content').textContent += text;
  }

  function scrollToBottom() {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'auto' });
  }

  function reset() {
    timeline.innerHTML = '';
    openAssistant = null;
  }

  window.addEventListener('message', function (ev) {
    const msg = ev.data;
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'init') {
      reset();
      taskIdEl.textContent = msg.taskId || '';
      if (msg.userPrompt) {
        const entry = makeEntry('user', msg.timestamp);
        appendText(entry, msg.userPrompt);
      }
      scrollToBottom();
      return;
    }
    if (msg.type === 'chunk') {
      if (msg.contentDelta) {
        if (!openAssistant) {
          openAssistant = makeEntry('assistant', msg.timestamp);
        }
        appendText(openAssistant, msg.contentDelta);
      }
      if (msg.finishReason) {
        openAssistant = null;
      }
      scrollToBottom();
      return;
    }
    if (msg.type === 'event') {
      const entry = makeEntry('event', msg.timestamp);
      const text = msg.action + (msg.state ? ' (state=' + msg.state + ')' : '');
      appendText(entry, text);
      // A graph node boundary or tool boundary closes any running
      // assistant entry so subsequent tokens belong to a new turn.
      openAssistant = null;
      scrollToBottom();
      return;
    }
    if (msg.type === 'prompt') {
      const entry = makeEntry('prompt', msg.timestamp);
      entry.setAttribute('data-prompt-id', msg.promptId);
      const label = msg.toolName + (msg.summary ? ' — ' + msg.summary : '');
      appendText(entry, label);
      openAssistant = null;
      scrollToBottom();
      return;
    }
    if (msg.type === 'decision') {
      const entry = timeline.querySelector('[data-prompt-id="' + msg.promptId + '"]');
      if (entry) {
        entry.classList.add(msg.allow ? 'allow' : 'deny');
        const note = document.createElement('div');
        note.className = 'decision ' + (msg.allow ? 'allow' : 'deny');
        note.textContent = msg.allow ? 'Allowed' : 'Denied';
        if (msg.reason) {
          const reason = document.createElement('span');
          reason.className = 'reason';
          reason.textContent = '— ' + msg.reason;
          note.appendChild(reason);
        }
        entry.querySelector('.body').appendChild(note);
      }
      scrollToBottom();
      return;
    }
  });
})();
`;
