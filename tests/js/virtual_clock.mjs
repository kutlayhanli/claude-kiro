// Test-only virtual clock. Pending timers sit in a heap ordered by (due time, creation sequence). `drive` advances
// virtual time only when every pending microtask has drained (and no external work is in flight), then fires the
// earliest timer, so timer order depends only on the simulated times and never on machine load.
export class VirtualClock {
  constructor() {
    this._now = 0
    this._seq = 0
    this._heap = []
    this._external = 0
    this.sleep = ms => this._sleep(ms)
    this.now = () => this._now
  }

  _less(a, b) { return a.due < b.due || (a.due === b.due && a.seq < b.seq) }

  _push(t) {
    const h = this._heap
    h.push(t)
    let i = h.length - 1
    while (i > 0) {
      const p = (i - 1) >> 1
      if (!this._less(h[i], h[p])) break
      ;[h[i], h[p]] = [h[p], h[i]]
      i = p
    }
  }

  _pop() {
    const h = this._heap
    const top = h[0]
    const last = h.pop()
    if (h.length) {
      h[0] = last
      let i = 0
      for (;;) {
        const l = 2 * i + 1, r = l + 1
        let m = i
        if (l < h.length && this._less(h[l], h[m])) m = l
        if (r < h.length && this._less(h[r], h[m])) m = r
        if (m === i) break
        ;[h[i], h[m]] = [h[m], h[i]]
        i = m
      }
    }
    return top
  }

  _sleep(ms) {
    return new Promise(resolve => this._push({ due: this._now + Math.max(0, Number(ms) || 0), seq: this._seq++, resolve }))
  }

  // Wrap real work (a subprocess) so virtual time stands still until it finishes.
  async external(promise) {
    this._external++
    try { return await promise } finally { this._external-- }
  }

  // Resolve with `promise`, firing timers in order whenever everything else is idle.
  drive(promise) {
    let settled = false
    let value, error, failed = false
    promise.then(v => { settled = true; value = v }, e => { settled = true; failed = true; error = e })
    return new Promise(async (resolve, reject) => {
      while (!settled) {
        await new Promise(r => setImmediate(r))  // a macrotask: every pending microtask has run first
        if (settled) break
        if (this._external > 0 || !this._heap.length) {
          if (this._external > 0 || !this._heap.length) await new Promise(r => setTimeout(r, 1))  // wait for real I/O
          continue
        }
        const t = this._pop()
        this._now = t.due
        t.resolve()
      }
      await new Promise(r => setImmediate(r))
      failed ? reject(error) : resolve(value)
    })
  }
}
