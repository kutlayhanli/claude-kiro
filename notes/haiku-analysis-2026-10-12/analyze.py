#!/usr/bin/env python3
"""Per run x role x model analysis of ck spec-implement workflow transcripts.

Read-only over ~/.claude/projects and /tmp/claude-1000 workflow output files.
Writes CSV/markdown tables next to this script.
"""
import json, glob, os, re, statistics, collections, csv, datetime

P = os.path.expanduser('~/.claude/projects')
T = '/tmp/claude-1000'
OUT = os.path.dirname(os.path.abspath(__file__))

PLAN = P + '/-home-kutlay-Workspaces-sihman-sihman-shopify--claude-worktrees-ai-first-email-tool-plan/c23d1f18-4a88-4262-a632-2ded5e727466'
RUNS = {
    'bench-v2 (ck0.9.1, 10-12)': dict(
        session=P + '/-home-kutlay-Workspaces-sihman-sihman-bench-v2/1216e2e1-4dae-4b75-ace2-9058dca44219',
        wfs=['wf_24af0913-08d'], outs=[T + '/-home-kutlay-Workspaces-sihman-sihman-bench-v2/1216e2e1-4dae-4b75-ace2-9058dca44219/tasks/w7n72rn4u.output']),
    'bench-final (ck0.9.0, 10-12)': dict(
        session=P + '/-home-kutlay-Workspaces-sihman-sihman-bench-final/80110ae8-c269-49a3-8489-e343b811a1d8',
        wfs=['wf_8066254e-f3c'], outs=[T + '/-home-kutlay-Workspaces-sihman-sihman-bench-final/80110ae8-c269-49a3-8489-e343b811a1d8/tasks/wll8glykl.output']),
    'escalation-pilot (10-10)': dict(
        session=P + '/-home-kutlay-Workspaces-sihman-sihman-bench-escalation/3e4a2b38-c317-4b96-827e-7b023e7a4186',
        wfs=['wf_62b6635c-0f0'], outs=[T + '/-home-kutlay-Workspaces-sihman-sihman-bench-escalation/3e4a2b38-c317-4b96-827e-7b023e7a4186/tasks/ws32lekhw.output']),
    'all-haiku (10-10)': dict(
        session=P + '/-home-kutlay-Workspaces-sihman-sihman-shopify--claude-worktrees-ai-first-email-tool-haiku-bench/0821d1e7-23f4-46fd-85d5-2145afaee633',
        wfs=['wf_d7be5f0f-b06'], outs=[T + '/-home-kutlay-Workspaces-sihman-sihman-shopify--claude-worktrees-ai-first-email-tool-haiku-bench/0821d1e7-23f4-46fd-85d5-2145afaee633/tasks/w1i1ll2ya.output']),
    'baseline sonnet+opus (10-10..12)': dict(
        session=PLAN,
        wfs=['wf_115efb28-519', 'wf_77d681b0-926', 'wf_af726446-77a', 'wf_5f73eb7e-51b', 'wf_3a5fc7b1-aa9',
             'wf_ad8f61f7-980', 'wf_80af0ba2-9a7', 'wf_5d73ff4b-551', 'wf_8e322fbe-289'],
        outs=[T + '/-home-kutlay-Workspaces-sihman-sihman-shopify/c23d1f18-4a88-4262-a632-2ded5e727466/tasks/' + x + '.output'
              for x in ['wz94nv7ft', 'w4vqhm5nv', 'wcgpfnlvo', 'w9n5yfomq', 'w4qmesd0i', 'w3cn6oxp0', 'wudhq5mc9', 'wixns4a2i', 'wojxa1yks']]),
}

