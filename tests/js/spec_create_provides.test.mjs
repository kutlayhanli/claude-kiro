// TC-113a: spec-create's tasks-writing prompts tell the writer to add **Provides:** to every impl task whose
// commands ck cannot infer, and to check the result with `ck graph` or `ck lint`.
// Run: node tests/js/spec_create_provides.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { stub } from './fake_create.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPTS = {
  plugin: path.join(here, '../../plugin/workflows/spec-create.js'),
  project: path.join(here, '../../.claude/workflows/spec-create.js'),
}
const args = mode => ({ mode, feature: 'f', projectRoot: '/p', specDir: '/p/specs/f', date: '2026-10-04' })

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

for (const [which, script] of Object.entries(SCRIPTS)) {
  await test(`tc113a ${which}: the impl-track prompt asks for **Provides:** and a ck graph or ck lint check`, async () => {
    const s = stub()
    await runWorkflow(script, { agent: s.agent, args: args('create') })
    const prompt = s.prompts['write implementation track']
    assert.ok(prompt, 'the implementation track prompt was sent')
    assert.match(prompt, /\*\*Provides:\*\*/)
    assert.match(prompt, /ck graph|ck lint/)
    assert.match(prompt, /commands? ck cannot infer|cannot infer/i)
  })

  await test(`tc113a ${which}: the task block template carries the **Provides:** field`, async () => {
    const s = stub()
    await runWorkflow(script, { agent: s.agent, args: args('create') })
    const withProvides = Object.values(s.prompts).filter(p => /\*\*Provides:\*\*/.test(p))
    assert.ok(withProvides.length >= 1)
    assert.ok(withProvides.some(p => /### Task/.test(p) || /TASK_BLOCK|\*\*Dependencies:\*\*/.test(p)))
  })
}
