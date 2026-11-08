// Red team of Story 12 (spec unattended-correctness), written from the requirements only:
//   12.5 a missing-command gate failure for a command another unfinished task provides adds that dependency
//        edge; the task waits for the provider to merge, then retries; it SHALL NOT raise a test conflict or
//        count the attempt as a retry.
//   12.7 a live edge that would create a dependency cycle SHALL NOT be added; the task is reported blocked,
//        naming the cycle.
// Tests named tc114_redteam (12.5) and tc117_redteam (12.7). They run the real script under the fake runtime
// and judge by the trace, the log and the result (failed, testFixes, liveEdges). A failure entry carries
// `missingCommand: {command, providers}` as ck prints it (ck-gate/1); the message text is varied freely.
// Run: node tests/js/command_deps_redteam.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow, useVirtualTime } from './load_workflow.mjs'
import { project } from './fake_project.mjs'

useVirtualTime()  // sleeps run on the simulated clock, so the ordering assertions do not depend on machine load
const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')
const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

const RUN_LIMIT_MS = 20000
async function run(p, args) {
  let timer
  const limit = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`the run did not settle within ${RUN_LIMIT_MS} ms`)), RUN_LIMIT_MS) })
  try { return await Promise.race([runWorkflow(SCRIPT, { agent: p.agent, args }), limit]) } finally { clearTimeout(timer) }
}
const graphArgs = deps => ({
  deps: Object.fromEntries(Object.entries(deps).map(([n, ds]) => [String(n), ds.map(String)])),
  remaining: Object.keys(deps).map(String),
})

const CMD = 'mtr thesis new'
const failure = (message, providers, command = CMD, extra = {}) => ({
  node: 'tests/test_thesis.py::test_tc9_new', tcId: 'TC-9', tasks: ['2'], attributedBy: 'tc', file: 'tests/test_thesis.py',
  kind: 'assertion', message, truncated: false, command: 'uv run pytest -q', status: 'regression', excusedBy: [], rerun: 'failed',
  class: null, check: null, missingCommand: providers === null ? null : { command, providers }, ...extra,
})
const MESSAGES = {
  plain: "Error: No such command 'thesis'.",
  curly: 'Error: No such command ‘thesis’.',
  curlyDouble: 'Error: No such command “thesis new”.',
  ansi: '\u001b[31mError:\u001b[0m No such command \u001b[1m‘thesis’\u001b[0m.',
  wrapped: 'Error: No such\ncommand\n  ‘thesis’.',
  argparse: 'mtr: error: unrecognized arguments: thesis new',
  invalidChoice: "mtr: error: argument command: invalid choice: 'thesis' (choose from 'version')",
}

// --- trace helpers ----------------------------------------------------------------
const reqIdx = (trace, re) => trace.map((e, i) => [e, i]).filter(([e]) => e.ev === 'request' && re.test(e.label)).map(([, i]) => i)
const landEndIdx = (trace, n) => trace.findIndex(e => e.ev === 'end' && /^land /.test(e.label) && e.label.slice(5).split(' ').includes(String(n)))
const task2Requests = trace => reqIdx(trace, /^task 2( |$)/)
const retryLabels = events => events.filter(e => /^task 2 retry/.test(e) && !e.endsWith(' end'))

// No test conflict, by the design's own records: no test-fix ran (testFixes empty) and the task is not parked as
// needs-input with kind "test-fix" (design, error handling 4.5/4.6).
function assertNoTestConflict(result, v) {
  assert.deepEqual(result.testFixes ?? [], [], 'no test conflict is raised (no test fix)')
  const parkedForTestFix = (result.needsInput ?? []).filter(e => String(e.task) === String(v) && e.kind === 'test-fix')
  assert.deepEqual(parkedForTestFix, [], `task ${v} is not parked for a test fix`)
}

