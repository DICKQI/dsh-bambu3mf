/**
 * Tool definitions for the Bambu 3MF editor.
 *
 * Definitions are plain objects: this bundle deliberately imports nothing from
 * the harness packages so a linked checkout resolves without a node_modules
 * tree. The registry enforces `output.schema`; argument validation lives in the
 * Python kernel, which returns a stable error code for every rejected call.
 */

import { readFile } from 'node:fs/promises'
import path from 'node:path'

import { ToolError, callPython } from './bridge.mjs'
import { sliceProject } from './slice.mjs'

const objectRef = { oneOf: [{ type: 'string' }, { type: 'integer' }] }

const primitiveSpec = {
  type: 'object',
  additionalProperties: false,
  properties: {
    shape: { type: 'string', enum: ['box', 'cylinder', 'sphere'] },
    size: { type: 'array', items: { type: 'number' }, minItems: 3, maxItems: 3 },
    radius: { type: 'number' },
    height: { type: 'number' },
    axis: { type: 'string', enum: ['x', 'y', 'z'] },
    center: { type: 'array', items: { type: 'number' }, minItems: 3, maxItems: 3 },
    segments: { type: 'integer' },
  },
  required: ['shape'],
}

const operandSpec = {
  oneOf: [
    {
      type: 'object',
      additionalProperties: false,
      properties: { object: objectRef },
      required: ['object'],
    },
    {
      type: 'object',
      additionalProperties: false,
      properties: {
        stl: { type: 'string' },
        translate: { type: 'array', items: { type: 'number' }, minItems: 3, maxItems: 3 },
      },
      required: ['stl'],
    },
    {
      type: 'object',
      additionalProperties: false,
      properties: { primitive: primitiveSpec },
      required: ['primitive'],
    },
  ],
}

const diagnosticSchema = {
  type: 'object',
  properties: {
    severity: { type: 'string' },
    code: { type: 'string' },
    message: { type: 'string' },
    objectId: { type: 'integer' },
  },
  required: ['severity', 'code', 'message'],
  additionalProperties: true,
}

const changeSchema = {
  type: 'object',
  properties: {
    kind: { type: 'string' },
    objectId: { type: 'integer' },
    detail: { type: 'string' },
  },
  required: ['kind', 'detail'],
  additionalProperties: false,
}

const validationSchema = {
  type: 'object',
  properties: {
    changedEntries: { type: 'array', items: { type: 'string' } },
    preservedEntries: { type: 'integer' },
    dryRun: { type: 'boolean' },
  },
  required: ['changedEntries', 'preservedEntries'],
  additionalProperties: false,
}

const bboxSchema = { type: 'array', items: { type: 'number' } }

function outputSchema(properties, required) {
  return {
    type: 'object',
    properties: {
      file: { type: 'string' },
      outputFile: { oneOf: [{ type: 'string' }, { type: 'null' }] },
      summary: { type: 'string' },
      changes: { type: 'array', items: changeSchema },
      diagnostics: { type: 'array', items: diagnosticSchema },
      validation: validationSchema,
      // _finish() (kernel) attaches this to ANY in_place save, not just edit;
      // declaring it once here keeps edit/mesh/settings from drifting apart.
      backup: {
        type: 'string',
        description: 'in_place 写盘时生成的 <名>.bak-<时间戳>.3mf 备份路径',
      },
      ...properties,
    },
    required: ['file', 'summary', 'changes', 'diagnostics', 'validation', ...required],
    additionalProperties: false,
  }
}

// ------------------------------------------------------------------ rendering
function textBlocks(value) {
  const sections = [value.summary || '']
  if (Array.isArray(value.changes) && value.changes.length) {
    const lines = value.changes.slice(0, 14).map((item) => `• ${item.detail}`)
    if (value.changes.length > 14) lines.push(`… 其余 ${value.changes.length - 14} 项`)
    sections.push(lines.join('\n'))
  }
  const warnings = (value.diagnostics || []).filter(
    (item) => item.severity === 'warning' || item.severity === 'error',
  )
  if (warnings.length) {
    const lines = warnings
      .slice(0, 8)
      .map((item) => `[${item.severity}] ${item.code}: ${item.message}`)
    if (warnings.length > 8) lines.push(`… 其余 ${warnings.length - 8} 条诊断`)
    sections.push(lines.join('\n'))
  }
  const blocks = [{ type: 'text', text: sections.filter(Boolean).join('\n\n') }]
  if (Array.isArray(value.images)) {
    for (const image of value.images) {
      blocks.push({ type: 'image', attachment: image })
    }
  }
  return blocks
}

function genericCard(title) {
  return { card: 'generic', title }
}

