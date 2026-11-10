/**
 * Bambu Studio CLI slicing: the strongest proof that an edited package still
 * opens. The CLI exits 0 even when exporting fails, so the real verdict comes
 * from result.json plus the artifacts it left behind.
 */

import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { ToolError, callPython } from './bridge.mjs'

const MAX_LOG_BYTES = 256 * 1024

let cachedStudio = null

function studioCandidates(config) {
  const candidates = []
  if (config.bambuStudioPath) candidates.push(config.bambuStudioPath)
  if (process.platform === 'win32') {
    const programFiles = process.env.ProgramFiles || 'C:\\Program Files'
    const programFilesX86 = process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)'
    const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')
    candidates.push(
      path.join(programFiles, 'Bambu Studio', 'bambu-studio.exe'),
      path.join(programFilesX86, 'Bambu Studio', 'bambu-studio.exe'),
      path.join(localAppData, 'Programs', 'Bambu Studio', 'bambu-studio.exe'),
      'bambu-studio.exe',
    )
  } else {
    candidates.push('bambu-studio', 'BambuStudio', '/usr/bin/bambu-studio')
  }
  return candidates
}

export function resolveBambuStudio(config = {}) {
  if (cachedStudio) return cachedStudio
  const tried = []
  for (const candidate of studioCandidates(config)) {
    if (candidate.includes(path.sep) && !existsSync(candidate)) {
      tried.push(`${candidate}: not found`)
      continue
    }
    if (candidate.includes(path.sep)) {
      cachedStudio = candidate
      return cachedStudio
    }
    const probe = spawnSync(candidate, ['--help'], {
      timeout: 15000,
      windowsHide: true,
      encoding: 'utf8',
    })
    if (probe.error) {
      tried.push(`${candidate}: ${probe.error.code || probe.error.message}`)
      continue
    }
    cachedStudio = candidate
    return cachedStudio
  }
  throw new ToolError(
    'CLI_MISSING',
    '找不到 Bambu Studio CLI。请在插件配置 bambuStudioPath 指向 bambu-studio.exe。',
    { tried },
  )
}

/**
 * Run the kernel's clearance preflight. Returns null when the check itself is
 * unavailable (no project settings, kernel error): slicing must still be tried.
 */
async function runPreflight(file, plate, config, signal) {
  try {
    const value = await callPython(
      'preflight',
      { file, plate },
      { config, signal, timeoutMs: 120000 },
    )
    return value
  } catch {
    return null
  }
}

function preflightFailure(value) {
  const lines = [value.summary || '切片预检失败']
  const ops = moveOps(value.suggestions)
  if (ops) lines.push(`可直接用的 ops：${ops}`)
  return new ToolError('PREFLIGHT_FAILED', lines.join('\n'), {
    blocking: value.blocking || [],
    suggestions: value.suggestions || [],
    outputDir: null,
    logTail: '',
  })
}

/** Turn preflight suggestions into ready-to-paste `move` ops (frame independent). */
function moveOps(suggestions) {
  if (!Array.isArray(suggestions) || !suggestions.length) return ''
  return suggestions
    .filter((item) => item.move)
    .map(
      (item) =>
        `{"op":"move","object":${item.objectId},"dx":${item.move.dx},"dy":${item.move.dy}}`,
    )
    .join(', ')
}

