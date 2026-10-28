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

export async function runWorkflow(scriptPath, { agent, args }) {
  hookDump()
  const workflow = path.basename(scriptPath).replace(/\.js$/, '')
  const recordingAgent = (prompt, opts) => {
    if (opts && typeof opts.label === 'string') labelCorpus.push({ workflow, label: opts.label, phase: opts.phase ?? null })
    return agent(prompt, opts)
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
  return { result, logs }
}

export const sleep = ms => new Promise(r => setTimeout(r, ms))
