#!/usr/bin/env python3
"""Recorder for real Claude Code hook inputs (used by capture.sh).

Usage in a hook command: python3 record_hook.py <out-dir>

Writes one file per call, `<out-dir>/<event>-<seq>.json`, holding {"input": <stdin JSON>, "env": {CLAUDE_*/CK_* keys},
"envAll": {all keys}}. For a PostToolUse call whose Bash command contains CAPTURE_BLOCK it blocks with a
`[ck post-check]` message, so the session transcript holds a real `hook_blocking_error` line. Never fails a call
otherwise: it prints nothing and exits 0.
"""
import json
import os
import sys
import time


def main() -> int:
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {"unparsed": raw}
    event = str(data.get("hook_event_name", "unknown")) if isinstance(data, dict) else "unknown"
    record = {
        "input": data,
        "env": {k: v for k, v in os.environ.items() if k.startswith(("CLAUDE_", "CK_"))},
        "envAll": dict(os.environ),
    }
    name = f"{event}-{time.time_ns()}-{os.getpid()}.json"
    with open(os.path.join(out_dir, name), "w") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
    command = ((data.get("tool_input") or {}).get("command") or "") if isinstance(data, dict) else ""
    if event == "PostToolUse" and "CAPTURE_BLOCK" in command:
        sys.stderr.write("[ck post-check] capture block: this message exists only to record a real blocking error.\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
