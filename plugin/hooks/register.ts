import type { EngineInterface, Register } from 'claude-code'

// token-optimizer as a mod: once the context reaches the threshold, the
// session is handed over to a fresh one through /compress -> /clear ->
// /whats-next, run with $.command.run. The context size comes from the
// engine (session.measure). The engine prefixes every status, log line and
// command output with the plugin's name, so the texts here carry none.

const STEP = 50_000 // no new auto start until the context grew this much
const STALE_MS = 30 * 60_000 // a chain stuck this long is dropped
const RESTORE_MS = 1_500 // wait for the engine's own suggestion before ours

type Phase = 'idle' | 'compress' | 'clear' | 'whatsnext'

const k = (n: number) => `${Math.round(n / 1000)}k`

// Module state survives /clear (same process), not a reload.
const chain = {
  phase: 'idle' as Phase,
  since: 0,
  turn: undefined as string | undefined, // the /compress or /whats-next turn
  nextPath: '',
  nextMtime: 0,
  base: 0,
  tokens: 0,
  suggestion: undefined as string | undefined, // last dim suggestion while idle
  restore: undefined as string | undefined, // suggestion to put back after the chain
  isAfterClear: false,
}

function reset($: EngineInterface, why?: string) {
  chain.phase = 'idle'
  chain.turn = undefined
  chain.restore = undefined
  if (why) {
    $.ui.toast(why)
    $.ui.log(why)
  }
}

// NEXT.md belongs next to TASK.md (ticket folder), else into the cwd.
async function findNext($: EngineInterface) {
  const cwd = await $.session.cwd()
  for (let dir = cwd; dir.length > 1; dir = dir.slice(0, dir.lastIndexOf('/')) || '/') {
    if (await $.fs.exists(`${dir}/TASK.md`)) return `${dir}/NEXT.md`
  }
  return `${cwd}/NEXT.md`
}

async function mtime($: EngineInterface, path: string) {
  return $.fs.stat(path).then(s => s.mtimeMs, () => 0)
}

// NEXT.md as the note the conversation after /clear starts from ('' if none).
async function nextNote($: EngineInterface) {
  const path = await findNext($)
  const text = await $.fs.read(path).catch(() => '')
  return text.trim() ? `NEXT.md (${path}), written before /clear:\n\n${text}` : ''
}

async function startChain($: EngineInterface) {
  if (chain.phase !== 'idle') return
  chain.nextPath = await findNext($)
  chain.nextMtime = await mtime($, chain.nextPath)
  chain.phase = 'compress'
  chain.since = await $.clock.now()
  chain.turn = undefined
  chain.base = chain.tokens
  chain.restore = chain.suggestion
  $.ui.status('/compress …')
  $.ui.log(`start at ${k(chain.tokens)}, NEXT.md = ${chain.nextPath}` +
    (chain.restore ? `, suggestion kept: ${chain.restore}` : ''))
  void $.command.run({ command: 'compress' }).catch(err => reset($, `/compress failed: ${err}`))
}

async function finishChain($: EngineInterface) {
  chain.phase = 'clear'
  $.ui.status('/clear …')
  try {
    await $.command.run({ command: 'clear' })
    if (chain.isAfterClear) {
      chain.isAfterClear = false
      const note = await nextNote($)
      if (note) {
        await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: note }] } })
        $.ui.log('NEXT.md handed to the new conversation')
      }
    }
    chain.phase = 'whatsnext'
    chain.turn = undefined
    chain.base = 0
    $.ui.status('/whats-next …')
    await $.command.run({ command: 'whats-next' })
  } catch (err) {
    reset($, `/clear or /whats-next failed: ${err}`)
  }
}

// Fallback when the engine proposes nothing after /whats-next.
async function restoreSuggestion($: EngineInterface) {
  const text = chain.restore
  chain.restore = undefined
  if (text === undefined) return
  const { isShown } = await $.prompt.suggest({ text })
  $.ui.log(isShown ? `suggestion restored: ${text}` : 'suggestion not restored (box not empty)')
}

