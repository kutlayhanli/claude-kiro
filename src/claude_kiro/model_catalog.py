"""OpenCode model rates from the models.dev catalog (issue #9).

`resources/opencode_rates.json` is a committed snapshot of https://models.dev/api.json, cut down to the
OpenCode providers. `ck pricing refresh` regenerates it; the bench and stats pricing read it, so a new or
re-priced model needs a refresh, not a hand edit.

Every model carries all four usage rates (USD per 1M tokens): a number, or None when the catalog does
not list it. None means unknown, never $0: a run that used that kind of token prices as unknown. A model
whose listed costs are all zero is a free tier and prices as $0.00.
"""

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

SOURCE_URL = "https://models.dev/api.json"
PROVIDERS = ("opencode", "opencode-go")
SNAPSHOT_NAME = "opencode_rates.json"
SCHEMA = "ck-model-rates/1"
# models.dev cost keys -> the usage keys of a run record.
COST_KEYS = {"input": "input", "output": "output", "cache_read": "cacheRead", "cache_write": "cacheCreation"}
USAGE_KEYS = ("input", "output", "cacheRead", "cacheCreation")


def _rate(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def model_rates(cost: Any) -> Optional[Dict[str, Any]]:
    """The four usage rates of one catalog `cost` object; None when it has no input or output rate."""
    if not isinstance(cost, Mapping):
        return None
    out: Dict[str, Any] = {usage: _rate(cost.get(key)) for key, usage in COST_KEYS.items()}
    if out["input"] is None or out["output"] is None:
        return None
    listed = [v for v in out.values() if v is not None]
    if listed and all(v == 0 for v in listed):
        out = {k: 0.0 for k in USAGE_KEYS}
        out["free"] = True
    return out


def snapshot_from_api(api: Mapping, fetched: str) -> Dict[str, Any]:
    """The committed snapshot shape from a models.dev api.json document."""
    models: Dict[str, Dict[str, Any]] = {}
    for provider in PROVIDERS:
        entry = api.get(provider) if isinstance(api, Mapping) else None
        listed = entry.get("models") if isinstance(entry, Mapping) else None
        if not isinstance(listed, Mapping):
            continue
        for model_id, model in sorted(listed.items()):
            rates = model_rates(model.get("cost") if isinstance(model, Mapping) else None)
            if rates is not None:
                models[f"{provider}/{model_id}"] = rates
    return {"schema": SCHEMA, "source": SOURCE_URL, "fetched": fetched, "providers": list(PROVIDERS), "models": models}


@lru_cache(maxsize=1)
def load_snapshot() -> Dict[str, Any]:
    try:
        text = resources.files("claude_kiro.resources").joinpath(SNAPSHOT_NAME).read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return {"schema": SCHEMA, "source": SOURCE_URL, "fetched": None, "providers": list(PROVIDERS), "models": {}}
    return data


def catalog_pricing() -> Dict[str, Dict[str, Any]]:
    """provider/model -> rates, from the committed snapshot."""
    return {ref: dict(r) for ref, r in load_snapshot().get("models", {}).items() if isinstance(r, dict)}


def snapshot_path() -> Path:
    """Where `ck pricing refresh` writes by default: the snapshot beside this module."""
    return Path(__file__).resolve().parent / "resources" / SNAPSHOT_NAME


def drift(old: Mapping, new: Mapping) -> Dict[str, Any]:
    """Added, removed and re-priced refs between two snapshots."""
    a, b = old.get("models", {}) or {}, new.get("models", {}) or {}
    changed = sorted(ref for ref in set(a) & set(b) if a[ref] != b[ref])
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)), "changed": changed}
