"""Issue #9: OpenCode rates come from the models.dev snapshot, and every usage key has a rate or is
explicitly unknown (never a silent $0)."""

import json

import pytest
from click.testing import CliRunner

from claude_kiro import bench, model_catalog as mc
from claude_kiro.cli.main import cli

USAGE = 1_000_000


def test_every_snapshot_model_carries_all_four_usage_rates():
    snap = mc.load_snapshot()
    assert snap["models"], "the packaged snapshot is empty"
    assert snap.get("fetched"), "the snapshot records when it was fetched"
    for ref, r in snap["models"].items():
        assert ref.split("/", 1)[0] in mc.PROVIDERS, ref
        assert set(mc.USAGE_KEYS) <= set(r), (ref, r)
        for key in mc.USAGE_KEYS:
            assert r[key] is None or (isinstance(r[key], float) and r[key] >= 0), (ref, key, r[key])
        if r.get("free"):
            assert all(r[k] == 0.0 for k in mc.USAGE_KEYS), (ref, r)


@pytest.mark.parametrize("key", mc.USAGE_KEYS)
def test_every_opencode_ref_prices_a_used_key_or_reports_unknown(key):
    """For each model and each usage key: a known rate prices it, an unknown rate makes the cost
    unknown (None). A paid model never prices non-zero usage of an unknown rate as $0."""
    for ref in bench.OPENCODE_PRICING:
        r = bench.rates(ref)
        assert r is not None, ref
        cost = bench.price_usage(ref, {key: USAGE})
        if r.get("free"):
            assert cost == 0.0, (ref, key, cost)
        elif r.get(key) is None:
            assert cost is None, (ref, key, cost, "an unknown rate must not price as a number")
        else:
            assert cost == pytest.approx(r[key]), (ref, key, cost, r[key])


def test_the_equity_model_prices_cache_reads_from_the_catalog():
    r = bench.rates("opencode-go/muse-spark-1.3-contributor")
    assert (r["input"], r["output"], r["cacheRead"]) == (0.1, 0.2, 0.002)
    assert r["cacheCreation"] is None  # the catalog lists no cache-write rate for it
    usage = {"input": 56_211_068, "output": 1_240_471, "cacheRead": 496_453_645}
    assert bench.price_usage("opencode-go/muse-spark-1.3-contributor", usage) == pytest.approx(6.86, abs=0.01)


def test_snapshot_from_api_keeps_unknowns_unknown_and_skips_other_providers():
    api = {
        "opencode-go": {"models": {
            "paid": {"cost": {"input": 1, "output": 2, "cache_read": 0.5}},
            "free-tier": {"cost": {"input": 0, "output": 0}},
            "no-input": {"cost": {"output": 2}},
            "no-cost": {},
            "bad": {"cost": {"input": "1", "output": 2}},
        }},
        "anthropic": {"models": {"claude-x": {"cost": {"input": 3, "output": 15}}}},
    }
    snap = mc.snapshot_from_api(api, "2026-11-04")
    assert set(snap["models"]) == {"opencode-go/paid", "opencode-go/free-tier"}
    assert snap["models"]["opencode-go/paid"] == {"input": 1.0, "output": 2.0, "cacheRead": 0.5, "cacheCreation": None}
    assert snap["models"]["opencode-go/free-tier"]["free"] is True
    assert snap["fetched"] == "2026-11-04" and snap["schema"] == mc.SCHEMA


def test_pricing_refresh_writes_a_snapshot_and_check_reports_drift(tmp_path):
    api = {"opencode": {"models": {"m": {"cost": {"input": 1, "output": 2}}}}}
    src = tmp_path / "api.json"
    src.write_text(json.dumps(api))
    out = tmp_path / "rates.json"
    runner = CliRunner()
    wrote = runner.invoke(cli, ["pricing", "refresh", "--source", str(src), "--out", str(out), "--date", "2026-11-04"])
    assert wrote.exit_code == 0, wrote.output
    assert json.loads(out.read_text())["models"]["opencode/m"]["input"] == 1.0
    same = runner.invoke(cli, ["pricing", "refresh", "--source", str(src), "--out", str(out), "--check"])
    assert same.exit_code == 0 and "current" in same.output, same.output
    api["opencode"]["models"]["m"]["cost"]["input"] = 3
    src.write_text(json.dumps(api))
    drifted = runner.invoke(cli, ["pricing", "refresh", "--source", str(src), "--out", str(out), "--check"])
    assert drifted.exit_code == 1 and "re-priced" in drifted.output and "changed: opencode/m" in drifted.output, drifted.output
    assert json.loads(out.read_text())["models"]["opencode/m"]["input"] == 1.0  # --check writes nothing


def test_pricing_refresh_refuses_a_catalog_without_opencode_models(tmp_path):
    src = tmp_path / "api.json"
    src.write_text(json.dumps({"anthropic": {"models": {}}}))
    out = tmp_path / "rates.json"
    res = CliRunner().invoke(cli, ["pricing", "refresh", "--source", str(src), "--out", str(out)])
    assert res.exit_code == 2 and not out.exists(), res.output


def test_pricing_show_names_unknown_rates():
    res = CliRunner().invoke(cli, ["pricing", "show", "opencode-go/muse-spark-1.3-contributor"])
    assert res.exit_code == 0 and "cacheCreation unknown" in res.output, res.output
    missing = CliRunner().invoke(cli, ["pricing", "show", "nobody/no-such-model"])
    assert missing.exit_code == 1 and "cost unknown" in missing.output
