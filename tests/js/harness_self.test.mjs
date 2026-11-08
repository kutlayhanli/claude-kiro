// Self-checks of the fake runtime additions (Task 2 of unattended-correctness): the existing options still behave and
// the new ones answer as specified. Run: node tests/js/harness_self.test.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn, execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { project, directCk, isCkCall, gateJson } from './fake_project.mjs'
import { loadDecisions, UNATTENDED_NAMES, NAMES } from './load_decisions.mjs'
import { realAgents, promptCommands } from './real_agents.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}
const CK_CALL = { type: 'object', properties: { exitCode: { type: 'integer' }, json: { type: ['object', 'null'] } }, required: ['exitCode', 'json'] }
const THIN = { schema: CK_CALL }
const LEAN = { agentType: 'ck-runner' }
const deps = { 1: [], 2: ['1'] }
const hangs = p => Promise.race([p.then(() => 'settled', () => 'settled'), new Promise(r => setTimeout(() => r('hang'), 50))])

await test('loadDecisions without extra still loads only today\'s names', () => {
  const d = loadDecisions()
  assert.deepEqual(Object.keys(d).sort(), [...NAMES].sort())
  assert.equal(UNATTENDED_NAMES.length, 8)
  assert.ok(UNATTENDED_NAMES.includes('writeSetHold') && UNATTENDED_NAMES.includes('gateOutcome'))
})

await test('isCkCall: only a CK_CALL schema or an agentType', () => {
  assert.equal(isCkCall({ label: 'x' }), false)
  assert.equal(isCkCall({ schema: { type: 'object', properties: { green: {} } } }), false)
  assert.equal(isCkCall(THIN), true)
  assert.equal(isCkCall(LEAN), true)
})

await test('existing thin labels: today\'s answer without schema or agentType, the envelope with either', async () => {
  const calls = [
    ['plan 1', 'ck plan demo'], ['report', 'ck worktree report'], ['park 1', 'park'], ['relink 1', 'relink'],
    ['land 1', 'ck worktree land demo 1'], ['full gate @1', 'gate'], ['re-check 1', 'gate'], ['final gate', 'gate'],
  ]
  for (const [label, prompt] of calls) {
    const plain = await project({ deps }).agent(prompt, { label })
    for (const extra of [THIN, LEAN, { ...THIN, ...LEAN }]) {
      const wrapped = await project({ deps }).agent(prompt, { label, ...extra })
      assert.equal(wrapped.exitCode, 0, label)
      if (/gate/.test(label) || label.startsWith('re-check')) {
        assert.equal(wrapped.json.schema, 'ck-gate/1', label)
        assert.equal(wrapped.json.ok, plain.green, label)
      } else {
        const strip = o => JSON.parse(JSON.stringify(o, (k, v) => (k === 'startedAt' || k === 'finishedAt' ? undefined : v)))
        assert.deepEqual(strip(wrapped.json), strip(plain), label)
      }
    }
    assert.equal(plain.exitCode, undefined, `${label} unwrapped`)
  }
})

await test('a red re-check and a red full gate become a red ck-gate/1 object in the envelope', async () => {
  const p = project({ deps, fullRed: true, recheckRed: 1 })
  const full = await p.agent('g', { label: 'full gate @1', ...THIN })
  assert.equal(full.exitCode, 1)
  assert.equal(full.json.ok, false)
  assert.equal(full.json.failures.length, 1)
  const re = await p.agent('g', { label: 're-check 1', ...LEAN })
  assert.equal(re.json.ok, false)
  const re2 = await p.agent('g', { label: 're-check 2', ...LEAN })
  assert.equal(re2.json.ok, true)
  assert.deepEqual(gateJson(true).failures, [])
})

