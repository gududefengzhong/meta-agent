// VS Code settings reader for the meta-agent extension.
//
// All values come from the `metaAgent.*` configuration namespace
// declared in `package.json`. The reader is intentionally pure so
// tests (when we add them) can pass a fake VS Code workspace.

import * as vscode from "vscode";

import type { PermissionMode } from "./client";

export interface MetaAgentConfig {
    apiUrl: string;
    token: string;
    permissionMode: PermissionMode;
}

export function readConfig(): MetaAgentConfig {
    const cfg = vscode.workspace.getConfiguration("metaAgent");
    return {
        apiUrl: cfg.get<string>("apiUrl", "http://localhost:8000"),
        token: cfg.get<string>("token", ""),
        permissionMode: normalizePermissionMode(
            cfg.get<string>("permissionMode", "plan"),
        ),
    };
}

function normalizePermissionMode(raw: string): PermissionMode {
    switch (raw) {
        case "auto":
        case "approve_before_push":
        case "approve_each_tool":
        case "plan":
            return raw;
        default:
            return "plan";
    }
}
