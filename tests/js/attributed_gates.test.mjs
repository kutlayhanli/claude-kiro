// Attributed periodic, recheck and final gates in the workflow (unattended-correctness Story 2, Story 3 JS side).
// Runs the real plugin/workflows/spec-implement.js under the fake runtime; only the runtime, the agents and the `ck`
// global are faked. Assertions use direct `ck()` argv (OpenCode path), thin-agent options and prompts (Claude Code
// path), the virtual clock, and the result's `gates[]`, `fullGates[]`, `finalGate`, `testConflicts`.
// TC-18 to TC-20, TC-22 to TC-25, TC-26a, TC-27a, TC-33a, TC-34, TC-35, TC-36 (JS), TC-184.
// The test-fix prompt source of TC-36 is asserted with the test-fix loop (Task 83).
// Run: node tests/js/attributed_gates.test.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { project, directCk } from './fake_project.mjs'
import { VirtualClock } from './virtual_clock.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')
const FAILURE_SCHEMA = JSON.parse(fs.readFileSync(path.join(here, '../fixtures/unattended/failure.schema.json'), 'utf8'))

const TARGET = '/repo/.ck/worktrees/demo-integration'
const RUNNER = 'spec:runner'
const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: TARGET }
const WT = n => `/repo/.ck/worktrees/demo-task-${n}`

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

const linear = n => Object.fromEntries(Array.from({ length: n }, (_, i) => [String(i + 1), i ? [String(i)] : []]))
const withGraph = (deps, extra = {}) => ({ ...baseArgs, deps, remaining: Object.keys(deps), ...extra })
const claudeArgs = (deps, extra = {}) => withGraph(deps, { runnerAgent: RUNNER, ...extra })
const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

// --- fixtures ------------------------------------------------------------------------------------------------
const failure = (file, id, over = {}) => ({
  node: `${file}::test_${id}`, tcId: 'TC-1', tasks: ['1'], attributedBy: 'tc', file, kind: 'assertion', message: 'assert nope',
  truncated: false, command: 'pytest', status: 'regression', excusedBy: [], rerun: 'failed', class: null, check: null, missingCommand: null, ...over,
})
const REG_A = failure('tests/test_a.py', 'alpha', { tcId: 'TC-11', tasks: ['3'], message: 'assert 0.05 == 0.04' })
const REG_B = failure('tests/test_b.py', 'beta', { tcId: 'TC-12', tasks: ['4'], kind: 'error', message: 'ValueError: bad rate' })
const full = (ok, failures = [], extra = {}) => ({
  schema: 'ck-gate/1', spec: 'demo', tasks: [], ok, attribute: true, checks: [], failures,
  summary: { regressions: failures.filter(f => f.status === 'regression').length, excused: failures.filter(f => f.status === 'excused').length, flaky: failures.filter(f => f.status === 'flaky').length, unattributed: 0 },
  warnings: [], report: '/repo/.git/ck/demo/gate-reports/20261108T100000Z-42.json', durationS: 1, ...extra,
})
const red = regs => full(false, regs)
const green = () => full(true)
const compact = (ok, over = {}) => ({
  schema: 'ck-gate-compact/1', spec: 'demo', tasks: [], ok, attribute: true, failedChecks: [], regressions: [], unattributed: [], flaky: [], excusedByTask: {},
  summary: { regressions: 0, excused: 0, flaky: 0, unattributed: 0 }, warnings: [], report: '/repo/.git/ck/demo/gate-reports/20261108T100001Z-43.json', durationS: 1, ...over,
})

// --- helpers -------------------------------------------------------------------------------------------------
const isGateArgv = a => a[0] === '-C' && a[2] === 'gate'
const isLandArgv = a => a.includes('worktree') && a.includes('land')
const gateCallsOf = d => d.calls.filter(isGateArgv)
const requests = trace => trace.filter(e => e.ev === 'request').map(e => e.label)
const GATE_LABEL = /^(full gate @|re-check |final gate$|gate$)/
const gateLine = (dir = TARGET) => new RegExp(`^ck -C ${esc(dir)} gate demo --attribute(?: --install)? --json(?: --compact)?[ \\t]*$`, 'm')
const compactLine = (dir = TARGET) => new RegExp(`^ck -C ${esc(dir)} gate demo --attribute(?: --install)? --json --compact[ \\t]*$`, 'm')