await test('gateAnswers: consumed in order across thin gate labels (json, {code, stdout}, compact, throw, hang)', async () => {
  const compact = { schema: 'ck-gate-compact/1', spec: 'demo', tasks: [], ok: false, attribute: true, failedChecks: [], regressions: [], unattributed: [], flaky: [], excusedByTask: {}, summary: { regressions: 0, excused: 0, flaky: 0, unattributed: 0 }, warnings: [], report: null, durationS: 1 }
  const p = project({ deps, gateAnswers: [{ schema: 'ck-gate/1', ok: false, failures: [] }, { code: 3, stdout: 'not json' }, { code: 1, stdout: '{"ok":false}' }, compact, 'throw', 'hang'] })
  assert.deepEqual(await p.agent('g', { label: 'full gate @1', ...THIN }), { exitCode: 1, json: { schema: 'ck-gate/1', ok: false, failures: [] } })
  const raw = await p.agent('g', { label: 're-check 1', ...THIN })
  assert.equal(raw.exitCode, 3); assert.equal(raw.json, null)
  assert.deepEqual((await p.agent('g', { label: 'final gate', ...THIN })).json, { ok: false })
  assert.equal((await p.agent('g', { label: 'full gate @2', ...THIN })).json.schema, 'ck-gate-compact/1')
  await assert.rejects(p.agent('g', { label: 'full gate @3', ...THIN }))
  assert.equal(await hangs(p.agent('g', { label: 'final gate', ...THIN })), 'hang')
  const after = await p.agent('g', { label: 'final gate', ...THIN })
  assert.equal(after.json.ok, true, 'exhausted: default green')
  assert.equal(p.state.gateCalls.length, 7)
})

await test('gateAnswers are not consumed by calls without an envelope', async () => {
  const p = project({ deps, gateAnswers: [{ ok: false }] })
  assert.deepEqual(await p.agent('g', { label: 'final gate' }), { green: true })
  assert.deepEqual((await p.agent('g', { label: 'final gate', ...THIN })).json, { ok: false })
})

await test('directCk answers gate argv from gateAnswers and the new argv with CK_CALL envelopes', async () => {
  const p = project({ deps, gateAnswers: [{ ok: false, failures: [] }, { code: 2, stdout: 'oops' }, 'throw'], runStart: { integration: { branch: 'b' }, hooks: { installed: true }, releasedClaims: [{ task: '1', run: 'k', reason: 'stale' }], keptClaims: [], warnings: [] }, audit: { schema: 'ck-audit/1', complete: true, clean: false, drift: [{ file: 'f', change: 'changed' }] }, fixtureStatus: { locked: [], drift: [{ file: 'f', task: '1', change: 'modified' }], unlabeled: [], configError: null }, fidelity: { checked: [], mismatches: [{ id: 'x' }], unparsed: [], unmatched: [] }, missingCommand: { command: 'ck x', providers: ['4'] } })
  const d = directCk(p)
  const g = await d.fn(['gate', 'demo', '--task', '1', '--json'])
  assert.equal(g.code, 1); assert.deepEqual(g.json, { ok: false, failures: [] })
  const g2 = await d.fn(['-C', '/r', 'gate', 'demo', '--attribute', '--json', '--compact'])
  assert.equal(g2.code, 2); assert.equal(g2.json, null); assert.equal(g2.stdout, 'oops')
  await assert.rejects(d.fn(['gate', 'demo']))
  assert.equal((await d.fn(['worktree', 'start', 'demo', '--run', 'k', '--json'])).json.releasedClaims[0].task, '1')
  assert.equal((await d.fn(['worktree', 'audit', 'demo', '--json'])).json.clean, false)
  assert.deepEqual((await d.fn(['fixtures', 'status', '--json'])).json.drift, [{ file: 'f', task: '1', change: 'modified' }])
  assert.equal((await d.fn(['lint', 'demo', '--fidelity', '--json'])).json.mismatches.length, 1)
  assert.equal((await d.fn(['graph', 'demo', '--json'])).json.missingCommand.command, 'ck x')
  assert.equal((await d.fn(['graph', 'demo', '--json'])).json.spec, 'demo')
  assert.equal((await d.fn(['testfix', 'record', 'demo'])).json.recorded, true)
  assert.equal((await d.fn(['frobnicate'])).code, 2)
  assert.equal(d.calls.length, 11)
})

