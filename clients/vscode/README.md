# meta-agent VS Code extension (v0)

Runs meta-agent tasks from inside VS Code: submit a prompt, watch LLM
tokens stream into an output channel, decide on inline permission
prompts via native VS Code notifications.

## What this v0 does

- **`meta-agent: Run task`** — prompts for a natural-language task,
  submits it via `POST /v1/tasks`, and tails the LLM stream + lifecycle
  events into a dedicated output channel.
- **`meta-agent: Tail existing task`** — attach to an in-flight task by
  ID. Useful after a server-side worker resumes.
- **Inline permission prompts** — when a task is in `approve_each_tool`
  or `plan` mode, the agent's permission requests surface as VS Code
  notifications with Allow / Deny buttons. The user's decision routes
  back via `POST /v1/tasks/{id}/permissions/{prompt_id}/decide`.

What v0 deliberately does **not** do (later PRs):

- Webview-rendered diff review UI
- Workspace tree showing tasks / sessions
- Rich trajectory viewer (audit + checkpoints + LLM usage)
- Session resume — single-shot submission only
- Secret storage for the bearer token (today it lives in plain
  settings; do not use this on a shared workstation)

## Settings

| Setting | Default | Notes |
|---|---|---|
| `metaAgent.apiUrl` | `http://localhost:8000` | Base URL of the meta-agent API. |
| `metaAgent.token` | _(empty)_ | Bearer token. Required. |
| `metaAgent.permissionMode` | `plan` | One of `auto` / `approve_before_push` / `approve_each_tool` / `plan`. |

## Build

```bash
cd clients/vscode
npm install
npm run compile
```

`npm run lint` runs `tsc --noEmit` against the source.

For local development inside VS Code itself: open this directory as a
workspace and press F5 to launch an Extension Development Host.

## Architecture

```
src/
├── extension.ts   # entry point + command registration
├── client.ts      # HTTP + SSE client (fetch + ReadableStream)
├── config.ts      # settings reader
└── ui.ts          # output channel + permission prompt UI
```

The TypeScript client mirrors the Python `meta_agent.cli.client` so
both clients consume the same wire shape.
