#!/bin/bash
# gate-echo-lib.sh
# Shared logic for hooks: project root detection + gate parsing.

# find_project_root
# Walk up from $PWD looking for .agent/tasks/ (legacy) or .agent/<user>/tasks/
# (multi-user) — the definitive playbook marker.
# CLAUDE.md and MIND_MAP.md alone are NOT sufficient — they exist in non-playbook
# projects and would cause hooks to fire where they shouldn't.
# Outputs the project root path, or empty string if not found.
find_project_root() {
    local dir="$PWD"
    while true; do
        # Legacy layout
        if [ -d "$dir/.agent/tasks" ]; then
            echo "$dir"
            return 0
        fi
        # Multi-user layout: .agent/<user>/tasks/
        if [ -d "$dir/.agent" ]; then
            local sub
            for sub in "$dir/.agent"/*/; do
                if [ -d "${sub}tasks" ]; then
                    echo "$dir"
                    return 0
                fi
            done
        fi
        local parent
        parent=$(dirname "$dir")
        if [ "$parent" = "$dir" ]; then
            break
        fi
        dir="$parent"
    done
    echo ""
    return 0  # "not found" communicated via empty output, not exit code (set -e safe)
}

# find_agent_root_pid
# Walk parent process tree. Output PID of the highest ancestor whose
# `comm` is claude/codex/agy/pi, or empty if none found within 20 hops.
# Mirrors `find_agent_root_pid()` in src/tasks/core.py — both walk the
# same `ps` tree and converge on the same PID. Used as fallback when
# PLAYBOOK_SESSION_ID env var isn't propagated.
find_agent_root_pid() {
    # Windows/MSYS: the ancestor scan is non-functional — Git-Bash `ps` has no
    # `-o` flag, and MSYS vs native-Windows PID namespaces are disjoint. Skip it
    # (mirrors the win32 guard in core.py find_agent_root_pid) and let
    # resolve_session_id fall back to PLAYBOOK_SESSION_ID / $PPID. POSIX is
    # untouched: the guard only matches MSYS/Cygwin/MinGW shells.
    case "${OSTYPE:-}" in
        msys*|cygwin*) echo ""; return 0 ;;
    esac
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*) echo ""; return 0 ;;
    esac
    local pid=$PPID
    local last_agent=""
    local count=0
    local info ppid comm
    while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ] && [ "$count" -lt 20 ]; do
        info=$(ps -p "$pid" -o ppid=,comm= 2>/dev/null) || break
        [ -z "$info" ] && break
        ppid=$(echo "$info" | awk '{print $1}')
        comm=$(echo "$info" | awk '{$1=""; sub(/^ +/, ""); print}')
        comm="${comm##*/}"  # parameter expansion: strip path; safe for "-zsh" (basename would error)
        case "$comm" in
            claude|codex|agy|pi) last_agent=$pid ;;
        esac
        [ "$ppid" = "$pid" ] && break
        pid=$ppid
        count=$((count + 1))
    done
    echo "$last_agent"
}

# resolve_session_id
# Returns the session_id used to namespace .agent/sessions/<id>/.
# Order: PLAYBOOK_SESSION_ID env → ancestor scan (root agent PID) →
# immediate-parent PID. Mirrors resolve_session_id() in src/tasks/core.py
# — Python and bash converge on the same value when env var is unset.
resolve_session_id() {
    if [ -n "${PLAYBOOK_SESSION_ID:-}" ]; then
        echo "$PLAYBOOK_SESSION_ID"
        return 0
    fi
    local agent_pid
    agent_pid=$(find_agent_root_pid)
    if [ -n "$agent_pid" ]; then
        echo "pid-$agent_pid"
    else
        echo "pid-$PPID"
    fi
}

