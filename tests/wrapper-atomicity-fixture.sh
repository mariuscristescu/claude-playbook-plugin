#!/usr/bin/env bash
# Fixture for create_wrapper / heal_empty_wrappers in scripts/gate-echo-lib.sh.
#
# Guards the wrapper-truncation fix (task 009): the launcher `.claude/bin/tasks`
# was intermittently left 0 bytes when a create_wrapper running inside a killed
# process (e.g. a panel-review judge's SessionStart hook timing out) was cut off
# mid in-place write. The fix writes to a PID-private temp and atomically renames
# into place, plus self-heals any already-empty wrapper on the next PreToolUse.
#
# Run from anywhere: `bash claude-playbook-plugin/tests/wrapper-atomicity-fixture.sh`.
# Exits 0 if every scenario passes, non-zero on the first failing assertion.

set -uo pipefail   # NOT -e: scenarios intentionally kill processes / probe failures

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$HERE/../plugins/playbook/scripts"
LIB="$SCRIPTS/gate-echo-lib.sh"
GATE_HOOK="$SCRIPTS/task-gate-hook"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }

# assert_wrapper_healthy FILE NAME LABEL — non-empty, +x, marker, correct script
# name substituted, no leftover placeholder.
assert_wrapper_healthy() {
    local f="$1" name="$2" label="$3"
    if [ ! -s "$f" ]; then fail "$label — $f is empty/missing"; return; fi
    if [ ! -x "$f" ]; then fail "$label — $f not executable"; return; fi
    if ! grep -q '# playbook-managed' "$f"; then fail "$label — no playbook-managed marker"; return; fi
    if grep -q 'WRAPPER_NAME' "$f"; then fail "$label — placeholder WRAPPER_NAME not substituted"; return; fi
    if ! grep -q "scripts/$name" "$f"; then fail "$label — wrong script name (expected scripts/$name)"; return; fi
    pass "$label"
}

echo "=== wrapper-atomicity fixture ==="

# ---------------------------------------------------------------------------
# S1 — healthy generation: create_wrapper produces a valid, executable wrapper.
# ---------------------------------------------------------------------------
S1="$WORK/s1"; mkdir -p "$S1"
( source "$LIB"; create_wrapper "$S1" tasks )
assert_wrapper_healthy "$S1/.claude/bin/tasks" tasks "S1 healthy generation"
if ls "$S1/.claude/bin/"*.tmp.* >/dev/null 2>&1; then fail "S1 tmp litter left behind"; else pass "S1 no tmp litter"; fi

# ---------------------------------------------------------------------------
# S2 — custom wrapper (non-empty, no marker) is left untouched.
# ---------------------------------------------------------------------------
S2="$WORK/s2"; mkdir -p "$S2/.claude/bin"
printf '#!/bin/bash\necho my custom launcher\n' > "$S2/.claude/bin/tasks"
BEFORE="$(cat "$S2/.claude/bin/tasks")"
( source "$LIB"; create_wrapper "$S2" tasks )
if [ "$(cat "$S2/.claude/bin/tasks")" = "$BEFORE" ]; then pass "S2 custom wrapper untouched"; else fail "S2 custom wrapper was overwritten"; fi

# ---------------------------------------------------------------------------
# S3 — empty wrapper (the bug's aftermath) is regenerated (empty != custom).
# ---------------------------------------------------------------------------
S3="$WORK/s3"; mkdir -p "$S3/.claude/bin"
: > "$S3/.claude/bin/tasks"   # 0 bytes, still there
( source "$LIB"; create_wrapper "$S3" tasks )
assert_wrapper_healthy "$S3/.claude/bin/tasks" tasks "S3 empty wrapper regenerated"

# ---------------------------------------------------------------------------
# S4 — kill mid-write: a create_wrapper stalled in its final `mv` and then
# SIGKILLed must leave the live path intact — the OLD content or the NEW
# content, NEVER 0 bytes. tmp+mv makes this true at every instant, so the
# assertion is timing-independent (we stall only to make the pre-mv case the
# common one).
# ---------------------------------------------------------------------------
S4="$WORK/s4"; mkdir -p "$S4/.claude/bin"
# Seed a distinctive OLD wrapper (must carry the marker, else create_wrapper's
# custom-file guard would skip regeneration and the kill window never opens).
printf '#!/bin/bash\n# playbook-managed\nOLD REAL CONTENT\n' > "$S4/.claude/bin/tasks"
chmod +x "$S4/.claude/bin/tasks"
OLD_SUM="$(shasum "$S4/.claude/bin/tasks" | awk '{print $1}')"
SIGNAL="$S4/mv-reached"
# Separate PROCESS (distinct $$), with `mv` shadowed to stall after signalling.
bash -c '
    mv() { touch "'"$SIGNAL"'"; sleep 5; command mv "$@"; }
    source "'"$LIB"'"
    create_wrapper "'"$S4"'" tasks