// The 12.5 contract for one waiting task V on provider P: edge listed, no conflict, no retry used, V re-requested only
// after P's land ended, V merged in the end.
function assertWaited({ result, trace, logs, p }, v, provider, command = CMD) {
  assert.ok(Array.isArray(result.liveEdges), 'the result lists liveEdges')
  const edges = result.liveEdges.filter(e => e.task === v)
  assert.ok(edges.some(e => e.dependsOn === provider && e.command === command), `live edge ${v} -> ${provider} missing: ${JSON.stringify(result.liveEdges)}`)
  assertNoTestConflict(result, v)
  assert.deepEqual(retryLabels(p.state.events), [], `no retry label: ${p.state.events.join(', ')}`)
  const reqs = task2Requests(trace)
  assert.ok(reqs.length >= 2, `task ${v} is requested again after waiting`)
  assert.ok(reqs[1] > landEndIdx(trace, provider) && landEndIdx(trace, provider) >= 0, `task ${v} re-requested before task ${provider}'s land ended`)
  assert.ok(result.merged.includes(v) && result.merged.includes(provider), `merged: ${result.merged}`)
  assert.ok(logs.some(l => String(l).includes(command) && /Task 2/.test(String(l)) && /Task 5/.test(String(l))), 'the log names the live edge')
}

// ================= 12.5 =================

// The workflow reads ck's `missingCommand` mark, not the message text: these cases set the mark by hand and only
// confirm that the text is ignored when the mark is set. Surface-form detection (curly quotes, ANSI, wrapped lines,
// argparse) is ck's job and is not exercised here.
for (const [form, message] of Object.entries(MESSAGES)) {
  await test(`tc114_redteam text is ignored when the mark is set (${form} message): edge added, no conflict, no retry spent (maxRetries 0)`, async () => {
    const p = project({ deps: { 2: [], 5: [] }, failUntil: { 2: 1 }, durations: { 2: 5, 5: 40 }, mergeDurations: { 5: 10 }, taskFailures: { 2: [failure(message, ['5'])] } })
    const o = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), maxRetries: 0, maxConcurrent: 2, mergeBatch: 1 })
    assertWaited({ ...o, p }, '2', '5')
  })
}

await test('tc114_redteam text is ignored when the mark is set (empty message)', async () => {
  const p = project({ deps: { 2: [], 5: [] }, failUntil: { 2: 1 }, durations: { 2: 5, 5: 40 }, taskFailures: { 2: [failure('', ['5'])] } })
  const o = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), maxRetries: 0, maxConcurrent: 2, mergeBatch: 1 })
  assertWaited({ ...o, p }, '2', '5')
})

await test('tc114_redteam a missing-command text inside an assertion on an exit code (no missingCommand mark) adds no edge', async () => {
  const msg = "assert 2 == 0\n +  where 2 = CliRunner().invoke(cli, ['thesis', 'new']).exit_code  # Error: No such command 'thesis'."
  const p = project({ deps: { 2: [], 5: [] }, failUntil: { 2: 1 }, durations: { 2: 5, 5: 40 }, taskFailures: { 2: [failure(msg, null)] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), maxRetries: 1, maxConcurrent: 2, mergeBatch: 1 })
  assert.deepEqual(result.liveEdges ?? [], [], 'only a ck-marked missing command adds an edge')
  assert.ok(p.state.events.includes('task 2 retry 1'), 'an ordinary failure uses its retry')
})

await test('tc114_redteam an unmarked "invalid choice" assertion quoting a command adds no edge', async () => {
  const p = project({ deps: { 2: [], 5: [] }, failUntil: { 2: 1 }, durations: { 2: 5, 5: 40 }, taskFailures: { 2: [failure("AssertionError: 'invalid choice' not in output of `mtr thesis new`", null)] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), maxRetries: 1, maxConcurrent: 2, mergeBatch: 1 })
  assert.deepEqual(result.liveEdges ?? [], [])
})

