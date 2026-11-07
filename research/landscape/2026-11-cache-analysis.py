"""Cache write/read analysis of a ck workflow run (research 2026-11-07).

Usage:
    python3 -I research/landscape/2026-11-cache-analysis.py \
        ~/.claude/projects/<proj>/<sid>/subagents/workflows/wf_<id>

Prints (1) cache writes/reads per workflow phase with the median first request,
(2) the dollar split at 5.5-generation list prices, (3) the share of cache writes
made on each agent's first request. Usage rows are deduplicated by requestId.
"""
import collections
import glob
import json
import sys

# 5.5-generation list prices per MTok: input, cache read, 5-min cache write, output
PRICES = {"opus": (4, 0.20, 5, 20), "sonnet": (2, 0.10, 2.5, 10), "haiku": (0.10, 0.01, 0.125, 0.5)}


def requests(transcript):
    """Yield (model, usage) once per API request in an agent transcript."""
    seen = set()
    for line in open(transcript):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") or {}
        usage, rid = msg.get("usage"), row.get("requestId")
        if not usage or rid in seen:
            continue
        seen.add(rid)
        yield msg.get("model", ""), usage


def main(run_dir):
    phases = collections.defaultdict(lambda: [0, 0, 0, 0])  # agents, requests, write, read
    firsts = collections.defaultdict(list)
    dollars = collections.Counter()
    first_writes = total_writes = 0
    for meta_path in glob.glob(run_dir + "/*.meta.json"):
        meta = json.load(open(meta_path))
        phase = meta.get("workflowPhase") or (meta.get("description") or "?").split(":")[0]
        agg = phases[phase]
        agg[0] += 1
        for i, (model, u) in enumerate(requests(meta_path.replace(".meta.json", ".jsonl"))):
            w, r = u.get("cache_creation_input_tokens", 0), u.get("cache_read_input_tokens", 0)
            agg[1] += 1
            agg[2] += w
            agg[3] += r
            total_writes += w
            if i == 0:
                firsts[phase].append((w, r))
                first_writes += w
            fam = next((f for f in PRICES if f in model), None)
            if fam:
                p = PRICES[fam]
                dollars["input"] += u.get("input_tokens", 0) * p[0] / 1e6
                dollars["cache_read"] += r * p[1] / 1e6
                dollars["cache_write"] += w * p[2] / 1e6
                dollars["output"] += u.get("output_tokens", 0) * p[3] / 1e6

    print(f"{'phase':28} agents  reqs  write(M)  read(M)  write%  first req (write/read)")
    for phase, (n, reqs, w, r) in sorted(phases.items(), key=lambda kv: -kv[1][2]):
        f = sorted(firsts[phase])
        mid = f[len(f) // 2] if f else (0, 0)
        print(f"{phase[:28]:28} {n:6} {reqs:5} {w/1e6:9.2f} {r/1e6:8.2f} {100*w/max(w+r,1):6.1f}  {mid[0]//1000}k/{mid[1]//1000}k")

    total = sum(dollars.values())
    print()
    for k, v in dollars.most_common():
        print(f"{k:12} ${v:8.2f}  {100*v/max(total,1e-9):5.1f}%")
    print(f"total ${total:.2f} at 5.5-generation list prices")
    print(f"\nfirst-request writes {first_writes/1e6:.2f}M of {total_writes/1e6:.2f}M "
          f"= {100*first_writes/max(total_writes,1):.0f}%")


if __name__ == "__main__":
    main(sys.argv[1])