# $/MTok list prices (claude-api skill table, cached 2026-10-06).
# cache write 5m = 1.25x input, 1h = 2x input. Cache read: Opus 5.5 $0.20 and Sonnet 5.5 $0.20 (stated);
# Haiku 5.5 not stated -> assume 0.1x input. Haiku: prompts >100K tokens billed $0.50/$2.50 (applied per request).
PRICES = {
    'claude-opus-5-5': dict(inp=4.0, out=20.0, cr=0.20),
    'claude-sonnet-5-5': dict(inp=2.0, out=10.0, cr=0.20),
    'claude-haiku-5-5': dict(inp=0.10, out=0.50, cr=0.01),
}
HAIKU_LONG = dict(inp=0.50, out=2.50, cr=0.05)
SHORT = {'claude-opus-5-5': 'opus', 'claude-sonnet-5-5': 'sonnet', 'claude-haiku-5-5': 'haiku'}


def ts(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def msg_cost(model, u):
    p = PRICES.get(model)
    if not p:
        return 0.0
    inp = u.get('input_tokens', 0) or 0
    cr = u.get('cache_read_input_tokens', 0) or 0
    cc = u.get('cache_creation_input_tokens', 0) or 0
    det = u.get('cache_creation') or {}
    c1h = det.get('ephemeral_1h_input_tokens', 0) or 0
    c5m = cc - c1h
    if model == 'claude-haiku-5-5' and inp + cr + cc > 100_000:
        p = HAIKU_LONG
    return (inp * p['inp'] + u.get('output_tokens', 0) * p['out'] + cr * p['cr'] + c5m * p['inp'] * 1.25 + c1h * p['inp'] * 2) / 1e6


def parse_transcript(path, t0=None, t1=None):
    """Return per-message list (deduped by message id) and line timestamps."""
    msgs = collections.OrderedDict()
    prev_nonassist = None  # timestamp of last user/tool_result line (request start proxy)
    first = last = None
    for line in open(path):
        try:
            x = json.loads(line)
        except Exception:
            continue
        t = x.get('timestamp')
        if not t:
            continue
        tt = ts(t)
        if t0 and (tt < t0 or tt > t1):
            continue
        first = tt if first is None else min(first, tt)
        last = tt if last is None else max(last, tt)
        typ = x.get('type')
        if typ == 'user':
            prev_nonassist = tt
        elif typ == 'assistant':
            m = x.get('message', {})
            mid = m.get('id') or x.get('uuid')
            u = m.get('usage') or {}
            final = 'iterations' in u or 'output_tokens_details' in u or 'server_tool_use' in u
            e = msgs.get(mid)
            if e is None:
                e = msgs[mid] = dict(model=m.get('model'), usage=u, start=prev_nonassist, end=tt, final=final)
            else:
                if (u.get('output_tokens', 0) or 0) >= (e['usage'].get('output_tokens', 0) or 0):
                    e['usage'] = u
                e['end'] = tt
                e['final'] = e['final'] or final
    return list(msgs.values()), first, last


def role_of(label):
    l = label.lower()
    if l.startswith('plan'):
        return 'plan'
    if l.startswith('land') or l.startswith('merge'):
        return 'land'
    if 'gate' in l and not l.startswith('fix'):
        return 'gate'
    if l.startswith('review'):
        return 'review'
    if l.startswith('fix') or l.startswith('re-check'):
        return 'fix'
    if l.startswith('resolve'):
        return 'resolve'
    if l.startswith('task'):
        kind = 'test' if '[test]' in l else 'impl'
        if ' revise' in l:
            return kind + '-revise'
        if ' retry' in l:
            return kind + '-retry'
        if ' resume' in l:
            return kind + '-resume'
        return kind
    return 'other'


def task_id(label):
    m = re.search(r'task-(\d+)', label) or re.search(r'^(?:task|review|land|merge|fix)\s+(\d+)', label)
    return m.group(1) if m else None


agents = []      # one row per agent
outcomes = []    # per run per task
wp_cmp = []
main_rows = []
incidents = collections.defaultdict(list)

for run, cfg in RUNS.items():
    wp_by_agent = {}
    out_results = []
    for o in cfg['outs']:
        if os.path.exists(o):
            d = json.load(open(o))
            for a in d.get('workflowProgress') or []:
                if a.get('type') == 'workflow_agent':
                    wp_by_agent[a['agentId']] = a
            r = d.get('result')
            if isinstance(r, str):
                try:
                    r = json.loads(r)
                except Exception:
                    r = None
            out_results.append(r or {})
    run_windows = []
    for wf in cfg['wfs']:
        wd = cfg['session'] + '/subagents/workflows/' + wf
        jl = [json.loads(l) for l in open(wd + '/journal.jsonl')]
        labels = {}
        results = {}
        for e in jl:
            if e['type'] == 'started':
                labels[e['agentId']] = e['label']
            elif e['type'] == 'result':
                results[e['agentId']] = e.get('result')
        wfirst = wlast = None
        for aid, label in labels.items():
            f = wd + '/agent-' + aid + '.jsonl'
            if not os.path.exists(f):
                continue
            msgs, first, last = parse_transcript(f)
            if first is None:
                continue
            wfirst = first if wfirst is None else min(wfirst, first)
            wlast = last if wlast is None else max(wlast, last)
            models = collections.Counter(m['model'] for m in msgs if m['model'] in PRICES)
            model = models.most_common(1)[0][0] if models else None
            u_tot = collections.Counter()
            cost = 0.0
            gen_out = gen_t = 0.0
            for m in msgs:
                for k in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
                    u_tot[k] += m['usage'].get(k, 0) or 0
                cost += msg_cost(m['model'], m['usage'])
                if m['final'] and m['start'] and m['end'] > m['start']:
                    gen_out += m['usage'].get('output_tokens', 0)
                    gen_t += m['end'] - m['start']
            # naive method: sum output / sum gaps between consecutive assistant message ends
            ends = sorted(m['end'] for m in msgs)
            naive_t = sum(b - a for a, b in zip(ends, ends[1:]))
            wp = wp_by_agent.get(aid, {})
            res = results.get(aid)
            row = dict(run=run, wf=wf, agent=aid, label=label, role=role_of(label), task=task_id(label),
                       model=SHORT.get(model, model), turns=len(msgs), wall_s=last - first,
                       wp_duration_s=(wp.get('durationMs') or 0) / 1000, wp_tokens=wp.get('tokens'),
                       input=u_tot['input_tokens'], output=u_tot['output_tokens'],
                       cache_read=u_tot['cache_read_input_tokens'], cache_write=u_tot['cache_creation_input_tokens'],
                       cost=cost, tps=(gen_out / gen_t if gen_t else None), gen_s=gen_t,
                       naive_tps=(u_tot['output_tokens'] / naive_t if naive_t else None),
                       final_frac=(sum(m['final'] for m in msgs) / len(msgs) if msgs else 0),
                       start=first, end=last, result=res)
            agents.append(row)
        if wfirst:
            run_windows.append((wf, wfirst, wlast))
    # orchestrator main session, only inside workflow windows
    sess = cfg['session'] + '.jsonl'
    for wf, a, b in run_windows:
        msgs, first, last = parse_transcript(sess, a - 60, b + 120)
        models = collections.Counter(m['model'] for m in msgs if m['model'] in PRICES)
        model = models.most_common(1)[0][0] if models else None
        cost = sum(msg_cost(m['model'], m['usage']) for m in msgs)
        gen_out = sum(m['usage'].get('output_tokens', 0) for m in msgs if m['final'] and m['start'] and m['end'] > m['start'])
        gen_t = sum(m['end'] - m['start'] for m in msgs if m['final'] and m['start'] and m['end'] > m['start'])
        agents.append(dict(run=run, wf=wf, agent='main', label='orchestrator main session', role='orchestrator', task=None,
                           model=SHORT.get(model, model), turns=len(msgs), wall_s=b - a, wp_duration_s=None, wp_tokens=None,
                           input=sum(m['usage'].get('input_tokens', 0) for m in msgs),
                           output=sum(m['usage'].get('output_tokens', 0) for m in msgs),
                           cache_read=sum(m['usage'].get('cache_read_input_tokens', 0) or 0 for m in msgs),
                           cache_write=sum(m['usage'].get('cache_creation_input_tokens', 0) or 0 for m in msgs),
                           cost=cost, tps=gen_out / gen_t if gen_t else None, gen_s=gen_t, naive_tps=None, final_frac=None,
                           start=a, end=b, result=None))
    RUNS[run]['windows'] = run_windows
    RUNS[run]['out_results'] = out_results

# ---------- tables ----------
def med(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None

ROLE_ORDER = ['impl', 'impl-retry', 'impl-revise', 'impl-resume', 'test', 'test-retry', 'test-revise', 'test-resume',
              'review', 'fix', 'resolve', 'plan', 'land', 'gate', 'orchestrator', 'other']

with open(OUT + '/agents.csv', 'w', newline='') as fh:
    cols = ['run', 'wf', 'agent', 'label', 'role', 'task', 'model', 'turns', 'wall_s', 'wp_duration_s', 'gen_s', 'tps', 'naive_tps',
            'final_frac', 'input', 'output', 'cache_read', 'cache_write', 'wp_tokens', 'cost']
    w = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore')
    w.writeheader()
    for a in agents:
        w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in a.items() if k in cols})

