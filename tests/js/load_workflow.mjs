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
export async function runWorkflow(scriptPath, { agent, args, slots }) {
  hookDump()
  const workflow = path.basename(scriptPath).replace(/\.js$/, '')
  const trace = []
  const capped = Number.isInteger(slots) && slots > 0
  let active = 0
  const waiting = []
  const run = async (prompt, opts) => {
    const label = opts && typeof opts.label === 'string' ? opts.label : ''
    trace.push({ t: performance.now(), ev: 'request', label })
    if (capped && active >= slots) await new Promise(resolve => waiting.push(resolve))
    active++
    trace.push({ t: performance.now(), ev: 'start', label })
    try { return await agent(prompt, opts) } finally {
      active--
      trace.push({ t: performance.now(), ev: 'end', label })
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
  const result = await fn(recordingAgent, pipeline, parallel, () => {}, m => logs.push(m), args)
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

export const sleep = ms => new Promise(r => setTimeout(r, ms))
