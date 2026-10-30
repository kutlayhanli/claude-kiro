"""Red team for requirement 7.2: the notification SHALL NOT carry the quoted test or requirement text.

Every case parks through the real CLI with a real subprocess notify command, then checks all three channels the command
sees (stdin, CK_NOTIFY_MESSAGE, argv): no line and no 12+ character substring of any quote, and no injected command ran.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from guard_helpers import ck_run_cmd, notify_script, set_notify
from stats_helpers import spec_repo
from test_guard_park import clean_env, park, report

PWNED = "PWNED_marker_7f3a"
WINDOW = 12

ADVERSARIAL = {
    "quotes": ("it's \"quoted\" `tick` text-alpha", "req \"double\" 'single' text-bravo"),
    "subst": (f"$(touch {PWNED}) and more words here", f"`touch {PWNED}` plus other words"),
    "metachars": (f"; touch {PWNED}; echo done-charlie", f"&& touch {PWNED} || true | cat <<EOF-delta"),
    "newlines": ("first line of the test\nsecond line of the test\r\nthird", "req line one here\nreq line two here"),
    "ansi": ("\x1b[31mred zulu text\x1b[0m\x1b]0;title\x07", "\x1b[2Jclear-screen requirement text"),
    "homoglyph": ("tеst with cyrillic а and ‮RTL override", "рequirement with ＄（touch）"),
    "long": ("x" * 5000 + " end-of-test", "long-req " * 800),
    "braces": ("${IFS}touch${IFS}" + PWNED + " {a,b}", "$((1+1)) %s %d {0} {{x}}"),
}


def leaks(channel: str, quote: str) -> bool:
    if any(line.strip() and line.strip() in channel and len(line.strip()) >= WINDOW for line in quote.splitlines()):
        return True
    return any(quote[i:i + WINDOW] in channel for i in range(0, max(len(quote) - WINDOW + 1, 0)))


def channels(sent: dict) -> list:
    return [sent["stdin"], sent["env"].get("CK_NOTIFY_MESSAGE", ""), "\n".join(sent["argv"])]


def setup(tmp: Path, spec: str = "demo"):
    repo = spec_repo(tmp / "repo", spec=spec)
    out = tmp / "notify.json"
    assert set_notify(notify_script(tmp / "bin", "ok", out)).returncode == 0
    return repo, out


def assert_clean(repo: Path, out: Path, tmp: Path, *quotes: str) -> None:
    sent = json.loads(out.read_text())
    for channel in channels(sent):
        for quote in quotes:
            assert not leaks(channel, quote), (channel[:200], quote[:80])
    for where in (repo, tmp, Path.cwd()):
        assert not (where / PWNED).exists()


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_quoted_text_never_reaches_the_notify_command(tmp_path, name):
    test_text, req_text = ADVERSARIAL[name]
    repo, out = setup(tmp_path)
    note = {"file": "tests/test_a.py", "kind": "test", "test": test_text, "requirement": req_text,
            "decision": "d-" + test_text[:40]}
    done = park(repo, "2", note)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["notification"] == "sent"
    assert_clean(repo, out, tmp_path, test_text, req_text, note["decision"])


@pytest.mark.parametrize("field", ["file", "kind", "decision"])
def test_quotes_in_other_fields_stay_out_too(tmp_path, field):
    repo, out = setup(tmp_path)
    hostile = f"it's \"hostile\" $(touch {PWNED}) `touch {PWNED}` value-{field}-victor"
    note = {"file": "tests/test_a.py", "kind": "test", "test": "plain test text for the case",
            "requirement": "plain requirement text for the case", "decision": "plain decision text for the case"}
    note[field] = hostile
    done = park(repo, "2", note)
    assert done.returncode == 0, done.stdout + done.stderr
    assert_clean(repo, out, tmp_path, hostile, note["test"], note["requirement"])


@pytest.mark.parametrize("spec", ["it's-a-spec", 'say-"hi"', "a$(touch-" + PWNED + ")", "back`tick`"])
def test_hostile_spec_names_never_inject(tmp_path, spec):
    repo, out = setup(tmp_path, spec=spec)
    test_text, req_text = ADVERSARIAL["quotes"]
    done = park(repo, "2", {"file": "f", "kind": "test", "test": test_text, "requirement": req_text, "decision": "d"}, spec=spec)
    assert done.returncode in (0, 2, 3), done.stdout + done.stderr
    for where in (repo, tmp_path, Path.cwd()):
        assert not (where / PWNED).exists()
    if out.exists():
        for channel in channels(json.loads(out.read_text())):
            assert not leaks(channel, test_text) and not leaks(channel, req_text)


@pytest.mark.parametrize("task", ["2'x", '2"x', "2;touch " + PWNED, "$(touch " + PWNED + ")", "`touch " + PWNED + "`", "2\nx"])
def test_hostile_task_ids_never_inject(tmp_path, task):
    repo, out = setup(tmp_path)
    test_text, req_text = ADVERSARIAL["subst"]
    done = park(repo, task, {"file": "f", "kind": "test", "test": test_text, "requirement": req_text, "decision": "d"})
    assert done.returncode in (0, 2, 3), done.stdout + done.stderr
    for where in (repo, tmp_path, Path.cwd()):
        assert not (where / PWNED).exists()
    if out.exists():
        for channel in channels(json.loads(out.read_text())):
            assert not leaks(channel, test_text) and not leaks(channel, req_text)


def test_a_reread_park_note_fed_into_a_second_park_stays_clean(tmp_path):
    repo, out = setup(tmp_path)
    test_text, req_text = ADVERSARIAL["subst"]
    first = park(repo, "2", {"file": "f", "kind": "test", "test": test_text, "requirement": req_text, "decision": "d"})
    assert first.returncode == 0
    entry = report(repo)["needsInput"][0]
    shown = ck_run_cmd(repo, "worktree", "report", "demo", env=clean_env()).stdout
    out.unlink()
    second = park(repo, "3", {"file": entry["file"], "kind": entry["kind"], "test": entry["test"],
                              "requirement": entry["requirement"], "decision": entry["decision"] + shown[:300]})
    assert second.returncode == 0, second.stdout + second.stderr
    assert_clean(repo, out, tmp_path, test_text, req_text, shown[:300])
    sent = json.loads(out.read_text())
    assert "task 3" in json.loads(sent["stdin"])["message"]
