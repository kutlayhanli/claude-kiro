// The automatic test-fix loop in the workflow (unattended-correctness Stories 4 and 9): TC-36 (test-fix source),
// TC-38, TC-40, TC-42 to TC-49 and TC-84c. Runs the real plugin/workflows/spec-implement.js; only the runtime, the agents
// and the `ck` answers are faked (tests/js/fake_project.mjs).
// Run: node tests/js/test_fix.test.mjs [tc<N>_ ...]  (no arguments: every case; with them, the cases whose names start with one)
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { project } from './fake_project.mjs'
import { arbiterProject, repairRuling } from './arbiter_project.mjs'

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
  reviewer: { model: 'sonnet', effort: 'medium', enabled: true, rounds: 1, risky_model: 'opus', risky_effort: 'xhigh' },
  fixer: { model: 'sonnet', effort: 'medium' },
  orchestrator: { model: 'sonnet', effort: 'low' },
  resolver: { model: 'opus', effort: 'high' },
}
const FILE_A = 'tests/test_a.py'
const FILE_B = 'tests/test_b.py'
const conflict = (file, test, extra = {}) => ({ file, test, criterion: '4.1', reason: `${test} can never pass: rel_tol 1e-12 on a rounded input`, fix: `${test}: use rel_tol 1e-5`, ...extra })
const node = c => `${c.file}::${c.test}`
const C1 = conflict(FILE_A, 'test_tc4_decay')

