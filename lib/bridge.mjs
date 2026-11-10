/**
 * Bridge to the Python 3MF kernel.
 *
 * One JSON request on stdin, one JSON response on stdout. The Python process is
 * short-lived: spawning per call keeps cancellation and crash isolation simple,
 * and the kernel's real cost is XML/mesh work, not interpreter startup.
 */

import { spawn, spawnSync } from 'node:child_process'
import { existsSync, readdirSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PACKAGE_DIR = path.resolve(HERE, '..')
const PYTHON_DIR = path.join(PACKAGE_DIR, 'python')
const CLI_PATH = path.join(PYTHON_DIR, 'threemf', 'cli.py')

const MAX_STREAM_BYTES = 16 * 1024 * 1024

export class ToolError extends Error {
  constructor(code, message, details) {
    super(message)
    this.name = 'ToolError'
    this.code = code
    this.details = details
  }
}

let cachedPython = null

function runtimeCandidates() {
  const candidates = []
  const home = process.env.DSH_HOME || path.join(homedir(), '.dsh')
  const runtimes = path.join(home, 'dsh-runtimes')
  try {
    const dirs = readdirSync(runtimes, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => path.join(runtimes, entry.name, 'dependencies', 'python', 'python.exe'))
      .filter((file) => existsSync(file))
    dirs.sort((a, b) => {
      try {
        return statSync(b).mtimeMs - statSync(a).mtimeMs
      } catch {
        return 0
      }
    })
    candidates.push(...dirs)
  } catch {
    // No runtime tree; fall through to PATH candidates.
  }
  if (process.platform === 'win32') {
    candidates.push('python.exe', 'python')
  } else {
    candidates.push('python3', 'python')
  }
  return candidates
}

function probePython(command) {
  const script = 'import sys, lxml.etree, numpy, PIL.Image; sys.stdout.write("ok")'
  const result = spawnSync(command, ['-c', script], {
    encoding: 'utf8',
    timeout: 20000,
    windowsHide: true,
  })
  return result.status === 0 && (result.stdout || '').includes('ok')
}

export function resolvePython(config = {}) {
  if (cachedPython) return cachedPython
  const explicit = config.pythonPath
  const candidates = explicit ? [explicit, ...runtimeCandidates()] : runtimeCandidates()
  const failures = []
  for (const candidate of candidates) {
    if (candidate.includes(path.sep) && !existsSync(candidate)) {
      failures.push(`${candidate}: not found`)
      continue
    }
    if (probePython(candidate)) {
      cachedPython = candidate
      return cachedPython
    }
    failures.push(`${candidate}: cannot import lxml/numpy/PIL`)
  }
  throw new ToolError(
    'PYTHON_MISSING',
    '找不到可用的 Python 运行时（需要 lxml、numpy、Pillow）。请在插件配置 pythonPath 指向 python.exe。',
    { tried: failures },
  )
}

function readStream(stream, sink) {
  if (!stream) return
  stream.on('data', (chunk) => {
    if (sink.bytes >= MAX_STREAM_BYTES) return
    sink.bytes += chunk.length
    sink.chunks.push(chunk)
  })
}

export function callPython(command, args, options = {}) {
  const { config = {}, signal, timeoutMs = 300000 } = options
  const python = resolvePython(config)
  const payload = JSON.stringify({ cmd: command, args: args || {}, config: sanitizeConfig(config) })

  return new Promise((resolve, reject) => {
    let settled = false
    const stdout = { chunks: [], bytes: 0 }
    const stderr = { chunks: [], bytes: 0 }
    let child
    try {
      child = spawn(python, ['-u', CLI_PATH], {
        cwd: PYTHON_DIR,
        windowsHide: true,
        env: {
          ...process.env,
          PYTHONIOENCODING: 'utf-8',
          PYTHONUTF8: '1',
          PYTHONDONTWRITEBYTECODE: '1',
        },
      })
    } catch (error) {
      reject(new ToolError('PYTHON_FAILED', `无法启动 Python：${error.message}`))
      return
    }

    const finish = (fn, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', onAbort)
      fn(value)
    }
    const kill = () => {
      try {
        child.kill('SIGKILL')
      } catch {
        // Already gone.
      }
    }
    const onAbort = () => {
      kill()
      finish(reject, new ToolError('ABORTED', '操作已取消'))
    }
    const timer = setTimeout(() => {
      kill()
      finish(
        reject,
        new ToolError('TIMEOUT', `Python 处理超时（${Math.round(timeoutMs / 1000)}s）`),
      )
    }, timeoutMs)

    if (signal) {
      if (signal.aborted) {
        onAbort()
        return
      }
      signal.addEventListener('abort', onAbort, { once: true })
    }

    readStream(child.stdout, stdout)
    readStream(child.stderr, stderr)

    child.on('error', (error) => {
      finish(reject, new ToolError('PYTHON_FAILED', `Python 启动失败：${error.message}`))
    })

    child.on('close', (code) => {
      if (settled) return
      const out = Buffer.concat(stdout.chunks).toString('utf8').trim()
      const err = Buffer.concat(stderr.chunks).toString('utf8').trim()
      if (!out) {
        finish(
          reject,
          new ToolError(
            'PYTHON_FAILED',
            `Python 没有返回结果（exit ${code}）${err ? `：${tail(err)}` : ''}`,
          ),
        )
        return
      }
      let payloadValue
      try {
        payloadValue = JSON.parse(out)
      } catch {
        finish(
          reject,
          new ToolError('PYTHON_FAILED', `Python 返回了非 JSON 内容：${tail(out)}`),
        )
        return
      }
      if (payloadValue.ok) {
        finish(resolve, payloadValue.value)
        return
      }
      const error = payloadValue.error || {}
      finish(
        reject,
        new ToolError(
          error.code || 'INTERNAL',
          error.message || '未知错误',
          error.details,
        ),
      )
    })

    child.stdin.on('error', () => {})
    child.stdin.end(payload, 'utf8')
  })
}

function sanitizeConfig(config) {
  const allowed = ['renderWidth', 'maxTriangles', 'sliceTimeoutMs']
  const clean = {}
  for (const key of allowed) {
    if (config[key] !== undefined && config[key] !== null) clean[key] = config[key]
  }
  return clean
}

function tail(text, limit = 1200) {
  const value = String(text)
  return value.length <= limit ? value : `…${value.slice(-limit)}`
}
