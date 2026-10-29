"""Run analytics: collect, share and submit anonymised `ck` run records."""

import importlib

SCHEMA_LOCAL = "ck-run-local/1"
SCHEMA_SHARED = "ck-run-analytics/1"

_EXPORTS = {
    "ALLOWED_WORKFLOWS": "runfile",
    "ALLOWLISTED_WORKFLOWS": "runfile",
    "is_ck_workflow": "runfile",
    "ROLES": "roles",
    "parse_label": "roles",
    "build_shareable": "share",
    "SHAREABLE_FIELDS": "share",
    "enum_value": "share",
    "model_family": "share",
    "MODEL_FAMILIES": "share",
    "OUTCOMES": "share",
    "VERDICTS": "share",
    "EFFORTS": "share",
    "STACK_HINTS": "share",
    "month_of": "share",
    "valid_machine_label": "claude_kiro.agents",
    "ISSUE_CODES": "issues",
    "issue_codes": "issues",
    "confidential_terms": "leak",
    "leak_scan": "leak",
    "is_target_repo": "submit",
    "pr_text": "submit",
    "pseudonym": "collect",
    "SPEC_AGENT_TYPES": "roles",
    "AGENT_CREATE_STEPS": "roles",
    "is_agent_run_id": "agentrun",
    "new_agent_run_id": "agentrun",
    "parse_agent_description": "agentrun",
    "implement_outcome": "agentrun",
    "implement_summary": "agentrun",
    "open_manifest_due": "agentrun",
    "classify_manifest": "agentrun",
    "group_backfill": "backfill",
}

__all__ = ["SCHEMA_LOCAL", "SCHEMA_SHARED", *_EXPORTS]


def __getattr__(name):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = target if "." in target else f"{__name__}.{target}"
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