export async function sliceProject(args, config = {}, signal) {
  const file = path.resolve(String(args.file || ''))
  if (!existsSync(file)) throw new ToolError('NOT_FOUND', `文件不存在：${file}`)
  const studio = resolveBambuStudio(config)
  const plate = Number.isInteger(args.plate) ? args.plate : 0
  const exportMode = String(args.export || 'gcode.3mf')
  if (!['gcode.3mf', 'gcode', 'both'].includes(exportMode)) {
    throw new ToolError('INVALID_ARGS', 'export 必须是 gcode.3mf、gcode 或 both')
  }
  // Advisory by default: clearance is reported, not enforced. Bambu accepts
  // plates this check used to refuse, so blocking on distance only produced
  // false failures. `preflight: "strict"` restores the gate for callers that
  // want it; `false` skips the check entirely.
  const preflightMode = args.preflight === undefined ? 'report' : args.preflight
  let readiness = null
  if (preflightMode !== false) {
    readiness = await runPreflight(file, plate, config, signal)
    if (readiness && readiness.ready === false && preflightMode === 'strict') {
      throw preflightFailure(readiness)
    }
  }
  const stem = path.basename(file).replace(/\.(gcode\.)?3mf$/i, '')
  const outDir = path.resolve(
    String(args.output_dir || path.join(path.dirname(file), `${stem}_sliced`)),
  )
  mkdirSync(outDir, { recursive: true })
  const exportName = `${stem}.gcode.3mf`
  const timeoutMs = Number(args.timeout_ms) > 0
    ? Number(args.timeout_ms)
    : Number(config.sliceTimeoutMs) > 0
      ? Number(config.sliceTimeoutMs)
      : 1800000

  const argv = ['--slice', String(plate), '--outputdir', outDir, '--allow-newer-file', '--debug', '2']
  if (exportMode !== 'gcode') argv.push('--export-3mf', exportName)
  if (args.settings) argv.push('--load-settings', String(args.settings))
  if (args.filaments) argv.push('--load-filaments', String(args.filaments))
  argv.push(file)

  const started = Date.now()
  return new Promise((resolve, reject) => {
    let settled = false
    const logs = { stdout: [], stderr: [], bytes: 0 }
    let child
    try {
      child = spawn(studio, argv, { windowsHide: true, env: process.env })
    } catch (error) {
      reject(new ToolError('CLI_FAILED', `无法启动 Bambu Studio：${error.message}`))
      return
    }
    const collect = (chunk) => {
      if (logs.bytes >= MAX_LOG_BYTES) return
      logs.bytes += chunk.length
      logs.stdout.push(chunk)
    }
    const collectErr = (chunk) => {
      if (logs.bytes >= MAX_LOG_BYTES) return
      logs.bytes += chunk.length
      logs.stderr.push(chunk)
    }
    const finish = (fn, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', onAbort)
      fn(value)
    }
    const onAbort = () => {
      try {
        child.kill('SIGKILL')
      } catch {
        // ignore
      }
      finish(reject, new ToolError('ABORTED', '切片已取消'))
    }
    const timer = setTimeout(() => {
      try {
        child.kill('SIGKILL')
      } catch {
        // ignore
      }
      finish(
        reject,
        new ToolError('TIMEOUT', `Bambu Studio 切片超时（${Math.round(timeoutMs / 1000)}s）`),
      )
    }, timeoutMs)
    if (signal) {
      if (signal.aborted) {
        onAbort()
        return
      }
      signal.addEventListener('abort', onAbort, { once: true })
    }
    child.stdout.on('data', collect)
    child.stderr.on('data', collectErr)
    child.on('error', (error) => {
      finish(reject, new ToolError('CLI_FAILED', `Bambu Studio 启动失败：${error.message}`))
    })
    child.on('close', async (code) => {
      if (settled) return
      let value
      try {
        value = summarizeSlice(file, outDir, exportName, exportMode, code, started, logs)
      } catch (error) {
        // The child is gone, so the wall-clock limit no longer applies; clear it
        // before the (async) advisory lookup, or it could replace SLICE_FAILED
        // with TIMEOUT while we are still explaining the failure.
        clearTimeout(timer)
        finish(reject, await withAdvice(error, file, plate, config, signal, readiness))
        return
      }
      if (readiness) {
        value.preflight = {
          ready: readiness.ready !== false,
          summary: readiness.summary || '',
          blocking: readiness.blocking || [],
          warnings: readiness.warnings || [],
          suggestions: readiness.suggestions || [],
        }
        for (const item of readiness.warnings || []) {
          value.diagnostics.push({
            severity: 'warning',
            code: item.code,
            message: item.message,
            ...(item.objectId === undefined ? {} : { objectId: item.objectId }),
          })
        }
      }
      finish(resolve, value)
    })
  })
}

/**
 * A slice failure from the CLI is one generic sentence ("G-code conflicts
 * detected…"). Attach the clearance report so the message names the actual
 * candidates instead of leaving the caller to guess.
 */