// Every JSON object in a prompt, whatever the wrapper (a Failure list, {regressions: [...]}, pretty-printed).
function jsonObjectsIn(text) {
  const out = []
  for (let i = 0; i < text.length; i++) {
    if (text[i] !== '{') continue
    let depth = 0, inStr = false, end = -1
    for (let j = i; j < text.length; j++) {
      const c = text[j]
      if (inStr) { if (c === '\\') j++; else if (c === '"') inStr = false; continue }
      if (c === '"') inStr = true
      else if (c === '{') depth++
      else if (c === '}' && --depth === 0) { end = j; break }
    }
    if (end < 0) continue
    try { out.push(JSON.parse(text.slice(i, end + 1))); i = end } catch { /* not JSON: keep scanning inside it */ }
  }
  return out
}
const walk = (v, visit) => {
  if (Array.isArray(v)) v.forEach(x => walk(x, visit))
  else if (v && typeof v === 'object') { visit(v); Object.values(v).forEach(x => walk(x, visit)) }
}
const failuresIn = value => { const out = []; walk(value, o => { if ('attributedBy' in o && 'node' in o) out.push(o) }); return out }
const failuresInText = text => failuresIn(jsonObjectsIn(text))
const carries = (text, f) => failuresInText(text).some(o => { try { assert.deepEqual(o, f); return true } catch { return false } })

// A small JSON-schema checker for the subset failure.schema.json uses.
function validate(schema, v, at = '$') {
  const errs = []
  const types = schema.type === undefined ? null : [].concat(schema.type)
  const typeOf = x => (x === null ? 'null' : Array.isArray(x) ? 'array' : typeof x)
  if (types && !types.includes(typeOf(v))) return [`${at}: type ${typeOf(v)} not in ${types}`]
  if (schema.enum && !schema.enum.some(e => e === v)) errs.push(`${at}: ${JSON.stringify(v)} not in enum`)
  if (schema.pattern && typeof v === 'string' && !new RegExp(schema.pattern).test(v)) errs.push(`${at}: ${v} does not match ${schema.pattern}`)
  if (schema.maxLength !== undefined && typeof v === 'string' && v.length > schema.maxLength) errs.push(`${at}: longer than ${schema.maxLength}`)
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    for (const k of schema.required || []) if (!(k in v)) errs.push(`${at}: missing ${k}`)
    for (const [k, s] of Object.entries(schema.properties || {})) if (k in v) errs.push(...validate(s, v[k], `${at}.${k}`))
  }
  if (Array.isArray(v) && schema.items) v.forEach((x, i) => errs.push(...validate(schema.items, x, `${at}[${i}]`)))
  return errs
}
const assertValid = (f, where) => assert.deepEqual(validate(FAILURE_SCHEMA, f), [], `${where}: failure entry ${f.node} violates failure.schema.json`)

const sameFields = (entry, c) => Object.entries(c).every(([k, v]) => JSON.stringify(entry[k]) === JSON.stringify(v))
const never = () => new Promise(() => {})
const STEP_TIMEOUT_MS = 30 * 60 * 1000

// Runs under the OpenCode path (the ck global) over a project; returns the pieces assertions need.
async function runDirect(p, args, ckFn) {
  const d = directCk(p)
  const ck = ckFn ? ckFn(d.fn) : d.fn
  const run = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(args.deps, args.extra), ck })
  return { ...run, d }
}
// Runs under the Claude Code path (no ck global, lean runner agent).
const runClaude = (p, deps, extra = {}, more = {}) => runWorkflow(SCRIPT, { agent: more.agent ?? p.agent, args: claudeArgs(deps, extra), ...more.run })

