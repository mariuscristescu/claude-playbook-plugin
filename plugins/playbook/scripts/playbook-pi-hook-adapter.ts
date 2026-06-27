import type {
  ExtensionAPI,
  ToolCallEvent,
  ToolResultEvent,
} from "@earendil-works/pi-coding-agent";
import { existsSync, readdirSync } from "node:fs";
import { dirname, isAbsolute, join } from "node:path";
import { spawnSync } from "node:child_process";

type HookResult = {
  status: number | null;
  stdout: string;
  stderr: string;
};

const TOOL_NAME_MAP: Record<string, string> = {
  bash: "Bash",
  read: "Read",
  edit: "Edit",
  write: "Write",
  grep: "Grep",
  find: "Glob",
  ls: "LS",
};

// Mirror gate-echo-lib.sh find_project_root: walk up looking for either the
// legacy `.agent/tasks/` marker OR a multi-user `.agent/<user>/tasks/` marker.
// CLAUDE.md/MIND_MAP.md alone are deliberately NOT sufficient.
function hasPlaybookMarker(dir: string): boolean {
  if (existsSync(join(dir, ".agent", "tasks"))) return true;
  const agentDir = join(dir, ".agent");
  if (!existsSync(agentDir)) return false;
  try {
    for (const entry of readdirSync(agentDir, { withFileTypes: true })) {
      if (entry.isDirectory() && existsSync(join(agentDir, entry.name, "tasks"))) {
        return true;
      }
    }
  } catch {
    // unreadable .agent — treat as no marker
  }
  return false;
}

function findProjectRoot(start: string): string | undefined {
  let dir = start;
  while (dir !== dirname(dir)) {
    if (hasPlaybookMarker(dir)) return dir;
    dir = dirname(dir);
  }
  return undefined;
}

function sessionId(): string {
  if (process.env.PLAYBOOK_SESSION_ID) return process.env.PLAYBOOK_SESSION_ID;
  return `pid-${process.pid}`;
}

function hookDir(projectRoot: string): string {
  return process.env.PLAYBOOK_HOOK_DIR || join(projectRoot, "scripts");
}

function absPath(path: unknown, cwd: string): unknown {
  if (typeof path !== "string" || path.length === 0) return path;
  return isAbsolute(path) ? path : join(cwd, path);
}

function normalizeToolInput(event: ToolCallEvent | ToolResultEvent, cwd: string): Record<string, unknown> {
  const input = { ...(event.type === "tool_call" ? event.input : event.input) };
  switch (event.toolName) {
    case "bash":
      return input;
    case "write":
      return {
        ...input,
        file_path: absPath(input.path ?? input.file_path, cwd),
      };
    case "edit": {
      const edits = Array.isArray(input.edits) ? input.edits : [];
      const firstEdit = edits[0] || {};
      return {
        ...input,
        file_path: absPath(input.path ?? input.file_path, cwd),
        old_string: input.old_string ?? firstEdit.oldText,
        new_string: input.new_string ?? firstEdit.newText,
      };
    }
    case "read":
    case "grep":
    case "find":
    case "ls":
      return {
        ...input,
        file_path: absPath(input.path ?? input.file_path, cwd),
      };
    default:
      return input;
  }
}

function hookPayload(
  hookEventName: string,
  cwd: string,
  extra: Record<string, unknown>,
): Record<string, unknown> {
  return {
    hook_event_name: hookEventName,
    session_id: sessionId(),
    cwd,
    ...extra,
  };
}

function runHook(projectRoot: string, scriptName: string, payload: Record<string, unknown>): HookResult {
  const scriptPath = join(hookDir(projectRoot), scriptName);
  const env = {
    ...process.env,
    PLAYBOOK_PROVIDER: "pi",
    PLAYBOOK_SESSION_ID: sessionId(),
    PLAYBOOK_PROJECT_ROOT: projectRoot,
  };
  const result = spawnSync(scriptPath, {
    cwd: projectRoot,
    env,
    input: JSON.stringify(payload),
    encoding: "utf8",
  });
  return {
    status: result.status,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
  };
}

function parseHookJson(stdout: string): any | undefined {
  const trimmed = stdout.trim();
  if (!trimmed.startsWith("{")) return undefined;
  try {
    return JSON.parse(trimmed);
  } catch {
    return undefined;
  }
}

function applyUpdatedInput(event: ToolCallEvent, stdout: string): void {
  const parsed = parseHookJson(stdout);
  const updated = parsed?.hookSpecificOutput?.updatedInput;
  if (!updated || typeof updated !== "object") return;
  Object.assign(event.input, updated);
}

function additionalContext(stdout: string): string | undefined {
  const parsed = parseHookJson(stdout);
  const value = parsed?.hookSpecificOutput?.additionalContext;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (_event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return;
    runHook(projectRoot, "session-start-hook", hookPayload("SessionStart", ctx.cwd, {}));
  });

  pi.on("input", async (event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return { action: "continue" };
    runHook(
      projectRoot,
      "chat-log-hook",
      hookPayload("UserPromptSubmit", ctx.cwd, { prompt: event.text }),
    );
    return { action: "continue" };
  });

  pi.on("tool_call", async (event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return;

    const result = runHook(
      projectRoot,
      "task-gate-hook",
      hookPayload("PreToolUse", ctx.cwd, {
        tool_name: TOOL_NAME_MAP[event.toolName] || event.toolName,
        tool_input: normalizeToolInput(event, ctx.cwd),
      }),
    );

    applyUpdatedInput(event, result.stdout);

    if (result.status === 2) {
      return {
        block: true,
        reason: result.stderr.trim() || "Blocked by Playbook task-gate-hook",
      };
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return;

    const result = runHook(
      projectRoot,
      "state-echo-hook",
      hookPayload("PostToolUse", ctx.cwd, {
        tool_name: TOOL_NAME_MAP[event.toolName] || event.toolName,
        tool_input: normalizeToolInput(event, ctx.cwd),
        tool_result: {
          is_error: event.isError,
          content: event.content,
        },
      }),
    );

    const context = additionalContext(result.stdout);
    if (!context) return;
    return {
      content: [
        ...event.content,
        { type: "text" as const, text: `\n\n[Playbook]\n${context}` },
      ],
    };
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return;
    runHook(projectRoot, "session-end-hook", hookPayload("SessionEnd", ctx.cwd, {}));
  });
}
