/**
 * Step-time admission for the `bambu3mf_*` tools.
 *
 * The eight tools cost several thousand tokens of tool definitions on every
 * request. Most sessions never touch a 3MF, so registering them unconditionally
 * makes the plugin a fixed tax. `register(definition, { when })` is evaluated once per
 * model step, and a false answer removes the tool from the executor's
 * description, the PTC bindings and dispatch at the same time.
 *
 * Three signals decide admission, cheapest first:
 *
 *   1. the session's working directory holds a 3MF (or an STL) at its top level;
 *   2. this session has already worked on a 3MF, so the tools stay;
 *   3. the conversation mentions a 3MF path and the tool is a cheap entry point.
 *
 * Two rules shape the implementation:
 *
 *   - A condition must never throw. A throw fails step preparation as
 *     `TOOL_ACTIVATION_FAILED`, which would break the whole step rather than
 *     just hide a tool, so every filesystem and session read is guarded and a
 *     failure means "not visible".
 *   - A condition must be cheap. It runs inside the pre-step path of every
 *     model step, so this module only ever does one non-recursive `readdirSync`
 *     per directory and never spawns a process. Probing the Python kernel here
 *     would cost ~175ms per step.
 */

import { readdirSync } from 'node:fs'

/** Tools that are cheap entry points: useful even from a passing mention. */
const LIGHT = new Set(['bambu3mf_inspect', 'bambu3mf_render', 'bambu3mf_diff'])

/** Tools that reshape or write a package; they need real 3MF evidence. */
const HEAVY = new Set(['bambu3mf_edit', 'bambu3mf_mesh', 'bambu3mf_build', 'bambu3mf_settings'])

/** `bambu3mf_slice` is admitted only when a slicer is resolvable. */
const SLICE = 'bambu3mf_slice'

/** How long a directory answer stays fresh, in ms. */
const DIR_TTL_MS = 5000

/** Cap on remembered directories, so a long session cannot grow unbounded. */
const DIR_CACHE_MAX = 32

/**
 * How many trailing user messages the mention scan looks at.
 *
 * This counts messages, not events. One tool call appends about five events
 * (`tool/call`, `job/registered`, `job/settled`, `tool/result`, `step/end`), so
 * an event window of 20 is exhausted after roughly four tool calls and the
 * mention silently falls out mid-turn — the tools appeared and then vanished
 * again inside a single conversation.
 */
const HISTORY_WINDOW = 20

const PROJECT_PATTERN = /\.(?:gcode\.)?3mf\b/i
const STL_PATTERN = /\.stl\b/i

/** One cached directory answer. */
function evidenceOf(dir) {
  const names = readdirSync(dir)
  let project = false
  let stl = false
  for (const name of names) {
    if (!project && /\.(?:gcode\.)?3mf$/i.test(name)) project = true
    else if (!stl && /\.stl$/i.test(name)) stl = true
    if (project && stl) break
  }
  return { project, stl }
}

/**
 * Build the gate.
 *
 * @param {object} options
 * @param {object} options.config resolved plugin config
 * @param {() => boolean} [options.sliceReady] whether a slicer CLI resolves
 * @param {(agent: object) => string} [options.cwdOf] live working directory of
 *   the agent's Session; defaults to the session header's original directory
 * @returns {{visible: (name: string, context: object) => boolean, markUsed: (agent: unknown) => void}}
 */