async function withAdvice(error, file, plate, config, signal, known) {
  if (!(error instanceof ToolError) || error.code !== 'SLICE_FAILED') return error
  const readiness = known || (await runPreflight(file, plate, config, signal))
  if (!readiness) return error
  const ops = moveOps(readiness.suggestions)
  const extras = [readiness.summary || '', ops ? `可直接用的 ops：${ops}` : ''].filter(Boolean)
  if (!extras.length) return error
  error.message = [error.message, ...extras].join('\n')
  error.details = {
    ...(error.details || {}),
    blocking: readiness.blocking || [],
    suggestions: readiness.suggestions || [],
  }
  return error
}

// ------------------------------------------------------- result interpretation
/** Bambu's feature names are stable English keys; the text output is Chinese. */
const FEATURE_LABELS = {
  'Outer wall': '外墙',
  'Inner wall': '内墙',
  'Overhang wall': '悬垂墙',
  'Sparse infill': '稀疏填充',
  'Internal solid infill': '内部实心填充',
  'Top surface': '顶面',
  'Bottom surface': '底面',
  'Gap infill': '间隙填充',
  Bridge: '桥接',
  Support: '支撑',
  Travel: '空驶',
  Flush: '冲刷',
  Custom: '自定义',
  Undefined: '未分类',
  Ironing: '熨烫',
}

/** Clock tolerance when deciding whether an artifact belongs to this run. */
const STALE_MS = 2000

function mtimeOf(file) {
  try {
    return statSync(file).mtimeMs
  } catch {
    return 0
  }
}

function freshSince(file, started) {
  const stamp = mtimeOf(file)
  return stamp > 0 && stamp >= started - STALE_MS
}

/**
 * Read result.json, but only when it belongs to this run.
 *
 * The output directory is reused across slices, so an old result.json would
 * silently report the previous model's timings as this one's. Absent and stale
 * are different states: absent means "the CLI wrote nothing", stale means
 * "there is a file, but it is from an earlier slice".
 */
function parseSliceResult(resultPath, started) {
  if (!existsSync(resultPath)) return { result: null, stale: false }
  if (!freshSince(resultPath, started)) return { result: null, stale: true }
  try {
    const text = readFileSync(resultPath, 'utf8').replace(/^\uFEFF/, '')
    return { result: JSON.parse(text), stale: false }
  } catch {
    return { result: null, stale: false }
  }
}

/**
 * Sum Bambu's per-feature times over plates.
 *
 * feature_type_times is the only place that says where the time goes (walls vs
 * infill vs travel). Its total is close to, but not the same as, the printed
 * estimate, so percentages are computed against this sum, never against
 * total_predication.
 */
function featureBreakdown(plates) {
  const totals = new Map()
  for (const plate of plates) {
    const times = plate && plate.feature_type_times
    if (!times || typeof times !== 'object') continue
    for (const [key, value] of Object.entries(times)) {
      const seconds = Number(value)
      if (!Number.isFinite(seconds)) continue
      totals.set(key, (totals.get(key) || 0) + seconds)
    }
  }
  const ordered = [...totals.entries()].sort((a, b) => b[1] - a[1])
  const featureTimes = {}
  let total = 0
  for (const [key, seconds] of ordered) {
    featureTimes[key] = round2(seconds)
    total += seconds
  }
  return { featureTimes, featureTotalSeconds: round2(total) }
}

/** Newer Studio puts slice-process timings (milliseconds) under plate.times. */
function secondsFromMs(value) {
  const number = Number(value)
  return Number.isFinite(number) ? round2(number / 1000) : null
}

function plateSliceSeconds(plate, result) {
  const times = plate && plate.times
  if (times && times.sliced_time !== undefined) return secondsFromMs(times.sliced_time)
  if (plate && plate.sliced_time !== undefined) return secondsFromMs(plate.sliced_time)
  return null
}

function plateExportSeconds(plate, result) {
  const times = plate && plate.times
  if (times && times.export_gcode_time !== undefined) {
    return secondsFromMs(times.export_gcode_time)
  }
  if (result && result.export_time !== undefined) return secondsFromMs(result.export_time)
  return null
}

