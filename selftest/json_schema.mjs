/**
 * Validate a value against the JSON Schema subset the tool definitions use.
 *
 * Extracted from schema_contract.mjs so the slice-result test can reuse the
 * exact same rules: the tools declare `additionalProperties: false`, and both
 * the kernel and lib/slice.mjs must stay inside that declaration.
 */

export function validateValue(value, schema, at, errors) {
  if (schema === true || schema === undefined) return
  if (schema.const !== undefined && value !== schema.const) {
    errors.push(`${at}: expected const ${JSON.stringify(schema.const)}`)
    return
  }
  if (Array.isArray(schema.enum) && !schema.enum.includes(value)) {
    errors.push(`${at}: ${JSON.stringify(value)} not in enum`)
    return
  }
  if (Array.isArray(schema.oneOf)) {
    const matched = schema.oneOf.filter((option) => {
      const local = []
      validateValue(value, option, at, local)
      return local.length === 0
    })
    if (matched.length !== 1) {
      errors.push(`${at}: matched ${matched.length} of ${schema.oneOf.length} oneOf branches`)
    }
    return
  }
  if (schema.type) {
    const type = schema.type
    const ok =
      type === 'object'
        ? value !== null && typeof value === 'object' && !Array.isArray(value)
        : type === 'array'
          ? Array.isArray(value)
          : type === 'integer'
            ? typeof value === 'number' && Number.isInteger(value)
            : type === 'number'
              ? typeof value === 'number'
              : type === 'null'
                ? value === null
                : typeof value === type
    if (!ok) {
      errors.push(`${at}: expected ${type}, got ${value === null ? 'null' : typeof value}`)
      return
    }
  }
  if (Array.isArray(schema.items) && Array.isArray(value)) {
    value.forEach((item, index) => validateValue(item, schema.items[index], `${at}[${index}]`, errors))
    return
  }
  if (schema.items && Array.isArray(value)) {
    value.forEach((item, index) => validateValue(item, schema.items, `${at}[${index}]`, errors))
  }
  if (schema.properties && value !== null && typeof value === 'object') {
    for (const [key, sub] of Object.entries(schema.properties)) {
      if (value[key] === undefined) continue
      validateValue(value[key], sub, `${at}.${key}`, errors)
    }
    if (schema.additionalProperties === false) {
      for (const key of Object.keys(value)) {
        if (!(key in schema.properties)) {
          errors.push(`${at}.${key}: not declared (additionalProperties: false)`)
        }
      }
    }
  }
  if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
    for (const [key, sub] of Object.entries(value || {})) {
      if (schema.properties && key in schema.properties) continue
      validateValue(sub, schema.additionalProperties, `${at}.${key}`, errors)
    }
  }
  if (Array.isArray(schema.required) && value !== null && typeof value === 'object') {
    for (const key of schema.required) {
      if (value[key] === undefined) errors.push(`${at}.${key}: required but missing`)
    }
  }
}