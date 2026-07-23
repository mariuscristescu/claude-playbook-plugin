#!/usr/bin/env python3
"""Normalize provider hook-payload dialects to the Claude Code schema.

The playbook bash hooks parse Claude Code's snake_case payloads
(tool_name/tool_input/prompt/...). Grok Build delivers camelCase payloads
with partially different tool vocabulary and tool_input keys — captured
live from grok 0.2.99 headless sessions (task 014):

  top-level:  toolName / toolInput / hookEventName / sessionId (camelCase)
  tool names: Edit arrives as "StrReplace", Bash as "Shell"
              (Write and Read already use their Claude names)
  input keys: file_path arrives as "path", content as "contents"
              (old_string / new_string / command already match Claude)
  prompt:     the user prompt is wrapped in <user_query>...</user_query>

This script reads ONE JSON payload on stdin and writes the normalized
payload to stdout. It is invoked once per hook run, right after
`INPUT=$(cat)`, so every downstream parse site in the hook reads the
normalized form — one normalizer instead of N per-site fallbacks
(task 014 plan-panel F-D).

Safety contract:
  - Claude payloads pass through BYTE-IDENTICALLY: a payload with no
    camelCase dialect marker (`hookEventName`/`toolName`/`toolInput`/
    `sessionId`) is echoed back exactly as received — never re-parsed,
    re-serialized, tool-renamed, or prompt-unwrapped. This makes the
    "claude unchanged" guarantee literal, not merely semantic (task 014
    impl-panel I2: unconditional transforms could rewrite a claude prompt
    that itself contained `<user_query>` tags, and re-serialization with
    json.dumps' default spaced separators broke chat-log-hook's jq-less
    `"prompt":"..."` grep fallback).
  - Any error (non-JSON stdin, unexpected shape) → the raw input is
    echoed back verbatim, so the hooks' own per-site `|| echo ""`
    fallbacks apply exactly as before this shim existed.

Cursor-compat note: Cursor's camelCase dialect shares the top-level key
shape, so this normalizer is deliberately not grok-branded.
"""
import json
import re
import sys

# Claude never emits these tool names; grok 0.2.99 does (captured live).
_TOOL_NAMES = {
    "StrReplace": "Edit",
    "Shell": "Bash",
    # Grok Build current names (docs + 2026-07 live sessions): Edit/Write alias
    # to search_replace, Bash to run_terminal_command; `write` is a distinct
    # create-file tool that must still map to Write for task-gate Guard 0/1.
    "search_replace": "Edit",
    "write": "Write",
    "run_terminal_command": "Bash",
}

# Remap these even WITHOUT camelCase dialect markers (hybrid hosts, panel 020).
# StrReplace/Shell stay dialect-gated so a pure snake_case Claude payload that
# happens to mention those strings is still byte-identical (task 014 contract).
_TOOL_NAMES_ALWAYS = frozenset(
    {"search_replace", "write", "run_terminal_command"}
)

# grok-native tool_input keys → Claude keys. Applied additively (grok key
# kept, Claude key added) and only when the Claude key is absent.
_INPUT_KEYS = {"path": "file_path", "contents": "content"}

_TOP_KEYS = {
    "hookEventName": "hook_event_name",
    "sessionId": "session_id",
    "toolName": "tool_name",
    "toolInput": "tool_input",
    "workspaceRoot": "workspace_root",
    "transcriptPath": "transcript_path",
}

# Presence of ANY of these top-level camelCase keys marks a grok/Cursor-dialect
# payload. Absent → treat as a native claude payload and pass through untouched
# (the byte-identity contract). Claude never emits camelCase top-level keys.
_DIALECT_MARKERS = ("hookEventName", "toolName", "toolInput", "sessionId")

# grok wraps the UserPromptSubmit prompt; unwrap only when the wrapper spans
# the whole value (a Claude prompt merely MENTIONING the tag is untouched).
_USER_QUERY_RE = re.compile(
    r"^\s*<user_query>\s*(.*?)\s*</user_query>\s*$", re.DOTALL
)


def is_foreign_dialect(payload) -> bool:
    """True iff the payload carries a camelCase dialect marker (grok/Cursor).

    A native claude payload has none of these, so normalize() leaves it
    entirely alone — the transforms below only ever run on foreign dialects.
    """
    return isinstance(payload, dict) and any(k in payload for k in _DIALECT_MARKERS)


def has_foreign_tool_name(payload) -> bool:
    """True if tool_name is a Grok name that must remap without dialect markers.

    Hybrid hosts may send write/search_replace/run_terminal_command with
    snake_case keys only; without remap, Guard 1 fail-opens (panel 020).
    StrReplace/Shell remain dialect-marker-gated (byte-identity for Claude).
    """
    if not isinstance(payload, dict):
        return False
    tn = payload.get("tool_name")
    return isinstance(tn, str) and tn in _TOOL_NAMES_ALWAYS


def needs_normalize(payload) -> bool:
    return is_foreign_dialect(payload) or has_foreign_tool_name(payload)


def normalize(payload):
    if not needs_normalize(payload):
        return payload
    out = dict(payload)
    for camel, snake in _TOP_KEYS.items():
        if camel in out and snake not in out:
            out[snake] = out[camel]
    tool_name = out.get("tool_name")
    if isinstance(tool_name, str) and tool_name in _TOOL_NAMES:
        out["tool_name"] = _TOOL_NAMES[tool_name]
    tool_input = out.get("tool_input")
    if isinstance(tool_input, dict):
        tool_input = dict(tool_input)
        for theirs, ours in _INPUT_KEYS.items():
            if theirs in tool_input and ours not in tool_input:
                tool_input[ours] = tool_input[theirs]
        out["tool_input"] = tool_input
    prompt = out.get("prompt")
    if isinstance(prompt, str):
        m = _USER_QUERY_RE.match(prompt)
        if m:
            out["prompt"] = m.group(1)
    return out


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception:
        sys.stdout.write(raw)  # non-JSON → verbatim (hooks' own fallbacks apply)
        return
    if not needs_normalize(payload):
        sys.stdout.write(raw)  # native claude → BYTE-IDENTICAL passthrough
        return
    # Foreign dialect / foreign tool names: emit the normalized payload.
    # Compact separators keep the output shape claude-native (no space after
    # ':'/',') so downstream grep fallbacks that expect the compact form still match.
    sys.stdout.write(json.dumps(normalize(payload), separators=(",", ":")))

if __name__ == "__main__":
    main()
