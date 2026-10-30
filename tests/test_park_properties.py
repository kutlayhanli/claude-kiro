"""P-17 and P-18: the notification never carries quoted text, and no notify outcome blocks or fails a park."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st

from guard_helpers import ck_run_cmd, notify_script, set_notify
from stats_helpers import spec_repo
from test_guard_park import clean_env, park

PROPERTIES = {"P-17": "test_notification_never_carries_quoted_text", "P-18": "test_every_notify_outcome_parks_and_exits_zero"}

WINDOW = 12
PWNED = "PWNED_prop_9c1e"
PURE = dict(max_examples=200, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))
CLI = dict(max_examples=40, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))

SPECS = ["demo", "ledger-sync", "x-1", "zebra-quartz", "auth"]
FRAGMENTS = [f"$(touch {PWNED})", f"`touch {PWNED}`", "'", '"', "; rm -rf x", "line\nbreak", "café 中文", "&& true", "${HOME}"]


def texts(spec_hint: str = "") -> st.SearchStrategy:
    word = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), min_size=1, max_size=300)
    return st.lists(st.one_of(word, st.sampled_from(FRAGMENTS)), min_size=1, max_size=8).map(
        lambda parts: (" ".join(parts) + " " + spec_hint)[:2000])


def leaks(message: str, quote: str) -> bool:
    for line in quote.splitlines():
        line = line.strip()
        if len(line) >= WINDOW and line in message:
            return True
    return any(quote[i:i + WINDOW] in message for i in range(max(len(quote) - WINDOW + 1, 0)))


@settings(**PURE)
@given(spec=st.sampled_from(SPECS), task=st.integers(1, 99).map(str), test=texts(), req=texts())
def test_message_names_spec_task_and_decision_only(spec, task, test, req):
    from claude_kiro import notify

    message = notify.notify_message(spec, task)
    assert spec in message and task in message and "decision" in message
    assert not leaks(message, test) and not leaks(message, req)


@settings(**CLI)
@given(spec=st.sampled_from(SPECS), task=st.integers(1, 4).map(str), test=texts("zebra-quartz demo"), req=texts("ledger-sync"))
def test_notification_never_carries_quoted_text(spec, task, test, req):
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        repo = spec_repo(tmp / "repo", spec=spec)
        out = tmp / "notify.json"
        env = clean_env({"CK_CONFIG_HOME": str(tmp / "cfg")})
        assert set_notify(notify_script(tmp / "bin", "ok", out), env=env).returncode == 0
        done = park(repo, task, {"file": "tests/t.py", "kind": "test", "test": test, "requirement": req, "decision": "d"},
                    spec=spec, env=env)
        assert done.returncode == 0, done.stdout + done.stderr
        sent = json.loads(out.read_text())
        message = json.loads(sent["stdin"])["message"]
        assert spec in message and task in message and "decision" in message
        for channel in (sent["stdin"], sent["env"]["CK_NOTIFY_MESSAGE"], " ".join(sent["argv"])):
            body = channel.replace(spec, "").replace("zebra-quartz", "").replace("ledger-sync", "")
            assert not leaks(body, test.replace(spec, "")) and not leaks(body, req.replace(spec, ""))
        for where in (repo, tmp, Path.cwd()):
            assert not (where / PWNED).exists()


OUTCOMES = st.one_of(
    st.just(("unset", None)), st.just(("ok", 0)), st.integers(1, 255).map(lambda c: ("exit", c)),
    st.just(("slow", None)), st.just(("missing", None)), st.just(("stderr", None)), st.just(("huge", None)))


def _command(tmp: Path, kind: str, code):
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    if kind == "ok":
        return notify_script(bin_dir, "ok", tmp / "out.json")
    if kind in ("slow", "missing"):
        return notify_script(bin_dir, kind, tmp / "out.json")
    scripts = {"exit": f"#!/bin/sh\nexit {code}\n",
               "stderr": "#!/bin/sh\nhead -c 100000 /dev/urandom >&2\nexit 5\n",
               "huge": "#!/bin/sh\nhead -c 5000000 /dev/zero | tr '\\0' 'y'\nexit 0\n"}
    path = bin_dir / f"notify-{kind}-{code}"
    path.write_text(scripts[kind])
    path.chmod(0o755)
    return str(path)


@settings(**CLI)
@given(outcome=OUTCOMES)
def test_every_notify_outcome_parks_and_exits_zero(outcome):
    kind, code = outcome
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        repo = spec_repo(tmp / "repo")
        env = clean_env({"CK_CONFIG_HOME": str(tmp / "cfg")})
        if kind != "unset":
            assert set_notify(_command(tmp, kind, code), env=env).returncode == 0
        started = time.monotonic()
        done = park(repo, "2", env=env)
        assert time.monotonic() - started < 15
        assert done.returncode == 0, done.stdout + done.stderr
        result = json.loads(done.stdout)
        assert result["parked"] is True
        expect_sent = kind in ("ok", "huge") or (kind == "exit" and code == 0)
        assert (result["notification"] == "sent") == expect_sent, (outcome, result)
        if kind == "unset":
            assert result["notification"] == "not_configured"
        elif not expect_sent:
            assert result["notification"] == "failed" and result["detail"]
        rep = ck_run_cmd(repo, "worktree", "report", "demo", "--json", env=env)
        assert [e["task"] for e in json.loads(rep.stdout)["needsInput"]] == ["2"]