await test('tc114_redteam provider is a Done task: no edge, no wait', async () => {
  const p = project({ deps: { 2: [], 5: [] }, done: ['5'], failUntil: { 2: 1 }, taskFailures: { 2: [failure(MESSAGES.plain, ['5'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), remaining: ['2'], maxRetries: 1 })
  assert.deepEqual(result.liveEdges ?? [], [], 'a Done provider is not an unfinished task')
})

await test('tc114_redteam provider is the failing task itself: no self edge, the run settles', async () => {
  const p = project({ deps: { 2: [], 5: [] }, failUntil: { 2: 1 }, taskFailures: { 2: [failure(MESSAGES.plain, ['2'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), maxRetries: 1, maxConcurrent: 2 })
  assert.ok(!(result.liveEdges ?? []).some(e => e.dependsOn === e.task), `self edge: ${JSON.stringify(result.liveEdges)}`)
})

await test('tc114_redteam provider is not a task of the run (dangling id): no edge to it', async () => {
  const p = project({ deps: { 2: [], 5: [] }, failUntil: { 2: 1 }, taskFailures: { 2: [failure(MESSAGES.plain, ['99'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), maxRetries: 1, maxConcurrent: 2 })
  assert.ok(!(result.liveEdges ?? []).some(e => e.dependsOn === '99'), 'an edge to an unknown task would wait forever')
})

await test('tc114_redteam a command provided by two unfinished tasks: edges only to providers, no conflict, no retry', async () => {
  const deps = { 2: [], 5: [], 6: [] }
  const p = project({ deps, failUntil: { 2: 1 }, durations: { 2: 5, 5: 40, 6: 60 }, mergeDurations: { 5: 10, 6: 10 }, taskFailures: { 2: [failure(MESSAGES.curly, ['5', '6'])] } })
  const { result, trace } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 0, maxConcurrent: 3, mergeBatch: 1 })
  const edges = result.liveEdges.filter(e => e.task === '2')
  assert.ok(edges.length >= 1 && edges.every(e => ['5', '6'].includes(e.dependsOn)), JSON.stringify(result.liveEdges))
  assert.equal(new Set(edges.map(e => e.dependsOn)).size, edges.length, 'no edge listed twice')
  assert.deepEqual(result.testFixes ?? [], [])
  assert.deepEqual(retryLabels(p.state.events), [])
  const reqs = task2Requests(trace)
  for (const e of edges) assert.ok(reqs[1] > landEndIdx(trace, e.dependsOn), `re-requested before the land of ${e.dependsOn}`)
  assert.ok(result.merged.includes('2'))
})

await test('tc114_redteam a provider listed twice gives one edge', async () => {
  const p = project({ deps: { 2: [], 5: [] }, failUntil: { 2: 1 }, durations: { 2: 5, 5: 40 }, taskFailures: { 2: [failure(MESSAGES.plain, ['5', '5'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), maxRetries: 0, maxConcurrent: 2, mergeBatch: 1 })
  assert.equal(result.liveEdges.filter(e => e.task === '2' && e.dependsOn === '5').length, 1)
})

await test('tc114_redteam the same failure reported twice adds the edge once', async () => {
  const f = failure(MESSAGES.plain, ['5'])
  const p = project({ deps: { 2: [], 5: [] }, failUntil: { 2: 1 }, durations: { 2: 5, 5: 40 }, taskFailures: { 2: [f, { ...f }] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), maxRetries: 0, maxConcurrent: 2, mergeBatch: 1 })
  assert.equal(result.liveEdges.filter(e => e.task === '2' && e.dependsOn === '5').length, 1, JSON.stringify(result.liveEdges))
})

await test('tc114_redteam the same missing command again after the provider merged adds no second edge', async () => {
  const p = project({ deps: { 2: [], 5: [] }, failUntil: { 2: 2 }, durations: { 2: 5, 5: 40 }, taskFailures: { 2: [failure(MESSAGES.plain, ['5'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs({ 2: [], 5: [] }), maxRetries: 2, maxConcurrent: 2, mergeBatch: 1 })
  assert.equal(result.liveEdges.filter(e => e.task === '2' && e.dependsOn === '5').length, 1, JSON.stringify(result.liveEdges))
})

await test('tc114_redteam the edge already in deps (a liveEdges record written back by relink): no new edge, no conflict', async () => {
  const deps = { 2: ['5'], 5: [] }
  const p = project({ deps, failUntil: { 2: 1 }, taskFailures: { 2: [failure(MESSAGES.plain, ['5'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 1 })
  assert.deepEqual(result.liveEdges ?? [], [], 'the provider is a declared dependency and already merged')
  assert.deepEqual(result.testFixes ?? [], [])
})

// Two waits in a row: attempt 1 misses Task 5's command; after Task 5 merged, attempt 2 misses Task 6's command
// (Task 6 depends on Task 5, so it had not even started when attempt 1 ran). Neither wait spends a retry.
await test('tc114_redteam a task that waits twice in a row (a second missing command surfaces after the first provider merged) spends no retries', async () => {
  const deps = { 2: [], 5: [], 6: ['5'] }
  const p = project({ deps, failUntil: { 2: 2 }, durations: { 2: 5, 5: 30, 6: 30 }, mergeDurations: { 5: 5, 6: 5 },
    taskFailures: { 2: [[failure(MESSAGES.plain, ['5'])], [failure('Error: No such command ‘other’.', ['6'], 'mtr thesis list')]] } })
  const { result, trace } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 0, maxConcurrent: 3, mergeBatch: 1 })
  assert.deepEqual(retryLabels(p.state.events), [], `no retry label: ${p.state.events.join(', ')}`)
  assertNoTestConflict(result, '2')
  assert.ok(Array.isArray(result.liveEdges), 'the result lists liveEdges')
  assert.deepEqual(result.liveEdges.filter(e => e.task === '2').map(e => e.dependsOn).sort(), ['5', '6'])
  const reqs = task2Requests(trace)
  assert.equal(reqs.length, 3, `three attempts (fail, wait, fail, wait, pass): ${p.state.events.join(', ')}`)
  assert.ok(reqs[1] > landEndIdx(trace, '5') && reqs[2] > landEndIdx(trace, '6'), 'each re-request comes after the land of the provider it waited for')
  assert.ok(result.merged.includes('2'), `merged: ${result.merged}`)
})

// ================= 12.7 =================

const cycleRe = (...ids) => new RegExp(ids.map(i => `(?:Task )?${i}`).join(' -> '))

await test('tc117_redteam direct cycle: no edge, blocked naming 2 -> 5 -> 2, no conflict, no retry', async () => {
  const deps = { 2: [], 5: ['2'] }
  const p = project({ deps, failUntil: { 2: 1 }, taskFailures: { 2: [failure(MESSAGES.plain, ['5'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 1 })
  assert.deepEqual(result.liveEdges ?? [], [], 'the edge that would close the cycle is not added')
  const f = result.failed.find(x => x.task === '2')
  assert.ok(f && /^blocked/.test(f.reason) && cycleRe(2, 5, 2).test(f.reason), JSON.stringify(result.failed))
  assert.deepEqual(result.testFixes ?? [], [])
  assert.ok(!result.merged.includes('2') && !result.merged.includes('5'))
})

await test('tc117_redteam text is ignored when the mark is set (curly, ANSI, wrapped, argparse messages): the cycle is still blocked', async () => {
  for (const m of [MESSAGES.curly, MESSAGES.ansi, MESSAGES.wrapped, MESSAGES.argparse]) {
    const deps = { 2: [], 5: ['2'] }
    const p = project({ deps, failUntil: { 2: 1 }, taskFailures: { 2: [failure(m, ['5'])] } })
    const { result } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 1 })
    assert.deepEqual(result.liveEdges ?? [], [], JSON.stringify(m))
    assert.ok(result.failed.some(x => x.task === '2' && cycleRe(2, 5, 2).test(x.reason)), JSON.stringify(m))
  }
})

await test('tc117_redteam a longer cycle through three tasks is found: 2 -> 5 -> 4 -> 2', async () => {
  const deps = { 2: [], 4: ['2'], 5: ['4'] }
  const p = project({ deps, failUntil: { 2: 1 }, taskFailures: { 2: [failure(MESSAGES.plain, ['5'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 1 })
  assert.deepEqual(result.liveEdges ?? [], [])
  const f = result.failed.find(x => x.task === '2')
  assert.ok(f && cycleRe(2, 5, 4, 2).test(f.reason), JSON.stringify(result.failed))
  assert.deepEqual(result.testFixes ?? [], [])
})

await test('tc117_redteam a four-task chain cycle is found', async () => {
  const deps = { 2: [], 3: ['2'], 4: ['3'], 5: ['4'] }
  const p = project({ deps, failUntil: { 2: 1 }, taskFailures: { 2: [failure(MESSAGES.plain, ['5'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 1 })
  assert.deepEqual(result.liveEdges ?? [], [])
  assert.ok(result.failed.some(x => x.task === '2' && cycleRe(2, 5, 4, 3, 2).test(x.reason)), JSON.stringify(result.failed))
})

await test('tc117_redteam a cycle formed only with a previously added live edge is refused, the earlier edge stays', async () => {
  // Task 2 fails first and waits for 5 (edge 2 -> 5). Task 5 then fails on a command Task 2 provides: 5 -> 2 would close it.
  const deps = { 2: [], 5: [] }
  const p = project({ deps, failUntil: { 2: 1, 5: 1 }, durations: { 2: 5, 5: 60 }, mergeDurations: { 5: 5 },
    taskFailures: { 2: [failure(MESSAGES.plain, ['5'])], 5: [failure("Error: No such command 'thesis list'.", ['2'], 'mtr thesis list')] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 1, maxConcurrent: 2, mergeBatch: 1 })
  assert.deepEqual(result.liveEdges.map(e => [e.task, e.dependsOn]), [['2', '5']], JSON.stringify(result.liveEdges))
  const f = result.failed.find(x => x.task === '5')
  assert.ok(f && /^blocked/.test(f.reason) && cycleRe(5, 2, 5).test(f.reason), JSON.stringify(result.failed))
  assert.deepEqual(result.testFixes ?? [], [])
  assert.ok(!result.merged.includes('5'))
})

// 12.7 applies to the 2 -> 5 edge whatever happens to 2 -> 6: that edge is not added and Task 2 is reported blocked,
// naming the cycle. Whether the safe 2 -> 6 edge is also recorded is left open by the design, so it is not asserted.
await test('tc117_redteam one provider cycles and another does not: the cyclic edge is not added, the task is blocked naming 2 -> 5 -> 2', async () => {
  const deps = { 2: [], 5: ['2'], 6: [] }
  const p = project({ deps, failUntil: { 2: 1 }, durations: { 2: 5, 6: 40 }, mergeDurations: { 6: 5 }, taskFailures: { 2: [failure(MESSAGES.plain, ['5', '6'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 0, maxConcurrent: 3, mergeBatch: 1 })
  assert.ok(!(result.liveEdges ?? []).some(e => e.task === '2' && e.dependsOn === '5'), JSON.stringify(result.liveEdges))
  const f = result.failed.find(x => x.task === '2')
  assert.ok(f && /^blocked/.test(f.reason) && cycleRe(2, 5, 2).test(f.reason), JSON.stringify(result.failed))
  assert.ok(!result.merged.includes('2'), 'a blocked task does not merge')
  assertNoTestConflict(result, '2')
})

await test('tc117_redteam the blocked reason of a cycle is not a test conflict reason', async () => {
  const deps = { 2: [], 5: ['2'] }
  const p = project({ deps, failUntil: { 2: 1 }, taskFailures: { 2: [failure(MESSAGES.plain, ['5'])] } })
  const { result } = await run(p, { ...baseArgs, ...graphArgs(deps), maxRetries: 1 })
  assert.ok(result.failed.some(x => x.task === '2' && /^blocked: command cycle/.test(String(x.reason))), JSON.stringify(result.failed))
  assertNoTestConflict(result, '2')
})
