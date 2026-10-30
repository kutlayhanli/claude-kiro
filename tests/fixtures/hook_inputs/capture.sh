#!/usr/bin/env bash
# Capture real Claude Code hook inputs for the prompt-free-runs tests (Task 2 runs this, outside any sandbox).
# It starts paid `claude -p` sessions. Usage: capture.sh [out-dir]   (default: this directory)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE}"
RAW="$(mktemp -d)"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT

command -v claude >/dev/null || { echo "claude not found on PATH" >&2; exit 1; }
mkdir -p "$OUT"

# 1. A temp git project with specs/ck.json and settings registering the recorder on PreToolUse (and PostToolUse).
git -C "$PROJECT" init -q -b main
git -C "$PROJECT" config user.email capture@example.com
git -C "$PROJECT" config user.name Capture
mkdir -p "$PROJECT/specs" "$PROJECT/.claude"
echo '{}' > "$PROJECT/specs/ck.json"
cat > "$PROJECT/.claude/settings.json" <<JSON
{
  "hooks": {
    "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|Bash",
                    "hooks": [{"type": "command", "command": "python3 $HERE/record_hook.py $RAW"}]}],
    "PostToolUse": [{"matcher": "Bash",
                     "hooks": [{"type": "command", "command": "python3 $HERE/record_hook.py $RAW"}]}]
  }
}
JSON
git -C "$PROJECT" add -A
git -C "$PROJECT" commit -qm init

# 2. Headless, Workflow agent and Agent-tool subagent in one `claude -p` run: the prompt runs one Bash call itself,
#    then a one-agent Workflow whose agent runs one Bash call, then one Agent-tool subagent that runs one Bash call.
PROMPT='Do these four things in order, with no other tool calls.
1. Run the Bash command: echo CAPTURE_HEADLESS
2. Run a Workflow with an inline script that starts exactly one agent. The agent must run the Bash command: echo CAPTURE_WORKFLOW and then reply "ok".
3. Use the Agent tool to start one subagent. It must run the Bash command: echo CAPTURE_AGENT and then reply "ok".
4. Finally run the Bash command: echo CAPTURE_BLOCK
Then stop.'
( cd "$PROJECT" && claude -p --permission-mode bypassPermissions "$PROMPT" ) > "$RAW/claude-output.txt" 2>&1 || {
  echo "claude -p failed; see $RAW/claude-output.txt" >&2; exit 1; }
CLAUDE_VERSION="$(claude --version 2>/dev/null | head -n 1 || echo unknown)"

# 3. Main session: an interactive session in the same project, where one Bash call is approved. Manual by nature.
cat <<MSG

MANUAL STEP (main session). In another terminal run:
    cd $PROJECT && claude
Ask it to run the Bash command: echo CAPTURE_MAIN   and approve the call, then exit.
Press Enter here when done.
MSG
read -r _ || true

# 4. Rewrite names to invented ones, ids to stable fakes, HOME/PATH to placeholders; keep every key; write the summary.
python3 - "$RAW" "$OUT" "$PROJECT" "$CLAUDE_VERSION" <<'PY'
import glob, json, os, re, sys, datetime

raw, out, project, version = sys.argv[1:5]
home = os.path.expanduser("~")
records = []
for path in sorted(glob.glob(os.path.join(raw, "*.json"))):
    with open(path) as fh:
        records.append(json.load(fh))

def command_of(rec):
    return ((rec["input"].get("tool_input") or {}).get("command") or "")

def pick(marker, event="PreToolUse"):
    for rec in records:
        if rec["input"].get("hook_event_name") == event and marker in command_of(rec):
            return rec
    return None

ids = {}
def fake(value, prefix):
    if not value:
        return value
    return ids.setdefault((prefix, value), f"{prefix}-{len([k for k in ids if k[0] == prefix]) + 1:04d}")

def scrub(obj):
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            if k in ("session_id", "sessionId"):
                res[k] = fake(v, "session")
            elif k in ("agent_id", "agentId"):
                res[k] = fake(v, "agent")
            elif k in ("tool_use_id", "toolUseID"):
                res[k] = fake(v, "toolu")
            elif k == "transcript_path":
                res[k] = "/home/user/.claude/projects/demo/transcript.jsonl"
            else:
                res[k] = scrub(v)
        return res
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        s = obj.replace(project, "/work/demo")
        if home and home != "/":
            s = s.replace(home, "/home/user")
        return s
    return obj

def write(name, payload):
    with open(os.path.join(out, name), "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")

wanted = {
    "headless-pretooluse.json": pick("CAPTURE_HEADLESS"),
    "workflow-agent-pretooluse.json": pick("CAPTURE_WORKFLOW"),
    "agent-tool-subagent-pretooluse.json": pick("CAPTURE_AGENT"),
    "main-session-pretooluse.json": pick("CAPTURE_MAIN"),
}
for name, rec in wanted.items():
    if rec is None:
        print(f"WARNING: no recorded input for {name}", file=sys.stderr)
    else:
        write(name, scrub(rec["input"]))

head = wanted["headless-pretooluse.json"]
if head is not None:
    env = dict(head["envAll"])
    env["HOME"] = "<HOME>"
    env["PATH"] = "<PATH>"
    write("headless-env.json", scrub(env))

# postcheck-block-attachment.json: the real `hook_blocking_error` transcript line from the CAPTURE_BLOCK call.
block_line = None
for tp in glob.glob(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(home, ".claude")), "projects", "*", "*.jsonl")):
    with open(tp) as fh:
        for line in fh:
            if "hook_blocking_error" in line and "[ck post-check]" in line:
                try:
                    block_line = json.loads(line)
                except ValueError:
                    pass
if block_line is not None:
    write("postcheck-block-attachment.json", scrub(block_line))
else:
    print("WARNING: no hook_blocking_error line found in the session transcripts", file=sys.stderr)

wf = wanted["workflow-agent-pretooluse.json"]
ag = wanted["agent-tool-subagent-pretooluse.json"]
write("capture-summary.json", {
    "workflowAgentHasAgentId": bool(wf and "agent_id" in wf["input"]),
    "agentToolSubagentHasAgentId": bool(ag and "agent_id" in ag["input"]),
    "claudeCodeVersion": version,
    "capturedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
})
print("wrote:", ", ".join(sorted(os.listdir(out))))
PY

echo "Review the written files for leftovers of real names, then commit them (Task 2)."
