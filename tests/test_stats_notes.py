"""Interactive issues note of `ck stats export --note` (TC-55..TC-58), driven under a real pty.

Only `claude` (the drafter) and `$EDITOR` are fakes; export, leak check and file writes are the real ck.
"""

import contextlib
import os
import re
from pathlib import Path

from stats_helpers import (
    CK, all_bytes, ck_sub, fake_claude_drafter, fake_editor, fresh_env, kiro_checkout, run_tty, seeded_home, stats,
)

RUN_ID = "wf_zephyr-impl-1"
DRAFT = "Resolver needed 3 rounds on zephyr-payroll lockfile"
CLEAN = "Resolver needed 3 rounds on a lockfile conflict"
# any line naming all three choices, in any order and with any separators
PROMPT = r"(?im)^(?=.*accept)(?=.*edit)(?=.*discard).*$"


@contextlib.contextmanager
def note_setup(tmp_path, draft=DRAFT, editor_text=CLEAN):
    """A collected seeded record, a kiro checkout, a fake drafter and a fake editor -> (env, checkout, drafter)."""
    with fresh_env() as base:
        home, _terms = seeded_home(base.root / "seed")
        bin_dir = base.root / "bin"
        bin_dir.mkdir()
        drafter = fake_claude_drafter(bin_dir, draft)
        editor = fake_editor(bin_dir, editor_text)
        env = {
            **base,
            "CLAUDE_CONFIG_DIR": str(home.root),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "EDITOR": str(editor),
            "VISUAL": str(editor),
        }
        checkout, _bare = kiro_checkout(base.root)
        collected = stats("collect", "--run", RUN_ID, env=env)
        assert collected.exit_code == 0, collected.output
        yield env, checkout, drafter, base


def files_in(checkout: Path, suffix: str):
    """Files the export wrote: only under <checkout>/analytics/runs (the checkout's own README.md etc. are not ours)."""
    root = checkout / "analytics" / "runs"
    return sorted(root.rglob(f"*{suffix}")) if root.exists() else []


def export_argv(checkout, *flags):
    return [*CK, "stats", "export", "--to", str(checkout), *flags]


def test_tc55_edit_then_accept_writes_exactly_the_edited_note(tmp_path):
    with note_setup(tmp_path) as (env, checkout, drafter, base):
        code, transcript = run_tty(
            export_argv(checkout, "--note"), env=env, cwd=checkout,
            script=[(PROMPT, "e\n"), (PROMPT, "a\n")],
        )
        assert code == 0, transcript
        assert "zephyr-payroll" in transcript  # the draft is shown, with its hit
        assert re.search(r"\.md: line \d+: ", transcript), transcript  # the hit is reported
        notes = files_in(checkout, ".md")
        records = files_in(checkout, ".json")
        assert len(notes) == 1 and len(records) == 1, transcript
        assert notes[0].read_text() == CLEAN
        assert notes[0].stem == records[0].stem and notes[0].parent == records[0].parent
        calls = drafter.calls()
        assert calls and all("-p" in c["argv"] or "--print" in c["argv"] for c in calls)
        assert b"zephyr-payroll" not in all_bytes(checkout)


def test_tc56_edited_text_still_confidential_is_not_written(tmp_path):
    with note_setup(tmp_path, editor_text="Resolver needed 3 rounds at acme-corp") as (env, checkout, drafter, base):
        code, transcript = run_tty(
            export_argv(checkout, "--note"), env=env, cwd=checkout,
            script=[(PROMPT, "e\n"), (PROMPT, "a\n")],
        )
        assert files_in(checkout, ".md") == [], transcript
        assert len(files_in(checkout, ".json")) == 1, transcript
        prompts = list(re.finditer(PROMPT, transcript))
        assert len(prompts) >= 2, transcript
        assert re.search(r"\.md: line 1: ", transcript[prompts[-1].end():]), transcript
        assert b"acme-corp" not in all_bytes(checkout)


def test_tc57_note_is_withheld_without_interactive_approval(tmp_path):
    with note_setup(tmp_path, draft=CLEAN) as (env, checkout, drafter, base):
        out = ck_sub("stats", "export", "--to", str(checkout), "--note", env=env, cwd=checkout)
        assert out.exit_code == 0, out.output
        assert re.search(r"(?i)withheld", out.output) and re.search(r"(?i)interactive", out.output), out.output
        assert files_in(checkout, ".md") == []
        records = files_in(checkout, ".json")
        assert len(records) == 1 and b"issues" in records[0].read_bytes()
        assert drafter.calls() == []
        assert CLEAN.encode() not in all_bytes(checkout, base.xdg)

    with note_setup(tmp_path, draft=CLEAN) as (env, checkout, drafter, base):
        code, transcript = run_tty(export_argv(checkout, "--note", "--yes"), env=env, cwd=checkout)
        assert code == 0, transcript
        assert re.search(r"(?i)withheld", transcript) and re.search(r"(?i)interactive", transcript), transcript
        assert files_in(checkout, ".md") == []
        records = files_in(checkout, ".json")
        assert len(records) == 1 and b"issues" in records[0].read_bytes()
        assert drafter.calls() == []
        assert CLEAN.encode() not in all_bytes(checkout, base.xdg)


def test_tc58_discard_exports_the_record_without_a_note(tmp_path):
    with note_setup(tmp_path) as (env, checkout, drafter, base):
        code, transcript = run_tty(
            export_argv(checkout, "--note"), env=env, cwd=checkout,
            script=[(PROMPT, "d\n")],
        )
        assert code == 0, transcript
        assert files_in(checkout, ".md") == []
        assert len(files_in(checkout, ".json")) == 1
        assert b"zephyr-payroll" not in all_bytes(checkout)