await test('directCk plan, land and relink behave as before', async () => {
  const p = project({ deps })
  const d = directCk(p)
  const plan = await d.fn(['plan', 'demo', '--json'])
  assert.deepEqual(plan.json.ready, ['1'])
  const land = await d.fn(['worktree', 'land', 'demo', '1', '--into', 'integrate/demo'])
  assert.equal(land.json.tasks[0].state, 'MERGED')
})

await test('thin labels run start, audit, testfix ledger/prove: raw without envelope, wrapped with it', async () => {
  const p = project({ deps, priorFixes: { 'tests/test_a.py': true }, testFix: { 'tests/test_a.py': { verdict: 'fixed', proof: { proved: false, reason: 'rejected' } } } })
  assert.equal((await p.agent('x', { label: 'run start' })).hooks.installed, true)
  assert.deepEqual((await p.agent('x', { label: 'run start' })).releasedClaims, [])
  assert.equal((await p.agent('x', { label: 'run start', ...THIN })).exitCode, 0)
  assert.equal((await p.agent('x', { label: 'audit', ...LEAN })).json.schema, 'ck-audit/1')
  assert.equal((await p.agent('x', { label: 'audit', ...LEAN })).exitCode, 0)
  assert.equal((await project({ deps, audit: { schema: 'ck-audit/1', complete: false, clean: false } }).agent('x', { label: 'audit', ...LEAN })).exitCode, 3)
  assert.equal((await project({ deps, audit: { schema: 'ck-audit/1', complete: true, clean: false } }).agent('x', { label: 'audit', ...LEAN })).exitCode, 1)
  const l1 = (await p.agent('ck testfix check demo --test tests/test_a.py::test_x --test tests/test_b.py::test_y', { label: 'testfix ledger 1', ...THIN })).json
  assert.deepEqual(l1.allowed, ['tests/test_b.py::test_y'])
  assert.deepEqual(l1.alreadyFixed.map(e => e.node), ['tests/test_a.py::test_x'])
  assert.deepEqual((await p.agent('ck testfix check demo --test tests/test_b.py::test_y', { label: 'testfix ledger 2', ...THIN })).json.alreadyFixed, [])
  const pr = (await p.agent('ck testfix prove tests/test_a.py', { label: 'testfix prove 1', ...THIN })).json
  assert.equal(pr.proof.proved, false); assert.equal(pr.scope.in_scope, true); assert.equal(pr.against, 'integration'); assert.equal(pr.proof.against, 'integration')
  assert.deepEqual(pr.scope.spec_docs_edited, [])
  const pb = (await p.agent('ck testfix prove demo --blocked 3 --test tests/test_b.py::test_y', { label: 'testfix prove 2', ...THIN })).json
  assert.equal(pb.against, 'blocked:3'); assert.equal(pb.proof.against, 'blocked:3')
  assert.deepEqual(pb.proof.outcomes, [{ node: 'tests/test_b.py::test_y', passed: true, cls: null }])
  const bad = project({ deps, audit: 'throw' })
  await assert.rejects(bad.agent('x', { label: 'audit', ...THIN }))
})

await test('taskFailures and testConflictsList ride the task result', async () => {
  const failure = { node: 'tests/t.py::test_tc1_a', tcId: 'TC-1', kind: 'assertion' }
  const conflict = { file: 'tests/t.py', test: 'test_tc1_a', criterion: '1.1', reason: 'r', fix: 'f' }
  const p = project({ deps, failUntil: { 1: 1 }, taskFailures: { 1: [failure] }, testConflictsList: { 2: { conflicts: [conflict], attempts: 1 } } })
  const a = await p.agent('p', { label: 'task 1' })
  assert.deepEqual(a.failures, [failure]); assert.equal(a.status, 'in_progress')
  const b = await p.agent('p', { label: 'task 1' })
  assert.equal(b.failures, undefined); assert.equal(b.status, 'done')
  const c = await p.agent('p', { label: 'task 2' })
  assert.equal(c.status, 'blocked'); assert.deepEqual(c.testConflicts, [conflict])
  assert.equal((await p.agent('p', { label: 'task 2' })).status, 'done')
})

