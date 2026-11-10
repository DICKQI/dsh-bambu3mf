/**
 * Output-schema contract test.
 *
 * The kernel (Python) and the tool definitions (JS) declare the same data twice:
 * once as returned dicts, once as JSON Schema with `additionalProperties: false`.
 * Nothing in the Python self test can see that mismatch, because it never touches
 * the JS layer — which is exactly how a key (`objects[].size`) shipped in 0.3.0
 * while its schema still rejected it.
 *
 * This script closes that gap: `selftest/make_fixtures.py` records real kernel
 * output into <dir>/<tool>.json, and here each recording is validated against the
 * schema the tool actually declares.
 *
 * Usage: node selftest/schema_contract.mjs <fixtures-dir>
 */

import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { createTools } from '../lib/tools.mjs'
import { validateValue } from './json_schema.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))

function main() {
  const fixturesDir = process.argv[2] || path.join(HERE, 'fixtures')
  const tools = new Map(createTools({ config: {} }).map((tool) => [tool.name, tool]))
  let files
  try {
    files = readdirSync(fixturesDir).filter((name) => name.endsWith('.json'))
  } catch {
    console.error(`no fixtures under ${fixturesDir}; run selftest/make_fixtures.py first`)
    return 2
  }
  if (!files.length) {
    console.error(`no fixtures under ${fixturesDir}`)
    return 2
  }

  let failures = 0
  const seenKeys = new Map()
  for (const name of files.sort()) {
    const payload = JSON.parse(readFileSync(path.join(fixturesDir, name), 'utf8'))
    const tool = tools.get(payload.tool)
    if (!tool) {
      console.log(`FAIL  ${name}  [unknown tool ${payload.tool}]`)
      failures += 1
      continue
    }
    const seen = seenKeys.get(payload.tool) || new Set()
    if (payload.value && typeof payload.value === 'object' && !Array.isArray(payload.value)) {
      for (const key of Object.keys(payload.value)) seen.add(key)
    }
    seenKeys.set(payload.tool, seen)

    const errors = []
    validateValue(payload.value, tool.output.schema, 'value', errors)
    if (errors.length) {
      failures += 1
      console.log(`FAIL  ${name}  [${payload.tool} / ${payload.case}]`)
      for (const error of errors.slice(0, 6)) console.log(`        ${error}`)
    } else {
      console.log(`PASS  ${name}  [${payload.tool} / ${payload.case}]`)
    }
  }
  // Coverage, not a gate: a declared property that no fixture ever produced is
  // a branch nobody tests (that is how `backup` slipped through).
  const unexercised = []
  for (const [name, tool] of tools) {
    const declared = Object.keys(tool.output?.schema?.properties || {})
    const seen = seenKeys.get(name) || new Set()
    const unseen = declared.filter((key) => !seen.has(key))
    if (unseen.length) unexercised.push(`  ${name}: ${unseen.join(', ')}`)
  }
  if (unexercised.length) {
    console.log('\nnever exercised (declared, but no fixture in this set produced it):')
    for (const line of unexercised) console.log(line)
  }
  console.log(`\n${files.length - failures}/${files.length} output contracts valid`)
  return failures ? 1 : 0
}

// Importing this module (the slice test reuses the validator) must not run the
// contract check. Only execute when invoked as a script.
const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href
if (invokedDirectly) process.exit(main())
