import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const TICKET = '/work/ZO-1-ticket'
const CLEAR = { reason: 'clear', sessionId: 's1', resume: { id: 's1' } } as const

// Stands in for the host: a ticket folder with TASK.md, a NEXT.md whose
// mtime the test moves, and a log of the slash commands the mod runs.
function host(engine: Engine, on: On) {
  const state = { ran: [] as string[], nextMtime: 0, toasts: [] as string[], shown: [] as string[], appended: [] as string[], context: [] as string[] }
  on('session.cwd', () => ({ value: `${TICKET}/ZO-1-repo` }))
  on('fs.exists', (_$, e) => ({ value: e.path === `${TICKET}/TASK.md` }))
  on('fs.stat', () => ({ value: { kind: 'file', size: 1, mtimeMs: state.nextMtime, isLink: false } }))
  on('clock.now', () => ({ value: 0 }))
  on('clock.after', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.toast', (_$, e) => {
    state.toasts.push(String(e.text))
    return { value: undefined }
  })
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  on('prompt.suggest', (_$, e) => {
    state.shown.push(e.text)
    return { isShown: true }
  })
  on('session.end', (_$, e) => ({ sessionId: e.sessionId }))
  on('session.append', (_$, e, next) => {
    for (const b of e.message.content) if (typeof b !== 'string' && b.type === 'text') state.appended.push(String(b.text))
    return next(e)
  })
  on('prompt.submit', (_$, e) => {
    state.context.push(...(e.context ?? []))
    return { text: e.text, context: e.context }
  })
  on('fs.read', () => ({ value: '# Next\n\n1. [ ] weiter' }))
  on('command.run', async (_$, e) => {
    state.ran.push(e.command)
    if (e.command === 'clear') await engine.session.end(CLEAR) // as the engine's /clear does
    return { text: '' }
  })
  return state
}

// The person typing /tokopt-chain at the prompt.
const TYPED = {
  command: 'tokopt-chain',
  args: '',
  origin: { kind: 'composer' },
  presentation: { isFullscreen: false, columns: 120 },
} as const

async function settle(until: () => boolean) {
  for (let i = 0; i < 1000 && !until(); i++) await Promise.resolve()
}

test('chain runs /compress, then /clear and /whats-next once NEXT.md is written', async ($, on) => {
  const h = host($, on)

  await $.command.run(TYPED)
  await settle(() => h.ran.length >= 1)
  expect({ ran: h.ran, toasts: h.toasts }).toEqual({ ran: ['compress'], toasts: [] })

  await $.turn.start({ text: '/compress', turnId: 't1' })
  h.nextMtime = 1
  await $.turn.complete({ answer: 'NEXT.md geschrieben', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
  await settle(() => h.ran.length >= 3)
  expect(h.ran).toEqual(['compress', 'clear', 'whats-next'])
})

test('chain stops without /clear when NEXT.md was not written', async ($, on) => {
  const h = host($, on)

  await $.command.run(TYPED)
  await settle(() => h.ran.length >= 1)
  await $.turn.start({ text: '/compress', turnId: 't1' })
  await $.turn.complete({ answer: 'nichts gemacht', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
  await settle(() => h.toasts.length >= 1)

  expect(h.ran).toEqual(['compress'])
  expect(h.toasts[0]).toContain('NEXT.md was not written')
})

const ENGINE = { kind: 'suggestion' } as const

test('the dim suggestion from before the chain comes back after /whats-next', async ($, on) => {
  const h = host($, on)

  await $.prompt.suggest({ text: 'mach mit Punkt 2 weiter', origin: ENGINE })
  await $.command.run(TYPED)
  await settle(() => h.ran.length >= 1)
  await $.turn.start({ text: '/compress', turnId: 't1' })
  h.nextMtime = 1
  await $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
  await settle(() => h.ran.length >= 3)
  await $.turn.start({ text: '/whats-next', turnId: 't2' })
  await $.turn.complete({ answer: '1. weiter', durationMs: 1, isAborted: false, turnId: 't2', reason: 'answer' })

  await $.prompt.suggest({ text: 'engine guess', origin: ENGINE })
  expect(h.shown).toEqual(['mach mit Punkt 2 weiter', 'mach mit Punkt 2 weiter'])
})

test('the chain hands NEXT.md to the conversation after /clear', { options: { injectNext: true } }, async ($, on) => {
  const h = host($, on)

  await $.command.run(TYPED)
  await settle(() => h.ran.length >= 1)
  await $.turn.start({ text: '/compress', turnId: 't1' })
  h.nextMtime = 1
  await $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
  await settle(() => h.ran.length >= 3)

  expect(h.ran).toEqual(['compress', 'clear', 'whats-next'])
  expect(h.appended.join()).toContain('1. [ ] weiter')
})

test('after a /clear by hand the first prompt carries NEXT.md', { options: { injectNext: true } }, async ($, on) => {
  const h = host($, on)

  await $.session.end(CLEAR)
  await $.prompt.submit({ text: 'weiter', wait: false, origin: { kind: 'composer' } })
  await $.prompt.submit({ text: 'noch was', wait: false, origin: { kind: 'composer' } })

  expect(h.context.length).toBe(1)
  expect(h.context[0]).toContain('1. [ ] weiter')
})
