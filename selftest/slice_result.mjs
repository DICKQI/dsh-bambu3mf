/**
 * Slice-result parsing test.
 *
 * lib/slice.mjs reads Bambu's result.json; the Python self test cannot see that
 * layer, and a real slice takes minutes. This runs the parser against a
 * recorded result.json (selftest/data/slice_result.json, a real 4mm part sliced
 * by the installed Studio) and validates the return value against the slice
 * tool's declared output schema.
 */

import { copyFileSync, mkdtempSync, utimesSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { createTools } from '../lib/tools.mjs'
import { summarizeSlice } from '../lib/slice.mjs'
import { validateValue } from './json_schema.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const failures = []

function check(ok, name, detail) {
  if (!ok) failures.push(name)
  console.log((ok ? 'PASS' : 'FAIL') + '  ' + name + (detail ? '  [' + detail + ']' : ''))
}

const source = path.join(HERE, 'data', 'slice_result.json')
const dir = mkdtempSync(path.join(os.tmpdir(), 'b3mf-slice-test-'))
const exportName = 'slice-test.gcode.3mf'
const artifact = path.join(dir, exportName)
const resultPath = path.join(dir, 'result.json')
writeFileSync(artifact, 'placeholder')
copyFileSync(source, resultPath)
const now = new Date()
utimesSync(resultPath, now, now)
utimesSync(artifact, now, now)

const logs = { stdout: [], stderr: [], bytes: 0 }
const started = Date.now() - 1000

function run() {
  return summarizeSlice(path.join(dir, 'input.3mf'), dir, exportName, 'gcode.3mf', 0, started, logs)
}

let value
try {
  value = run()
} catch (error) {
  check(false, 'slice result parses', error.code || error.message)
  console.log('1 failed')
  process.exit(1)
}

const stats = value.sliceStats
check(Math.abs(stats.featureTimes['Outer wall'] - 1664.08) < 0.01, 'feature times parsed', stats.featureTimes['Outer wall'] + 's outer wall')
check(Math.abs(stats.estimateMinutes - 34.4) < 0.05, 'estimate uses total_predication', stats.estimateMinutes + ' min')
check(stats.plateStats.length === 1 && Math.abs(stats.plateStats[0].sliceSeconds - 3.9) < 0.01, 'process times converted from ms', stats.plateStats[0] && stats.plateStats[0].sliceSeconds + 's')
check(stats.resultStale === false, 'fresh result accepted')
check(value.summary.includes('外墙') && value.summary.includes('耗时分解'), 'summary shows the breakdown')
check(!value.diagnostics.some((item) => item.code === 'STALE_RESULT'), 'no stale diagnostic on a fresh result')

const tool = createTools({ config: {} }).find((item) => item.name === 'bambu3mf_slice')
const errors = []
validateValue(value, tool.output.schema, 'value', errors)
check(errors.length === 0, 'output matches the slice schema', errors[0] || Object.keys(stats).length + ' stats keys')

// An old result.json must not be reported as this run's timing.
const old = new Date(Date.now() - 3600 * 1000)
utimesSync(resultPath, old, old)
const stale = run()
check(stale.sliceStats.resultStale === true && Object.keys(stale.sliceStats.featureTimes).length === 0, 'stale result ignored')
check(stale.diagnostics.some((item) => item.code === 'STALE_RESULT'), 'stale result warned')
check(!stale.summary.includes('外墙'), 'stale summary has no breakdown')

// A stale export must fail instead of being reported as a success.
utimesSync(artifact, old, old)
let failed = null
try {
  run()
} catch (error) {
  failed = error
}
check(Boolean(failed) && failed.code === 'SLICE_FAILED', 'stale export is a failure', failed && failed.message.split('\n')[0])

console.log(failures.length ? '\n' + failures.length + ' failed' : '\nall slice-result checks passed')
process.exit(failures.length ? 1 : 0)