// ------------------------------------------------------------------------------------------------------------
await test('TC-18: OpenCode path, 25 linear tasks: attributed gates run as direct ck calls after merges 10, 20 and once at the end (2.1, 2.2)', async () => {
  const deps = linear(25)
  const p = project({ deps })
  const { result, trace, d } = await runDirect(p, { deps })
  assert.equal(result.merged.length, 25)
  let lands = 0
  const at = []
  for (const a of d.calls) {
    if (isLandArgv(a)) lands += a.slice(a.indexOf('land') + 2).filter(t => /^\d+$/.test(t) && a.indexOf(t) < a.indexOf('--into')).length
    if (isGateArgv(a)) at.push(lands)
  }
  const gates = gateCallsOf(d)
  assert.deepEqual(at, [10, 20, 25], `gate calls after this many merged tasks: ${at}`)
  for (const a of gates) {
    assert.deepEqual(a.slice(0, 4), ['-C', TARGET, 'gate', 'demo'], a.join(' '))
    assert.ok(a.includes('--attribute') && a.includes('--json'), a.join(' '))
    assert.ok(!a.includes('--compact'), 'direct calls read the full JSON')
    for (const flag of a.slice(4)) assert.ok(['--attribute', '--json', '--install'].includes(flag), `unexpected gate flag ${flag}`)
  }
  for (const l of requests(trace)) assert.ok(!GATE_LABEL.test(l) && !/gate/.test(l), `no agent runs a gate: ${l}`)
  for (const [l, prompt] of Object.entries(p.state.prompts)) assert.ok(!/gate demo --attribute/.test(prompt), `${l} prompt holds an attributed gate`)
})

await test('TC-19: args.fullGateEvery = 3 and 9 tasks: attributed gates after merges 3, 6, 9, plus the final gate (2.1)', async () => {
  const deps = linear(9)
  const p = project({ deps })
  const { result, d } = await runDirect(p, { deps, extra: { fullGateEvery: 3 } })
  assert.equal(result.merged.length, 9)
  let lands = 0
  const at = []
  for (const a of d.calls) {
    if (isLandArgv(a)) lands++
    if (isGateArgv(a)) at.push(lands)
  }
  assert.deepEqual(at, [3, 6, 9, 9], `gate calls after this many landings: ${at}`)
  assert.deepEqual(result.fullGates.map(g => g.afterMerges), [3, 6, 9].slice(0, result.fullGates.length))
  assert.deepEqual(result.gates.map(g => g.kind), ['periodic', 'periodic', 'periodic', 'final'])
  assert.deepEqual(result.gates.map(g => g.afterMerges).slice(0, 3), [3, 6, 9])
})

await test('TC-20: Claude Code path: each periodic and final gate is one lean-agent call with the thin schema; its JSON is used as is (2.3, 19.1)', async () => {
  const deps = linear(2)
  const p = project({ deps, gateAnswers: [red([REG_A]), green()] })
  const { result } = await runClaude(p, deps, { fullGateEvery: 2 })
  assert.deepEqual(result.merged, ['1', '2'])
  const thinSchema = p.state.opts['plan 1'].schema
  assert.ok(thinSchema, 'thin calls carry the shared CK_CALL schema')
  assert.equal(thinSchema.type, 'object')
  assert.equal(thinSchema.properties.exitCode.type, 'integer')
  assert.deepEqual(thinSchema.required, ['exitCode', 'json'])
  for (const label of ['full gate @2', 'final gate']) {
    const o = p.state.opts[label]
    assert.ok(o, `${label} was an agent call`)
    assert.equal(o.agentType, RUNNER, `${label}: lean agent type`)
    assert.deepEqual(o.schema, thinSchema, `${label}: the shared thin schema`)
    assert.ok(!('model' in o) && !('effort' in o), `${label}: no model or effort: ${JSON.stringify(Object.keys(o))}`)
    assert.match(p.state.prompts[label], gateLine(), `${label}: the gate command is on its own line`)
  }
  const fixLabel = p.state.events.find(l => /^fix \d+/.test(l))
  assert.ok(fixLabel, `a fixer started: ${p.state.events.join(' | ')}`)
  assert.ok(carries(p.state.prompts[fixLabel], REG_A), 'the regression reaches the fixer prompt unchanged')
})

