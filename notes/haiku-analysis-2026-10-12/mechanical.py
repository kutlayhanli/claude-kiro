#!/usr/bin/env python3
"""Mechanical agents (plan/land/gate): tool-error rates, poll timeouts, ck time vs agent wall, schema/transcription defects."""
import json, glob, os, re, collections, datetime, csv, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
P = os.path.expanduser('~/.claude/projects')
OUT = os.path.dirname(os.path.abspath(__file__))

def ts(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()

RUNDIRS = {
    'bench-v2': P + '/-home-kutlay-Workspaces-sihman-sihman-bench-v2',
    'bench-final': P + '/-home-kutlay-Workspaces-sihman-sihman-bench-final',
    'escalation-pilot': P + '/-home-kutlay-Workspaces-sihman-sihman-bench-escalation',
    'all-haiku': P + '/-home-kutlay-Workspaces-sihman-sihman-shopify--claude-worktrees-ai-first-email-tool-haiku-bench',
    'baseline': P + '/-home-kutlay-Workspaces-sihman-sihman-shopify--claude-worktrees-ai-first-email-tool-plan',
}
SKIP = {'wf_c2db79be-a64'}  # spec-create workflow

def role(label):
    l = label.lower()
    if l.startswith('plan'): return 'plan'
    if l.startswith('land') or l.startswith('merge'): return 'land'
    if 'gate' in l and not l.startswith('fix'): return 'gate'
    return None

rows = []
for run, d in RUNDIRS.items():
    for wd in glob.glob(d + '/*/subagents/workflows/*'):
        if os.path.basename(wd) in SKIP:
            continue
        labels, res = {}, {}
        for l in open(wd + '/journal.jsonl'):
            e = json.loads(l)
            if e['type'] == 'started': labels[e['agentId']] = e['label']
            elif e['type'] == 'result': res[e['agentId']] = e.get('result')
        for a, lab in labels.items():
            r = role(lab)
            if not r: continue
            f = wd + '/agent-' + a + '.jsonl'
            if not os.path.exists(f): continue
            models = collections.Counter(); tss = []; errs = 0; timeouts = 0; bash = 0; sleep_s = 0
            for line in open(f):
                x = json.loads(line)
                if 'timestamp' in x: tss.append(ts(x['timestamp']))
                m = x.get('message', {})
                if x.get('type') == 'assistant':
                    models[m.get('model')] += 1
                    for c in m.get('content', []):
                        if c.get('type') == 'tool_use' and c['name'] == 'Bash':
                            bash += 1
                if isinstance(m.get('content'), list):
                    for c in m['content']:
                        if c.get('type') == 'tool_result':
                            s = c.get('content'); s = s if isinstance(s, str) else json.dumps(s)
                            if c.get('is_error') or s.startswith('Exit code') or 'No such option' in s: errs += 1
                            if 'did not complete within' in s or 'WAIT=124' in s: timeouts += 1
            model = models.most_common(1)[0][0] if models else None
            wall = max(tss) - min(tss)
            rr = res.get(a)
            ck_s = None
            if isinstance(rr, dict) and rr.get('startedAt') and rr.get('finishedAt'):
                try: ck_s = ts(rr['finishedAt']) - ts(rr['startedAt'])
                except Exception: pass
            defect = ''
            if r == 'plan' and isinstance(rr, dict) and any('/' in str(i) for i in rr.get('ready') or []):
                defect = 'path-as-task-id'
            if r == 'land' and isinstance(rr, dict):
                if rr.get('mergedAt', '').endswith('T00:00:00Z') or any(str(t.get('mergedAt', '')).endswith('T00:00:00Z') for t in rr.get('tasks', []) or []):
                    defect = 'invented mergedAt'
                if rr.get('tasks') == [] and not rr.get('error'):
                    defect = 'empty result, error dropped'
            rows.append(dict(run=run, wf=os.path.basename(wd), label=lab, role=r, model=(model or '').replace('claude-', '').replace('-5-5', ''),
                             wall_s=round(wall), ck_s=ck_s, bash=bash, tool_errors=errs, poll_timeouts=timeouts, defect=defect))

with open(OUT + '/mechanical_agents.csv', 'w', newline='') as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

print('| run | role | model | n | median wall s | sum wall min | agents w/ tool error | agents w/ 600s poll timeout | min lost in timeouts (approx) | defects | median ck-reported s (land) |')
print('|---|---|---|---|---|---|---|---|---|---|---|')
g = collections.defaultdict(list)
for r in rows: g[(r['run'], r['role'], r['model'])].append(r)
for k in sorted(g):
    x = g[k]
    dfs = collections.Counter(r['defect'] for r in x if r['defect'])
    cks = [r['ck_s'] for r in x if r['ck_s'] is not None]
    print('| %s | %s | %s | %d | %.0f | %.1f | %d | %d | %.0f | %s | %s |' % (k[0], k[1], k[2], len(x), statistics.median(r['wall_s'] for r in x),
          sum(r['wall_s'] for r in x) / 60, sum(r['tool_errors'] > 0 for r in x), sum(r['poll_timeouts'] > 0 for r in x),
          sum(r['poll_timeouts'] for r in x) * 10, '; '.join('%s x%d' % kv for kv in dfs.items()) or '-',
          '%.0f' % statistics.median(cks) if cks else '-'))
