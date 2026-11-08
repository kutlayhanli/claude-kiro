// Test-track routing in the workflow (spec unattended-correctness): TC-55, TC-59b, TC-64b, TC-66b, TC-67, TC-68,
// TC-71b, TC-73b, TC-139b. Runs the real script (plugin/workflows/spec-implement.js) under the fake runtime
// (load_workflow.mjs) with the fake project (fake_project.mjs); nothing of the project's own code is mocked.
// Fidelity and fixture data reach the script through the direct `ck` global (directCk over the fake project).
// Run: node tests/js/test_track_routing.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow, useVirtualTime } from './load_workflow.mjs'
import { project, directCk } from './fake_project.mjs'

useVirtualTime()  // timers fall due on a simulated clock: two runs of one scenario play out identically (TC-68 compares traces)

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')
const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
// reviewer.cross_test is deliberately absent: the default (on) is what these cases exercise.
const ROLES = {
  implementer: { model: 'sonnet', effort: 'medium' },
  test_writer: { model: 'opus', effort: 'high' },
  reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 1 },
  fixer: { model: 'sonnet', effort: 'medium' },
  orchestrator: { model: 'sonnet', effort: 'low' },
  resolver: { model: 'opus', effort: 'high' },
}
const withCross = v => ({ ...ROLES, reviewer: { ...ROLES.reviewer, cross_test: v } })

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}
const RUN_LIMIT_MS = 20000
async function run(p, args, extra = {}) {
  let timer
  const limit = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`the run did not settle within ${RUN_LIMIT_MS} ms`)), RUN_LIMIT_MS) })
  try { return await Promise.race([runWorkflow(SCRIPT, { agent: p.agent, args, ...extra }), limit]) } finally { clearTimeout(timer) }
}
const failure = over => ({ node: 'tests/test_x.py::test_tc1_x', tcId: 'TC-1', tasks: ['1'], attributedBy: 'tc', file: 'tests/test_x.py', kind: 'assertion', message: 'assert 0 == 1', truncated: false, command: 'pytest', status: 'regression', excusedBy: [], rerun: 'failed', class: null, check: null, missingCommand: null, ...over })
const RETRY1 = 'task 1 [test] retry 1'
const reqIdx = (trace, re, from = 0) => trace.findIndex((e, i) => i >= from && e.ev === 'request' && re.test(e.label))
const endIdx = (trace, re, from = 0) => trace.findIndex((e, i) => i >= from && e.ev === 'end' && re.test(e.label))
const reviewPrompts = p => Object.entries(p.state.prompts).filter(([l]) => l.startsWith('review')).map(([, t]) => t)

// --- TC-55 / TC-59b: a failing test-track gate goes back to the test writer ------------------------------------

await test('TC-55: a test-track gate with class "bad" entries retries with the test-writer role and the marked list', async () => {
  const bad = failure({ node: 'tests/test_x.py::test_tc1_wrong_reason', class: 'bad', message: 'ZZ_BAD_MARK ImportError: no module named calc' })
  const unclear = failure({ node: 'tests/test_x.py::test_tc1_other', class: 'unclear', message: 'ZZ_UNCLEAR_MARK assert 1 == 2' })
  const p = project({ deps: { 1: [] }, failUntil: { 1: 1 }, taskFailures: { 1: [bad, unclear] } })
  const { result } = await run(p, { ...baseArgs, tracks: { 1: 'test' }, agents: ROLES })
  const retry = p.state.opts[RETRY1]
  assert.ok(retry, 'the failed gate was retried')
  assert.equal(retry.model, 'opus', 'the retry runs with the test-writer role, not the implementer')
  const prompt = p.state.prompts[RETRY1]
  assert.match(prompt, /test_tc1_wrong_reason/)
  assert.match(prompt, /ZZ_BAD_MARK/)
  assert.match(prompt, /"class":\s*"bad"/, 'the entry stays marked bad')
  assert.deepEqual(result.merged, ['1'])
})

await test('TC-59b: a check "validate" failure sends the task back to the test writer', async () => {
  const tail = Array.from({ length: 40 }, (_, i) => `validate line ${i}`).join('\n')
  const v = failure({ node: null, tcId: null, tasks: ['1'], file: null, kind: 'error', check: 'validate', command: 'node scripts/validate.js', message: `ZZ_VALIDATE_TAIL ${tail}`, status: 'regression' })
  const p = project({ deps: { 1: [], 2: ['1'] }, failUntil: { 1: 1 }, taskFailures: { 1: [v] } })
  const { result } = await run(p, { ...baseArgs, tracks: { 1: 'test', 2: 'impl' }, agents: ROLES })
  const retry = p.state.opts[RETRY1]
  assert.ok(retry, 'the validation failure was retried')
  assert.equal(retry.model, 'opus')
  const prompt = p.state.prompts[RETRY1]
  assert.match(prompt, /ZZ_VALIDATE_TAIL/)
  assert.match(prompt, /"check":\s*"validate"/)
  assert.match(prompt, /node scripts\/validate\.js/)
  assert.ok(result.merged.includes('1'))
})

