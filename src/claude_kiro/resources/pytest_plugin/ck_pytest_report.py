"""pytest report plugin for `ck gate` (load with `-p ck_pytest_report`).

Appends one JSON line per failed or errored report to `$CK_GATE_REPORT`, and when `CK_GATE_COLLECT` is set one
line per collected node ID. Lines are `{"kind": "report", ...}` or `{"kind": "collect", "node": ...}`.

Nothing is written unless `CK_GATE_REPORT` is set. The plugin reads its variables in `pytest_configure`, then
removes them and its own `-p ck_pytest_report` token from `os.environ` (and `PYTEST_ADDOPTS`), so a nested pytest
started by a test inherits nothing. It writes only when `config.rootpath` equals `CK_GATE_ROOT`. Under xdist the
controller reads the variables before the workers start, so only the controller writes. Standard library only;
it never touches the network.
"""

import json
import os
import re

ENV_REPORT = "CK_GATE_REPORT"
ENV_ROOT = "CK_GATE_ROOT"
ENV_COLLECT = "CK_GATE_COLLECT"
PLUGIN_NAME = "ck_pytest_report"
DETAIL_LIMIT = 4000

_state = {"report": None, "collect": False, "root": None}


def _strip_addopts():
    raw = os.environ.get("PYTEST_ADDOPTS")
    if raw is None:
        return
    tokens = raw.split()
    kept = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "-p" and i + 1 < len(tokens) and tokens[i + 1] == PLUGIN_NAME:
            i += 2
            continue
        if tokens[i] == "-p" + PLUGIN_NAME:
            i += 1
            continue
        kept.append(tokens[i])
        i += 1
    if kept:
        os.environ["PYTEST_ADDOPTS"] = " ".join(kept)
    else:
        os.environ.pop("PYTEST_ADDOPTS", None)


def pytest_configure(config):
    report = os.environ.pop(ENV_REPORT, None)
    root = os.environ.pop(ENV_ROOT, None)
    collect = os.environ.pop(ENV_COLLECT, None)
    _strip_addopts()
    _state["report"] = None
    if not report:
        return
    if hasattr(config, "workerinput"):  # an xdist worker never writes
        return
    if not root or os.path.realpath(str(config.rootpath)) != os.path.realpath(root):
        return
    _state.update(report=report, collect=bool(collect), root=str(config.rootpath))


def _write(obj):
    with open(_state["report"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def _relative(path):
    root = _state["root"]
    if not path or not root:
        return path
    full = os.path.realpath(str(path))
    base = os.path.realpath(root)
    if full == base or full.startswith(base + os.sep):
        return os.path.relpath(full, base).replace(os.sep, "/")
    return str(path)


_EXC_PREFIX = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")
_EXC_LAST = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))(?::\s?(.*))?$")


_RAISED = {}  # (nodeid, phase) -> exception class name, from the call's excinfo (controller process only)


def pytest_runtest_makereport(item, call):
    if call.excinfo is not None:
        cls = call.excinfo.type
        # A subclass of AssertionError is an assertion failure (3.2); other names keep their own class name.
        _RAISED[(item.nodeid, call.when)] = "AssertionError" if issubclass(cls, AssertionError) else cls.__name__


def _crash(longrepr, exc_hint=None):
    """(exc_type, message, crash_path, crash_line, detail) from a pytest longrepr."""
    text = ""
    if longrepr is not None:
        try:
            text = str(longrepr)
        except Exception:  # a repr that cannot render is still a failure
            text = repr(longrepr)
    crash = getattr(longrepr, "reprcrash", None)
    first = ""
    path = None
    line = None
    if crash is not None:
        first = (getattr(crash, "message", "") or "").splitlines()[0:1]
        first = first[0] if first else ""
        path = getattr(crash, "path", None)
        lineno = getattr(crash, "lineno", None)  # pytest's reprcrash line is already 1-based
        line = lineno if isinstance(lineno, int) and lineno >= 1 else None
    exc, message = "", first
    m = _EXC_PREFIX.match(first)
    if m and m.group(1).endswith(("Error", "Exception", "Warning", "Exit", "Interrupt")):
        exc = m.group(1).split(".")[-1]
        message = m.group(2) or ""
    if not exc and crash is None:  # collection errors carry no reprcrash: take the last exception line
        for raw in reversed(text.splitlines()):
            line_text = raw.strip()
            if line_text.startswith("E ") or line_text.startswith("E\t"):
                line_text = line_text[1:].strip()
            m = _EXC_LAST.match(line_text)
            if m:
                exc = m.group(1).split(".")[-1]
                message = (m.group(2) or "").strip()
                break
    if exc_hint:
        exc = exc_hint
    elif not exc and first.startswith("assert"):  # pytest rewrote a bare assert with no exception name
        exc = "AssertionError"
    if path is not None:
        path = _relative(path)
    return exc, message, path, line, text[:DETAIL_LIMIT]


def pytest_collection_finish(session):
    if not _state["report"] or not _state["collect"]:
        return
    for item in session.items:
        _write({"kind": "collect", "node": item.nodeid})


def pytest_collectreport(report):
    if not _state["report"] or not report.failed:
        return
    exc, message, path, line, detail = _crash(report.longrepr)
    _write({"kind": "report", "node": report.nodeid, "phase": "collect", "exc_type": exc,
            "crash_path": path, "crash_line": line, "message": message, "detail": detail})


def pytest_runtest_logreport(report):
    if not _state["report"] or not report.failed or report.when not in ("setup", "call", "teardown"):
        return
    exc, message, path, line, detail = _crash(report.longrepr, _RAISED.pop((report.nodeid, report.when), None))
    _write({"kind": "report", "node": report.nodeid, "phase": report.when, "exc_type": exc,
            "crash_path": path, "crash_line": line, "message": message, "detail": detail})