// Task 4's first attempt reports `conflicts` once; the arbiter then rules each one `test_repair` (a fake `rulings` answer
// for `arbiter task 4`, its `testRepair.change` and `why` the conflict's `fix` and `reason`), the real `ck arbiter open`,
// `settle` and `record` run against a temporary git project, and the fixer follows the ruling. A later attempt passes.
async function run(conflicts, { testFix = { [FILE_A]: {} }, deps = { 4: [], 5: ['4'] }, args = {}, ...rest } = {}) {
  const proj = arbiterProject()
  const p = project({ deps, testConflictsList: { 4: { conflicts, attempts: 1 } }, rulings: { 4: conflicts.map(c => repairRuling('4', c)) }, execCk: proj.execCk, testFix, ...rest })
  const out = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, ...proj.args, agents: AGENTS, arbiterAgent: 'spec:arbiter', ...args } })
  return { p, proj, ...out }
}
// An entry holds the conflict's five fields and the settled ruling object, whose test-repair change and why are the entry's fix and reason.
const sameEntry = (e, c) => {
  const { ruling, ...rest } = e
  return JSON.stringify(rest) === JSON.stringify({ file: c.file, test: c.test, criterion: c.criterion, reason: c.reason, fix: c.fix })
    && ruling && typeof ruling === 'object' && ruling.outcome === 'test_repair' && JSON.stringify(ruling).includes(c.fix) && JSON.stringify(ruling).includes(c.reason)
}
const requests = trace => trace.filter(e => e.ev === 'request').map(e => e.label)
const fixers = trace => requests(trace).filter(l => /^testfix \d+ \(Task /.test(l))
const reviews = trace => requests(trace).filter(l => /^testfix review /.test(l))
const proves = trace => requests(trace).filter(l => /^testfix prove /.test(l))
const fixLands = trace => requests(trace).filter(l => /^land /.test(l) && /testfix/.test(l))
const taskLand = (trace, n) => trace.findIndex(e => e.ev === 'end' && /^land /.test(e.label) && e.label.slice(5).split(' ').includes(n))
const firstReq = (trace, re) => trace.findIndex(e => e.ev === 'request' && re.test(e.label))
// Every balanced JSON array in a prompt that parses.
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
const toMe = (result, task = '4') => (result.needsInput || []).filter(e => String(e.task) === task && e.kind === 'test-fix')
const noLand = trace => assert.deepEqual(fixLands(trace), [], 'no worktree land request for the test-fix branch')

await test('tc38_ a conflict ruled test_repair starts one test-fixer in the test-writer role on a branch created from INTO', async () => {
  const { p, trace } = await run([C1])
  assert.deepEqual(fixers(trace), ['testfix 1 (Task 4)'])
  const reqs = requests(trace)
  assert.equal(reqs.filter(l => /^arbiter task 4/.test(l)).length, 1, 'one arbiter ruling')
  assert.ok(!reqs.some(l => /^task 4 retry/.test(l)) || firstReq(trace, /^arbiter task 4/) < firstReq(trace, /^task 4 retry/), 'no confirmation attempt before the ruling')
  assert.ok(firstReq(trace, /^arbiter task 4/) < firstReq(trace, /^testfix 1 /), 'the arbiter rules before the fixer runs')
  const o = p.state.opts['testfix 1 (Task 4)']
  assert.deepEqual([o.model, o.effort], ['opus', 'high'], 'the test-writer model and effort')
  const prompt = p.state.prompts['testfix 1 (Task 4)']
  assert.match(prompt, /worktree create demo testfix1[^\n]*--base integrate\/demo/, 'the branch is created from the integration branch')
  assert.ok(jsonArrays(prompt).some(a => a.length === 1 && sameEntry(a[0], C1)), 'the prompt carries the structured conflict entries (testRepairEntries: the conflict with the ruling)')
})

await test('tc36_ the test-fixer prompt carries the conflicts as schema-valid entries; the thin testfix calls carry the lean agentType', async () => {
  const c2 = conflict(FILE_A, 'test_tc4_other')
  const { p, trace } = await run([C1, c2], { args: { runnerAgent: 'ck-runner' } })
  const entries = jsonArrays(p.state.prompts['testfix 1 (Task 4)']).find(a => a.length === 2)
  assert.ok(entries, 'one array of two entries')
  for (const e of entries) {
    for (const k of ['file', 'test', 'criterion', 'reason', 'fix']) assert.equal(typeof e[k], 'string', `entry.${k} is a string`)
    assert.equal(typeof e.ruling, 'object', 'entry.ruling is the settled ruling')
    assert.deepEqual(Object.keys(e).sort(), ['criterion', 'file', 'fix', 'reason', 'ruling', 'test'], 'no extra keys')
  }
  for (const l of ['testfix ledger 1', 'testfix prove 1']) {
    assert.ok(requests(trace).includes(l), `${l} was requested`)
    assert.equal(p.state.opts[l].agentType, 'ck-runner', `${l} carries the lean agentType`)
  }
})

await test('tc40_ a failed proof: no review, no land, the conflict goes to needs-input with the proof result', async () => {
  const { p, trace, result } = await run([C1], { testFix: { [FILE_A]: { proof: { proved: false, reason: 'PROOF-FAILED-XYZ: still red on the blocked branch' } } } })
  assert.deepEqual(proves(trace), ['testfix prove 1'], 'the proof ran')
  assert.deepEqual(reviews(trace), [], 'no review')
  noLand(trace)
  const e = toMe(result)
  assert.equal(e.length, 1, JSON.stringify(result.needsInput))
  assert.match(JSON.stringify(e[0]), /PROOF-FAILED-XYZ/, 'the proof result is in the note')
  assert.ok(!result.merged.includes('4'))
  assert.ok(!p.state.events.includes('task 5'), 'the dependent never starts')
})

await test('tc42_ a proved fix is reviewed at the reviewer risky tier against the test-plan rows and the guard checklist', async () => {
  const { p, trace } = await run([C1])
  assert.deepEqual(reviews(trace), ['testfix review 1'])
  const o = p.state.opts['testfix review 1']
  assert.deepEqual([o.model, o.effort], ['opus', 'xhigh'], 'risky_model and risky_effort, not the plain reviewer')
  const prompt = p.state.prompts['testfix review 1']
  assert.match(prompt, /testfix1|diff/i, 'the diff reference')
  assert.match(prompt, /test-plan/i, 'the test-plan rows')
  assert.match(prompt, /TC-4\b/, 'the TC ID of the changed test')
  assert.match(prompt, /weaken/i, 'guard checklist: no weakening')
  assert.match(prompt, /requirements/i, 'guard checklist: no requirements edits')
  assert.ok(proves(trace).length === 1 && firstReq(trace, /^testfix prove 1/) < firstReq(trace, /^testfix review 1/), 'the review follows the proof')
})

await test('tc43_ after approval: land with --gate, then a fresh task agent, then the dependent after Task 4 merges', async () => {
  const { p, trace, result } = await run([C1])
  const lands = fixLands(trace)
  assert.equal(lands.length, 1)
  assert.match(p.state.prompts[lands[0]], /worktree land demo testfix1[^\n]*--gate/)
  const fixLandEnd = trace.findIndex(e => e.ev === 'end' && e.label === lands[0])
  assert.ok(firstReq(trace, /^testfix review 1/) < trace.findIndex(e => e.ev === 'request' && e.label === lands[0]), 'review before land')
  const fresh = trace.findIndex((e, i) => e.ev === 'request' && /^task 4/.test(e.label) && i > fixLandEnd)
  assert.ok(fresh > fixLandEnd, 'a fresh task agent for Task 4 starts after the fix landed')
  const t4 = taskLand(trace, '4')
  assert.ok(t4 > fresh, 'Task 4 merges after its fresh attempt')
  assert.ok(firstReq(trace, /^task 5/) > t4, 'Task 5 starts only after Task 4 merged')
  assert.deepEqual(result.merged.filter(t => !/^testfix/.test(t)).sort(), ['4', '5'])
})

await test('tc44_ a scope violation: no review agent, needs-input, no land', async () => {
  for (const scope of [{ in_scope: false, outside: ['src/calc.py'], reason: 'SCOPE-XYZ changes src/calc.py' }, { in_scope: false, spec_docs_edited: ['specs/demo/test-plan.md'], reason: 'SCOPE-XYZ edits test-plan.md' }]) {
    const { trace, result } = await run([C1], { testFix: { [FILE_A]: { scope } } })
    assert.deepEqual(reviews(trace), [], 'no review agent')
    noLand(trace)
    const e = toMe(result)
    assert.equal(e.length, 1, JSON.stringify(result.needsInput))
    assert.match(JSON.stringify(e[0]), /SCOPE-XYZ/)
    assert.ok(!result.merged.includes('4'))
  }
})

await test('tc45_ a rejected review: nothing lands, needs-input carries the findings', async () => {
  const review = { verdict: 'changes', findings: [{ severity: 'blocking', file: FILE_A, issue: 'REVIEW-FINDING-XYZ: the tolerance was loosened beyond the criterion' }], summary: 'rejected' }
  const { trace, result } = await run([C1], { testFix: { [FILE_A]: { review } } })
  assert.deepEqual(reviews(trace), ['testfix review 1'])
  noLand(trace)
  const e = toMe(result)
  assert.equal(e.length, 1, JSON.stringify(result.needsInput))
  assert.match(JSON.stringify(e[0]), /REVIEW-FINDING-XYZ/)
  assert.ok(!result.merged.includes('4'))
})

await test('tc46_ verdict test-correct on a ruled test: no review, no land, no retry; one test-fix entry carries the ruling and the finding', async () => {
  const finding = { file: 'src/calc.py', test: 'test_tc4_decay', issue: 'FINDING-XYZ: decay() rounds before it compares' }
  const { trace, result } = await run([C1], { testFix: { [FILE_A]: { verdict: 'test-correct', finding } } })
  assert.deepEqual(reviews(trace), [])
  assert.deepEqual(proves(trace), [])
  noLand(trace)
  const fixAt = firstReq(trace, /^testfix 1 /)
  assert.ok(fixAt >= 0, 'the fixer ran')
  assert.ok(!trace.some((e, i) => e.ev === 'request' && /^task 4/.test(e.label) && i > fixAt), 'Task 4 is not retried after the fixer disagrees with the ruling')
  assert.equal(requests(trace).filter(l => /^arbiter task 4/.test(l)).length, 1, 'no second ruling')
  const e = toMe(result)
  assert.equal(e.length, 1, JSON.stringify(result.needsInput))
  assert.match(JSON.stringify(e[0]), /FINDING-XYZ/, 'the finding is in the entry')
  assert.ok(e[0].ruling != null, 'the entry carries the ruling')
  assert.match(JSON.stringify(e[0].testRepair), /rel_tol 1e-5/, 'and its test repair')
  assert.ok(!result.merged.includes('4'))
})

await test('tc47_ a node in priorFixes starts no fixer and goes to needs-input', async () => {
  const { trace, result } = await run([C1], { priorFixes: { [FILE_A]: true } })
  assert.deepEqual(fixers(trace), [], 'no test-fixer')
  assert.ok(requests(trace).includes('testfix ledger 1'), 'the ledger was read')
  noLand(trace)
  const e = toMe(result)
  assert.equal(e.length, 1, JSON.stringify(result.needsInput))
  assert.match(JSON.stringify(e[0]), /already|fixed|ledger/i, 'the note says it was fixed before')
  assert.ok(!result.merged.includes('4'))
})

await test('tc48_ three conflicts in one file and one in another: exactly two fixers, the first with all three', async () => {
  const a = ['test_tc4_one', 'test_tc4_two', 'test_tc4_three'].map(t => conflict(FILE_A, t))
  const b = conflict(FILE_B, 'test_tc4_four')
  const { p, trace } = await run([...a, b], { testFix: { [FILE_A]: {}, [FILE_B]: {} } })
  const fx = fixers(trace)
  assert.equal(fx.length, 2, fx.join(', '))
  assert.deepEqual(fx.map(l => l.match(/^testfix (\d+)/)[1]).sort(), ['1', '2'])
  const first = p.state.prompts[fx.find(l => l.startsWith('testfix 1'))]
  for (const c of a) assert.ok(first.includes(c.test), `the first fixer carries ${c.test}`)
  assert.ok(!first.includes('test_tc4_four'), 'the other file is not in the first fixer')
  assert.ok(p.state.prompts[fx.find(l => l.startsWith('testfix 2'))].includes('test_tc4_four'))
})

await test('tc49_ testFixes[] records a landed fix and one sent to me', async () => {
  const ca = conflict(FILE_A, 'test_tc4_a')
  const cb = conflict(FILE_B, 'test_tc4_b')
  const testFix = { [FILE_A]: {}, [FILE_B]: { proof: { proved: false, reason: 'PROOF-FAILED-B' } } }
  const { result } = await run([ca, cb], { testFix })
  const recs = result.testFixes
  assert.ok(Array.isArray(recs) && recs.length === 2, JSON.stringify(recs))
  const landed = recs.find(r => r.outcome === 'landed')
  const sent = recs.find(r => r.outcome === 'to-me')
  assert.ok(landed && sent, JSON.stringify(recs.map(r => r.outcome)))
  assert.equal(landed.fix, 'testfix1')
  assert.equal(landed.blocked, '4')
  assert.equal(landed.source, 'conflict')
  assert.deepEqual(landed.tests, [node(ca)])
  assert.equal(landed.proof.proved, true)
  assert.equal(landed.scope.in_scope, true)
  assert.equal(landed.review.verdict, 'approve')
  assert.equal(sent.fix, 'testfix2')
  assert.equal(sent.blocked, '4')
  assert.deepEqual(sent.tests, [node(cb)])
  assert.equal(sent.proof.proved, false)
  assert.match(String(sent.proof.reason), /PROOF-FAILED-B/)
})

await test('tc84_ a PROTECTED landing parks the task as needs-input kind protected with the approve command, and nothing lands it', async () => {
  const p = project({ deps: { 4: [], 5: ['4'] }, protectedTasks: ['4'] })
  const { trace, result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: AGENTS } })
  const lands4 = requests(trace).filter(l => /^land /.test(l) && l.slice(5).split(' ').includes('4'))
  assert.equal(lands4.length, 1, 'exactly one land request: the one that answered PROTECTED')
  assert.ok(!requests(trace).some(l => /^(resolve|fix) /.test(l)), 'nothing tries to land it another way')
  assert.equal(p.state.landings.filter(l => l.includes('4')).length, 1)
  assert.ok(!p.state.merged.has('4'), 'Task 4 never merged')
  const e = (result.needsInput || []).filter(x => String(x.task) === '4')
  assert.equal(e.length, 1, JSON.stringify(result.needsInput))
  assert.equal(e[0].kind, 'protected')
  assert.match(JSON.stringify(e[0]), /ck worktree approve demo 4/)
  assert.ok(!(result.merged || []).includes('4'))
  assert.ok(!p.state.events.includes('task 5'), 'Task 5 never starts')
})
