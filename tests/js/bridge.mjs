// Long-lived JSON-lines bridge: one request object per stdin line, one response object per stdout line.
//   {op:'ready', order, deps, done, exclude, blocked} -> {ok:true, ready}
//   {op:'ladder', model}                              -> {ok:true, ladder}
//   {op:'run', project, args, slots}                  -> {ok:true, result, logs, trace, prompts, opts, events}
// Any throw -> {ok:false, error}. Durations in the project options are milliseconds (1-40).
import readline from 'node:readline'
import { runWorkflow, useVirtualTime } from './load_workflow.mjs'
import { project } from './fake_project.mjs'
import { loadDecisions, SCRIPT, GUARD_NAMES } from './load_decisions.mjs'


// Fake agent durations run on a virtual clock, so a scenario replays identically however loaded the machine is.
useVirtualTime()

const baseArgs ={ spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }

async function handle(req) {
  if (req.op === 'ready') {
    const { readyTasks } = req.script ? loadDecisions(req.script) : loadDecisions()  // req.script: test hook for a script without markers
    return { ready: readyTasks({ order: req.order ?? Object.keys(req.deps), deps: req.deps, done: req.done ?? [], exclude: req.exclude ?? [], blocked: req.blocked ?? [] }) }
  }
  if (req.op === 'ladder') {
    const { guardLadder } = loadDecisions({ extra: GUARD_NAMES })
    return { ladder: guardLadder(req.model) }
  }
  if (req.op === 'run') {
    const p = project(req.project)
    const slots = req.slots ?? undefined
    const args = { ...baseArgs, ...req.args, ...(slots ? { agentSlots: slots } : {}) }
    const { result, logs, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args, slots })
    return { result, logs, trace, prompts: p.state.prompts, opts: p.state.opts, events: p.state.events }
  }
  throw new Error(`unknown op: ${req.op}`)
}

const rl = readline.createInterface({ input: process.stdin })
// Requests are handled one at a time, in order, so responses stay line-aligned.
let chain = Promise.resolve()
rl.on('line', line => {
  if (!line.trim()) return
  chain = chain.then(async () => {
    let out
    try { out = { ok: true, ...(await handle(JSON.parse(line))) } } catch (e) { out = { ok: false, error: String((e && e.message) || e) } }
    process.stdout.write(JSON.stringify(out) + '\n')
  })
})
rl.on('close', () => { chain.then(() => process.exit(0)) })