export const register: Register = (on, options) => {
  const auto = options.auto === true
  const threshold = Number(options.threshold ?? 250_000)
  const injectNext = options.injectNext === true

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'tokopt-chain',
      description: 'Hand the session over now: /compress, /clear, /whats-next',
    })
    return next(e)
  })

  // A command.run hook may not run another command itself (it would wait on
  // its own turn): answer at once and start the chain from a timer.
  on('command.run', { command: 'tokopt-chain' }, async $ => {
    if (chain.phase !== 'idle') return { text: `chain already running (${chain.phase})` }
    $.clock.after(0, () => void startChain($))
    return { text: 'starting /compress → /clear → /whats-next' }
  })

  on('session.measure', async ($, e, next) => {
    if (e.context.tokens !== undefined) {
      chain.tokens = e.context.tokens
      if (chain.phase === 'idle') {
        $.ui.status(`ctx ${k(chain.tokens)}/${k(threshold)}${auto ? '' : ' (manual)'}`)
      }
    }
    if (chain.phase !== 'idle' && (await $.clock.now()) - chain.since > STALE_MS) {
      reset($, `chain stuck in ${chain.phase}, dropped`)
    }
    if (auto && chain.phase === 'idle' && chain.tokens >= Math.max(threshold, chain.base + STEP)) {
      await startChain($)
    }
    return next(e)
  })

  // The first main-loop turn after queueing a command is that command's turn.
  on('turn.start', async ($, e, next) => {
    if ((chain.phase === 'compress' || chain.phase === 'whatsnext') && chain.turn === undefined) {
      chain.turn = e.turnId
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined || e.turnId !== chain.turn) return result

    if (chain.phase === 'compress') {
      const isWritten = (await mtime($, chain.nextPath)) > chain.nextMtime
      // The skill's closing line names /clear: that counts as done too.
      const isClaimed = /\/clear/.test(e.answer)
      if (e.isAborted || !(isWritten || isClaimed)) {
        reset($, 'NEXT.md was not written, chain stopped')
      } else {
        void finishChain($)
      }
    } else if (chain.phase === 'whatsnext') {
      chain.phase = 'idle'
      chain.turn = undefined
      $.ui.status(`ctx ${k(chain.tokens)}/${k(threshold)}${auto ? '' : ' (manual)'}`)
      if (chain.restore !== undefined) $.clock.after(RESTORE_MS, () => void restoreSuggestion($))
    }
    return result
  })

  // Remember the engine's dim suggestion while idle; after the chain, put the
  // remembered one in place of the engine's next guess.
  on('prompt.suggest', async ($, e, next) => {
    if (e.origin.kind !== 'suggestion') return next(e)
    if (chain.phase === 'idle' && chain.restore !== undefined) {
      const text = chain.restore
      chain.restore = undefined
      $.ui.log(`suggestion restored: ${text}`)
      return next({ ...e, text })
    }
    const shown = await next(e)
    if (chain.phase === 'idle' && shown.isShown) chain.suggestion = e.text
    // The auto start fires on the turn's measurement, which may come before
    // the engine's guess for that same turn: take it while /compress waits.
    if (chain.phase === 'compress' && chain.turn === undefined) chain.restore = e.text
    return shown
  })

  // A prompt the person sent makes the old suggestion stale.
  // After a /clear by hand, the first prompt carries NEXT.md as context.
  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'composer') chain.suggestion = undefined
    if (!chain.isAfterClear || chain.phase !== 'idle') return next(e)
    chain.isAfterClear = false
    const note = await nextNote($)
    if (!note) return next(e)
    $.ui.log('NEXT.md handed to the new conversation')
    return next({ ...e, context: [...(e.context ?? []), note] })
  })

  // After /clear the new conversation gets NEXT.md. prompt.context does not
  // fire again after /clear, so the chain appends a row and a /clear by hand
  // hands it over with the first prompt.
  on('session.end', async ($, e, next) => {
    if (e.reason === 'clear' && injectNext) chain.isAfterClear = true
    return next(e)
  })
}
