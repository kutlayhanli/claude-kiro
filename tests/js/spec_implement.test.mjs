// Scheduler tests for spec_implement.js with stub agents. Run: node tests/js/spec_implement.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow, sleep } from './load_workflow.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../src/claude_kiro/resources/templates/workflows/spec_implement.js')

const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.claude/worktrees/demo-integration' }

// A fake project: `ck plan` semantics over deps/done, task durations, failures, timestamps.
function project({ deps, done = [], durations = {}, failUntil = {}, noCommitsFirst = [], busy = [], cycles = [], verifyCycles = [], verifyCyclesFromStart = false, checkRed = [], fullRed = false, relink = null, reviews = {}, mergeDurations = {}, reviewDurations = {} }) {
  const state = { deps: { ...deps }, done: new Set(done), events: [], prompts: {}, opts: {}, reviewCount: {}, attempts: {}, plans: 0, merging: 0, maxMerging: 0, running: 0, maxRunning: 0 }
  let clock = Date.parse('2026-10-04T10:00:00Z')
  const stamp = () => new Date((clock += 60000)).toISOString().replace('.000', '')
  const ready = exclude =>
    Object.keys(state.deps).filter(n => !state.done.has(n) && !exclude.includes(n) && !verifyCycles.some(v => v.task === n) && state.deps[n].every(d => state.done.has(d)))
  const agent = async (prompt, opts) => {
    const l = opts.label
    state.events.push(l)
    state.prompts[l] = prompt
    state.opts[l] = opts
    if (l.startsWith('plan')) {
      state.plans++
      if (relink && state.plans === relink.atPlan) Object.assign(state.deps, relink.deps)
      const exclude = ((prompt.match(/--exclude ([\d,]+)/) || [])[1] || '').split(',').filter(Boolean)
      const slotsMatch = prompt.match(/Take the first (\d+|all) task/)
      let r = ready(exclude)
      if (slotsMatch && slotsMatch[1] !== 'all') r = r.slice(0, Number(slotsMatch[1]))
      return {
        done: [...state.done],
        ready: r.filter(n => !busy.includes(n)),
        busy: r.filter(n => busy.includes(n)).map(n => ({ task: n, detail: 'claimed: someone' })),
        remaining: Object.keys(state.deps).filter(n => !state.done.has(n)),
        cycles,
        verifyCycles: state.plans === 1 && !verifyCyclesFromStart ? [] : verifyCycles,
      }
    }
    if (l.startsWith('task')) {
      const n = l.match(/task (\d+)/)[1]
      state.attempts[n] = (state.attempts[n] || 0) + 1
      state.running++
      state.maxRunning = Math.max(state.maxRunning, state.running)
      const startedAt = stamp()
      await sleep(durations[n] ?? 5)
      state.running--
      state.events.push(`${l} end`)
      if (noCommitsFirst.includes(n) && state.attempts[n] === 1) {
        return { task: n, status: 'blocked', gatePassed: false, summary: 'answered the wave question', commits: '', blocker: 'user asked a question', startedAt, finishedAt: stamp() }
      }
      const pass = state.attempts[n] > (failUntil[n] || 0)
      return { task: n, status: pass ? 'done' : 'in_progress', gatePassed: pass, summary: `attempt ${state.attempts[n]}`, commits: 'abc123', startedAt, finishedAt: stamp() }
    }
    if (l.startsWith('merge')) {
      const n = l.match(/merge (\d+)/)[1]
      state.merging++
      state.maxMerging = Math.max(state.maxMerging, state.merging)
      await sleep(mergeDurations[n] ?? 2)
      state.merging--
      state.events.push(`${l} end`)
      state.done.add(n)
      return { merged: true, checkGreen: !checkRed.includes(n), checkTail: checkRed.includes(n) ? 'red' : '', mergedAt: stamp() }
    }
    if (l.startsWith('review')) {
      const n = l.match(/review (\d+)/)[1]
      state.reviewCount[n] = (state.reviewCount[n] || 0) + 1
      await sleep(reviewDurations[n] ?? 0)
      state.events.push(`${l} end`)
      const verdict = (reviews[n] || [])[state.reviewCount[n] - 1] || 'approve'
      return { verdict, findings: verdict === 'changes' ? [{ severity: 'blocking', issue: 'contradicts design.md' }] : [], summary: verdict }
    }
    if (l.startsWith('fix')) return { green: true }
    if (l.startsWith('re-check')) return { green: true }
    if (l.startsWith('full gate')) return { green: !fullRed }
    if (l === 'final gate') return { green: true }
    throw new Error('unexpected agent ' + l)
  }
  return { agent, state }
}

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}
const idx = (s, l) => s.events.indexOf(l)