await test('TC-22: two regressions start a fixer carrying both entries; the recheck is again an attributed gate, never a default agent (2.6, 3.9)', async () => {
  // OpenCode path: the recheck is a direct ck call.
  const deps = { 1: [] }
  const p = project({ deps, gateAnswers: [red([REG_A, REG_B]), green()] })
  const { result, trace, d } = await runDirect(p, { deps })
  const fixLabel = p.state.events.find(l => /^fix \d+/.test(l))
  assert.ok(fixLabel, `a fixer started: ${p.state.events.join(' | ')}`)
  assert.ok(carries(p.state.prompts[fixLabel], REG_A) && carries(p.state.prompts[fixLabel], REG_B), 'both entries in the fixer prompt')
  const gates = gateCallsOf(d)
  assert.equal(gates.length, 2, 'the final gate and its recheck are both direct gate calls')
  assert.ok(gates.every(a => a.includes('--attribute') && a.includes('--json')), 'the recheck is attributed')
  assert.ok(!requests(trace).some(l => /^re-check/.test(l)), 're-check is not an agent call under OpenCode')
  assert.ok(result.gates.some(g => g.kind === 'recheck' && g.state === 'green' && g.green === true), JSON.stringify(result.gates))

  // Claude Code path: the recheck is a thin agent with the lean type, not a default agent.
  const q = project({ deps, gateAnswers: [red([REG_A, REG_B]), green()] })
  const claude = await runClaude(q, deps)
  const fix2 = q.state.events.find(l => /^fix \d+/.test(l))
  assert.ok(carries(q.state.prompts[fix2], REG_A) && carries(q.state.prompts[fix2], REG_B))
  const recheck = q.state.events.find(l => /^re-check/.test(l))
  assert.ok(recheck, 'a re-check call')
  assert.equal(q.state.opts[recheck].agentType, RUNNER)
  assert.deepEqual(q.state.opts[recheck].schema, q.state.opts['plan 1'].schema)
  assert.match(q.state.prompts[recheck], gateLine())
  assert.ok(claude.result.gates.some(g => g.kind === 'final' && g.state === 'red' && g.regressions.length === 2), JSON.stringify(claude.result.gates))
})

await test('TC-23: gates that stay red through the whole fixer ladder: red-target stop, remaining regressions, no new task starts (2.7)', async () => {
  const deps = linear(5)
  const p = project({ deps, gateAnswers: [red([REG_A, REG_B]), ...Array.from({ length: 8 }, () => red([REG_A]))] })
  const { result, trace } = await runDirect(p, { deps, extra: { fullGateEvery: 2 } })
  assert.equal(result.halted, true)
  assert.equal(result.targetGreen, false)
  assert.match(String(result.reason), /integrate\/demo is red/, `the red-target stop names the target: ${result.reason}`)
  assert.deepEqual(result.merged, ['1', '2'])
  const lastRed = [...result.gates].reverse().find(g => g.state === 'red')
  assert.ok(lastRed, JSON.stringify(result.gates))
  assert.deepEqual(lastRed.regressions, [REG_A], 'regressions lists what remains after the ladder')
  assert.ok(result.gates.every(g => g.green === false))
  assert.equal(result.fullGates[0].green, false)
  assert.ok(!requests(trace).some(l => /^task [345]/.test(l)), `no new task starts after the red gate: ${requests(trace).join(' | ')}`)
})

