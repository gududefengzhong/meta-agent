// Line-level diff helper for the edit-review webview.
//
// Uses the classic LCS (longest common subsequence) algorithm. We
// deliberately don't pull in `diff` from npm — it's a 30KB
// dependency to compute line-level diffs the agent's patches
// rarely exceed a few hundred lines. The implementation here is
// O(n*m) in time + space, which is fine for that scale.
//
// The output is a list of side-by-side rows: each row aligns one
// (possibly empty) old line with one (possibly empty) new line and
// tags whether the row is unchanged, inserted, deleted, or both
// (when LCS produces an old/new pair that differ).
//
// We don't try to implement Myers' diff or hunk-merging — the
// webview wants the full file rendered, not a unified hunk view,
// so the simple LCS walk is enough.

export type DiffRowKind = "unchanged" | "inserted" | "deleted" | "modified";

export interface DiffRow {
    kind: DiffRowKind;
    oldLineNumber: number | null;
    newLineNumber: number | null;
    oldText: string;
    newText: string;
}

export function computeDiff(oldText: string, newText: string): DiffRow[] {
    const oldLines = splitLines(oldText);
    const newLines = splitLines(newText);
    const lcs = longestCommonSubsequence(oldLines, newLines);
    return walkLcs(oldLines, newLines, lcs);
}

function splitLines(text: string): string[] {
    // Preserve empty trailing lines: ``"a\n"`` → ``["a", ""]`` so the
    // diff carries the trailing newline through correctly. Strip the
    // synthetic trailing empty if the input is the empty string —
    // otherwise an empty file looks like a 1-line file with an empty
    // first line.
    if (text === "") {
        return [];
    }
    const parts = text.split("\n");
    // ``split`` on a string ending in "\n" gives a trailing "". Keep
    // it — the diff distinguishes "file ends with newline" from "file
    // does not."
    return parts;
}

interface LcsMatrix {
    rows: number;
    cols: number;
    data: Uint32Array;
}

function longestCommonSubsequence(a: string[], b: string[]): LcsMatrix {
    const rows = a.length + 1;
    const cols = b.length + 1;
    const data = new Uint32Array(rows * cols);
    for (let i = 1; i < rows; i++) {
        for (let j = 1; j < cols; j++) {
            if (a[i - 1] === b[j - 1]) {
                data[i * cols + j] = data[(i - 1) * cols + (j - 1)] + 1;
            } else {
                const up = data[(i - 1) * cols + j];
                const left = data[i * cols + (j - 1)];
                data[i * cols + j] = up >= left ? up : left;
            }
        }
    }
    return { rows, cols, data };
}

function walkLcs(
    oldLines: string[],
    newLines: string[],
    lcs: LcsMatrix,
): DiffRow[] {
    // Walk backwards through the LCS matrix, prepending rows so the
    // final list is in source order. We aggregate adjacent
    // delete+insert pairs into a single ``modified`` row so the
    // side-by-side view aligns them on the same line — that matches
    // what users expect from a code diff (an edit looks like one
    // line, not two).
    const out: DiffRow[] = [];
    let i = oldLines.length;
    let j = newLines.length;
    const cols = lcs.cols;
    while (i > 0 || j > 0) {
        const here = i * cols + j;
        if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
            out.unshift({
                kind: "unchanged",
                oldLineNumber: i,
                newLineNumber: j,
                oldText: oldLines[i - 1],
                newText: newLines[j - 1],
            });
            i--;
            j--;
            continue;
        }
        const up = i > 0 ? lcs.data[here - cols] : -1;
        const left = j > 0 ? lcs.data[here - 1] : -1;
        if (j > 0 && (i === 0 || left >= up)) {
            // Insertion: consumed a `new` line that has no match.
            // Try to fold with an immediately-previous deletion to
            // form a ``modified`` row.
            if (out.length > 0 && out[0].kind === "deleted" && out[0].newLineNumber === null) {
                out[0] = {
                    kind: "modified",
                    oldLineNumber: out[0].oldLineNumber,
                    newLineNumber: j,
                    oldText: out[0].oldText,
                    newText: newLines[j - 1],
                };
            } else {
                out.unshift({
                    kind: "inserted",
                    oldLineNumber: null,
                    newLineNumber: j,
                    oldText: "",
                    newText: newLines[j - 1],
                });
            }
            j--;
        } else if (i > 0) {
            // Deletion: consumed an `old` line that has no match.
            if (out.length > 0 && out[0].kind === "inserted" && out[0].oldLineNumber === null) {
                out[0] = {
                    kind: "modified",
                    oldLineNumber: i,
                    newLineNumber: out[0].newLineNumber,
                    oldText: oldLines[i - 1],
                    newText: out[0].newText,
                };
            } else {
                out.unshift({
                    kind: "deleted",
                    oldLineNumber: i,
                    newLineNumber: null,
                    oldText: oldLines[i - 1],
                    newText: "",
                });
            }
            i--;
        } else {
            // Should be unreachable; both pointers at 0 ends the loop.
            break;
        }
    }
    return out;
}

export function summarizeDiff(rows: DiffRow[]): {
    inserted: number;
    deleted: number;
    modified: number;
    unchanged: number;
} {
    let inserted = 0;
    let deleted = 0;
    let modified = 0;
    let unchanged = 0;
    for (const row of rows) {
        switch (row.kind) {
            case "inserted":
                inserted++;
                break;
            case "deleted":
                deleted++;
                break;
            case "modified":
                modified++;
                break;
            case "unchanged":
                unchanged++;
                break;
        }
    }
    return { inserted, deleted, modified, unchanged };
}