# resolve_agent_dir PROJECT_DIR
# Echoes the agent state directory:
#   absent .agent/current_user  → PROJECT_DIR/.agent        (legacy)
#   valid  .agent/current_user  → PROJECT_DIR/.agent/<user> (multi-user)
#   invalid content             → stderr + exit 1
resolve_agent_dir() {
    local project_dir="$1"
    local marker="$project_dir/.agent/current_user"
    if [ ! -f "$marker" ]; then
        echo "$project_dir/.agent"
        return 0
    fi
    local name
    name=$(sed 's/^[[:space:]]*//;s/[[:space:]]*$//' "$marker")
    # Validate: non-empty, not . or .., no slash, matches [a-zA-Z0-9][a-zA-Z0-9_.-]*
    if [ -z "$name" ] || [ "$name" = "." ] || [ "$name" = ".." ]; then
        echo "Error: .agent/current_user contains invalid username '${name}'. Must be non-empty and not . or .." >&2
        exit 1
    fi
    case "$name" in
        */*) echo "Error: .agent/current_user contains invalid username '${name}'. Slashes not allowed." >&2; exit 1 ;;
        [a-zA-Z0-9]*) ;;
        *) echo "Error: .agent/current_user contains invalid username '${name}'. Must start with a letter or digit." >&2; exit 1 ;;
    esac
    if ! echo "$name" | grep -qE '^[a-zA-Z0-9][a-zA-Z0-9_.-]*$'; then
        echo "Error: .agent/current_user contains invalid username '${name}'. Use only letters, digits, hyphens, underscores, dots." >&2
        exit 1
    fi
    echo "$project_dir/.agent/$name"
}

# agent_dir_writable PROJECT_DIR
# Returns 0 if the resolved agent dir exists and is writable, 1 otherwise.
# Use this before any hook that writes to .agent/ — in sandbox mode
# the directory may exist but be read-only.
agent_dir_writable() {
    local agent_dir
    agent_dir=$(resolve_agent_dir "$1")
    [ -d "$agent_dir" ] && [ -w "$agent_dir" ]
}

# get_gate_info TASK_FILE
# Outputs: done_count total_count gate_line gate_text
# If all done: gate_line and gate_text are empty
get_gate_info() {
    local task_file="$1"

    if [ ! -f "$task_file" ]; then
        echo "0 0 0 ''"
        return 1
    fi

    # Count total and done checkboxes (only at line start, not in backticks)
    # Pattern: only match [ ], [x], [X] — not [8] or [40] (reference links)
    local total
    total=$(grep -cE '^[[:space:]]*- \[( |x|X)\]' "$task_file" 2>/dev/null) || total=0
    local done
    done=$(grep -cE '^[[:space:]]*- \[[xX]\]' "$task_file" 2>/dev/null) || done=0

    # Find first unchecked gate
    local gate_line=""
    local gate_text=""

    while IFS= read -r line; do
        local lineno="${line%%:*}"
        local content="${line#*:}"
        if echo "$content" | grep -qE '^[[:space:]]*- \[ \]'; then
            gate_line="$lineno"
            gate_text=$(echo "$content" | sed 's/^[[:space:]]*- \[ \] *//')
            break
        fi
    done < <(grep -nE '^[[:space:]]*- \[ \]' "$task_file" 2>/dev/null)

    echo "$done $total $gate_line $gate_text"
}

# read_counter FILE KEY
# Read a key=value from the counter file. Outputs the value, or empty if missing.
read_counter() {
    local file="$1" key="$2"
    if [ -f "$file" ]; then
        sed -n "s/^${key}=//p" "$file" 2>/dev/null | head -1
    fi
}

# write_counter FILE KEY VALUE
# Set a key=value in the counter file. Creates file if missing, updates in-place if key exists.
# Uses grep-filter-append instead of sed to avoid delimiter collisions with gate text
# containing |, backticks, or other special characters.
write_counter() {
    local file="$1" key="$2" value="$3"
    local tmp="${file}.tmp.$$"
    if [ -f "$file" ]; then
        grep -v "^${key}=" "$file" > "$tmp" 2>/dev/null || true
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    mv "$tmp" "$file"
}

# reset_counters FILE
# Reset tools=0 and writes=0, preserving gate_* fields. Creates file if missing.
reset_counters() {
    local file="$1"
    if [ -f "$file" ]; then
        # Preserve gate_* lines, reset tools/writes
        local gate_lines
        gate_lines=$(grep '^gate_' "$file" 2>/dev/null || true)
        printf 'tools=0\nwrites=0\n' > "$file"
        if [ -n "$gate_lines" ]; then
            echo "$gate_lines" >> "$file"
        fi
    else
        printf 'tools=0\nwrites=0\n' > "$file"
    fi
}

