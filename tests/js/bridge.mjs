// Long-lived JSON-lines bridge: one request object per stdin line, one response object per stdout line.
//   {op:'ready', order, deps, done, exclude, blocked} -> {ok:true, ready}
//   {op:'ladder', model}                              -> {ok:true, ladder}
//   {op:'run', project, args, slots, clock, execCk}   -> {ok:true, result, logs, trace, prompts, opts, events, execLog}
//       clock: 'virtual' (a VirtualClock: sleeps and trace t are exact virtual ms) | 'real' (real timers) | unset (as before)
//       execCk: {cmd, env, cwd, dry}: fake agents record progress events through the real CLI (see fake_project.mjs)
//   {op:'create', args, overrides, execCk}            -> {ok:true, result, logs, trace, prompts, execLog}
// Any throw -> {ok:false, error}. Durations in the project options are milliseconds (1-40).
import readline from 'node:readline'
import { runWorkflow, useVirtualTime } from './load_workflow.mjs'
import { project } from './fake_project.mjs'
import { stub } from './fake_create.mjs'
import { VirtualClock } from './virtual_clock.mjs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { execFile } from 'node:child_process'
import { loadDecisions, SCRIPT, GUARD_NAMES } from './load_decisions.mjs'


// Fake agent durations run on a virtual clock, so a scenario replays identically however loaded the machine is.
useVirtualTime()

const CREATE_SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), '../../plugin/workflows/spec-create.js')
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
    // With execCk and no clock given, default to 'virtual': the global pump would keep advancing simulated timers
    // while a real subprocess is in flight, so such runs would not be time-stable.
    const mode = req.clock ?? (req.execCk ? 'virtual' : undefined)
    const clock = mode === 'virtual' ? new VirtualClock() : null
    const timing = clock ? { sleep: clock.sleep, external: p => clock.external(p) } : mode === 'real' ? { sleep: ms => new Promise(r => setTimeout(r, ms)) } : {}
    const p = project({ ...req.project, ...timing, ...(req.execCk ? { execCk: req.execCk } : {}) })
    const slots = req.slots ?? undefined
    const args = { ...baseArgs, ...req.args, ...(slots ? { agentSlots: slots } : {}) }
    const { result, logs, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args, slots, clock })
    return { result, logs, trace, prompts: p.state.prompts, opts: p.state.opts, events: p.state.events, execLog: p.state.execLog }
  }
  if (req.op === 'create') {
    const clock = new VirtualClock()
    const execLog = []
    const s = stub(req.overrides ?? {})
    const agent = async (prompt, opts) => {
      if (req.execCk) {
        // Only `ck progress event` commands written in a prompt are run, as written, through the real CLI.
        const { writtenCommands } = await import('./fake_project.mjs')
        for (const argv of writtenCommands(prompt).filter(a => a[0] === 'progress')) {
          execLog.push(argv)
          if (!req.execCk.dry) await clock.external(new Promise(resolve => {
            const [bin, ...pre] = req.execCk.cmd
            execFile(bin, [...pre, ...argv], { env: { ...process.env, ...(req.execCk.env || {}), CK_PROGRESS_TEST_KINDS: '1' }, cwd: req.execCk.cwd || process.cwd() }, () => resolve())
          }))
        }
      }
      return s.agent(prompt, opts)
    }
    const createArgs = { mode: 'create', feature: 'f', projectRoot: '/p', specDir: '/p/specs/f', date: '2026-10-04', ...req.args }
    const { result, logs, trace } = await runWorkflow(CREATE_SCRIPT, { agent, args: createArgs, clock })
    return { result, logs, trace, prompts: s.prompts, execLog }
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