await test('testFix, crossTest and the plan extras', async () => {
  const p = project({ deps, testFix: { 'tests/a.py': { verdict: 'test-correct', finding: { file: 'tests/a.py', test: 't', issue: 'i' }, review: { verdict: 'changes', findings: [] } }, 'tests/b.py': {} }, crossTest: [{ verdict: 'changes', findings: [{ severity: 'blocking', test: 't' }], summary: 's' }, { verdict: 'approve', findings: [], summary: 'ok' }], owns: { 1: ['tests/a.py'] }, overlaps: [['1', '2']], testVerifies: { 1: ['1.1'] }, provides: { 1: ['ck x'] } })
  const fa = await p.agent('p', { label: 'testfix 1 (Task 5)' })
  assert.equal(fa.verdict, 'test-correct'); assert.equal(fa.changedTests[0].file, 'tests/a.py'); assert.equal(fa.finding.issue, 'i')
  assert.equal((await p.agent('p', { label: 'testfix 2 (Task 5)' })).verdict, 'fixed')
  assert.equal((await p.agent('p', { label: 'testfix review 1' })).verdict, 'changes')
  assert.equal((await p.agent('p', { label: 'cross-test 1' })).findings[0].test, 't')
  assert.equal((await p.agent('p', { label: 'cross-test 2' })).verdict, 'approve')
  assert.equal((await p.agent('p', { label: 'cross-test 3' })).verdict, 'approve')
  const plan = await p.agent('p', { label: 'plan 1' })
  assert.deepEqual([plan.owns, plan.overlaps, plan.testVerifies, plan.provides], [{ 1: ['tests/a.py'] }, [['1', '2']], { 1: ['1.1'] }, { 1: ['ck x'] }])
  const plain = await project({ deps }).agent('p', { label: 'plan 1' })
  assert.ok(!('owns' in plain) && !('overlaps' in plain))
  assert.equal((await project({ deps }).agent('p', { label: 'cross-test 1' })).verdict, 'changes')
})

// --- bridge and realCk ---------------------------------------------------------------------------------------------------
const bridge = () => {
  const proc = spawn('node', [path.join(here, 'bridge.mjs')], { stdio: ['pipe', 'pipe', 'pipe'] })
  let buf = ''
  const waiting = []
  proc.stdout.on('data', d => { buf += d; let i; while ((i = buf.indexOf('\n')) >= 0) { const line = buf.slice(0, i); buf = buf.slice(i + 1); waiting.shift()(JSON.parse(line)) } })
  return { ask: req => new Promise(r => { waiting.push(r); proc.stdin.write(JSON.stringify(req) + '\n') }), close: () => proc.stdin.end() }
}

await test('bridge op call: calls any named decision with args; unknown names fail', async () => {
  const b = bridge()
  try {
    const ok = await b.ask({ op: 'call', name: 'readyTasks', args: [{ order: ['1', '2'], deps: { 1: [], 2: ['1'] }, done: [], exclude: [], blocked: [] }] })
    assert.deepEqual(ok, { ok: true, value: ['1'] })
    const bad = await b.ask({ op: 'call', name: 'noSuchDecision', args: [] })
    assert.equal(bad.ok, false); assert.match(bad.error, /noSuchDecision/)
  } finally { b.close() }
})