// --- TC-64b / TC-66b: cross-test default and switch --------------------------------------------------------

const crossArgs = agents => ({ ...baseArgs, wave: { 1: 1, 2: 1, 3: 2 }, tracks: { 1: 'test', 2: 'test', 3: 'impl' }, agents })
const crossDeps = { 1: [], 2: [], 3: ['1', '2'] }
const APPROVE = { verdict: 'approve', findings: [], summary: 'consistent' }

await test('TC-64b: args without cross_test request one cross-test agent once the test-track wave has merged', async () => {
  const p = project({ deps: crossDeps, crossTest: APPROVE })
  const { result } = await run(p, crossArgs(ROLES))
  const labels = p.state.events.filter(e => e.startsWith('cross-test') && !e.endsWith(' end'))
  assert.deepEqual(labels, ['cross-test wave 1'])
  const lastLand = p.state.events.findIndex(e => e.startsWith('land ') && e.endsWith(' end') && e.replace(/ end$/, '').slice(5).split(' ').includes('2'))
  assert.ok(p.state.events.indexOf('cross-test wave 1') > lastLand, 'requested after the last test task of the wave merged')
  assert.equal(result.crossTests.length, 1)
  assert.deepEqual(result.crossTests[0].tasks, ['1', '2'])
})

await test('TC-66b: cross_test false means no cross-test agent', async () => {
  const p = project({ deps: crossDeps })
  const { result } = await run(p, crossArgs(withCross(false)))
  assert.ok(!p.state.events.some(e => e.startsWith('cross-test')), p.state.events.join('\n'))
  assert.deepEqual(result.crossTests, [])
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'])
})

// --- TC-67: an open finding holds the tasks its tests verify, and only those ----------------------------------

await test('TC-67: an open finding holds Task 2 until the test-fix lands while unrelated Task 9 runs', async () => {
  const finding = { severity: 'blocking', file: 'tests/test_a.py', test: 'tests/test_a.py::test_tc1_a', issue: 'reads "state" where tests/test_b.py says "status"' }
  const p = project({
    deps: { 1: [], 3: [], 2: ['1', '3'], 9: ['1', '3'] },
    crossTest: [{ verdict: 'changes', findings: [finding], summary: 'one drift' }, APPROVE],
    testVerifies: { 'tests/test_a.py': ['2'] },
    testFix: { 'tests/test_a.py': {} },
    mergeDurations: { testfix1: 80 },  // the fix's landing takes long enough for the held and free tasks to separate
  })
  const { result, trace } = await run(p, { ...baseArgs, wave: { 1: 1, 3: 1, 2: 2, 9: 2 }, tracks: { 1: 'test', 3: 'test', 2: 'impl', 9: 'impl' }, agents: ROLES })
  const fixLand = endIdx(trace, /^land .*testfix/)
  assert.ok(fixLand >= 0, 'the test-fix was landed: ' + trace.filter(e => e.ev === 'request').map(e => e.label).join(' | '))
  const t9 = reqIdx(trace, /^task 9\b/)
  const t2 = reqIdx(trace, /^task 2\b/)
  assert.ok(t9 >= 0 && t9 < fixLand, 'unrelated Task 9 ran while the finding was open')
  assert.ok(t2 > fixLand, 'Task 2 was not requested before the test-fix landed')
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '9'])
})

// --- TC-68: no findings, same trace as cross_test false --------------------------------------------------------

await test('TC-68: with no findings the trace equals that of cross_test false, apart from the cross-test agent', async () => {
  const mk = () => project({ deps: crossDeps, crossTest: APPROVE })
  const on = mk()
  const off = mk()
  const a = await run(on, crossArgs(ROLES))
  const b = await run(off, crossArgs(withCross(false)))
  assert.ok(on.state.events.some(e => e.startsWith('cross-test')), 'the default run did ask the cross-test agent')
  assert.ok(!off.state.events.some(e => e.startsWith('cross-test')))
  assert.ok(!on.state.events.some(e => e.startsWith('testfix')), 'no test-fixer without findings')
  const shape = t => t.trace.filter(e => !/^cross-test/.test(e.label)).map(e => `${e.ev} ${e.label}`)
  assert.deepEqual(shape(a), shape(b))
  assert.deepEqual(a.result.merged, b.result.merged)
  assert.deepEqual(on.state.landings, off.state.landings)
})

