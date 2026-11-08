"""Red team of 12.5: the Python seam that parses a missing-command message into the `missingCommand` mark.

The JS red team (tests/js/command_deps_redteam.test.mjs) sets the mark by hand; these cases feed the message
forms to `missing_command` itself, so the surface forms are exercised where the text is parsed.
"""
import pytest

from claude_kiro.failures import missing_command

FORMS = [
    ("$ mtr thesis new\nError: No such command 'thesis'.", "mtr thesis new thesis"),
    ("Usage: mtr [OPTIONS]\nError: No such command 'thesis'.", "mtr thesis"),
    ("$ mtr thesis new\nmtr: error: unrecognized arguments: thesis new", None),
    ("mtr: error: argument command: invalid choice: 'thesis' (choose from 'version')", "mtr thesis"),
]


@pytest.mark.parametrize("text,expected", FORMS)
def test_tc114_redteam_message_forms(text, expected):
    got = missing_command(text, ("mtr",))
    if expected is None:
        assert got is None or got.startswith("mtr")
    else:
        assert got == expected


def test_tc114_redteam_unrelated_text_is_not_a_missing_command():
    assert missing_command("assert exit_code == 2  # No such command text in a comment", ("mtr",)) is None
    assert missing_command("", ("mtr",)) is None