await test('no wave barrier: a task starts as soon as its own dependencies merge', async () => {
  // 1 -> {2 (fast), 3 (slow)}; 4 needs only 2. Waves would hold 4 until 3 finished.
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['1'], 4: ['2'] }, durations: { 1: 2, 2: 5, 3: 80, 4: 5 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, wave: { 1: 1, 2: 2, 3: 2, 4: 3 } } })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'])
  assert.ok(idx(p.state, 'task 4') < idx(p.state, 'task 3 end'), 'task 4 started while task 3 was still running')
  assert.equal(p.state.maxMerging, 1, 'merges are serialized')
  assert.ok(p.state.events.includes('final gate'))
})

await test('already-Done tasks are skipped', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, done: ['1'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(!p.state.events.includes('task 1'))
  assert.deepEqual(result.merged, ['2'])
  assert.equal(result.stats.initiallyDone, 1)
})

await test('refuses to launch on a dependency cycle or verify-order cycle', async () => {
  const p = project({ deps: { 1: ['2'], 2: ['1'] }, cycles: [['1', '2', '1']] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.equal(result.refused, true)
  assert.ok(!p.state.events.some(e => e.startsWith('task')))

  const q = project({ deps: { 1: [], 2: [] }, verifyCycles: [{ task: '1', message: 'Task 1 is verified by Task 9 ...' }], verifyCyclesFromStart: true })
  const r2 = await runWorkflow(SCRIPT, { agent: q.agent, args: baseArgs })
  assert.equal(r2.result.refused, true)
  assert.deepEqual(r2.result.verifyCycles.map(v => v.task), ['1'])
  assert.ok(!q.state.events.some(e => e.startsWith('task')), 'nothing starts, not even the unaffected task 2')
})

await test('a verify-order cycle found mid-run skips that task and keeps going', async () => {
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['1'] }, verifyCycles: [{ task: '3', message: 'Task 3 is verified by Task 9, whose tests import runner (Task 4)' }] })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(!p.state.events.includes('task 3'))
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.ok(result.failed.some(f => f.task === '3' && f.reason.startsWith('verify-order cycle')))
  assert.ok(logs.some(l => l.includes('verify-order cycle')))
})

await test('relinks in tasks.md apply without a restart', async () => {
  // 3 spuriously depends on 2 (slow); a relink after the first task removes the edge.
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['2'] }, durations: { 2: 80 }, relink: { atPlan: 2, deps: { 3: ['1'] } } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(idx(p.state, 'task 3') < idx(p.state, 'task 2 end'), 'task 3 started before task 2 finished, after the relink')
})

await test('continue (default): dependents of a failed task never start, others do', async () => {
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['1'], 4: ['3'], 5: ['2'] }, failUntil: { 3: 99 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxRetries: 1 } })
  assert.equal(p.state.attempts['3'], 2)
  assert.ok(p.state.events.includes('task 5'))
  assert.ok(!p.state.events.includes('task 4'))
  assert.deepEqual(result.notStarted, ['4'])
  assert.deepEqual(result.failed.map(f => f.task), ['3'])
  assert.equal(result.targetGreen, true)
})

await test('halt: a failure stops new launches, running tasks still finish and merge', async () => {
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['1'], 4: ['2'] }, failUntil: { 3: 99 }, durations: { 2: 30, 3: 5 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, onFailure: 'halt', maxRetries: 0 } })
  assert.equal(result.halted, true)
  assert.ok(p.state.events.includes('merge 2'), 'task 2 was running and still merged')
  assert.ok(!p.state.events.includes('task 4'))
  assert.match(result.reason, /Task 3 failed/)
})