# role x model (+ per run)
def group_table(keyf):
    g = collections.defaultdict(list)
    for a in agents:
        g[keyf(a)].append(a)
    return g

lines = []
def emit(s=''):
    lines.append(s)

def table(g, title):
    emit('## ' + title)
    emit('| key | n | median tok/s | p25-p75 tok/s | median wall s | median turns | sum wall min | sum $ | $/agent | cache-read share of input | output tok |')
    emit('|---|---|---|---|---|---|---|---|---|---|---|')
    for k in sorted(g, key=lambda k: tuple(str(x) for x in k)):
        v = g[k]
        tps = sorted(x['tps'] for x in v if x['tps'])
        q = (tps[len(tps) // 4], tps[3 * len(tps) // 4]) if len(tps) >= 4 else (None, None)
        tot_in = sum(x['input'] + x['cache_read'] + x['cache_write'] for x in v)
        cr = sum(x['cache_read'] for x in v)
        cost = sum(x['cost'] for x in v)
        emit('| %s | %d | %s | %s | %s | %s | %.1f | %.2f | %.3f | %s | %d |' % (
            ' / '.join(str(x) for x in k), len(v), '%.0f' % med([x['tps'] for x in v]) if med([x['tps'] for x in v]) else '-',
            '%.0f-%.0f' % q if q[0] else '-', '%.0f' % med([x['wall_s'] for x in v]), '%.0f' % med([x['turns'] for x in v]),
            sum(x['wall_s'] for x in v) / 60, cost, cost / len(v), '%.1f%%' % (100 * cr / tot_in) if tot_in else '-',
            sum(x['output'] for x in v)))
    emit()

table(group_table(lambda a: (a['role'], a['model'])), 'All runs: role x model')
table(group_table(lambda a: (a['run'], a['role'], a['model'])), 'Per run: role x model')
table(group_table(lambda a: (a['model'],)), 'Per model (all roles)')

# cost by run, and workflowProgress comparison
emit('## Cost and token totals per run')
emit('| run | agents | sum $ (transcript) | of which orchestrator main | transcript tokens (in+out+cr+cw) | workflowProgress tokens (sum per agent) | output file totalTokens | wp/transcript |')
emit('|---|---|---|---|---|---|---|---|')
for run, cfg in RUNS.items():
    v = [a for a in agents if a['run'] == run]
    sub = [a for a in v if a['agent'] != 'main']
    tt = sum(a['input'] + a['output'] + a['cache_read'] + a['cache_write'] for a in sub)
    wpt = sum(a['wp_tokens'] or 0 for a in sub)
    tot = 0
    for o in cfg['outs']:
        if os.path.exists(o):
            tot += json.load(open(o)).get('totalTokens') or 0
    emit('| %s | %d | %.2f | %.2f | %.1fM | %.1fM | %.1fM | %.2f |' % (run, len(sub), sum(a['cost'] for a in v),
         sum(a['cost'] for a in v if a['agent'] == 'main'), tt / 1e6, wpt / 1e6, tot / 1e6, wpt / tt if tt else 0))
emit()

# where time goes: agent-seconds and critical-path-ish share by role class
CLASS = {'plan': 'mechanical: plan', 'land': 'mechanical: land', 'gate': 'mechanical: gate',
         'review': 'review', 'orchestrator': 'orchestrator'}
def cls(role):
    if role in CLASS:
        return CLASS[role]
    if role.startswith('impl') or role.startswith('test'):
        return 'implementation (incl. retries/revisions)'
    return 'fix/resolve/other'

emit('## Where time goes (agent wall-clock seconds, summed; and serial share = union of intervals)')
emit('| run | class | n | agent-min | share of agent-min | union-min (wall covered) | share of run wall | $ | share of $ |')
emit('|---|---|---|---|---|---|---|---|---|')
def union(iv):
    iv = sorted(iv)
    tot = 0
    cs = ce = None
    for s, e in iv:
        if cs is None or s > ce:
            if cs is not None:
                tot += ce - cs
            cs, ce = s, e
        else:
            ce = max(ce, e)
    if cs is not None:
        tot += ce - cs
    return tot
for run, cfg in RUNS.items():
    v = [a for a in agents if a['run'] == run and a['agent'] != 'main']
    runwall = sum(b - a for _, a, b in cfg['windows'])
    totsec = sum(a['wall_s'] for a in v)
    totc = sum(a['cost'] for a in v)
    g = collections.defaultdict(list)
    for a in v:
        g[cls(a['role'])].append(a)
    for k in sorted(g):
        x = g[k]
        u = union([(a['start'], a['end']) for a in x])
        emit('| %s | %s | %d | %.0f | %.0f%% | %.0f | %.0f%% | %.2f | %.0f%% |' % (run, k, len(x), sum(a['wall_s'] for a in x) / 60,
             100 * sum(a['wall_s'] for a in x) / totsec, u / 60, 100 * u / runwall, sum(a['cost'] for a in x), 100 * sum(a['cost'] for a in x) / totc))
    emit('| %s | (run wall) | | | | %.0f | | | |' % (run, runwall / 60))
emit()

# ---------- outcomes per task ----------
emit('## Implementation outcomes per run (task-level)')
emit('Definitions: first-attempt success = the first impl/test agent for the task reported gatePassed and the first review approved (no retry, no revision, no escalation). Path-labelled tasks (".ck/worktrees/...-task-N") are folded into task N.')
emit()
task_rows = []
for run, cfg in RUNS.items():
    v = [a for a in agents if a['run'] == run and a['task']]
    merged = set()
    for r in cfg['out_results']:
        merged |= set(str(x) for x in (r.get('merged') or []))
    bytask = collections.defaultdict(list)
    for a in sorted(v, key=lambda a: a['start']):
        bytask[a['task']].append(a)
    for t, xs in bytask.items():
        work = [a for a in xs if a['role'].startswith('impl') or a['role'].startswith('test')]
        if not work:
            continue
        first = work[0]
        fr = first['result'] if isinstance(first['result'], dict) else {}
        reviews = [a for a in xs if a['role'] == 'review']
        verdicts = [(a['result'] or {}).get('verdict') if isinstance(a['result'], dict) else None for a in reviews]
        retries = sum(1 for a in work if 'retry' in a['role'])
        revisions = sum(1 for a in work if 'revise' in a['role'])
        esc = [a['label'] for a in work if '(sonnet)' in a['label'] or '(opus)' in a['label']]
        path_label = any('.ck/worktrees' in a['label'] for a in xs)
        fas = bool(fr.get('gatePassed')) and retries == 0 and revisions == 0 and (not verdicts or verdicts[0] == 'approve')
        row = dict(run=run, task=t, kind='test' if first['role'].startswith('test') else 'impl', first_model=first['model'],
                   first_gate=fr.get('gatePassed'), first_status=fr.get('status'), retries=retries, revisions=revisions,
                   review_rounds=len(reviews), review_models='/'.join(sorted(set(a['model'] or '?' for a in reviews))),
                   first_verdict=verdicts[0] if verdicts else None, final_verdict=verdicts[-1] if verdicts else None,
                   escalations=';'.join(esc), models_used='/'.join(sorted(set(a['model'] or '?' for a in work))),
                   merged=t in merged, first_attempt_success=fas, path_label=path_label,
                   impl_cost=sum(a['cost'] for a in work), review_cost=sum(a['cost'] for a in reviews),
                   impl_wall_s=sum(a['wall_s'] for a in work))
        task_rows.append(row)
with open(OUT + '/tasks.csv', 'w', newline='') as fh:
    w = csv.DictWriter(fh, fieldnames=list(task_rows[0].keys()))
    w.writeheader()
    for r in task_rows:
        w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})

