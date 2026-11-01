// Deterministic-plan example cases (Stories 1-4: TC-0 smoke here; TC-1/TC-2/TC-4..TC-7,
// TC-9..TC-22 and TC-26 arrive with the tasks that own them).
// Run: node tests/js/deterministic_plan.test.mjs [TC-id ...]  (no ids: run every case)
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { project } from './fake_project.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')

export const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
export const idx = (s, l) => s.events.indexOf(l)

// TC-id argv filter: with ids, only cases whose name starts with one of them run.
const only = new Set(process.argv.slice(2).filter(a => /^TC-\d+$/.test(a)))
const seen = new Set()
async function test(name, fn) {
  const id = (name.match(/^(TC-\d+)/) || [])[1]
  if (id) seen.add(id)
  if (only.size && !(id && only.has(id))) return
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

await test('TC-0 harness smoke: a healthy 2-task run completes', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.ok(idx(p.state, 'plan 1') >= 0, 'the real script requested a plan agent')
})

const unknown = [...only].filter(id => !seen.has(id))
if (unknown.length) { console.log('FAIL unknown TC id(s):', unknown.join(' ')); process.exitCode = 1 }