# format_context TASK_NUM DONE TOTAL GATE_TEXT GATE_LINE REL_PATH
# Outputs the formatted context string for the hook
format_context() {
    local task_num="$1"
    local done="$2"
    local total="$3"
    local gate_text="$4"
    local gate_line="$5"
    local rel_path="$6"

    if [ -z "$gate_line" ]; then
        echo "# [${task_num}] — all gates done. Stay for follow-up. Auto-closes on task switch."
    else
        echo "# Working on task [${task_num}] gate (${done}/${total}) -> [ ] ${gate_text}
# Done? Check the box: ${rel_path}:${gate_line}"
    fi
}

# write_log_append INPUT_JSON PROJECT_DIR
# Appends the written file's content to the persistent write log.
# Called from PostToolUse for Write/Edit tools. Extracts file_path from
# the tool input JSON, reads the file, appends with timestamp.
# Log lives at ~/.local/share/playbook/<project-slug>/write_log
# — outside the project tree so agent can't accidentally delete it.
write_log_append() {
    local input="$1" project_dir="$2"
    local file_path
    file_path=$(echo "$input" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")
    if [ -z "$file_path" ] || [ ! -f "$file_path" ]; then
        return 0
    fi
    # Project slug: absolute path with / replaced by -
    local slug
    slug=$(echo "$project_dir" | sed 's|^/||; s|/|-|g')
    local log_dir="$HOME/.local/share/playbook/$slug"
    mkdir -p "$log_dir" 2>/dev/null || return 0
    local log_file="$log_dir/write_log"
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local size
    size=$(wc -c < "$file_path" 2>/dev/null | tr -d ' ')
    {
        printf '=== %s %s (%s bytes) ===\n' "$ts" "$file_path" "$size"
        cat "$file_path"
        printf '\n'
    } >> "$log_file" 2>/dev/null || true
}

# create_wrapper PROJECT_DIR WRAPPER_NAME
# Creates .claude/bin/<WRAPPER_NAME> as a wrapper that resolves the plugin's
# scripts/<WRAPPER_NAME> (manifest-first, scope-aware, version-deterministic —
# see the template below) and execs into it.
# - Skips if file exists without "# playbook-managed" marker (custom wrapper)
# - Overwrites if file has the marker (stale playbook wrapper)
# - Creates .claude/bin/ directory if needed
create_wrapper() {
    local project_dir="$1"
    local wrapper_name="$2"
    local wrapper_path="$project_dir/.claude/bin/$wrapper_name"

    # Skip custom wrappers (no playbook-managed marker)
    # Empty files are NOT custom — overwrite them (self-healing)
    if [ -f "$wrapper_path" ] && [ -s "$wrapper_path" ]; then
        if ! grep -q '# playbook-managed' "$wrapper_path" 2>/dev/null; then
            return 0
        fi
    fi

    mkdir -p "$project_dir/.claude/bin"

    # Write to a PID-private temp, then atomically rename into place. The real
    # wrapper path is only ever touched by the final `mv` — it never passes
    # through a 0-byte O_TRUNC window, so a kill (e.g. a panel-review judge
    # subprocess timing out mid-SessionStart) can't leave it empty, and two
    # concurrent writers can't interleave a truncate with a write.
    local tmp="${wrapper_path}.tmp.$$"
    cat > "$tmp" <<'WRAPPER'
#!/bin/bash
# playbook-managed — do not edit; regenerated by playbook plugin
# Resolves the SAME plugin copy the harness hooks run: installed_plugins.json's
# installPath (what Claude Code points ${CLAUDE_PLUGIN_ROOT} at), scope-aware,
# newest version first. Falls back to a filesystem scan that prefers versioned
# cache dirs over marketplace clone tips, then to a plain deterministic find.
WRAPPER_DIR="$(cd "$(dirname "$0")" && pwd -P)"
PROJECT_ROOT="$(cd "$WRAPPER_DIR/../.." 2>/dev/null && pwd -P)"
SCRIPT="$(python3 - "$PROJECT_ROOT" 2>/dev/null <<'PYRESOLVE'
import glob, json, os, sys

def vkey(v):
    # "1.3.10" -> (1,3,10); non-numeric segments (e.g. "unknown") -> -1 so any
    # numbered version outranks them.
    return tuple(int(x) if x.isdigit() else -1 for x in str(v).split("."))

def same_dir(a, b):
    # inode compare survives case-insensitive filesystems (default APFS) where
    # realpath string equality can miss; realpath as fallback for missing paths.
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)

try:
    project = sys.argv[1] if len(sys.argv) > 1 else ""
    root = os.path.expanduser(os.path.join("~", ".claude", "plugins"))
    cands = []  # (rank, version_key, last_updated, path)
    try:
        with open(os.path.join(root, "installed_plugins.json")) as fh:
            data = json.load(fh)
        for key, installs in (data.get("plugins") or {}).items():
            if key.split("@")[0] != "playbook":
                continue
            for e in installs or []:
                ip = e.get("installPath") or ""
                s = ip + "/scripts/WRAPPER_NAME"
                if not (ip and os.access(s, os.X_OK)):
                    continue
                pp = e.get("projectPath") or ""
                if pp:
                    # Pinned to a project: only eligible for THAT project.
                    if not (project and same_dir(pp, project)):
                        continue
                    rank = 0
                else:
                    rank = 1
                cands.append((rank, vkey(e.get("version")), e.get("lastUpdated") or "", s))
    except Exception:
        pass
    if not cands:
        # No usable manifest entry: scan known layouts. Versioned cache dirs
        # (the layout hooks run from) outrank marketplace clone tips.
        for s in glob.glob(os.path.join(root, "cache", "*", "playbook", "*") + "/scripts/WRAPPER_NAME"):
            if os.access(s, os.X_OK):
                ver = os.path.basename(os.path.dirname(os.path.dirname(s)))
                cands.append((0, vkey(ver), "", s))
        if not cands:
            for pat in ("marketplaces/*/plugins/playbook", "cache/*/playbook"):
                for s in glob.glob(os.path.join(root, pat) + "/scripts/WRAPPER_NAME"):
                    if os.access(s, os.X_OK):
                        cands.append((1, (), "", s))
    if cands:
        # Multi-pass stable sort, least-significant key first. Final order:
        # rank asc, version desc, lastUpdated desc, path asc.
        cands.sort(key=lambda t: t[3])
        cands.sort(key=lambda t: t[2], reverse=True)
        cands.sort(key=lambda t: t[1], reverse=True)
        cands.sort(key=lambda t: t[0])
        print(cands[0][3])
except Exception:
    pass
PYRESOLVE
)"
if [ -z "$SCRIPT" ]; then
    # Last resort (python3 unavailable/broken — the tasks CLI couldn't run
    # anyway): deterministic, readdir-order-independent search.
    SCRIPT="$(find ~/.claude/plugins -path "*/playbook/scripts/WRAPPER_NAME" -type f 2>/dev/null | sort | head -1)"