// --------------------------------------------------------------------- tools
export function createTools(runtime) {
  const { config = {}, attachments } = runtime

  const call = (command, args, exec, timeoutMs) =>
    callPython(command, args, { config, signal: exec.signal, timeoutMs })

  const inspect = {
    name: 'bambu3mf_inspect',
    description:
      'Inspect a .3mf package: plates, objects with world bounding boxes and placement, filaments, printer/process settings and structural diagnostics. Read-only; call it before editing. A sliced .gcode.3mf also reports the slicer verdict (predicted time, filament weight) from Metadata/slice_info.config.',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: 'Absolute path to the .3mf file.' },
        detail: {
          type: 'string',
          enum: ['summary', 'full'],
          description:
            'full adds per-object matrices, settings, parts, connected shells, the entry list '
            + 'and shared-mesh references to the text output (and to the structured fields).',
        },
        include_settings: {
          type: 'boolean',
          description: 'Include the whole project_settings map in full mode.',
        },
        settings_keys: {
          type: 'array',
          items: { type: 'string' },
          description:
            'full mode: which project settings to print. Exact keys, prefixes ("wipe_tower") '
            + 'or wildcards ("*speed*"). Omit to get a short list of commonly edited keys.',
        },
        settings_limit: {
          type: 'integer',
          description: 'full mode: cap on printed setting lines (default 80).',
        },
      },
      required: ['file'],
      additionalProperties: false,
    },
    output: {
      schema: outputSchema(
        {
          format: { type: 'string' },
          title: { type: 'string' },
          application: { type: 'string' },
          unit: { type: 'string' },
          sizeBytes: { type: 'integer' },
          printer: {
            type: 'object',
            properties: {
              model: { type: 'string' },
              settingsId: { type: 'string' },
              bed: { type: 'array', items: { type: 'number' } },
            },
            required: ['model', 'settingsId', 'bed'],
            additionalProperties: false,
          },
          process: {
            type: 'object',
            properties: {
              layerHeight: { type: 'string' },
              walls: { type: 'string' },
              infill: { type: 'string' },
              support: { type: 'string' },
            },
            required: ['layerHeight', 'walls', 'infill', 'support'],
            additionalProperties: false,
          },
          filaments: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                index: { type: 'integer' },
                type: { type: 'string' },
                color: { type: 'string' },
              },
              required: ['index', 'type', 'color'],
              additionalProperties: false,
            },
          },
          plates: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                id: { type: 'integer' },
                name: { type: 'string' },
                objectIds: { type: 'array', items: { type: 'integer' } },
              },
              required: ['id', 'name', 'objectIds'],
              additionalProperties: false,
            },
          },
          objects: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                id: { type: 'integer' },
                name: { type: 'string' },
                plate: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
                extruder: { type: 'integer' },
                triangles: { type: 'integer' },
                vertices: { type: 'integer' },
                bbox: bboxSchema,
                position: { type: 'array', items: { type: 'number' } },
                sharedMesh: { type: 'boolean' },
                rotationZ: { type: 'number' },
                matrix: { type: 'array', items: { type: 'number' } },
                settings: { type: 'object', additionalProperties: true },
                parts: { type: 'array', items: { type: 'object', additionalProperties: true } },
                components: {
                  type: 'array',
                  items: { type: 'object', additionalProperties: true },
                  description:
                    'Connected shells inside the object (full mode, multi-shell objects only): index, triangles, bbox, size, center, volume. Feed these indexes to bambu3mf_mesh select.component.',
                },
                sources: {
                  type: 'array',
                  items: {
                    type: 'object',
                    properties: {
                      path: { type: 'string' },
                      objectId: { type: 'integer' },
                      triangles: { type: 'integer' },
                    },
                    required: ['path', 'objectId', 'triangles'],
                    additionalProperties: false,
                  },
                },
              },
              required: ['id', 'name', 'plate', 'triangles', 'bbox', 'position', 'sources'],
              additionalProperties: false,
            },
          },
          stats: {
            type: 'object',
            properties: {
              objects: { type: 'integer' },
              triangles: { type: 'integer' },
              plates: { type: 'integer' },
              entries: { type: 'integer' },
            },
            required: ['objects', 'triangles', 'plates', 'entries'],
            additionalProperties: false,
          },
          entries: { type: 'array', items: { type: 'string' } },
          projectSettings: { type: 'object', additionalProperties: true },
          sliceInfo: {
            type: 'object',
            description:
              'Slicer verdict from Metadata/slice_info.config (only inside an exported .gcode.3mf): '
              + 'predicted seconds, filament weight and per-plate rows.',
            properties: {
              clientVersion: { type: 'string' },
              plates: {
                type: 'array',
                items: {
                  type: 'object',
                  properties: {
                    index: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
                    predictionSeconds: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
                    weightGrams: { oneOf: [{ type: 'number' }, { type: 'null' }] },
                    firstLayerSeconds: { oneOf: [{ type: 'number' }, { type: 'null' }] },
                    supportUsed: { oneOf: [{ type: 'boolean' }, { type: 'null' }] },
                    pauseCount: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
                    objects: { type: 'array', items: { type: 'object', additionalProperties: true } },
                    filaments: { type: 'array', items: { type: 'object', additionalProperties: true } },
                    warnings: { type: 'array', items: { type: 'object', additionalProperties: true } },
                  },
                  required: ['index', 'predictionSeconds', 'weightGrams'],
                  additionalProperties: false,
                },
              },
            },
            required: ['clientVersion', 'plates'],
            additionalProperties: false,
          },
        },
        ['format', 'objects', 'plates', 'stats'],
      ),
      render: (_args, value) => textBlocks(value),
    },
    presentCall: (args) => genericCard(`检查 3MF · ${path.basename(String(args.file || ''))}`),
    presentResult: () => ({ card: 'generic', title: '3MF 结构' }),
    isConcurrencySafe: () => true,
    timeoutMs: 180000,
    async execute(args, exec) {
      return call('inspect', args, exec)
    },
  }

  const objectOperations = {
    type: 'array',
    items: {
      oneOf: [
        {
          type: 'object',
          properties: {
            op: { type: 'string', const: 'move' },
            object: objectRef,
            dx: { type: 'number' },
            dy: { type: 'number' },
            dz: { type: 'number' },
          },
          required: ['op', 'object'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: {
            op: { type: 'string', const: 'set_position' },
            object: objectRef,
            x: { type: 'number' },
            y: { type: 'number' },
            z: { type: 'number' },
            z_mode: {
              type: 'string',
              enum: ['origin', 'bed'],
              description:
                'How to read z. origin (default) sets the build-matrix offset, which is '
                + 'roughly the object centre for a mesh centred on its bounding box. '
                + 'bed means "I want the object\'s bottom at this z" and is what you '
                + 'usually want when placing on the plate.',
            },
          },
          required: ['op', 'object'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: {
            op: { type: 'string', const: 'rotate_z' },
            object: objectRef,
            angle: { type: 'number' },
            pivot: { type: 'string', enum: ['center', 'bed', 'origin'] },
          },
          required: ['op', 'object', 'angle'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: {
            op: { type: 'string', const: 'duplicate' },
            object: objectRef,
            dx: { type: 'number' },
            dy: { type: 'number' },
            dz: { type: 'number' },
            name: { type: 'string' },
          },
          required: ['op', 'object'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: { op: { type: 'string', const: 'delete' }, object: objectRef },
          required: ['op', 'object'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: {
            op: { type: 'string', const: 'rename' },
            object: objectRef,
            name: { type: 'string' },
          },
          required: ['op', 'object', 'name'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: {
            op: { type: 'string', const: 'set_extruder' },
            object: objectRef,
            extruder: { type: 'integer' },
          },
          required: ['op', 'object', 'extruder'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: {
            op: { type: 'string', const: 'set_plate' },
            object: objectRef,
            plate: { type: 'integer' },
          },
          required: ['op', 'object', 'plate'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: { op: { type: 'string', const: 'drop_to_bed' }, object: objectRef },
          required: ['op', 'object'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: {
            op: { type: 'string', const: 'set_printable' },
            object: objectRef,
            printable: { type: 'boolean' },
          },
          required: ['op', 'object', 'printable'],
          additionalProperties: false,
        },
        {
          type: 'object',
          properties: {
            op: { type: 'string', const: 'set_object_setting' },
            object: objectRef,
            key: { type: 'string' },
            value: { oneOf: [{ type: 'string' }, { type: 'number' }, { type: 'boolean' }] },
          },
          required: ['op', 'object', 'key', 'value'],
          additionalProperties: false,
        },
      ],
    },
  }

  const edit = {
    name: 'bambu3mf_edit',
    description:
      'Apply placement and object operations to a .3mf project as one atomic edit and write a new package. The input file is left untouched unless in_place is true; every untouched archive entry is copied byte for byte.',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: 'Absolute path to the project .3mf.' },
        ops: objectOperations,
        output: { type: 'string', description: 'Output path; defaults to <name>_edited.3mf beside the input.' },
        in_place: { type: 'boolean', description: 'Overwrite the input after making a timestamped backup.' },
        dry_run: { type: 'boolean', description: 'Validate the edit without writing a package.' },
      },
      required: ['file', 'ops'],
      additionalProperties: false,
    },
    output: {
      schema: outputSchema(
        {
          opsApplied: { type: 'integer' },
          objects: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                id: { type: 'integer' },
                name: { type: 'string' },
                plate: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
                position: { type: 'array', items: { type: 'number' } },
                bbox: bboxSchema,
                size: { type: 'array', items: { type: 'number' } },
              },
              required: ['id', 'name', 'position', 'bbox', 'size'],
              additionalProperties: false,
            },
          },
        },
        ['opsApplied'],
      ),
      render: (_args, value) => textBlocks(value),
    },
    presentCall: (args) =>
      genericCard(`编辑 3MF · ${path.basename(String(args.file || ''))} · ${(args.ops || []).length} 个操作`),
    presentResult: () => ({ card: 'generic', title: '编辑结果' }),
    timeoutMs: 240000,
    async execute(args, exec) {
      return call('edit', args, exec)
    },
  }

  const planeSchema = {
    oneOf: [
      {
        type: 'object',
        properties: {
          axis: { type: 'string', enum: ['x', 'y', 'z'] },
          value: { type: 'number' },
        },
        required: ['axis', 'value'],
        additionalProperties: false,
      },
      {
        type: 'object',
        properties: {
          point: { type: 'array', items: { type: 'number' } },
          normal: { type: 'array', items: { type: 'number' } },
        },
        required: ['point', 'normal'],
        additionalProperties: false,
      },
    ],
  }

  const mesh = {
    name: 'bambu3mf_mesh',
    description:
      'Reshape one object mesh: bake placement, scale/rotate/mirror/translate about a pivot, stretch one axis band, weld and clean, cut with a plane and cap the opening, or import/export STL. Geometry is edited in world space and stays where it was. op=components lists the connected shells inside one object; pass select to move/scale only that shell (or a box) and leave the rest of the mesh untouched. op=stretch lengthens a band without touching what lies outside it (the way a part is made longer while both ends stay identical) and never changes the triangle count. op=diagnose is read-only and reports winding consistency, reliable volume, degenerate faces, thin sheets and coplanar internal faces; op=section returns 2D section loops; op=measure returns a height map, material spans or a region extent. op=analyze is read-only and reports print-time drivers: volume, surface area, watertightness, through-hole count (genus), overhang area excluding build-plate contact, and the layer count.',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: 'Absolute path to the project .3mf.' },
        object: { ...objectRef, description: 'Object id or unique name; omit only for import_stl.' },
        op: {
          type: 'string',
          enum: [
            'bake',
            'scale',
            'rotate',
            'mirror',
            'translate',
            'stretch',
            'weld',
            'cleanup',
            'plane_cut',
            'flip',
            'import_stl',
            'export_stl',
            'components',
            'analyze',
            'diagnose',
            'section',
            'measure',
          ],
          description:
            'bake freezes placement into the vertices; plane_cut keeps one side of the plane; stretch grows one axis band and shifts the rest rigidly; components/diagnose/section/measure are read-only. diagnose checks winding, volume reliability, degenerate faces, thin sheets and coplanar internal faces; section returns 2D loops; measure returns a height map, material spans or a region extent.',
        },
        scale: {
          oneOf: [{ type: 'number' }, { type: 'array', items: { type: 'number' } }],
          description: 'scale factor(s), > 0; a single number means uniform.',
        },
        axis: { type: 'string', enum: ['x', 'y', 'z'], description: 'rotate/mirror axis.' },
        angle: { type: 'number', description: 'rotate angle in degrees.' },
        pivot: {
          type: 'string',
          enum: ['center', 'bed', 'origin', 'selection'],
          description:
            'Pivot for scale/rotate; center is the world bbox center, bed uses the bbox bottom, selection uses the selected shell centre (the default whenever select is given).',
        },
        select: {
          type: 'object',
          additionalProperties: false,
          properties: {
            component: {
              oneOf: [
                { type: 'integer' },
                { type: 'array', items: { type: 'integer' } },
              ],
              description:
                'Shell index inside the object (0 = largest), as reported by op=components or inspect detail=full.',
            },
            bbox: {
              type: 'object',
              additionalProperties: false,
              properties: {
                min: { type: 'array', items: { type: 'number' }, minItems: 3, maxItems: 3 },
                max: { type: 'array', items: { type: 'number' }, minItems: 3, maxItems: 3 },
              },
              description: 'World-space box; triangles whose centre falls inside it are selected.',
            },
          },
          description:
            'Restrict scale/rotate/mirror/translate to part of the mesh: one or more connected shells, or a world box. Everything outside the selection keeps its exact geometry.',
        },
        pivotPoint: {
          type: 'object',
          additionalProperties: false,
          properties: { x: { type: 'number' }, y: { type: 'number' }, z: { type: 'number' } },
          description: 'Explicit pivot in world millimetres.',
        },
        dx: { type: 'number' },
        dy: { type: 'number' },
        dz: { type: 'number' },
        plane: planeSchema,
        keep: {
          type: 'string',
          enum: ['negative', 'positive'],
          description: 'plane_cut keeps the side below/above along the normal; default negative (cut the top off).',
        },
        cap: { type: 'boolean', description: 'Close the cut opening; default true.' },
        band: {
          type: 'array',
          items: { type: 'number' },
          description:
            'stretch: [low, high] along axis, in world mm. Inside the band the mesh is scaled; '
            + 'outside it is shifted rigidly, so both ends keep their exact shape.',
        },
        delta: {
          type: 'number',
          description:
            'stretch: how much longer (mm) the band becomes; negative compresses. '
            + 'The new band length is (high - low) + delta.',
        },
        negative_share: {
          type: 'number',
          description:
            'stretch: fraction of the growth moved to the low side, 0..1. '
            + 'Default 0.5 (grows symmetrically); 0 grows only toward +axis.',
        },
        stl: { type: 'string', description: 'STL path for import_stl (input) or export_stl (output).' },
        overhang_angle: {
          type: 'number',
          description: 'analyze: overhang threshold in degrees; default 45, must be inside 0..90.',
        },
        mode: {
          type: 'string',
          enum: ['heightmap', 'spans', 'extent'],
          description: 'measure: heightmap (max surface along axis), spans (material intervals), extent (region bbox).',
        },
        bbox: {
          type: 'array',
          items: { type: 'number' },
          minItems: 6,
          maxItems: 6,
          description:
            'measure: world region [xmin,ymin,zmin,xmax,ymax,zmax]. Section uses its own plane; this bbox is for measure only.',
        },
        grid: {
          type: 'integer',
          description: 'measure mode=heightmap: cells per side, default 64, capped at 256.',
        },
        format: {
          type: 'string',
          enum: ['json', 'svg'],
          description: 'section: json (default) or write an SVG outline.',
        },
        out: {
          type: 'string',
          description: 'section SVG path, or measure heightmap PNG path.',
        },
        max_points: {
          type: 'integer',
          description: 'section: cap on points per loop (default 2000, decimated when exceeded).',
        },
        thin_threshold: {
          type: 'number',
          description: 'diagnose: a shell thinner than this (mm) counts as a zero-thickness sheet; default 0.01.',
        },
        plate: { type: 'integer', description: 'Target plate for import_stl; default 1.' },
        name: { type: 'string', description: 'Object name for import_stl.' },
        merge_components: {
          type: 'boolean',
          description: 'Allow geometry edits on assembly objects with several parts (they are merged).',
        },
        allow_overlap: {
          type: 'boolean',
          description:
            'import_stl only: suppress OBJECT_OVERLAP warnings involving the imported object '
            + '(replacing geometry intentionally overlaps the old one).',
        },
        output: { type: 'string' },
        in_place: { type: 'boolean' },
        dry_run: { type: 'boolean' },
      },
      required: ['file', 'op'],
      additionalProperties: false,
    },
    output: {
      schema: outputSchema(
        {
          objectId: { type: 'integer' },
          op: { type: 'string' },
          trianglesBefore: { type: 'integer' },
          trianglesAfter: { type: 'integer' },
          verticesBefore: { type: 'integer' },
          verticesAfter: { type: 'integer' },
          qualityBefore: { type: 'object', additionalProperties: true },
          qualityAfter: { type: 'object', additionalProperties: true },
          operationStats: { type: 'object', additionalProperties: true },
          detachedMesh: {
            type: 'boolean',
            description:
              'True when the edited mesh was shared with other objects and had to be '
              + 'copied, so those siblings kept their original geometry.',
          },
          diagnosis: {
            type: 'object',
            description:
              'op=diagnose: topology, per-shell volume/winding, degenerate faces, thin sheets and coplanar internal faces.',
            additionalProperties: true,
          },
          section: {
            type: 'object',
            description:
              'op=section: 2D loops in the plane basis, net area, perimeter, basis and optional svgPath.',
            additionalProperties: true,
          },
          measurement: {
            type: 'object',
            description:
              'op=measure: heightmap values/min/max/mean, material spans or a region extent.',
            additionalProperties: true,
          },
          printability: {
            type: 'object',
            description:
              'op=analyze: topology, area and overhang facts; the numbers that drive print time '
              + 'without running the slicer.',
            properties: {
              triangles: { type: 'integer' },
              vertices: { type: 'integer' },
              watertight: { type: 'boolean' },
              boundaryEdges: { type: 'integer' },
              nonManifoldEdges: { type: 'integer' },
              shells: { type: 'integer' },
              genus: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
              orientationConsistent: { type: 'boolean' },
              volumeReliable: { type: 'boolean' },
              negativeShells: { type: 'integer' },
              volumeMm3: { oneOf: [{ type: 'number' }, { type: 'null' }] },
              netVolumeMm3: { oneOf: [{ type: 'number' }, { type: 'null' }] },
              degenerateTriangles: { type: 'integer' },
              duplicateFaces: { type: 'integer' },
              opposingFaces: { type: 'integer' },
              thinSheets: { type: 'integer' },
              surfaceAreaMm2: { type: 'number' },
              size: { type: 'array', items: { type: 'number' } },
              heightMm: { type: 'number' },
              layerHeight: { oneOf: [{ type: 'number' }, { type: 'null' }] },
              estimatedLayers: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
              overhang: {
                type: 'object',
                properties: {
                  angleDegrees: { type: 'number' },
                  areaMm2: { type: 'number' },
                  percent: { type: 'number' },
                  bedContactMm2: { type: 'number' },
                  unsupportedMm2: { type: 'number' },
                  unsupportedPercent: { type: 'number' },
                },
                required: [
                  'angleDegrees',
                  'areaMm2',
                  'percent',
                  'bedContactMm2',
                  'unsupportedMm2',
                  'unsupportedPercent',
                ],
                additionalProperties: false,
              },
            },
            required: [
              'triangles',
              'vertices',
              'watertight',
              'boundaryEdges',
              'nonManifoldEdges',
              'shells',
              'genus',
              'orientationConsistent',
              'volumeReliable',
              'negativeShells',
              'volumeMm3',
              'netVolumeMm3',
              'degenerateTriangles',
              'duplicateFaces',
              'opposingFaces',
              'thinSheets',
              'surfaceAreaMm2',
              'size',
              'heightMm',
              'layerHeight',
              'estimatedLayers',
              'overhang',
            ],
            additionalProperties: false,
          },
        },
        ['op'],
      ),
      render: (_args, value) => textBlocks(value),
    },
    presentCall: (args) =>
      genericCard(`网格 ${args.op || ''} · ${path.basename(String(args.file || ''))}`),
    presentResult: () => ({ card: 'generic', title: '网格结果' }),
    async execute(args, exec) {
      return call('mesh', args, exec, 600000)
    },
  }

  const build = {
    name: 'bambu3mf_build',
    description:
      'Constructive geometry: subtract/union/intersect a target object with another object, an STL or a box/cylinder/sphere primitive; split an object into two independent objects with one plane; replace an object mesh from an STL/object/primitive. All coordinates are world millimetres and the target keeps its id, name, plate and extruder. Boolean operations need manifold3d; primitives, split and replace do not. Use mesh op=diagnose afterwards to confirm the result is a single watertight shell.',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: 'Absolute path to the project .3mf.' },
        ops: {
          type: 'array',
          items: {
            oneOf: [
              {
                type: 'object',
                additionalProperties: false,
                properties: {
                  op: { type: 'string', const: 'primitive' },
                  primitive: primitiveSpec,
                  name: { type: 'string' },
                  plate: { type: 'integer' },
                  extruder: { type: 'integer' },
                },
                required: ['op', 'primitive'],
              },
              {
                type: 'object',
                additionalProperties: false,
                properties: {
                  op: { type: 'string', const: 'boolean' },
                  object: objectRef,
                  operation: { type: 'string', enum: ['union', 'subtract', 'intersect'] },
                  with: operandSpec,
                  keep_operand: {
                    type: 'boolean',
                    description: 'Also create the operand as an object (useful for visual checks).',
                  },
                  repair: {
                    type: 'string',
                    enum: ['none', 'weld'],
                    description: 'weld (default) welds vertices and removes degenerate/duplicate faces before the boolean.',
                  },
                  merge_parts: {
                    type: 'boolean',
                    description: 'Required when the target has several parts; they are merged into one part.',
                  },
                },
                required: ['op', 'object', 'operation', 'with'],
              },
              {
                type: 'object',
                additionalProperties: false,
                properties: {
                  op: { type: 'string', const: 'split' },
                  object: objectRef,
                  plane: planeSchema,
                  names: {
                    type: 'array',
                    items: { type: 'string' },
                    minItems: 2,
                    maxItems: 2,
                    description: '[negative-side name, positive-side name]; defaults to <name>_A/_B.',
                  },
                  gap: {
                    type: 'number',
                    description: 'Separate the two halves by this distance (mm) along the plane normal; default 0.',
                  },
                  keep_original: {
                    type: 'boolean',
                    description: 'Keep the source object as well as the two halves; default false.',
                  },
                  merge_parts: { type: 'boolean' },
                },
                required: ['op', 'object', 'plane'],
              },
              {
                type: 'object',
                additionalProperties: false,
                properties: {
                  op: { type: 'string', const: 'replace' },
                  object: objectRef,
                  source: operandSpec,
                  merge_parts: { type: 'boolean' },
                },
                required: ['op', 'object', 'source'],
              },
            ],
          },
        },
        allow_overlap: {
          type: 'boolean',
          description: 'Suppress OBJECT_OVERLAP warnings involving the objects this call changed.',
        },
        output: { type: 'string' },
        in_place: { type: 'boolean' },
        dry_run: { type: 'boolean' },
      },
      required: ['file', 'ops'],
      additionalProperties: false,
    },
    output: {
      schema: outputSchema(
        {
          opsApplied: { type: 'integer' },
          objects: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                id: { type: 'integer' },
                name: { type: 'string' },
                plate: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
                position: { type: 'array', items: { type: 'number' } },
                bbox: bboxSchema,
                size: { type: 'array', items: { type: 'number' } },
              },
              required: ['id', 'name', 'position', 'bbox', 'size'],
              additionalProperties: false,
            },
          },
          operationStats: {
            type: 'object',
            description:
              'One entry per op: volumes, intersection volume, shell count, created/negative/positive ids, warnings.',
            additionalProperties: true,
          },
          detachedMesh: { type: 'boolean' },
          transformBaked: {
            type: 'boolean',
            description: 'True when the target transform was baked into the vertices (world position preserved).',
          },
          partsCollapsed: {
            type: 'integer',
            description: 'How many parts were merged into the single resulting normal_part.',
          },
        },
        ['opsApplied'],
      ),
      render: (_args, value) => textBlocks(value),
    },
    presentCall: (args) =>
      genericCard(`构建 3MF · ${path.basename(String(args.file || ''))} · ${(args.ops || []).length} 个操作`),
    presentResult: () => ({ card: 'generic', title: '构建结果' }),
    async execute(args, exec) {
      return call('build', args, exec, 600000)
    },
  }

  const render = {
    name: 'bambu3mf_render',
    description:
      'Render PNG previews of a .3mf project from isometric, top, front, right or plate-layout views. Use it to verify an edit visually before slicing.',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: 'Absolute path to the .3mf file.' },
        views: {
          type: 'array',
          items: { type: 'string', enum: ['iso', 'top', 'front', 'right', 'plate'] },
          description: 'Default ["iso","top"].',
        },
        width: { type: 'integer', description: 'Image width in pixels; default 900.' },
        objects: {
          type: 'array',
          items: objectRef,
          description: 'Render only these objects.',
        },
        out_dir: { type: 'string', description: 'Where the PNGs go; defaults to <file dir>/.bambu3mf-preview.' },
        max_triangles: {
          type: 'integer',
          description: 'Sampling ceiling per view; default 250000.',
        },
        framing: {
          type: 'string',
          enum: ['scene', 'objects', 'bbox'],
          description:
            'scene (default) fits objects plus the bed; objects fits only the drawn '
            + 'objects so a small part fills the frame; bbox uses the given world box.',
        },
        bbox: {
          type: 'array',
          items: { type: 'number' },
          minItems: 6,
          maxItems: 6,
          description: 'World box [xmin,ymin,zmin,xmax,ymax,zmax] for framing=bbox or cropping.',
        },
        crop: {
          type: 'boolean',
          description: 'Hide triangles outside bbox; defaults to true for framing=bbox.',
        },
        zoom: {
          type: 'number',
          description: 'Scale multiplier after framing; default 1, range 0.05..20.',
        },
        camera: {
          type: 'object',
          additionalProperties: false,
          properties: {
            azimuth: { type: 'number', description: 'Degrees around Z.' },
            elevation: { type: 'number', description: 'Degrees above the XY plane, -89..89.' },
          },
          required: ['azimuth', 'elevation'],
        },
        height: { type: 'integer', description: 'Image height in pixels; defaults to width*0.78.' },
      },
      required: ['file'],
      additionalProperties: false,
    },
    output: {
      schema: outputSchema(
        {
          views: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                path: { type: 'string' },
                width: { type: 'integer' },
                height: { type: 'integer' },
              },
              required: ['name', 'path', 'width', 'height'],
              additionalProperties: false,
            },
          },
          images: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                attachmentId: { type: 'string' },
                mediaType: { type: 'string' },
                bytes: { type: 'integer' },
                width: { type: 'integer' },
                height: { type: 'integer' },
                name: { type: 'string' },
              },
              required: ['attachmentId', 'mediaType', 'bytes', 'width', 'height'],
              additionalProperties: false,
            },
          },
          renderStats: { type: 'object', additionalProperties: true },
        },
        ['views', 'images'],
      ),
      render: (_args, value) => textBlocks(value),
    },
    presentCall: (args) => genericCard(`渲染预览 · ${path.basename(String(args.file || ''))}`),
    presentResult: () => ({ card: 'generic', title: '3MF 预览' }),
    timeoutMs: 300000,
    async execute(args, exec) {
      const value = await call('render', { ...args, out_dir: args.out_dir }, exec, 300000)
      const images = []
      if (attachments && Array.isArray(value.views)) {
        for (const view of value.views) {
          try {
            const buffer = await readFile(view.path)
            const ref = await attachments.saveImage({
              data: new Uint8Array(buffer),
              mediaType: 'image/png',
              name: `${path.basename(view.path, '.png')}.png`,
            })
            images.push(ref)
          } catch (error) {
            value.diagnostics.push({
              severity: 'warning',
              code: 'IMAGE_ATTACH_FAILED',
              message: `预览图无法附加到对话：${error.message}`,
            })
          }
        }
      }
      value.images = images
      if (!images.length) {
        value.summary += `\n预览图路径：${(value.views || []).map((view) => view.path).join('，')}`
      }
      return value
    },
  }

  const settings = {
    name: 'bambu3mf_settings',
    description:
      'Read or change print and object settings in a Bambu project: project_settings keys (layer height, walls, infill, support, filament colours) and model_settings object metadata.',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: 'Absolute path to the project .3mf.' },
        ops: {
          type: 'array',
          items: {
            oneOf: [
              {
                type: 'object',
                properties: {
                  op: { type: 'string', const: 'set' },
                  key: { type: 'string' },
                  value: {
                    oneOf: [
                      { type: 'string' },
                      { type: 'number' },
                      { type: 'boolean' },
                      { type: 'array', items: { type: 'string' } },
                    ],
                  },
                  description: { type: 'string' },
                },
                required: ['op', 'key', 'value'],
                additionalProperties: false,
              },
              {
                type: 'object',
                properties: { op: { type: 'string', const: 'unset' }, key: { type: 'string' } },
                required: ['op', 'key'],
                additionalProperties: false,
              },
              {
                type: 'object',
                properties: {
                  op: { type: 'string', const: 'set_filament_color' },
                  index: { type: 'integer' },
                  color: { type: 'string' },
                },
                required: ['op', 'index', 'color'],
                additionalProperties: false,
              },
              {
                type: 'object',
                properties: {
                  op: { type: 'string', const: 'set_object_setting' },
                  object: objectRef,
                  key: { type: 'string' },
                  value: { oneOf: [{ type: 'string' }, { type: 'number' }, { type: 'boolean' }] },
                },
                required: ['op', 'object', 'key', 'value'],
                additionalProperties: false,
              },
            ],
          },
        },
        output: { type: 'string' },
        in_place: { type: 'boolean' },
        dry_run: { type: 'boolean' },
      },
      required: ['file', 'ops'],
      additionalProperties: false,
    },
    output: {
      schema: outputSchema(
        {
          settings: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                key: { type: 'string' },
                old: {},
                new: {},
              },
              required: ['key'],
              additionalProperties: false,
            },
          },
        },
        ['settings'],
      ),
      render: (_args, value) => textBlocks(value),
    },
    presentCall: (args) => genericCard(`设置 · ${path.basename(String(args.file || ''))}`),
    presentResult: () => ({ card: 'generic', title: '设置结果' }),
    async execute(args, exec) {
      return call('settings', args, exec)
    },
  }

  const slice = {
    name: 'bambu3mf_slice',
    description:
      'Slice a project .3mf with the installed Bambu Studio CLI and export a sliced .gcode.3mf and/or G-code. This is the end-to-end check that an edited package still slices. The result includes Bambu\'s per-feature print times (walls, infill, travel, ...) so the caller can see where the time goes.',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: 'Absolute path to the project .3mf.' },
        plate: {
          type: 'integer',
          description: '1-based plate to slice; 0 slices every plate (default 0).',
        },
        export: {
          type: 'string',
          enum: ['gcode.3mf', 'gcode', 'both'],
          description: 'Default gcode.3mf (the CLI also writes per-plate .gcode).',
        },
        output_dir: {
          type: 'string',
          description: 'Output directory; defaults to <name>_sliced beside the input.',
        },
        settings: {
          type: 'string',
          description: 'Optional machine/process JSON list for --load-settings ("machine.json;process.json").',
        },
        filaments: {
          type: 'string',
          description: 'Optional filament JSON list for --load-filaments.',
        },
        preflight: {
          oneOf: [{ type: 'boolean' }, { type: 'string', enum: ['strict'] }],
          description:
            'Inter-object clearance check around slicing. Default (omitted) is advisory: '
            + 'the report is attached to the result, but slicing proceeds — Bambu accepts '
            + 'plates with parts a few mm apart, so a distance threshold must not block. '
            + '"strict" makes any bounding-box overlap abort before slicing; false skips '
            + 'the check.',
        },
        timeout_ms: { type: 'integer', description: 'Hard timeout; default 30 minutes.' },
      },
      required: ['file'],
      additionalProperties: false,
    },
    output: {
      schema: outputSchema(
        {
          sliceStats: {
            type: 'object',
            properties: {
              exitCode: { type: 'integer' },
              returnCode: { type: 'integer' },
              durationMs: { type: 'integer' },
              outputDir: { type: 'string' },
              plates: { type: 'array', items: { type: 'integer' } },
              estimateMinutes: { type: 'number' },
              filamentGrams: { type: 'number' },
              gcodeBytes: { type: 'integer' },
              error: { type: 'string' },
              resultStale: {
                type: 'boolean',
                description: 'True when result.json predates this run: the numbers are not from this slice.',
              },
              featureTotalSeconds: {
                type: 'number',
                description: 'Sum of the per-feature times; close to, but not identical with, the estimate.',
              },
              featureTimes: {
                type: 'object',
                description:
                  'Seconds per Bambu feature type (Outer wall, Travel, ...), summed over plates. Every value is a number of seconds.',
                additionalProperties: true,
              },
              plateStats: {
                type: 'array',
                items: {
                  type: 'object',
                  properties: {
                    id: { type: 'integer' },
                    mainMinutes: { type: 'number' },
                    totalMinutes: { type: 'number' },
                    filamentGrams: { type: 'number' },
                    triangles: { type: 'integer' },
                    objects: { type: 'integer' },
                    sliceSeconds: { oneOf: [{ type: 'number' }, { type: 'null' }] },
                    exportSeconds: { oneOf: [{ type: 'number' }, { type: 'null' }] },
                  },
                  required: ['id', 'mainMinutes', 'totalMinutes'],
                  additionalProperties: false,
                },
              },
            },
            required: ['exitCode', 'durationMs', 'outputDir'],
            additionalProperties: false,
          },
          outputFiles: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                path: { type: 'string' },
                bytes: { type: 'integer' },
              },
              required: ['name', 'path', 'bytes'],
              additionalProperties: false,
            },
          },
          preflight: {
            type: 'object',
            description: 'Clearance report gathered around this slice (advisory).',
            additionalProperties: true,
          },
        },
        ['sliceStats', 'outputFiles'],
      ),
      render: (_args, value) => textBlocks(value),
    },
    presentCall: (args) => genericCard(`切片 · ${path.basename(String(args.file || ''))}`),
    presentResult: () => ({ card: 'generic', title: '切片结果' }),
    async execute(args, exec) {
      return sliceProject(args, config, exec.signal)
    },
  }

  const diff = {
    name: 'bambu3mf_diff',
    description:
      'Compare two .3mf files key by key: project settings, objects (name/plate/extruder/'
      + 'triangles/bbox/position), plate membership and slice results (prediction/weight). '
      + 'With geometry=true it also compares the triangle sets and reports added/removed/'
      + 'flipped faces, volume/bbox deltas and where the differences cluster. '
      + 'Read-only. Use it to explain why two variants print differently before guessing.',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: 'Absolute path to the left .3mf.' },
        against: { type: 'string', description: 'Absolute path to the right .3mf.' },
        keys: {
          type: 'array',
          items: { type: 'string' },
          description:
            'Only compare these project_settings keys (exact, prefix or wildcard such as "*speed*"). '
            + 'When given, identical keys are listed too, so "is this key the same?" has an answer.',
        },
        limit: {
          type: 'integer',
          description: 'Cap on listed entries per section; default 100.',
        },
        geometry: {
          type: 'boolean',
          description:
            'Also compare triangle sets (added/removed/flipped, volume and bbox delta, '
            + 'difference clusters). Read-only; costs more than the metadata diff.',
        },
        geometry_limit: {
          type: 'integer',
          description: 'Skip objects above this triangle count in geometry diff; default 2000000.',
        },
      },
      required: ['file', 'against'],
      additionalProperties: false,
    },
    output: {
      schema: outputSchema(
        {
          diff: {
            type: 'object',
            description:
              'Structured comparison (settings/objects/plates/slice); the summary carries '
              + 'the readable form.',
            additionalProperties: true,
          },
        },
        ['diff'],
      ),
      render: (_args, value) => textBlocks(value),
    },
    presentCall: (args) =>
      genericCard(
        `对比 3MF · ${path.basename(String(args.file || ''))} ↔ ${path.basename(String(args.against || ''))}`,
      ),
    presentResult: () => ({ card: 'generic', title: '3MF 对比' }),
    isConcurrencySafe: () => true,
    timeoutMs: 300000,
    async execute(args, exec) {
      return call('diff', args, exec)
    },
  }

  const tools = [inspect, edit, mesh, build, render, settings, slice, diff]

  // Mark the session as 3MF-active on the first real call, so the gate keeps
  // these tools visible after the working directory moves elsewhere. Wrapping
  // here covers every tool at once; `exec.agent` is absent outside a Session, so
  // the gate tolerates undefined.
  const gate = runtime.gate
  if (gate === undefined) return tools
  return tools.map((tool) => ({
    ...tool,
    async execute(args, exec) {
      gate.markUsed(exec?.agent)
      return tool.execute(args, exec)
    },
  }))
}

export { ToolError }