function plateStats(plates, result) {
  const list = []
  for (const plate of plates) {
    if (!plate) continue
    const filaments = Array.isArray(plate.filaments) ? plate.filaments : []
    const grams = filaments.reduce((sum, item) => sum + (Number(item.total_used_g) || 0), 0)
    list.push({
      id: Number(plate.id) || 0,
      mainMinutes: round1((Number(plate.main_predication) || 0) / 60),
      totalMinutes: round1((Number(plate.total_predication) || 0) / 60),
      filamentGrams: round2(grams),
      triangles: Math.trunc(Number(plate.triangle_count) || 0),
      objects: Array.isArray(plate.objects) ? plate.objects.length : 0,
      sliceSeconds: plateSliceSeconds(plate, result),
      exportSeconds: plateExportSeconds(plate, result),
    })
  }
  return list
}

function round1(value) {
  return Math.round(value * 10) / 10
}

function round2(value) {
  return Math.round(value * 100) / 100
}

/** One line: where the print time went, top six features plus the rest. */
function breakdownLine(featureTimes, totalSeconds) {
  const entries = Object.entries(featureTimes)
  if (!entries.length || !totalSeconds) return ''
  const share = (seconds) => {
    const percent = Math.round((seconds / totalSeconds) * 100)
    return percent === 0 && seconds > 0 ? '<1%' : `${percent}%`
  }
  const parts = entries
    .slice(0, 6)
    .map(([key, seconds]) => `${FEATURE_LABELS[key] || key} ${(seconds / 60).toFixed(1)} 分 ${share(seconds)}`)
  let text = `耗时分解（特征合计 ${(totalSeconds / 60).toFixed(1)} 分钟）：${parts.join(' · ')}`
  const rest = entries.slice(6)
  if (rest.length) {
    const restSeconds = rest.reduce((sum, [, seconds]) => sum + seconds, 0)
    text += ` · 其余 ${rest.length} 项 ${(restSeconds / 60).toFixed(1)} 分 ${share(restSeconds)}`
  }
  return text
}