await test('TC-24: a green gate with 2 excused and 1 flaky entry starts no fixer and records excusedByTask, flaky and report; old reader fields stay (2.8)', async () => {
  const excused1 = failure('tests/test_c.py', 'gamma', { tasks: ['5'], status: 'excused', excusedBy: ['5'], rerun: undefined })
  const excused2 = failure('tests/test_d.py', 'delta', { tasks: ['6'], status: 'excused', excusedBy: ['6'], rerun: undefined })
  delete excused1.rerun; delete excused2.rerun
  const flaky = failure('tests/test_e.py', 'epsilon', { status: 'flaky', rerun: 'passed' })
  const REPORT = '/repo/.git/ck/demo/gate-reports/20261108T101500Z-4242.json'
  const answer = () => full(true, [excused1, excused2, flaky], { report: REPORT })
  const deps = linear(2)
  const p = project({ deps, gateAnswers: [answer(), answer()] })
  const { result, d } = await runDirect(p, { deps, extra: { fullGateEvery: 2 } })
  assert.deepEqual(result.merged, ['1', '2'])
  assert.ok(!p.state.events.some(l => /^fix /.test(l)), 'no fixer starts')
  assert.equal(gateCallsOf(d).length, 2)
  assert.equal(result.gates.length, 2)
  for (const g of result.gates) {
    assert.equal(g.state, 'green')
    assert.equal(g.green, true)
    assert.deepEqual(g.excusedByTask, { 5: 1, 6: 1 })
    assert.equal(g.flaky.length, 1)
    assert.equal(g.flaky[0].node, flaky.node)
    assert.equal(g.report, REPORT)
    assert.deepEqual(g.regressions, [])
  }
  assert.deepEqual(result.gates.map(g => g.kind), ['periodic', 'final'])
  assert.ok(result.gates.every(g => 'green' in g), 'every gates[] entry has green')
  assert.ok(result.fullGates.length >= 1 && result.fullGates.every(g => typeof g.afterMerges === 'number' && g.green === true), JSON.stringify(result.fullGates))
  assert.equal(result.fullGates[0].afterMerges, 2)
  assert.equal(result.finalGate.green, true)
})

// TC-25: the seven gate-error forms.
const THIN_FORMS = [
  ['code 2 and no JSON', () => ({ exitCode: 2, json: null })],
  ['stdout that is not JSON', () => ({ exitCode: 1, json: null, error: 'ck printed: not json at all' })],
  ['a thrown error', () => { throw new Error('gate agent failed') }],
  ['a hang past the step timeout', 'hang'],
  ['a thin agent error', () => ({ error: 'the agent ended without running ck' })],
  ['a thin answer missing json', () => ({ exitCode: 0 })],
  ['JSON without failures', () => ({ exitCode: 0, json: { schema: 'ck-gate/1', spec: 'demo', ok: true } })],
]
const DIRECT_FORMS = [
  ['code 2 and no JSON', () => ({ code: 2, stdout: '', stderr: 'ck: boom', json: null })],
  ['stdout that is not JSON', () => ({ code: 0, stdout: 'not json', stderr: '', json: null })],
  ['a thrown error', () => { throw new Error('spawn ck ENOENT') }],
  ['a hang past the step timeout', 'hang'],
  ['JSON without failures', () => ({ code: 0, stdout: '{"ok":true}', stderr: '', json: { schema: 'ck-gate/1', spec: 'demo', ok: true } })],
]
function assertGateError(result, what) {
  const finals = result.gates.filter(g => g.kind === 'final')
  assert.equal(finals.length, 1, `${what}: one final gate record: ${JSON.stringify(result.gates)}`)
  const g = finals[0]
  assert.equal(g.state, 'error', what)
  assert.equal(g.green, false, `${what}: an error is never green`)
  assert.ok(typeof g.error === 'string' && g.error.length > 0, `${what}: the reason is recorded: ${JSON.stringify(g)}`)
  assert.ok(result.gates.every(x => x.green === false), `${what}: no gate counts green`)
  assert.equal(result.finalGate.green, false, what)
  assert.equal(result.targetGreen, false, `${what}: the run is not reported green`)
  assert.notEqual(result.outcome, 'complete', what)
}
await test('TC-25: Claude Code path: each of the seven gate-error forms is recorded as a gate error and never counts green (2.9, 19.5)', async () => {
  for (const [what, form] of THIN_FORMS) {
    const clock = new VirtualClock()
    const deps = { 1: [] }
    const p = project({ deps, sleep: clock.sleep })
    const agent = (prompt, opts) => {
      if (!(opts.agentType && GATE_LABEL.test(opts.label))) return p.agent(prompt, opts)
      if (form === 'hang') return Promise.race([never(), clock.sleep(STEP_TIMEOUT_MS).then(() => { throw new Error('step timeout') })])
      return Promise.resolve().then(form)
    }
    const { result } = await runWorkflow(SCRIPT, { agent, args: claudeArgs(deps), clock })
    assertGateError(result, `thin: ${what}`)
  }
})
await test('TC-25: OpenCode path: each direct-ck gate-error form is recorded as a gate error and never counts green (2.9)', async () => {
  for (const [what, form] of DIRECT_FORMS) {
    const clock = new VirtualClock()
    const deps = { 1: [] }
    const p = project({ deps, sleep: clock.sleep })
    const d = directCk(p)
    const ck = argv => {
      if (!isGateArgv(argv)) return d.fn(argv)
      if (form === 'hang') return Promise.race([never(), clock.sleep(STEP_TIMEOUT_MS).then(() => { throw new Error('step timeout') })])
      return Promise.resolve().then(form)
    }
    const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps), ck, clock })
    assertGateError(result, `direct: ${what}`)
  }
})