emit('| run | first model | kind | tasks | first-attempt success | first gate green | first review approve | escalated | merged | median impl+fix $ per task | median review $ per task |')
emit('|---|---|---|---|---|---|---|---|---|---|---|')
g = collections.defaultdict(list)
for r in task_rows:
    g[(r['run'], r['first_model'], r['kind'])].append(r)
for k in sorted(g):
    x = g[k]
    n = len(x)
    rv = [r for r in x if r['first_verdict']]
    emit('| %s | %s | %s | %d | %d (%.0f%%) | %d | %d/%d | %d | %d | %.2f | %.2f |' % (k[0], k[1], k[2], n,
         sum(r['first_attempt_success'] for r in x), 100 * sum(r['first_attempt_success'] for r in x) / n,
         sum(bool(r['first_gate']) for r in x), sum(r['first_verdict'] == 'approve' for r in rv), len(rv),
         sum(bool(r['escalations']) for r in x), sum(r['merged'] for r in x),
         med([r['impl_cost'] for r in x]), med([r['review_cost'] for r in x]) or 0))
emit()

# per role success rate for mechanical agents
emit('## Mechanical-agent result health')
emit('| run | role | model | n | ok | notes |')
emit('|---|---|---|---|---|---|')
for run in RUNS:
    for role in ('plan', 'land', 'gate'):
        x = [a for a in agents if a['run'] == run and a['role'] == role]
        if not x:
            continue
        ok = 0
        notes = collections.Counter()
        for a in x:
            r = a['result']
            if role == 'plan':
                good = isinstance(r, dict) and 'ready' in r
                bad_ids = isinstance(r, dict) and any('/' in str(i) for i in (r.get('ready') or []))
                if bad_ids:
                    notes['ready[] contained worktree paths instead of task ids'] += 1
                ok += good and not bad_ids
                if not good:
                    notes['no/invalid result'] += 1
            elif role == 'land':
                ts_ = (r or {}).get('tasks') if isinstance(r, dict) else None
                if ts_ is None:
                    notes['no result'] += 1
                    continue
                st = collections.Counter(t.get('state') for t in ts_)
                for s_, c in st.items():
                    notes['state ' + str(s_)] += c
                ok += all(t.get('state') == 'MERGED' for t in ts_)
            else:
                ok += isinstance(r, dict) and r.get('green') is True
                if not (isinstance(r, dict) and r.get('green') is True):
                    notes['not green / no result'] += 1
        emit('| %s | %s | %s | %d | %d | %s |' % (run, role, '/'.join(sorted(set(str(a['model']) for a in x))), len(x), ok,
             '; '.join('%s x%d' % kv for kv in notes.most_common())))
emit()

open(OUT + '/tables.md', 'w').write('\n'.join(lines))
json.dump({k: dict(windows=v['windows']) for k, v in RUNS.items()}, open(OUT + '/windows.json', 'w'), indent=1)
print('\n'.join(lines))
