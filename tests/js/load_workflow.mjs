// Load a Workflow script and run it with stubbed agent/parallel/pipeline/phase/log.
import fs from 'node:fs'
import path from 'node:path'

// Label corpus: every opts.label passed to the stubbed `agent`, with the workflow script
// name and phase. With CK_DUMP_LABELS=<path> the corpus is written to that path on process
// exit (merged into what is already there, so several suites can share one fresh path).
export const labelCorpus = []
let dumpHooked = false
function hookDump() {
  if (dumpHooked || !process.env.CK_DUMP_LABELS) return
  dumpHooked = true
  process.on('exit', () => {
    const file = process.env.CK_DUMP_LABELS
    let prior = []
    try { prior = JSON.parse(fs.readFileSync(file, 'utf8')) } catch {}
    fs.writeFileSync(file, JSON.stringify([...prior, ...labelCorpus]))
  })
}

// With a positive integer `slots`, at most `slots` agent calls run at once and the rest wait FIFO,
// like the runtime queue. `trace` records request/start/end per call (performance.now()).
export async function runWorkflow(scriptPath, { agent, args, slots, clock }) {
  hookDump()
  const nowT = clock ? () => clock.now() : () => performance.now()  // a clock replaces the trace's real time
  const workflow = path.basename(scriptPath).replace(/\.js$/, '')
  const trace = []
  const capped = Number.isInteger(slots) && slots > 0
  let active = 0
  const waiting = []
  const run = async (prompt, opts) => {
    const label = opts && typeof opts.label === 'string' ? opts.label : ''
    trace.push({ t: nowT(), ev: 'request', label })
    if (capped && active >= slots) await new Promise(resolve => waiting.push(resolve))
    active++
    trace.push({ t: nowT(), ev: 'start', label })
    try { return await agent(prompt, opts) } finally {
      active--
      trace.push({ t: nowT(), ev: 'end', label })
      if (waiting.length) waiting.shift()()
    }
  }
  const recordingAgent = (prompt, opts) => {
    if (opts && typeof opts.label === 'string') labelCorpus.push({ workflow, label: opts.label, phase: opts.phase ?? null })
    return run(prompt, opts)
  }
  const src = fs.readFileSync(scriptPath, 'utf8').replace(/^export const meta/m, 'const meta')
  const logs = []
  const parallel = thunks => Promise.all(thunks.map(t => t().catch(() => null)))
  const pipeline = (items, ...stages) =>
    Promise.all(items.map(async (item, i) => {
      let r
      for (const [n, stage] of stages.entries()) {
        try { r = await stage(n === 0 ? item : r, item, i) } catch { return null }
      }
      return r
    }))
  const fn = new Function('agent', 'pipeline', 'parallel', 'phase', 'log', 'args', `return (async () => {${src}})()`)
  const running = fn(recordingAgent, pipeline, parallel, () => {}, m => logs.push(m), args)
  const result = await (clock ? clock.drive(running) : running)
  return { result, logs, trace }
}

// Extract a top-level `function <name>(...) {...}` declaration by brace matching and evaluate it alone.
export function loadFunction(scriptPath, name) {
  const src = fs.readFileSync(scriptPath, 'utf8')
  const m = new RegExp(`^(?:async\\s+)?function\\s+${name}\\s*\\(`, 'm').exec(src)
  if (!m) throw new Error(`function ${name} not found in ${scriptPath}`)
  const open = src.indexOf('{', src.indexOf(')', m.index))
  let depth = 0
  let end = -1
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++
    else if (src[i] === '}' && --depth === 0) { end = i + 1; break }
  }
  if (end < 0) throw new Error(`function ${name}: unbalanced braces`)
  const body = src.slice(m.index, end)
  try { return new Function(`${body}\nreturn ${name}`)() } catch (e) { throw new Error(`function ${name} cannot be evaluated alone: ${e.message}`) }
}

// Virtual time (opt-in, used by the bridge): `sleep` timers fall due on a simulated clock that moves only when
// every promise chain has settled, earliest due first and, on a tie, in the order they were set. Real timers let
// load (another test process, a GC pause) reorder two timers due within a millisecond of each other, so two runs
// of one scenario could differ; on the virtual clock a scenario always plays out the same way.
let virtual = null
export function useVirtualTime() {
  if (virtual) return
  virtual = { now: 0, seq: 0, timers: [], pumping: false }
}
function pump() {
  const v = virtual
  if (!v.timers.length) { v.pumping = false; return }
  let i = 0
  for (let j = 1; j < v.timers.length; j++) {
    const a = v.timers[j], b = v.timers[i]
    if (a.due < b.due || (a.due === b.due && a.seq < b.seq)) i = j
  }
  const [t] = v.timers.splice(i, 1)
  v.now = t.due
  t.resolve()
  setImmediate(pump)  // runs after the microtasks the resolved timer started have all settled
}
export const sleep = ms => {
  if (!virtual) return new Promise(r => setTimeout(r, ms))
  return new Promise(resolve => {
    virtual.timers.push({ due: virtual.now + Math.max(1, Number(ms) || 0)  /* as setTimeout: under 1 ms means 1 ms */, seq: virtual.seq++, resolve })
    if (!virtual.pumping) { virtual.pumping = true; setImmediate(pump) }
  })
}