await test('TC-26a: the workflow never adds --attribute to a worktree land, in argv or prompt (2.10)', async () => {
  const deps = linear(3)
  const p = project({ deps })
  const { result, d } = await runDirect(p, { deps })
  assert.equal(result.merged.length, 3)
  const lands = d.calls.filter(isLandArgv)
  assert.ok(lands.length >= 1)
  for (const a of lands) assert.ok(!a.includes('--attribute'), `land argv: ${a.join(' ')}`)

  const q = project({ deps })
  await runClaude(q, deps)
  const landLabels = Object.keys(q.state.prompts).filter(l => l.startsWith('land '))
  assert.ok(landLabels.length >= 1)
  for (const l of landLabels) assert.ok(!/--attribute/.test(q.state.prompts[l]), `${l} prompt`)
})

await test('TC-27a: the final gate is requested after the last land ended (2.11)', async () => {
  const deps = linear(3)
  const p = project({ deps })
  const claude = await runClaude(p, deps)
  const lastLandEnd = claude.trace.map((e, i) => (e.ev === 'end' && e.label.startsWith('land ') ? i : -1)).reduce((a, b) => Math.max(a, b), -1)
  const finalReq = claude.trace.findIndex(e => e.ev === 'request' && e.label === 'final gate')
  assert.ok(lastLandEnd >= 0 && finalReq > lastLandEnd, `final gate request ${finalReq} after last land end ${lastLandEnd}`)

  const q = project({ deps })
  const { d } = await runDirect(q, { deps })
  const lastLand = d.calls.map((a, i) => (isLandArgv(a) ? i : -1)).reduce((a, b) => Math.max(a, b), -1)
  const lastGate = d.calls.map((a, i) => (isGateArgv(a) ? i : -1)).reduce((a, b) => Math.max(a, b), -1)
  assert.ok(lastLand >= 0 && lastGate > lastLand, `final gate call ${lastGate} after last land call ${lastLand}`)
})

await test('TC-33a: the task prompt holds the task-scoped json gate command and asks for ck\'s failures; RESULT has a failures array of D3 items (3.6)', async () => {
  const deps = { 1: [] }
  const p = project({ deps })
  await runClaude(p, deps)
  const prompt = p.state.prompts['task 1']
  assert.ok(prompt.includes(`ck -C ${WT(1)} gate demo --task 1 --json`), 'the task-scoped json gate command')
  assert.match(prompt, /\bfailures\b/, 'the agent is told to return ck\'s failures')
  const f = p.state.opts['task 1'].schema.properties.failures
  assert.ok(f, 'RESULT has failures')
  assert.equal(f.type, 'array')
  for (const k of ['node', 'tcId', 'tasks', 'attributedBy', 'file', 'kind', 'message', 'truncated', 'command']) {
    assert.ok(f.items && f.items.properties && k in f.items.properties, `failures item property ${k}`)
  }
})