await test('bridge op run with realCk: direct ck() calls run the real subprocess with the given cwd and env', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'harness-self-'))
  const shim = path.join(dir, 'shim.mjs')
  fs.writeFileSync(shim, `import fs from 'node:fs'
fs.appendFileSync(process.env.SHIM_LOG, JSON.stringify({ argv: process.argv.slice(2), cwd: process.cwd(), tag: process.env.SHIM_TAG }) + '\\n')
if (process.argv.includes('plan')) console.log(JSON.stringify({ done: [], ready: [], busy: [], remaining: [], deps: {}, cycles: [], verifyCycles: [], startedAt: 'x', finishedAt: 'y' }))
`)
  const log = path.join(dir, 'log.jsonl')
  const b = bridge()
  try {
    const out = await b.ask({ op: 'run', project: { deps: { 1: [] } }, args: {}, realCk: { cmd: ['node', shim], cwd: dir, env: { SHIM_LOG: log, SHIM_TAG: 'T1' } } })
    assert.equal(out.ok, true, out.error)
    assert.ok(Array.isArray(out.ckCalls) && out.ckCalls.length >= 1, JSON.stringify(out.ckCalls))
    const lines = fs.readFileSync(log, 'utf8').trim().split('\n').map(l => JSON.parse(l))
    assert.equal(lines.length, out.ckCalls.length)
    assert.ok(lines.every(l => l.tag === 'T1' && fs.realpathSync(l.cwd) === fs.realpathSync(dir)))
    assert.deepEqual(lines[0].argv, out.ckCalls[0].argv)
  } finally { b.close(); fs.rmSync(dir, { recursive: true, force: true }) }
})

// --- scripted agents -------------------------------------------------------------------------------------------------------
await test('real_agents: claim with the prompt\'s command, write files, commit, run the gate through the ck given', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'harness-agents-'))
  const wt = path.join(dir, 'wt')
  fs.mkdirSync(wt)
  const git = (...a) => execFileSync('git', ['-C', wt, ...a], { encoding: 'utf8' })
  git('init', '-q'); git('-c', 'user.name=t', '-c', 'user.email=t@e.com', 'commit', '-q', '--allow-empty', '-m', 'init')
  const shim = path.join(dir, 'shim.mjs')
  const log = path.join(dir, 'log.jsonl')
  fs.writeFileSync(shim, `import fs from 'node:fs'
const a = process.argv.slice(2)
fs.appendFileSync(${JSON.stringify(log)}, JSON.stringify(a) + '\\n')
if (a.includes('create')) console.log(JSON.stringify([{ task: '1', state: 'CREATED', detail: 'x', path: ${JSON.stringify(wt)}, branch: 'b' }]))
else if (a.includes('gate')) { console.log(JSON.stringify({ ok: false, failures: [{ node: 'n', kind: 'assertion' }] })); process.exitCode = 1 }
else console.log('CLAIMED')
`)
  const prompt = `Run:\nck -C ${dir} worktree create demo 1 --json\nck -C ${wt} worktree claim demo 1 --attempt 1\nthen ck -C ${wt} gate demo --task 1\n`
  assert.deepEqual(promptCommands(prompt, ['gate']).map(a => a.slice(-3)), [['demo', '--task', '1']])
  const { agent, runs } = realAgents({ ck: { cmd: ['node', shim], cwd: dir }, scenario: { files: { 1: { 'src/a.txt': 'hello' } } } })
  try {
    const r = await agent(prompt, { label: 'task 1' })
    assert.equal(r.task, '1'); assert.equal(r.status, 'in_progress'); assert.equal(r.gatePassed, false)
    assert.equal(r.worktree.state, 'CREATED'); assert.ok(r.commits.length >= 7)
    assert.deepEqual(r.failures, [{ node: 'n', kind: 'assertion' }])
    assert.equal(fs.readFileSync(path.join(wt, 'src/a.txt'), 'utf8'), 'hello')
    assert.match(git('log', '--format=%s', '-1').trim(), /scripted/)
    assert.deepEqual(runs.filter(x => x.argv[0] !== 'git').map(x => x.argv.find(t => ['create', 'claim', 'gate'].includes(t))), ['create', 'claim', 'gate'])
    await assert.rejects(agent('p', { label: 'plan 1' }), /unexpected agent/)
  } finally { fs.rmSync(dir, { recursive: true, force: true }) }
})
