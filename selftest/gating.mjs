/**
 * Gate unit test.
 *
 * `lib/gate.mjs` runs inside the pre-step path of every model step, so its two
 * hard rules are that it never throws and that it stays cheap. Both are asserted
 * here against synthetic Agents, without a device, Python or a real Session.
 *
 * Usage: node selftest/gating.mjs
 */

import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { createGate } from '../lib/gate.mjs'

const ALL = ['bambu3mf_inspect', 'bambu3mf_edit', 'bambu3mf_mesh', 'bambu3mf_build', 'bambu3mf_render', 'bambu3mf_settings', 'bambu3mf_slice', 'bambu3mf_diff']
const LIGHT = ['bambu3mf_inspect', 'bambu3mf_render', 'bambu3mf_diff']

/** A Session stand-in: only `header.cwd` and `log` are read by the gate. */
function agentIn(cwd, log = []) {
  return { session: { header: { cwd }, log } }
}

function userSays(text) {
  return { type: 'user/message', data: { content: [{ type: 'text', text }] } }
}

const root = mkdtempSync(path.join(os.tmpdir(), 'bambu-gate-'))
const dirs = {
  empty: path.join(root, 'empty'),
  withProject: path.join(root, 'project'),
  withStl: path.join(root, 'stl'),
  withNested: path.join(root, 'nested'),
}
for (const dir of Object.values(dirs)) mkdirSync(dir, { recursive: true })
writeFileSync(path.join(dirs.withProject, 'model.3mf'), '')
writeFileSync(path.join(dirs.withStl, 'part.stl'), '')
mkdirSync(path.join(dirs.withNested, 'deep'))
writeFileSync(path.join(dirs.withNested, 'deep', 'model.3mf'), '')

const gate = createGate({ config: {}, sliceReady: () => true })
const visible = (name, context) => gate.visible(name, context)

const cases = []
function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  cases.push({ label, ok, actual, expected })
  assert.deepEqual(actual, expected, label)
}

// A directory with a 3MF admits everything.
check(
  'cwd holds a .3mf → all tools',
  ALL.filter((name) => visible(name, { agent: agentIn(dirs.withProject) })),
  ALL,
)

// An STL only helps mesh (it is the one tool that reads STL).
check(
  'cwd holds only an .stl → mesh only',
  ALL.filter((name) => visible(name, { agent: agentIn(dirs.withStl) })),
  ['bambu3mf_mesh'],
)

// An unrelated directory admits nothing, which is the whole point.
check(
  'cwd holds nothing relevant → no tools',
  ALL.filter((name) => visible(name, { agent: agentIn(dirs.empty) })),
  [],
)

// A 3MF one level down is deliberately not scanned: the check is top-level only.
check(
  'nested .3mf is not discovered',
  ALL.filter((name) => visible(name, { agent: agentIn(dirs.withNested) })),
  [],
)

// A mention in the conversation admits the cheap entry points, not the writers.
check(
  'conversation mentions a .3mf → light tools only',
  ALL.filter((name) => visible(name, { agent: agentIn(dirs.empty, [userSays('打开 H:/bambu/006.3mf 看看')]) })),
  LIGHT,
)

// The mention scan must ignore a mention that fell out of the trailing window.
const stale = [
  userSays('处理一下 /x/old.3mf'),
  ...Array.from({ length: 25 }, (_, index) => userSays(`无关消息 ${index}`)),
]
check(
  'a mention outside the history window is ignored',
  ALL.filter((name) => visible(name, { agent: agentIn(dirs.empty, stale) })),
  [],
)

// A model-authored path must not count: only user messages are scanned.
const assistantOnly = [{ type: 'assistant/message', data: { content: [{ type: 'text', text: '已写入 /y/new.3mf' }] } }]
check(
  'an assistant-authored .3mf does not admit tools',
  ALL.filter((name) => visible(name, { agent: agentIn(dirs.empty, assistantOnly) })),
  [],
)

// Sticky: after one real call the tools survive a directory change.
const used = agentIn(dirs.empty)
gate.markUsed(used)
check(
  'markUsed makes every tool visible regardless of cwd',
  ALL.filter((name) => visible(name, { agent: used })),
  ALL,
)

// Sticky is per Agent, not global.
check(
  'sticky does not leak to another session',
  ALL.filter((name) => visible(name, { agent: agentIn(dirs.empty) })),
  [],
)