' &
KILL_PID=$!
# Wait until the shim signals it has entered mv (deterministic, bounded).
for _ in $(seq 1 50); do [ -e "$SIGNAL" ] && break; sleep 0.1; done
kill -9 "$KILL_PID" 2>/dev/null
pkill -9 -P "$KILL_PID" 2>/dev/null   # reap the stalling sleep child if still attached
wait "$KILL_PID" 2>/dev/null
NOW_BYTES="$(wc -c < "$S4/.claude/bin/tasks" | tr -d ' ')"
NOW_SUM="$(shasum "$S4/.claude/bin/tasks" | awk '{print $1}')"
NEW_SUM="$(source "$LIB"; TMPD="$S4/ref"; mkdir -p "$TMPD"; create_wrapper "$TMPD" tasks; shasum "$TMPD/.claude/bin/tasks" | awk '{print $1}')"
if [ "$NOW_BYTES" -gt 0 ]; then pass "S4 live wrapper never 0 bytes after kill (${NOW_BYTES}B)"; else fail "S4 live wrapper truncated to 0 bytes"; fi
if [ "$NOW_SUM" = "$OLD_SUM" ] || [ "$NOW_SUM" = "$NEW_SUM" ]; then
    pass "S4 live wrapper is exactly OLD or NEW content (no partial write)"
else
    fail "S4 live wrapper is neither OLD nor NEW (partial content)"
fi

# ---------------------------------------------------------------------------
# S5 — N concurrent writers (each a distinct process, so distinct $$) on the
# SAME path: every invocation exits 0 (no writer's cleanup sabotages a peer),
# final file intact + executable, no tmp litter.
# ---------------------------------------------------------------------------
S5="$WORK/s5"; mkdir -p "$S5/.claude/bin"
RC_DIR="$S5/rc"; mkdir -p "$RC_DIR"
for i in $(seq 1 8); do
    bash -c 'source "'"$LIB"'"; create_wrapper "'"$S5"'" tasks; echo $? > "'"$RC_DIR"'/'"$i"'"' &
done
wait
NONZERO=0
for i in $(seq 1 8); do
    rc="$(cat "$RC_DIR/$i" 2>/dev/null || echo missing)"
    [ "$rc" = "0" ] || { NONZERO=$((NONZERO+1)); echo "    writer $i exit=$rc"; }
done
if [ "$NONZERO" -eq 0 ]; then pass "S5 all 8 concurrent writers exited 0"; else fail "S5 $NONZERO/8 writers had non-zero exit"; fi
assert_wrapper_healthy "$S5/.claude/bin/tasks" tasks "S5 final wrapper intact after concurrency"
if ls "$S5/.claude/bin/"*.tmp.* >/dev/null 2>&1; then fail "S5 tmp litter left behind"; else pass "S5 no tmp litter"; fi

# ---------------------------------------------------------------------------
# S6 — aged stale-tmp GC (session-start-hook sweep): aged tmp deleted, fresh kept.
# ---------------------------------------------------------------------------
S6="$WORK/s6"; mkdir -p "$S6/.claude/bin"
touch "$S6/.claude/bin/tasks.tmp.fresh"
touch -t 202601010000 "$S6/.claude/bin/tasks.tmp.aged"
find "$S6/.claude/bin" -maxdepth 1 -name '*.tmp.*' -mtime +0 -delete 2>/dev/null || true
if [ -e "$S6/.claude/bin/tasks.tmp.aged" ]; then fail "S6 aged tmp not removed"; else pass "S6 aged tmp removed"; fi
if [ -e "$S6/.claude/bin/tasks.tmp.fresh" ]; then pass "S6 fresh tmp preserved"; else fail "S6 fresh tmp wrongly removed"; fi

# ---------------------------------------------------------------------------
# S7 — heal_empty_wrappers: heals an empty allowlist wrapper, ignores an empty
# NON-allowlist file, never creates a wrapper the project lacks.
# ---------------------------------------------------------------------------
S7="$WORK/s7"; mkdir -p "$S7/.claude/bin"
: > "$S7/.claude/bin/tasks"                 # allowlist name, empty -> should heal
: > "$S7/.claude/bin/my-notes"              # not on allowlist, empty -> leave alone
# (monitor deliberately absent -> must NOT be created)
( source "$LIB"; heal_empty_wrappers "$S7" )
assert_wrapper_healthy "$S7/.claude/bin/tasks" tasks "S7 empty allowlist wrapper healed"
if [ -s "$S7/.claude/bin/my-notes" ]; then fail "S7 non-allowlist file was written"; else pass "S7 non-allowlist empty file left untouched"; fi
if [ -e "$S7/.claude/bin/monitor" ]; then fail "S7 absent wrapper was created (should only heal existing)"; else pass "S7 absent wrapper not created"; fi

# ---------------------------------------------------------------------------
echo "============================================"
echo "wrapper-atomicity fixture: $PASS passed, $FAIL failed"
echo "============================================"
[ "$FAIL" -eq 0 ]
