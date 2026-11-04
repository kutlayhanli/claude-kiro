"""Open-event audit for `ck` child processes (test-only; it only observes).

Python imports this module at start-up when tests/openaudit is first on PYTHONPATH. When $CK_TEST_OPEN_LOG is set,
every `open` audit event for a path whose name matches `agent-*.jsonl` appends that path to the log, one per line.
"""

import fnmatch
import os
import sys

_LOG = os.environ.get("CK_TEST_OPEN_LOG")
_busy = False


def _hook(event, args):
    global _busy
    if event != "open" or _busy or not _LOG:
        return
    path = args[0] if args else None
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str) or not fnmatch.fnmatch(os.path.basename(path), "agent-*.jsonl"):
        return
    _busy = True
    try:
        with open(_LOG, "a") as fh:
            fh.write(path + "\n")
    except OSError:
        pass
    finally:
        _busy = False


if _LOG:
    sys.addaudithook(_hook)