await test('an attempt with no commits (answered a chat message) is retried with the mandate', async () => {
  const p = project({ deps: { 1: [] }, noCommitsFirst: ['1'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.deepEqual(result.merged, ['1'])
  assert.match(p.state.prompts['task 1'], /^MANDATE: The user ran \/spec:implement demo/)
  assert.match(p.state.prompts['task 1 retry 1'], /made NO commits/)
  assert.match(p.state.prompts['task 1 retry 1'], /^MANDATE/)
})

await test('per-merge check: red after a merge gets one fix, then continues', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, checkRed: ['1'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(p.state.events.some(e => e.startsWith('fix 1')))
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.equal(result.tasks.find(t => t.task === '1').fixedAfterMerge, true)
})

await test('full gate every N merges; red full gate after a failed fix halts', async () => {
  const p = project({ deps: { 1: [], 2: [], 3: [], 4: ['1'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, fullGateEvery: 2 } })
  assert.deepEqual(result.fullGates.map(g => g.afterMerges), [2, 4])

  const q = project({ deps: { 1: [], 2: [], 3: ['1', '2'] }, fullRed: true })
  const agent = async (prompt, opts) => (opts.label.startsWith('re-check') ? { green: false } : q.agent(prompt, opts))
  const r2 = await runWorkflow(SCRIPT, { agent, args: { ...baseArgs, fullGateEvery: 2 } })
  assert.equal(r2.result.halted, true)
  assert.equal(r2.result.targetGreen, false)
  assert.ok(!q.state.events.includes('task 3'))
})

await test('busy worktree is reported, not run', async () => {
  const p = project({ deps: { 1: [], 2: [] }, busy: ['2'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(!p.state.events.includes('task 2'))
  assert.ok(result.failed.some(f => f.task === '2' && f.reason.startsWith('worktree busy')))
})

await test('maxConcurrent caps task agents at once', async () => {
  const p = project({ deps: { 1: [], 2: [], 3: [], 4: [] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 2 } })
  assert.equal(p.state.maxRunning, 2)
})

const REVIEW_ON = { reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 1 } }

await test('a task in review or waiting to merge does not hold a slot', async () => {
  // One slot. Task 1's merge is slow and task 2's review is slow; neither should keep the next task waiting.
  const p = project({ deps: { 1: [], 2: [], 3: [] }, durations: { 1: 2, 2: 2, 3: 2 }, mergeDurations: { 1: 80 }, reviewDurations: { 2: 80 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 1, agents: REVIEW_ON } })
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'])
  assert.equal(p.state.maxRunning, 1, 'still one implementing agent at a time')
  assert.ok(idx(p.state, 'task 2') < idx(p.state, 'merge 1 end'), 'task 2 started while task 1 waited to merge')
  assert.ok(idx(p.state, 'task 3') < idx(p.state, 'review 2 end'), 'task 3 started while task 2 was in review')
})

await test('a revision after review still runs when the slots are full', async () => {
  const p = project({ deps: { 1: [], 2: [] }, durations: { 1: 2, 2: 40 }, reviews: { 1: ['changes', 'approve'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 1, agents: REVIEW_ON } })
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.ok(idx(p.state, 'task 1 revise 1') > -1, 'task 1 was revised')
  assert.ok(idx(p.state, 'task 1 revise 1') < idx(p.state, 'task 2 end'), 'the revision did not wait for task 2')
})

await test('wall-clock timing per task and per wave group', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, wave: { 1: 1, 2: 2 } } })
  const t1 = result.tasks.find(t => t.task === '1')
  assert.ok(t1.agentMinutes > 0 && t1.toMergedMinutes >= t1.agentMinutes)
  assert.deepEqual(result.timing.phases.map(ph => ph.group).sort(), ['Wave 1', 'Wave 2'])
  assert.ok(result.timing.minutes > 0)
})

await test('model and effort: unset means task agents inherit the session', async () => {
  const p = project({ deps: { 1: [] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  const o = p.state.opts['task 1']
  assert.equal('model' in o, false)
  assert.equal('effort' in o, false)
  assert.deepEqual(result.taskAgents, { model: 'inherited', effort: 'inherited' })
})

await test('model and effort: applied to task, retry and fix agents only', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 1 }, checkRed: ['1'] })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, model: 'sonnet', effort: 'medium' } })
  for (const l of ['task 1', 'task 1 retry 1', 'fix 1 (after merging Task 1)']) {
    assert.equal(p.state.opts[l].model, 'sonnet', l)
    assert.equal(p.state.opts[l].effort, 'medium', l)
  }
  for (const l of ['plan 1', 'merge 1']) {
    assert.equal('model' in p.state.opts[l], false, l)
    assert.equal(p.state.opts[l].effort, 'low', l)
  }
  assert.equal('model' in p.state.opts['re-check 1'], false)
  assert.deepEqual(result.taskAgents, { model: 'sonnet', effort: 'medium' })
  assert.ok(logs.some(m => m.includes('impl sonnet/medium')))
})

await test('model and effort: unknown values are refused before any agent runs', async () => {
  for (const bad of [{ effort: 'fast' }, { model: 'gpt-5' }]) {
    const p = project({ deps: { 1: [] } })
    await assert.rejects(runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, ...bad } }), /unknown (effort|model)/)
    assert.equal(p.state.events.length, 0)
  }
})