export function createGate({ config = {}, sliceReady = () => true, cwdOf = defaultCwdOf } = {}) {
  /** Directory answers, newest last, so the oldest entry is the eviction target. */
  const dirCache = new Map()

  /** Sessions that have used a 3MF tool; the tools stay visible for the session. */
  const sticky = new WeakSet()

  function directoryEvidence(dir) {
    if (typeof dir !== 'string' || dir === '') return { project: false, stl: false }
    const now = Date.now()
    const hit = dirCache.get(dir)
    if (hit !== undefined && now - hit.at < DIR_TTL_MS) return hit.evidence
    let evidence
    try {
      evidence = evidenceOf(dir)
    } catch {
      // Missing directory, permission denied, or a transient IO error: no evidence
      // is the safe answer, and the condition must not throw.
      evidence = { project: false, stl: false }
    }
    dirCache.delete(dir)
    dirCache.set(dir, { at: now, evidence })
    if (dirCache.size > DIR_CACHE_MAX) {
      const oldest = dirCache.keys().next()
      if (!oldest.done) dirCache.delete(oldest.value)
    }
    return evidence
  }

  /** The working directory a session is in, without filesystem work. */
  function sessionCwd(agent) {
    try {
      const live = cwdOf(agent)
      if (typeof live === 'string' && live !== '') return live
    } catch {
      // Fall through to the header.
    }
    return defaultCwdOf(agent)
  }

  /**
   * The trailing user messages of the session, newest last.
   *
   * Only person-authored messages are collected: a 3MF path the person typed is
   * a deliberate signal, while a path the model itself produced is already
   * covered by the sticky flag once a tool actually ran. Runtime-context and
   * skill-catalog messages are skipped too — they are machine-generated
   * snapshots that restate the whole environment on every turn and would
   * otherwise fill the window with noise.
   *
   * Two sources are read because of when this runs. The agent loop claims the
   * inbox *before* it resolves conditions, and only appends the `user/message`
   * event once the step's request is being built, so on the first step of a turn
   * the message being answered is not in the log yet. It is still durable as the
   * `inserted` payload of `agent/inbox/spliced`, so the scan reads both and
   * de-duplicates by message id.
   */
  function recentUserText(agent) {
    const log = agent?.session?.log
    if (!Array.isArray(log) || log.length === 0) return []
    const texts = []
    const seen = new Set()
    const take = (message) => {
      const source = message?.source
      if (source !== undefined && source !== null && source.kind !== 'user') return
      const id = message?.id
      if (typeof id === 'string') {
        if (seen.has(id)) return
        seen.add(id)
      }
      const text = messageText(message)
      if (text !== '') texts.push(text)
    }
    for (let index = log.length - 1; index >= 0 && texts.length < HISTORY_WINDOW; index -= 1) {
      const event = log[index]
      if (event?.type === 'user/message') {
        take(event.data)
        continue
      }
      if (event?.type === 'agent/inbox/spliced') {
        const inserted = event.data?.inserted
        if (!Array.isArray(inserted)) continue
        for (let slot = inserted.length - 1; slot >= 0 && texts.length < HISTORY_WINDOW; slot -= 1) {
          take(inserted[slot])
        }
      }
    }
    return texts
  }

  /** Whether a trailing slice of the session mentions a 3MF. */
  function mentionsProject(agent) {
    return recentUserText(agent).some((text) => PROJECT_PATTERN.test(text))
  }

  /** Whether a trailing slice of the session mentions an STL. */
  function mentionsStl(agent) {
    return recentUserText(agent).some((text) => STL_PATTERN.test(text))
  }

  /**
   * Decide one tool's visibility for one model step.
   *
   * @param {string} name the tool being admitted
   * @param {{agent?: object}} context the activation context
   * @returns {boolean}
   */
  function visible(name, context) {
    try {
      const agent = context?.agent
      if (agent === undefined || agent === null) return false
      if (sticky.has(agent)) return true

      const evidence = directoryEvidence(sessionCwd(agent))
      if (evidence.project) {
        // Everything but slice is admitted by a project in the working directory.
        // Slice additionally needs Bambu Studio: without the CLI it could only
        // report CLI_MISSING, so it should not occupy a slot.
        if (name === SLICE) return sliceReady()
        return true
      }
      if (evidence.stl && name === 'bambu3mf_mesh') return true

      // Nothing on disk: only the cheap entry points answer to a passing mention,
      // and slice stays out because it is one of the heaviest definitions.
      if (name === SLICE) return false

      if (LIGHT.has(name)) return mentionsProject(agent)
      if (HEAVY.has(name)) return false
      return false
    } catch {
      // Never let a condition throw: a throw aborts step preparation entirely.
      return false
    }
  }

  /**
   * Record that this session has done real 3MF work.
   *
   * Called from each tool's `execute`, so the tools stay available for the rest
   * of the session even after the working directory moves away from the project.
   */
  function markUsed(agent) {
    try {
      if (agent !== undefined && agent !== null && typeof agent === 'object') sticky.add(agent)
    } catch {
      // A non-extensible or exotic agent is not worth failing a tool call over.
    }
  }

  return { visible, markUsed }
}

/** Flatten a message's text blocks for pattern matching. */
function messageText(data) {
  if (typeof data === 'string') return data
  const content = data?.content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts = []
  for (const block of content) {
    if (typeof block?.text === 'string') parts.push(block.text)
  }
  return parts.join('\n')
}

/**
 * The session's original directory, from its durable header.
 *
 * This is a fallback only: `header.cwd` is fixed when the Session is created and
 * never changes, so it does not follow `working_directory cd`. The live value
 * comes from the working-directory service, which the plugin passes in.
 */
function defaultCwdOf(agent) {
  const header = agent?.session?.header
  return typeof header?.cwd === 'string' ? header.cwd : ''
}

export { LIGHT, HEAVY, SLICE }