await test('TC-34: testConflicts reach the confirmation agent and the result; a legacy single testConflict is a list of one (3.7)', async () => {
  const C1 = { file: 'tests/test_a.py', test: 'test_one', criterion: '1.1', reason: 'asserts 4 where the spec says 5', fix: 'expect 5' }
  const C2 = { file: 'tests/test_b.py', test: 'test_two', criterion: '2.2', reason: 'imports a name nothing provides', fix: 'drop the import' }
  const deps = { 1: [] }
  const p = project({ deps, testConflictsList: { 1: [C1, C2] } })
  const { result } = await runClaude(p, deps)
  const confirm = p.state.events.find(l => /^task 1 retry/.test(l))
  assert.ok(confirm, `a confirmation agent ran: ${p.state.events.join(' | ')}`)
  const prompt = p.state.prompts[confirm]
  for (const c of [C1, C2]) assert.ok(prompt.includes(c.file) && prompt.includes(c.reason), `confirmation prompt holds ${c.file}`)
  assert.ok(Array.isArray(result.testConflicts), 'result.testConflicts is a list')
  for (const c of [C1, C2]) assert.ok(result.testConflicts.some(e => sameFields(e, c)), `${c.file} in the result: ${JSON.stringify(result.testConflicts)}`)

  const q = project({ deps, testConflicts: { 1: { conflict: C1 } } })
  const legacy = await runClaude(q, deps)
  assert.ok(Array.isArray(legacy.result.testConflicts))
  assert.equal(legacy.result.testConflicts.length, 1)
  assert.ok(sameFields(legacy.result.testConflicts[0], C1), JSON.stringify(legacy.result.testConflicts))
})

await test('TC-35: 120 failures over 3 files: the retry prompt holds the first 50 as JSON in order plus per-file counts of the other 70, untrimmed (3.8)', async () => {
  const letters = i => { let s = ''; i++; while (i > 0) { s = String.fromCharCode(96 + ((i - 1) % 26) + 1) + s; i = Math.floor((i - 1) / 26) } return s }
  const files = [['tests/test_a.py', 55], ['tests/test_b.py', 40], ['tests/test_c.py', 25]]
  const all = []
  for (const [file, n] of files) for (let k = 0; k < n; k++) all.push(failure(file, `${letters(all.length)}`))
  assert.equal(all.length, 120)
  const deps = { 1: [] }
  const p = project({ deps, failUntil: { 1: 1 }, taskFailures: { 1: all } })
  await runClaude(p, deps)
  const label = p.state.events.find(l => /^task 1 retry 1$/.test(l))
  assert.ok(label, `a retry ran: ${p.state.events.join(' | ')}`)
  const prompt = p.state.prompts[label]
  const shown = failuresInText(prompt)
  assert.deepEqual(shown.map(f => f.node), all.slice(0, 50).map(f => f.node), 'the first 50 entries, in order')
  for (const f of all.slice(50)) assert.ok(!prompt.includes(f.node), `entry ${f.node} beyond the first 50 is not listed`)
  const tail = {}
  for (const f of all.slice(50)) tail[f.file] = (tail[f.file] || 0) + 1
  assert.deepEqual(tail, { 'tests/test_a.py': 5, 'tests/test_b.py': 40, 'tests/test_c.py': 25 })
  for (const [file, n] of Object.entries(tail)) assert.match(prompt, new RegExp(`${esc(file)}\\D{0,12}\\b${n}\\b`), `per-file count ${file}: ${n}`)
  assert.ok(prompt.length > 3000, 'the failures are not trimmed to 3000 characters')
  assert.ok(prompt.includes(all[49].node) && prompt.includes(JSON.stringify(all[49].message)), 'the 50th entry is whole')
})