await test('every gate-running prompt tells the agent to wait for the real exit code', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 1 }, checkRed: ['1'] })
  await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  for (const l of ['task 1', 'task 1 retry 1', 'merge 1', 'fix 1 (after merging Task 1)', 're-check 1', 'final gate']) {
    assert.ok(p.state.prompts[l], `no prompt for ${l}`)
    assert.match(p.state.prompts[l], /A slow gate is not a failed gate/, l)
  }
  assert.doesNotMatch(p.state.prompts['plan 1'], /slow gate/)
})

const ROLES = {
  implementer: { model: 'sonnet', effort: 'medium' },
  test_writer: { model: 'opus', effort: 'high' },
  reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 1 },
  fixer: { model: 'sonnet', effort: 'medium' },
}

await test('roles: impl tasks use the implementer, test tasks the test writer, fixes the fixer', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, checkRed: ['2'] })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, tracks: { 1: 'test', 2: 'impl' }, agents: ROLES } })
  assert.deepEqual([p.state.opts['task 1 [test]'].model, p.state.opts['task 1 [test]'].effort], ['opus', 'high'])
  assert.deepEqual([p.state.opts['task 2'].model, p.state.opts['task 2'].effort], ['sonnet', 'medium'])
  assert.deepEqual([p.state.opts['fix 1 (after merging Task 2)'].model, p.state.opts['fix 1 (after merging Task 2)'].effort], ['sonnet', 'medium'])
})

await test('reviewer: approves, then the task merges; review runs at the reviewer settings', async () => {
  const p = project({ deps: { 1: [] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: ROLES } })
  assert.deepEqual(result.merged, ['1'])
  assert.deepEqual([p.state.opts['review 1'].model, p.state.opts['review 1'].effort], ['opus', 'high'])
  assert.ok(idx(p.state, 'review 1') < idx(p.state, 'merge 1'))
  assert.equal(result.tasks[0].review.verdict, 'approve')
  assert.match(p.state.prompts['review 1'], /diff integrate\/demo\.\.\.feat\/demo-task-1/)
})

await test('reviewer: blocking findings send the task back once, then it merges', async () => {
  const p = project({ deps: { 1: [] }, reviews: { 1: ['changes', 'approve'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: ROLES } })
  assert.deepEqual(result.merged, ['1'])
  assert.ok(p.state.events.includes('task 1 revise 1'))
  assert.match(p.state.prompts['task 1 revise 1'], /contradicts design\.md/)
  assert.equal(p.state.opts['task 1 revise 1'].model, 'sonnet')
  assert.ok(idx(p.state, 'review 1 round 2') < idx(p.state, 'merge 1'))
  assert.deepEqual([result.tasks[0].review.verdict, result.tasks[0].review.rounds], ['approve', 1])
})

await test('reviewer: findings that survive the revisions fail the task and nothing merges', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, reviews: { 1: ['changes', 'changes'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: ROLES } })
  assert.deepEqual(result.merged, [])
  assert.match(result.failed[0].reason, /review: blocking findings remain after 1 revision/)
  assert.ok(!p.state.events.includes('merge 1'))
  assert.ok(!p.state.events.includes('task 2'))  // its dependent never starts
})

await test('reviewer: enabled false skips review', async () => {
  const p = project({ deps: { 1: [] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: { ...ROLES, reviewer: { ...ROLES.reviewer, enabled: false } } } })
  assert.ok(!p.state.events.some(e => e.startsWith('review')))
})

await test('without agents settings every agent inherits and nothing is reviewed', async () => {
  const p = project({ deps: { 1: [] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(!p.state.events.some(e => e.startsWith('review')))
  assert.equal('model' in p.state.opts['task 1'], false)
  assert.equal(result.agents.reviewer, 'off')
})

await test('roles: inherit means the session model; model/effort args override code-writing roles', async () => {
  const p = project({ deps: { 1: [] } })
  const roles = { ...ROLES, implementer: { model: 'inherit', effort: 'inherit' } }
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: roles } })
  assert.equal('model' in p.state.opts['task 1'], false)
  const q = project({ deps: { 1: [] } })
  await runWorkflow(SCRIPT, { agent: q.agent, args: { ...baseArgs, agents: ROLES, model: 'haiku', effort: 'low' } })
  assert.deepEqual([q.state.opts['task 1'].model, q.state.opts['task 1'].effort], ['haiku', 'low'])
  assert.equal(q.state.opts['review 1'].model, 'opus')  // the override does not touch the reviewer
})