// Slicing additionally needs a resolvable CLI.
const noSlicer = createGate({ config: {}, sliceReady: () => false })
check(
  'slice is hidden when no slicer resolves',
  ALL.filter((name) => noSlicer.visible(name, { agent: agentIn(dirs.withProject) })),
  ALL.filter((name) => name !== 'bambu3mf_slice'),
)
const slicerButNoProject = createGate({ config: {}, sliceReady: () => true })
check(
  'slice stays hidden without 3MF evidence even when a slicer resolves',
  ALL.filter((name) => slicerButNoProject.visible(name, { agent: agentIn(dirs.empty) })),
  [],
)

// The condition must never throw, whatever it is handed.
const hostile = [
  ['undefined context', undefined],
  ['empty context', {}],
  ['null agent', { agent: null }],
  ['agent without session', { agent: {} }],
  ['session without header', { agent: { session: {} } }],
  ['cwd is not a string', { agent: { session: { header: { cwd: 42 } } } }],
  ['cwd does not exist', { agent: agentIn(path.join(root, 'does-not-exist')) }],
  ['log is not an array', { agent: { session: { header: { cwd: dirs.empty }, log: 'nope' } } }],
  ['log holds junk events', { agent: { session: { header: { cwd: dirs.empty }, log: [null, 1, 'x', {}] } } }],
  ['message data is null', { agent: agentIn(dirs.empty, [{ type: 'user/message', data: null }]) }],
]
for (const [label, context] of hostile) {
  let threw = null
  let value = null
  try {
    value = ALL.map((name) => visible(name, context))
  } catch (error) {
    threw = error
  }
  cases.push({ label: `never throws: ${label}`, ok: threw === null && value.every((item) => item === false) })
  assert.equal(threw, null, `threw on ${label}: ${threw?.message}`)
  assert.deepEqual(value, ALL.map(() => false), `expected all false for ${label}`)
}

// markUsed must tolerate a non-agent without throwing.
for (const value of [undefined, null, 0, 'x', {}]) {
  let threw = null
  try {
    gate.markUsed(value)
  } catch (error) {
    threw = error
  }
  cases.push({ label: `markUsed tolerates ${JSON.stringify(value) ?? 'undefined'}`, ok: threw === null })
  assert.equal(threw, null, `markUsed threw on ${JSON.stringify(value)}`)
}

// Cost: the condition sits in the pre-step path, so one call must stay well
// under a millisecond once the directory answer is cached.
const warm = agentIn(dirs.withProject)
visible('bambu3mf_inspect', { agent: warm })
const iterations = 5000
const started = process.hrtime.bigint()
for (let index = 0; index < iterations; index += 1) visible('bambu3mf_inspect', { agent: warm })
const perCallMs = Number(process.hrtime.bigint() - started) / 1e6 / iterations
cases.push({ label: `cached condition cost ${perCallMs.toFixed(4)}ms/call`, ok: perCallMs < 0.05 })
assert.ok(perCallMs < 0.05, `cached condition is too slow: ${perCallMs.toFixed(4)}ms per call`)

// Cache must expire, so a 3MF created in the working directory is noticed.
const fresh = path.join(root, 'fresh')
mkdirSync(fresh)
const freshAgent = agentIn(fresh)
assert.equal(visible('bambu3mf_inspect', { agent: freshAgent }), false, 'fresh directory starts empty')
writeFileSync(path.join(fresh, 'new.3mf'), '')
const beforeExpiry = visible('bambu3mf_inspect', { agent: freshAgent })
assert.equal(beforeExpiry, false, 'a cached empty answer holds until it expires')
await new Promise((resolve) => setTimeout(resolve, 5200))
const afterExpiry = visible('bambu3mf_inspect', { agent: freshAgent })
cases.push({ label: 'a new .3mf is noticed after the cache expires', ok: afterExpiry === true })
assert.equal(afterExpiry, true, 'the directory answer never refreshed')