await test('TC-36 (JS): every failure object in the retry, fixer and recheck prompts and in the result validates against failure.schema.json (3.9)', async () => {
  // Retry prompt: attempt 1 returns failures.
  const attempt = [REG_A, REG_B]
  const deps = { 1: [] }
  const p = project({ deps, failUntil: { 1: 1 }, taskFailures: { 1: attempt }, gateAnswers: [red([REG_A, REG_B]), red([REG_A]), green()] })
  const { result } = await runClaude(p, deps)
  const retry = failuresInText(p.state.prompts[p.state.events.find(l => /^task 1 retry 1$/.test(l))])
  assert.ok(retry.length >= 2, 'the retry prompt carries entries')
  retry.forEach(f => assertValid(f, 'retry prompt'))
  // Fixer prompts: the first from the final gate, the second from the recheck.
  const fixers = p.state.events.filter(l => /^fix \d+/.test(l))
  assert.ok(fixers.length >= 1, 'a fixer ran')
  for (const l of fixers) {
    const found = failuresInText(p.state.prompts[l])
    assert.ok(found.length >= 1, `${l} carries entries`)
    found.forEach(f => assertValid(f, `fixer prompt ${l}`))
  }
  // Recheck prompts: whatever failure entries they hold are valid.
  for (const l of p.state.events.filter(l => /^re-check/.test(l))) failuresInText(p.state.prompts[l]).forEach(f => assertValid(f, `recheck prompt ${l}`))
  // The result.
  const inResult = failuresIn(result)
  assert.ok(inResult.length >= 1, 'the result carries failure entries')
  inResult.forEach(f => assertValid(f, 'result'))
})

// TC-184: compact answers on the Claude Code path.
await test('TC-184: Claude Code path: every periodic, recheck and final gate command ends with --compact; the OpenCode argv has none (2.3, 2.8, 2.12)', async () => {
  const deps = linear(2)
  const p = project({ deps, gateAnswers: [compact(false, { regressions: [REG_A, REG_B], summary: { regressions: 2, excused: 0, flaky: 0, unattributed: 0 } }), compact(true), compact(true)] })
  const { result } = await runClaude(p, deps, { fullGateEvery: 2 })
  const gateLabels = Object.keys(p.state.prompts).filter(l => GATE_LABEL.test(l))
  assert.ok(gateLabels.some(l => l.startsWith('full gate @')) && gateLabels.some(l => l.startsWith('re-check')) && gateLabels.includes('final gate'), gateLabels.join(' | '))
  for (const l of gateLabels) assert.match(p.state.prompts[l], compactLine(), `${l} ends its ck command with --compact`)
  const fix = p.state.events.find(l => /^fix \d+/.test(l))
  assert.ok(fix, 'the red compact answer starts a fixer')
  assert.ok(carries(p.state.prompts[fix], REG_A) && carries(p.state.prompts[fix], REG_B), 'the compact regressions reach the fixer prompt')
  assert.ok(result.gates.every(g => g.green === (g.state === 'green')))

  const q = project({ deps })
  const { d } = await runDirect(q, { deps, extra: { fullGateEvery: 2 } })
  assert.ok(gateCallsOf(d).length >= 2)
  for (const a of gateCallsOf(d)) assert.ok(!a.includes('--compact'), a.join(' '))
})

await test('TC-184: a green compact answer records excusedByTask and report; regressions that are not an array is a gate error (2.9, 2.12)', async () => {
  const REPORT = '/repo/.git/ck/demo/gate-reports/20261108T110000Z-77.json'
  const deps = { 1: [] }
  const flaky = failure('tests/test_e.py', 'epsilon', { status: 'flaky', rerun: 'passed' })
  const p = project({ deps, gateAnswers: [compact(true, { excusedByTask: { 7: 3, 9: 1 }, flaky: [flaky], report: REPORT, summary: { regressions: 0, excused: 4, flaky: 1, unattributed: 0 } })] })
  const { result } = await runClaude(p, deps)
  const g = result.gates.find(x => x.kind === 'final')
  assert.ok(g, JSON.stringify(result.gates))
  assert.equal(g.state, 'green')
  assert.deepEqual(g.excusedByTask, { 7: 3, 9: 1 })
  assert.equal(g.report, REPORT)
  assert.equal(g.flaky.length, 1)
  assert.ok(!p.state.events.some(l => /^fix /.test(l)), 'no fixer for a green compact answer')

  const bad = project({ deps, gateAnswers: [compact(false, { regressions: 'two of them' })] })
  const { result: broken } = await runClaude(bad, deps)
  assertGateError(broken, 'compact answer with regressions not an array')
  assert.ok(!bad.state.events.some(l => /^fix /.test(l)), 'no fixer for a malformed answer')
})
