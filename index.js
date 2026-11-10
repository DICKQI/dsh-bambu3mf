/**
 * Bambu 3MF editor — host half.
 *
 * Registers eight tools backed by the Python kernel in ./python. The plugin keeps
 * its surface deliberately small: all 3MF surgery, geometry and rendering live
 * in the kernel so the loaded JavaScript module rarely needs to change.
 *
 * The tools are gated rather than always-on: eight tool definitions cost several
 * thousand tokens on every request, and most sessions never touch a 3MF. `lib/gate.mjs`
 * decides per model step whether they belong in the request at all.
 */

import { createGate } from './lib/gate.mjs'
import { resolveBambuStudio } from './lib/slice.mjs'
import { createTools } from './lib/tools.mjs'

export const name = 'bambu-3mf'
export const inject = ['tools']

export function apply(ctx, config = {}) {
  const resolved = config || {}

  // Probing for Bambu Studio spawns a process, so the answer is cached for the
  // life of the plugin fiber: the condition runs on every model step, and a
  // per-step probe would add ~15ms to each one.
  let sliceProbe = null
  const sliceReady = () => {
    if (sliceProbe === null) {
      try {
        resolveBambuStudio(resolved)
        sliceProbe = true
      } catch {
        sliceProbe = false
      }
    }
    return sliceProbe
  }

  // The live working directory comes from the working-directory service, read
  // lazily inside the condition: `session.header.cwd` is fixed at Session
  // creation and never follows `working_directory cd`, and at `apply` time the
  // service may not be injected yet, so a captured `undefined` would silently
  // pin the gate to the header. `get()` does no filesystem I/O.
  const cwdOf = (agent) => {
    const service = ctx.get('workingDirectory')
    if (service === undefined || service === null) return ''
    return service.get(agent.session)
  }

  const gate = createGate({ config: resolved, sliceReady, cwdOf })
  const runtime = {
    config: resolved,
    attachments: ctx.get('attachments'),
    gate,
  }

  for (const definition of createTools(runtime)) {
    ctx.effect(
      () => ctx.tools.register(definition, { when: (context) => gate.visible(definition.name, context) }),
      `bambu-3mf ${definition.name}`,
    )
  }
}