// The live working directory must win over the Session header. `header.cwd` is
// fixed at Session creation and does not follow `working_directory cd`, so a gate
// that reads only the header reports a false negative after the user moves into
// a project directory.
{
  const headerOnly = { session: { header: { cwd: dirs.empty }, log: [] } }
  const liveGate = createGate({
    config: {},
    sliceReady: () => true,
    cwdOf: () => dirs.withProject,
  })
  check(
    'the live working directory wins over the header',
    ALL.filter((name) => liveGate.visible(name, { agent: headerOnly })),
    ALL,
  )

  // A live directory that throws must not break the condition: fall back to the
  // header rather than propagating.
  const throwingGate = createGate({
    config: {},
    sliceReady: () => true,
    cwdOf: () => {
      throw new Error('working-directory service is gone')
    },
  })
  check(
    'a throwing cwdOf falls back to the header',
    ALL.filter((name) => throwingGate.visible(name, { agent: agentIn(dirs.withProject) })),
    ALL,
  )
  check(
    'a throwing cwdOf with an empty header admits nothing',
    ALL.filter((name) => throwingGate.visible(name, { agent: agentIn(dirs.empty) })),
    [],
  )

  // An empty live answer also falls back instead of treating "" as the cwd.
  const emptyGate = createGate({ config: {}, sliceReady: () => true, cwdOf: () => '' })
  check(
    'an empty cwdOf falls back to the header',
    ALL.filter((name) => emptyGate.visible(name, { agent: agentIn(dirs.withProject) })),
    ALL,
  )
}

// The mention window counts user messages, not raw events. One tool call appends
// about five events, so an event-counted window used to expire after ~4 calls and
// the tools disappeared again mid-turn.
{
  const gate2 = createGate({ config: {}, sliceReady: () => true, cwdOf: () => dirs.empty })
  const NAMES = ALL
  const mention = userSays('处理 H:/bambu/006.3mf')
  const oneToolCall = () => [
    { type: 'tool/call', data: {} },
    { type: 'job/registered', data: {} },
    { type: 'job/settled', data: {} },
    { type: 'tool/result', data: {} },
    { type: 'step/end', data: {} },
  ]
  const runtimeContext = {
    type: 'user/message',
    data: { content: [{ type: 'text', text: 'Current working directory: "H:/elsewhere".' }], source: { kind: 'runtime-context' } },
  }
  const log = [mention]
  const seen = []
  for (let step = 0; step < 8; step += 1) {
    seen.push(NAMES.filter((name) => gate2.visible(name, { agent: agentIn(dirs.empty, [...log]) })).length)
    log.push(...oneToolCall(), runtimeContext)
  }
  check('a mention survives eight tool calls (no mid-turn flicker)', seen, Array(8).fill(LIGHT.length))

  // Machine-generated snapshots restate the environment every turn; they must not
  // be mistaken for a person naming a file.
  check(
    'runtime-context messages are not scanned for mentions',
    NAMES.filter((name) =>
      gate2.visible(name, {
        agent: agentIn(dirs.empty, [
          { type: 'user/message', data: { content: [{ type: 'text', text: 'see /x/auto.3mf' }], source: { kind: 'runtime-context' } } },
        ]),
      }),
    ),
    [],
  )

  // The agent loop claims the inbox before resolving conditions, and appends the
  // `user/message` event only while building the request, so on the first step of
  // a turn the message being answered is not in the log yet. It is durable as the
  // `inserted` payload of `agent/inbox/spliced`; missing it hid the tools on
  // exactly the step where the user had just named a file.
  const pending = {
    type: 'agent/inbox/spliced',
    data: {
      target: 'next-turn',
      start: 0,
      inserted: [
        {
          id: 'msg-1',
          role: 'user',
          source: { kind: 'user' },
          content: [{ type: 'text', text: '打开 H:/bambu/006.3mf' }],
        },
      ],
    },
  }
  check(
    'a message still in the inbox is scanned (first step of a turn)',
    NAMES.filter((name) => gate2.visible(name, { agent: agentIn(dirs.empty, [pending]) })),
    LIGHT,
  )

  // Once committed, the same message appears as both an inbox splice and a
  // `user/message` event; reading it twice must not matter.
  const committed = {
    type: 'user/message',
    data: {
      id: 'msg-1',
      role: 'user',
      source: { kind: 'user' },
      content: [{ type: 'text', text: '打开 H:/bambu/006.3mf' }],
    },
  }
  check(
    'a committed message read from both sources still admits the light tools',
    NAMES.filter((name) => gate2.visible(name, { agent: agentIn(dirs.empty, [pending, committed]) })),
    LIGHT,
  )
}

rmSync(root, { recursive: true, force: true })

const failed = cases.filter((item) => !item.ok)
for (const item of cases) console.log(`${item.ok ? 'ok  ' : 'FAIL'} ${item.label}`)
console.log(`\ngating ok: ${cases.length - failed.length}/${cases.length} checks`)
if (failed.length > 0) process.exit(1)
