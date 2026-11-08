// Cross-test findings routed into the test-fix loop (unattended-correctness Stories 4 and 7): TC-39, TC-65, TC-186.
// Runs the real plugin/workflows/spec-implement.js; only the runtime, the agents and the `ck` answers are faked.
// Run: node tests/js/test_fix_cross.test.mjs [tc<N>_ ...]
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { project } from './fake_project.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')
const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }

const only = process.argv.slice(2).filter(a => /^tc\d+_/.test(a))
async function test(name, fn) {
  if (only.length && !only.some(p => name.startsWith(p))) return
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

const AGENTS = {
  implementer: { model: 'sonnet', effort: 'medium' },
  test_writer: { model: 'opus', effort: 'high' },
  reviewer: { model: 'sonnet', effort: 'medium', enabled: true, rounds: 1, risky_model: 'opus', risky_effort: 'xhigh', cross_test: true },
  fixer: { model: 'sonnet', effort: 'medium' },
  orchestrator: { model: 'sonnet', effort: 'low' },
  resolver: { model: 'opus', effort: 'high' },
}
const FILE = 'tests/test_b.py'
const finding = (test, extra = {}) => ({ severity: 'blocking', file: FILE, test, issue: `${test} reads "state" where design.md says "status"`, ...extra })
const entry = f => ({ file: f.file, test: f.test })

// Test tasks 1 and 3 (wave 1) merge; impl Task 2 (wave 2) depends on both and its tests verify FILE.
async function run(findings, testFix = { [FILE]: {} }, extra = {}) {
  const deps = { 1: [], 3: [], 2: ['1', '3'] }
  const p = project({ deps, testFix, testVerifies: { [FILE]: ['2'] }, crossTest: { verdict: 'changes', findings, summary: 'drift' }, ...extra })
  const out = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, wave: { 1: 1, 3: 1, 2: 2 }, tracks: { 1: 'test', 3: 'test', 2: 'impl' }, agents: AGENTS } })
  return { p, ...out }
}
const requests = trace => trace.filter(e => e.ev === 'request').map(e => e.label)
const fixers = trace => requests(trace).filter(l => /^testfix \d+ \(/.test(l))
const fixLands = trace => requests(trace).filter(l => /^land /.test(l) && /testfix/.test(l))
const firstReq = (trace, re) => trace.findIndex(e => e.ev === 'request' && re.test(e.label))
const toMe = result => (result.needsInput || []).filter(e => e.kind === 'test-fix')
function jsonArrays(text) {
  const out = []
  for (let i = 0; i < text.length; i++) {
    if (text[i] !== '[') continue
    let depth = 0, inStr = false
    for (let j = i; j < text.length; j++) {
      const ch = text[j]
      if (inStr) { if (ch === '\\') j++; else if (ch === '"') inStr = false; continue }
      if (ch === '"') inStr = true
      else if (ch === '[') depth++
      else if (ch === ']' && --depth === 0) { try { out.push(JSON.parse(text.slice(i, j + 1))) } catch {} break }
    }
  }
  return out
}

await test('tc39_ a blocking cross-test finding naming 2 tests starts a test-fixer with those 2 tests as conflict entries', async () => {
  const f1 = finding('test_tc7_one')
  const f2 = finding('test_tc7_two')
  const { p, trace } = await run([f1, f2])
  assert.equal(fixers(trace).length, 1, `one fixer for the one file: ${fixers(trace)}`)
  const prompt = p.state.prompts[fixers(trace)[0]]
  const entries = jsonArrays(prompt).find(a => a.length === 2)
  assert.ok(entries, 'a conflict array with the two entries')
  assert.deepEqual(entries.map(entry), [entry(f1), entry(f2)])
  for (const e of entries) for (const k of ['file', 'test', 'criterion', 'reason', 'fix']) assert.equal(typeof e[k], 'string', `entry.${k} is a string`)
  const o = p.state.opts[fixers(trace)[0]]
  assert.deepEqual([o.model, o.effort], ['opus', 'high'], 'the test-writer role')
})

await test('tc65_ every cross-test finding is blocking: turned into conflict entries for the test-fixer; no findings proceeds as today', async () => {
  const major = finding('test_tc7_blocking')
  const minor = finding('test_tc7_minor', { severity: 'minor' })
  const { p, trace } = await run([major, minor])
  assert.equal(fixers(trace).length, 1)
  const prompt = p.state.prompts[fixers(trace)[0]]
  assert.match(prompt, /test_tc7_blocking/)
  assert.match(prompt, /test_tc7_minor/, 'a minor finding is a conflict entry too')
  const none = await run([], { [FILE]: {} }, { crossTest: { verdict: 'approve', findings: [], summary: 'ok' } })
  assert.deepEqual(fixers(none.trace), [], 'no findings starts no fixer')
  assert.ok(none.result.merged.includes('2'), 'a run with no findings proceeds')
})

await test('tc186_ a cross-test fix proves with --held 2 and no --blocked, Task 2 starts after the fix lands, the record says so', async () => {
  const { p, trace, result } = await run([finding('test_tc7_one')])
  const prove = p.state.prompts['testfix prove 1']
  assert.ok(prove, 'testfix prove 1 ran')
  assert.match(prove, /--held[ =]2\b/)
  assert.doesNotMatch(prove, /--blocked/)
  const lands = fixLands(trace)
  assert.equal(lands.length, 1)
  assert.match(p.state.prompts[lands[0]], /--gate/)
  const landEnd = trace.findIndex(e => e.ev === 'end' && e.label === lands[0])
  assert.ok(landEnd >= 0)
  assert.ok(firstReq(trace, /^task 2( |$)/) > landEnd, 'Task 2 starts only after the fix landed')
  assert.ok(result.merged.includes('2'))
  const rec = (result.testFixes || []).find(r => r.source === 'cross-test')
  assert.ok(rec, JSON.stringify(result.testFixes))
  assert.equal(rec.blocked, null)
  assert.deepEqual(rec.held, ['2'])
  assert.equal(rec.outcome, 'landed')
})

await test('tc186_ with proof fail: nothing lands, Task 2 stays held, the conflict goes to needs-input', async () => {
  const { p, trace, result } = await run([finding('test_tc7_one')], { [FILE]: { proof: { proved: false, reason: 'PROOF-FAILED-XYZ' } } })
  assert.match(p.state.prompts['testfix prove 1'] || '', /--held[ =]2\b/, 'the proof ran with --held 2')
  assert.deepEqual(fixLands(trace), [], 'nothing lands')
  assert.ok(!requests(trace).some(l => /^task 2( |$)/.test(l)), 'Task 2 stays held')
  assert.ok(!result.merged.includes('2'))
  const e = toMe(result)
  assert.ok(e.length >= 1, JSON.stringify(result.needsInput))
  assert.match(JSON.stringify(e), /PROOF-FAILED-XYZ/)
  const rec = (result.testFixes || []).find(r => r.source === 'cross-test')
  assert.ok(rec && rec.outcome === 'to-me' && rec.blocked === null, JSON.stringify(result.testFixes))
})
