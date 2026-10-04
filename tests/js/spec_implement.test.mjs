// Control-flow tests for spec_implement.js with stub agents. Run: node tests/js/spec_implement.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow, sleep } from './load_workflow.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../src/claude_kiro/resources/templates/workflows/spec_implement.js')

const baseArgs = {
  spec: 'demo',
  root: '/repo',
  into: 'main',
  targetDir: '/repo',
  waves: [['1'], ['2', '3', '4'], ['5']],
  deps: { 1: [], 2: ['1'], 3: ['1'], 4: ['1'], 5: ['2', '3'] },
  titles: {},
  tracks: { 1: 'test' },
}

// Builds a stub agent from per-label behaviors; records calls and merge concurrency.
function stub({ done = [], taskFail = {}, verifyGreen = () => true, busy = [] } = {}) {
  const calls = []
  let merging = 0
  let maxMerging = 0
  const attempts = {}
  const agent = async (prompt, opts) => {
    calls.push(opts.label)
    const l = opts.label
    if (l.startsWith('setup')) {
      const nums = [...prompt.matchAll(/Of these tasks: ([\d, ]+)/g)][0][1].split(',').map(s => s.trim())
      return {
        done: nums.filter(n => done.includes(n)),
        ready: nums.filter(n => !done.includes(n) && !busy.includes(n)),
        busy: nums.filter(n => busy.includes(n)).map(n => ({ task: n, detail: 'claimed' })),
      }
    }
    if (l.startsWith('task')) {
      const n = l.match(/task (\d+)/)[1]
      attempts[n] = (attempts[n] || 0) + 1
      await sleep(5 * Number(n))  // finish at different times
      calls.push(`${l} end`)
      const failUntil = taskFail[n] || 0
      const pass = attempts[n] > failUntil
      return { task: n, status: pass ? 'done' : 'in_progress', gatePassed: pass, summary: `attempt ${attempts[n]}` }
    }
    if (l.startsWith('merge')) {
      merging++
      maxMerging = Math.max(maxMerging, merging)
      await sleep(10)
      merging--
      return { merged: true }
    }
    if (l.startsWith('verify') || l.startsWith('re-verify')) return { green: verifyGreen(l) }
    if (l.startsWith('fix')) return { green: false, failing: 'still red' }
    throw new Error('unexpected agent ' + l)
  }
  return { agent, calls, attempts, maxMerging: () => maxMerging }
}

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

await test('happy path: skips Done tasks, retries a failed gate, merges one at a time', async () => {
  const s = stub({ done: ['1'], taskFail: { 3: 1 } })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: s.agent, args: { ...baseArgs, maxRetries: 1 } })
  assert.equal(result.halted, false)
  assert.ok(!s.calls.includes('task 1 [test]'), 'Done task 1 must not run')
  assert.equal(s.attempts['3'], 2, 'task 3 retried once')
  assert.ok(s.calls.includes('task 3 retry 1'))
  assert.equal(s.maxMerging(), 1, 'merges are serialized')
  assert.deepEqual(s.calls.filter(c => c.startsWith('merge')).sort(), ['merge 2', 'merge 3', 'merge 4', 'merge 5'])
  // wave 1 has nothing to run but still verifies nothing extra; waves 2 and 3 verify
  assert.ok(s.calls.includes('verify wave 2') && s.calls.includes('verify wave 3'))
  assert.ok(logs.some(l => l.includes('Task 2: done and merged into main')))
})

await test('merges start as tasks finish, before slower tasks in the wave are done', async () => {
  const s = stub({ done: ['1'] })
  await runWorkflow(SCRIPT, { agent: s.agent, args: baseArgs })
  const merge2 = s.calls.indexOf('merge 2')
  assert.ok(s.calls.indexOf('task 4') < merge2, 'task 4 started before merge 2 (parallel)')
  assert.ok(merge2 < s.calls.indexOf('task 4 end'), 'merge 2 began while task 4 was still running')
})

await test('halt: a task failing all attempts stops before the next wave', async () => {
  const s = stub({ done: ['1'], taskFail: { 3: 99 } })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: { ...baseArgs, maxRetries: 1, onFailure: 'halt' } })
  assert.equal(result.halted, true)
  assert.equal(result.atWave, 2)
  assert.deepEqual(result.failedTasks, ['3'])
  assert.ok(!s.calls.some(c => c.startsWith('task 5')), 'wave 3 must not start')
  assert.ok(s.calls.includes('merge 2') && s.calls.includes('merge 4'), 'finished tasks still merge')
})

await test('continue (default): tasks depending on a failed task are skipped, others run', async () => {
  const args = { ...baseArgs, waves: [['1'], ['2', '3'], ['4', '5']], deps: { 1: [], 2: ['1'], 3: ['1'], 4: ['2'], 5: ['3'] }, maxRetries: 0 }
  const s = stub({ done: ['1'], taskFail: { 3: 99 } })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: s.agent, args })
  assert.equal(result.halted, false)
  assert.ok(s.calls.includes('task 4'), 'task 4 (depends on merged 2) runs')
  assert.ok(!s.calls.includes('task 5'), 'task 5 (depends on failed 3) is skipped')
  assert.deepEqual(result.failedTasks.sort(), ['3', '5'])
  assert.ok(logs.some(l => l.includes('skipping Task 5')))
})

await test('red target after a wave: one fix attempt, then halt even in continue mode', async () => {
  const s = stub({ done: ['1'], verifyGreen: () => false })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: { ...baseArgs, onFailure: 'continue' } })
  assert.equal(result.halted, true)
  assert.equal(result.atWave, 1 + 1)
  assert.ok(s.calls.includes('fix wave 2') && s.calls.includes('re-verify wave 2'))
})

await test('busy worktree is reported and not run; the run continues by default', async () => {
  const s = stub({ done: ['1'], busy: ['4'] })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: baseArgs })
  assert.ok(!s.calls.includes('task 4'))
  assert.equal(result.halted, false)
  assert.ok(s.calls.includes('task 5'), 'task 5 does not depend on 4')
  assert.deepEqual(result.failedTasks, ['4'])
  assert.deepEqual(result.report.find(r => r.wave === 2).busy, [{ task: '4', detail: 'claimed' }])
})

await test('maxConcurrent caps task agents at once', async () => {
  let active = 0
  let peak = 0
  const s = stub({ done: ['1'] })
  const agent = async (prompt, opts) => {
    if (!opts.label.startsWith('task')) return s.agent(prompt, opts)
    active++
    peak = Math.max(peak, active)
    try { return await s.agent(prompt, opts) } finally { active-- }
  }
  await runWorkflow(SCRIPT, { agent, args: { ...baseArgs, maxConcurrent: 1 } })
  assert.equal(peak, 1)
})