fi
if [ -z "$SCRIPT" ]; then
    echo "Error: playbook plugin not found." >&2
    echo "Install: claude plugin marketplace add horiacristescu/claude-playbook-plugin" >&2
    exit 1
fi
exec "$SCRIPT" "$@"
WRAPPER

    # Replace placeholder with actual wrapper name (on the temp, not the live file)
    sed -i.bak "s|WRAPPER_NAME|$wrapper_name|g" "$tmp" && rm -f "$tmp.bak"
    chmod +x "$tmp"
    mv -f "$tmp" "$wrapper_path"
}

# Known playbook-managed wrapper names. Kept in sync with the create_wrapper
# calls in `init` and `session-start-hook`.
PLAYBOOK_WRAPPER_NAMES="tasks sandbox monitor playbook-codex playbook-agy playbook-pi"

# heal_empty_wrappers PROJECT_DIR
# Repair any playbook wrapper that a pre-fix kill left 0 bytes. Cheap enough to
# call on every hook: for each KNOWN name that exists AND is empty, regenerate.
# - Allowlist only, so a legitimately-empty *custom* file in .claude/bin is never
#   clobbered (create_wrapper treats empty as non-custom and would overwrite it).
# - Only heals files that already exist — never provisions wrappers a project lacks.
# - Fully failure-tolerant: intended to be called as `heal_empty_wrappers … || true`
#   from hooks that run under `set -e` and may be in a read-only sandbox.
heal_empty_wrappers() {
    local project_dir="$1"
    [ -n "$project_dir" ] || return 0
    local bin_dir="$project_dir/.claude/bin"
    [ -d "$bin_dir" ] || return 0
    local name
    for name in $PLAYBOOK_WRAPPER_NAMES; do
        if [ -e "$bin_dir/$name" ] && [ ! -s "$bin_dir/$name" ]; then
            create_wrapper "$project_dir" "$name" 2>/dev/null || true
        fi
    done
    return 0
}
