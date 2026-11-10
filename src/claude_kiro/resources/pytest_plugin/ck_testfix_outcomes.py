"""pytest plugin for `ck testfix prove` (load with `-p ck_testfix_outcomes`).

Appends one JSON line per setup or call report to `$CK_TESTFIX_OUTCOMES`:
`{"node": ..., "when": ..., "outcome": "passed|failed|skipped", "xfail": bool}`, so the proof can tell a test that
passed from one that was skipped, xfailed or xpassed. The variable and the plugin's own `-p` token are removed in
`pytest_configure` so a nested pytest inherits nothing. Standard library only; never touches the network.
"""

import json
import os

ENV_OUT = "CK_TESTFIX_OUTCOMES"
PLUGIN_NAME = "ck_testfix_outcomes"

_state = {"out": None}


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
        kept.append(tokens[i])
        i += 1
    if kept:
        os.environ["PYTEST_ADDOPTS"] = " ".join(kept)
    else:
        os.environ.pop("PYTEST_ADDOPTS", None)


def pytest_configure(config):
    out = os.environ.pop(ENV_OUT, None)
    _strip_addopts()
    _state["out"] = None if hasattr(config, "workerinput") else out


def pytest_runtest_logreport(report):
    if not _state["out"] or report.when not in ("setup", "call"):
        return
    with open(_state["out"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"node": report.nodeid, "when": report.when, "outcome": report.outcome,
                             "xfail": hasattr(report, "wasxfail")}) + "\n")