// --- TC-71b / TC-73b: row-fidelity items in the review prompt -----------------------------------------------

const reviewArgs = { ...baseArgs, tracks: { 1: 'test', 2: 'impl' }, agents: ROLES }
const tv = (name, value) => ({ test: `tests/test_x.py::test_tc1_${name}`, kind: 'number', value, name, role: 'asserted', line: 12 })

await test('TC-71b: the test-task review prompt holds exactly the unparsed and unmatched items, none of the checked ones', async () => {
  const fidelity = {
    checked: [tv('checked_a', 'ZZ_CHECKED_A'), tv('checked_b', 'ZZ_CHECKED_B')],
    mismatches: [],
    unparsed: [tv('computed', 'ZZ_UNPARSED_BASE_TIMES_1_1')],
    unmatched: [{ ...tv('extra', 'ZZ_UNMATCHED_77'), role: 'fed' }],
  }
  const p = project({ deps: { 1: [], 2: ['1'] }, fidelity })
  const d = directCk(p)
  await run(p, reviewArgs, { ck: d.fn })
  assert.ok(d.calls.some(c => c.includes('--fidelity')), 'the script asked ck for the row-fidelity lists')
  const prompts = reviewPrompts(p)
  const testReviews = prompts.filter(t => /--contracts/.test(t))
  assert.ok(testReviews.length >= 1, 'the test task was reviewed')
  for (const t of testReviews) {
    assert.match(t, /ZZ_UNPARSED_BASE_TIMES_1_1/)
    assert.match(t, /ZZ_UNMATCHED_77/)
    assert.doesNotMatch(t, /ZZ_CHECKED_A|ZZ_CHECKED_B/)
  }
  for (const t of prompts.filter(t => !/--contracts/.test(t))) assert.doesNotMatch(t, /ZZ_UNPARSED_BASE_TIMES_1_1|ZZ_UNMATCHED_77/, 'the impl task review carries no fidelity items')
})

await test('TC-73b: when nothing is unparsed or unmatched the review prompt has no row-fidelity section', async () => {
  const fidelity = { checked: [tv('checked_a', 'ZZ_CHECKED_A')], mismatches: [], unparsed: [], unmatched: [] }
  const p = project({ deps: { 1: [], 2: ['1'] }, fidelity })
  const d = directCk(p)
  await run(p, reviewArgs, { ck: d.fn })
  const testReviews = reviewPrompts(p).filter(t => /--contracts/.test(t))
  assert.ok(testReviews.length >= 1, 'the test task was reviewed')
  for (const t of testReviews) {
    assert.doesNotMatch(t, /fidelity/i)
    assert.doesNotMatch(t, /ZZ_CHECKED_A/)
  }
})

// --- TC-139b: unlabeled fixtures -------------------------------------------------------------------------------

await test('TC-139b: fixtureStatus.unlabeled reaches the review prompt and a blocking finding sends the task back', async () => {
  const unlabeled = 'tests/fixtures/ZZ_unlabeled_capture.json'
  const fixtureStatus = { locked: [], drift: [], unlabeled: [unlabeled], configError: null }
  const finding = { task: '1', severity: 'blocking', file: unlabeled, issue: 'fixture has no capture metadata and is not marked handcrafted', fix: 'label it with ck fixtures mark' }
  const p = project({ deps: { 1: [], 2: ['1'] }, fixtureStatus, reviews: { 1: [{ verdict: 'changes', findings: [finding], summary: 'unlabeled fixture' }, 'approve'] } })
  const d = directCk(p)
  const { result } = await run(p, reviewArgs, { ck: d.fn })
  assert.ok(d.calls.some(c => c[0] === 'fixtures' || c.includes('fixtures')), 'the script asked ck for the fixture status')
  const testReviews = reviewPrompts(p).filter(t => /--contracts/.test(t))
  assert.ok(testReviews.length >= 1)
  assert.match(testReviews[0], /ZZ_unlabeled_capture\.json/)
  assert.ok(p.state.events.some(e => /^task 1\b.* revise /.test(e) && !e.endsWith(' end')), 'the blocking finding sent Task 1 back: ' + p.state.events.join(' | '))
  assert.ok(result.merged.includes('1'))
})

await test('TC-139b: with no unlabeled fixtures the review prompt names none', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, fixtureStatus: { locked: [], drift: [], unlabeled: [], configError: null } })
  const d = directCk(p)
  await run(p, reviewArgs, { ck: d.fn })
  for (const t of reviewPrompts(p).filter(t => /--contracts/.test(t))) assert.doesNotMatch(t, /unlabeled|handcrafted/i)
})