export function summarizeSlice(file, outDir, exportName, exportMode, exitCode, started, logs) {
  const durationMs = Date.now() - started
  const resultPath = path.join(outDir, 'result.json')
  const { result, stale: resultStale } = parseSliceResult(resultPath, started)
  const stdout = Buffer.concat(logs.stdout).toString('utf8')
  const stderr = Buffer.concat(logs.stderr).toString('utf8')
  const logTail = tail([stdout, stderr].filter(Boolean).join('\n'))
  const files = readdirSync(outDir)
    .filter((name) => /\.(gcode|3mf|json)$/i.test(name))
    .map((name) => {
      const full = path.join(outDir, name)
      let bytes = 0
      try {
        bytes = statSync(full).size
      } catch {
        bytes = 0
      }
      return { name, path: full, bytes }
    })
  // An artifact of the same name from a previous run must not count as this
  // run's output: that turned "CLI exited 0 without exporting" into a silent
  // "success" that reported the old package.
  const exported = files.find((entry) => entry.name === exportName)
  const sliced3mf = exported && freshSince(exported.path, started) ? exported : null
  const staleExport = Boolean(exported && !sliced3mf)
  const gcodes = files
    .filter((entry) => entry.name.endsWith('.gcode'))
    .filter((entry) => freshSince(entry.path, started))
  const returnCode = result && typeof result.return_code === 'number' ? result.return_code : exitCode
  const rawError = (result && result.error_string) || ''
  const errorString = /^success\.?$/i.test(rawError.trim()) ? '' : rawError
  const failure =
    (returnCode !== 0 && returnCode !== null)
    || (exportMode !== 'gcode' && !sliced3mf)
    || (exportMode === 'gcode' && !gcodes.length)
  const plates = Array.isArray(result && result.sliced_plates) ? result.sliced_plates : []
  // total_predication is what Bambu Studio shows as the print time (verified
  // against slice_info.config); main_predication excludes a couple of phases.
  const estimateSeconds = plates.reduce(
    (sum, plate) =>
      sum + (Number(plate.total_predication) || Number(plate.main_predication) || 0),
    0,
  )
  const { featureTimes, featureTotalSeconds } = featureBreakdown(plates)
  const perPlate = plateStats(plates, result)
  const filamentGrams = plates.reduce((sum, plate) => {
    const filaments = Array.isArray(plate.filaments) ? plate.filaments : []
    return sum + filaments.reduce((inner, item) => inner + (Number(item.total_used_g) || 0), 0)
  }, 0)
  const gcodeBytes = gcodes.reduce((sum, entry) => sum + entry.bytes, 0)

  if (failure) {
    const reason =
      errorString
      || (staleExport
        ? '同名切片产物是上一次的，本次没有导出新文件'
        : '没有生成切片产物')
    const error = new ToolError(
      returnCode === null || returnCode === undefined ? 'CLI_FAILED' : 'SLICE_FAILED',
      `Bambu Studio 切片失败：${reason}`,
      {
        exitCode,
        returnCode,
        outputDir: outDir,
        logTail,
      },
    )
    throw error
  }

  const produced = sliced3mf
    ? path.basename(sliced3mf.path)
    : gcodes[0]
      ? path.basename(gcodes[0].path)
      : outDir
  const estimateText = estimateSeconds
    ? `预估打印 ${(estimateSeconds / 60).toFixed(1)} 分钟`
    : (resultStale ? '耗时未知（result.json 是旧的）' : '耗时未知')
  const lines = [
    `切片成功：${path.basename(file)} → ${produced}`,
    `盘：${plates.map((plate) => plate.id).join(', ') || (result ? '全部' : '?')} · ` +
      `用时 ${(durationMs / 1000).toFixed(1)}s · ${estimateText} · ` +
      `耗材 ${filamentGrams.toFixed(1)} g`,
  ]
  const breakdown = breakdownLine(featureTimes, featureTotalSeconds)
  if (breakdown) lines.push(breakdown)
  const travel = Number(featureTimes.Travel) || 0
  if (featureTotalSeconds && travel / featureTotalSeconds >= 0.08) {
    const share = Math.round((travel / featureTotalSeconds) * 100)
    lines.push(
      `提示：空驶占 ${share}%——通孔或碎件多会让每层反复起停，合并零件/减少通孔能明显缩短这段`,
    )
  }
  if (resultStale) lines.push('（result.json 来自上一次切片，本次没有新的耗时数据）')
  if (gcodes.length) lines.push(`G-code：${gcodes.map((entry) => `${entry.name} (${mb(entry.bytes)})`).join('，')}`)
  if (sliced3mf) lines.push(`切片包：${sliced3mf.path} (${mb(sliced3mf.bytes)})`)

  const diagnostics = []
  if (errorString) {
    diagnostics.push({ severity: 'warning', code: 'SLICE_WARNING', message: errorString })
  }
  if (resultStale) {
    diagnostics.push({
      severity: 'warning',
      code: 'STALE_RESULT',
      message: 'result.json 早于本次切片，已忽略其中的时间数据',
    })
  }
  return {
    file,
    outputFile: sliced3mf ? sliced3mf.path : gcodes[0] ? gcodes[0].path : null,
    summary: lines.join('\n'),
    changes: [],
    diagnostics,
    validation: { changedEntries: [], preservedEntries: 0 },
    sliceStats: {
      exitCode: exitCode === null ? -1 : exitCode,
      returnCode: returnCode === null ? -1 : returnCode,
      durationMs,
      outputDir: outDir,
      plates: plates.map((plate) => Number(plate.id) || 0),
      estimateMinutes: round1(estimateSeconds / 60),
      filamentGrams: round2(filamentGrams),
      gcodeBytes,
      error: errorString,
      resultStale,
      featureTotalSeconds,
      featureTimes,
      plateStats: perPlate,
    },
    outputFiles: files,
  }
}

function tail(text, limit = 2000) {
  const value = String(text || '').trim()
  return value.length <= limit ? value : `…${value.slice(-limit)}`
}

function mb(bytes) {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
