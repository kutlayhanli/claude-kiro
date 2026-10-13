import json,sys,glob,collections,os
P=os.path.expanduser('~/.claude/projects')
pats=sys.argv[1].split(',')
for d in sys.argv[2:]:
    for wd in glob.glob(P+'/'+d+'/*/subagents/workflows/*'):
        labels={}
        for l in open(wd+'/journal.jsonl'):
            e=json.loads(l)
            if e['type']=='started': labels[e['agentId']]=e['label']
        for f in sorted(glob.glob(wd+'/agent-*.jsonl')):
            a=os.path.basename(f)[6:-6]
            models=collections.Counter(); hits=[]
            for l in open(f):
                x=json.loads(l)
                if x.get('type')!='assistant': continue
                m=x['message']; models[m.get('model')]+=1
                for c in m.get('content',[]):
                    if c.get('type')!='tool_use': continue
                    s=json.dumps(c['input'])
                    for p in pats:
                        if p in s and (c['name'] in ('Write','Edit','MultiEdit') or 'git commit' in s or 'git add' in s):
                            hits.append((c['name'],p,x['timestamp'][11:19]))
            if hits:
                print(d[-25:],labels.get(a),dict(models),sorted(set(h[:2] for h in hits)), hits[0][2])
