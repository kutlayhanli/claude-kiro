"""The needs-input notification: runs the command I configured as `run.notify_command`.

The message names the spec and the task only; it never carries the quoted test or requirement (S12). The command
gets a Notification-hook-shaped JSON on stdin and the text in CK_NOTIFY_MESSAGE, and never sees a shell command line
built from either, so quoted text cannot inject anything. `send` never raises.
"""

import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

NOTIFY_HINT = ("no notification command is set; to get a push when a task needs you, run "
               "`ck agents set run.notify_command \"<command>\"` (it receives the message on stdin as JSON and in CK_NOTIFY_MESSAGE)")
_DETAIL_LIMIT = 300


@dataclass(frozen=True)
class NotifyOutcome:
    status: str  # "sent" | "not_configured" | "failed"
    detail: str = ""


def notify_message(spec: str, task: str) -> str:
    return f"ck: {spec} task {task} needs a decision (parked as needs input)"


def _tail(text: str) -> str:
    text = " ".join((text or "").split())
    return text[-_DETAIL_LIMIT:]


def send(command: Optional[str], message: str, cwd: Path, timeout: float = 10.0, *,
         notification_type: str = "ck_needs_input") -> NotifyOutcome:
    """Run `command` (shell=True, in `cwd`) with the message; kill it after `timeout` seconds.

    `notification_type` goes into the stdin JSON and into CK_NOTIFY_TYPE. ck sends every type (`ck_needs_input`,
    `ck_eta_slip`, `ck_eta_blocked`, `ck_run_done`) the same way; a quieter route for done or slip notices needs the
    user's notify command to branch on CK_NOTIFY_TYPE.
    """
    if not command or not str(command).strip():
        return NotifyOutcome("not_configured", NOTIFY_HINT)
    payload = json.dumps({"hook_event_name": "Notification", "notification_type": notification_type,
                          "title": "ck", "message": message})
    env = dict(os.environ)
    env["CK_NOTIFY_MESSAGE"] = message
    env["CK_NOTIFY_TYPE"] = notification_type
    env.pop("CK_RUN_NOTIFY", None)
    try:
        proc = subprocess.Popen(command, shell=True, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                errors="replace", start_new_session=True)
    except Exception as exc:  # noqa: BLE001 - never raises
        return NotifyOutcome("failed", _tail(f"could not start the notify command: {exc}"))
    try:
        try:
            _, err = proc.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            return NotifyOutcome("failed", f"notify command timed out after {timeout:g} s")
        except Exception as exc:  # noqa: BLE001
            _kill(proc)
            return NotifyOutcome("failed", _tail(f"notify command error: {exc}"))
    finally:
        if proc.poll() is None:
            _kill(proc)
    if proc.returncode != 0:
        extra = _tail(err)
        suffix = f": {extra}" if extra else ""
        return NotifyOutcome("failed", f"notify command exit {proc.returncode}{suffix}")
    return NotifyOutcome("sent", "")


def _kill(proc: "subprocess.Popen") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate(timeout=2)
    except Exception:  # noqa: BLE001
        pass
