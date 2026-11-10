"""JSON command line used by the DSH plugin bridge.

Protocol: one JSON object on stdin, one JSON object on stdout.

    {"cmd": "inspect", "args": {...}, "config": {...}}
    {"ok": true, "value": {...}}
    {"ok": false, "error": {"code": "...", "message": "...", "details": {...}}}

Human-readable progress goes to stderr; stdout carries only the response.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "threemf"

from threemf.compare import compare, diff_lines  # noqa: E402
from threemf.constants import ThreeMFError  # noqa: E402
from threemf.scene import (  # noqa: E402
    apply_build,
    apply_edit,
    apply_mesh,
    apply_settings,
    describe,
    load,
    output_path_for,
    resolve_object,
    save,
)
from threemf.settings import match_keys  # noqa: E402
from threemf.slicing import format_slice_info  # noqa: E402
from threemf.validate import validate  # noqa: E402


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _emit({"ok": False, "error": {"code": "BAD_REQUEST", "message": f"invalid JSON: {exc}"}})
        return 2
    command = payload.get("cmd")
    args = payload.get("args") or {}
    config = payload.get("config") or {}
    try:
        value = dispatch(str(command), args, config)
        _emit({"ok": True, "value": value})
        return 0
    except ThreeMFError as exc:
        _emit({"ok": False, "error": exc.as_dict()})
        return 1
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all for the bridge
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "INTERNAL",
                    "message": f"{type(exc).__name__}: {exc}",
                    "details": {"traceback": traceback.format_exc()},
                },
            }
        )
        return 2


def dispatch(command: str, args: dict, config: dict) -> dict:
    if command == "inspect":
        return cmd_inspect(args)
    if command == "edit":
        return cmd_edit(args)
    if command == "build":
        return cmd_build(args)
    if command == "mesh":
        return cmd_mesh(args)
    if command == "settings":
        return cmd_settings(args)
    if command == "diff":
        return cmd_diff(args)
    if command == "render":
        from threemf.render import cmd_render

        return cmd_render(args, config)
    if command == "preflight":
        return cmd_preflight(args)
    raise ThreeMFError("BAD_REQUEST", f"unknown command: {command}")


# ---------------------------------------------------------------------- helpers
def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False))
    sys.stdout.flush()


def _clean(value):
    """Normalise numpy scalars, -0.0 and non-finite floats into lossless JSON."""
    import math

    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return 0.0 if value == 0 else float(value)
    item = getattr(value, "item", None)
    if callable(item):
        return _clean(item())
    return str(value)


def _diag(item: dict) -> dict:
    """Serialise a diagnostic, keeping its structured fields.

    Callers must be able to read the offending pair and the measured numbers
    without parsing the Chinese sentence, so everything ``_diag`` in validate.py
    attached is passed through.
    """
    out = {
        "severity": str(item.get("severity", "info")),
        "code": str(item.get("code", "")),
        "message": str(item.get("message", "")),
    }
    if item.get("objectId") is not None:
        out["objectId"] = int(item["objectId"])
    # Keep every structured field a diagnostic carries. A fixed allowlist used
    # to drop exactly the payload the caller needed (which pair, how many
    # shells, where the overlap is), so new diagnostics could not be consumed.
    for key, value in item.items():
        if key in ("severity", "code", "message", "objectId"):
            continue
        out[key] = _clean(value)
    return out


def _key(item: dict) -> tuple:
    """Identity of a diagnostic, ignoring its message.

    The message carries measured values (distances, sizes), so including it made
    an unchanged problem look new after every edit and flooded the result with
    "new" warnings.
    """
    ids = item.get("objectIds")
    identity = tuple(sorted(int(value) for value in ids)) if ids else item.get("objectId")
    return (item.get("code"), identity, item.get("plate"), item.get("path"))


def _snapshot(loaded) -> dict:
    """Diagnostics that already exist, so an edit only reports what it changed."""
    diagnostics = validate(loaded.scene, loaded.settings, loaded.project)
    return {
        "errors": {_key(item) for item in diagnostics if item["severity"] == "error"},
        "warnings": {_key(item) for item in diagnostics if item["severity"] == "warning"},
    }


def _finish(
    loaded,
    ops_label: str,
    changes: list[dict],
    extra: dict,
    output,
    in_place,
    dry_run: bool,
    snapshot: dict,
) -> dict:
    warnings = [
        item
        for item in validate(loaded.scene, loaded.settings, loaded.project)
        if item["severity"] == "warning" and _key(item) not in snapshot["warnings"]
    ]
    if dry_run:
        target, _backup = output_path_for(loaded, output, False)
        return {
            "file": loaded.path,
            "outputFile": None,
            "summary": f"[dry-run] {ops_label}：结构检查通过，未写文件。目标：{target}",
            "changes": changes,
            "diagnostics": [_diag(item) for item in warnings],
            "validation": {"changedEntries": [], "preservedEntries": 0, "dryRun": True},
            **extra,
        }
    saved = save(loaded, output, in_place, baseline_errors=snapshot["errors"])
    from threemf.scene import load as reload_package

    reloaded = reload_package(saved["output"])
    # New objects (split/primitive) and rewired meshes can carry warnings that
    # did not exist before the save; report those too instead of only the
    # pre-save snapshot.
    known = {_key(item) for item in warnings}
    for item in saved.get("warnings") or []:
        key = _key(item)
        if key in known or key in snapshot["warnings"]:
            continue
        known.add(key)
        warnings.append(item)
    new_errors = [
        item
        for item in validate(reloaded.scene, reloaded.settings, reloaded.project)
        if item["severity"] == "error" and _key(item) not in snapshot["errors"]
    ]
    if new_errors:
        try:
            os.remove(saved["output"])
        except OSError:
            pass
        raise ThreeMFError(
            "VALIDATION_FAILED",
            "编辑引入了新的结构错误，已回滚",
            {"errors": new_errors},
        )
    summary = (
        f"{ops_label}：{len(changes)} 处改动，"
        f"{saved['preservedEntries']} 个未改动条目保持不变 → {saved['output']}"
    )
    result = {
        "file": loaded.path,
        "outputFile": saved["output"],
        "summary": summary,
        "changes": changes,
        "diagnostics": [_diag(item) for item in warnings],
        "validation": {
            "changedEntries": saved["changedEntries"],
            "preservedEntries": int(saved["preservedEntries"]),
        },
        **extra,
    }
    if saved.get("backup"):
        result["backup"] = saved["backup"]
    return result


def _object_brief(loaded, object_id: str) -> dict:
    lo, hi = loaded.scene.world_bbox(object_id)
    build = loaded.scene.build_transform(object_id)
    return {
        "id": int(object_id),
        "name": loaded.object_name(object_id),
        "plate": loaded.settings.plate_of(object_id) if loaded.settings else None,
        "position": [round(float(v), 4) for v in build[3, :3]],
        "bbox": [round(float(v), 4) for v in [lo[0], lo[1], lo[2], hi[0], hi[1], hi[2]]],
        "size": [round(float(v), 3) for v in (hi - lo)],
    }


def _describe_objects(lines: list[str], objects: list[dict], with_size: bool = True) -> None:
    """Append one readable line per object so an edit's result is visible."""
    for item in objects:
        position = item.get("position") or [0, 0, 0]
        size = item.get("size") or [0, 0, 0]
        box = item.get("bbox") or [0] * 6
        text = f"  #{item['id']} 位置 ({position[0]:g}, {position[1]:g})"
        if with_size:
            # Bottom z is what a caller reasons about; origin z is what is stored.
            text += f" · 尺寸 {size[0]:g}×{size[1]:g}×{size[2]:g} mm"
        text += f" · 底面 z={box[2]:g}"
        if item.get("plate"):
            text += f" · 盘 {item['plate']}"
        lines.append(text)


def _require_path(args: dict) -> str:
    path = args.get("file") or args.get("path")
    if not path:
        raise ThreeMFError("INVALID_ARGS", "需要 file（3mf 绝对路径）")
    path = os.path.abspath(str(path))
    if not os.path.isfile(path):
        raise ThreeMFError("NOT_FOUND", f"文件不存在：{path}")
    return path


# ---------------------------------------------------------------------- inspect
# Keys worth showing when the caller did not ask for specific ones: they are the
# ones an edit routinely depends on, and they are cheap to read.
COMMON_SETTING_KEYS = (
    "printer_model",
    "curr_bed_type",
    "layer_height",
    "wall_loops",
    "sparse_infill_density",
    "enable_support",
    "enable_prime_tower",
    "wipe_tower_x",
    "wipe_tower_y",
    "print_sequence",
    "brim_type",
)


def _format_object_full(lines: list[str], item: dict, shared_names: dict) -> None:
    """Per-object detail block for ``detail=full``; appended under the brief line."""
    indent = "      "
    position = item.get("position") or [0, 0, 0]
    rotation = item.get("rotationZ")
    detail = [
        f"位置 ({position[0]:g}, {position[1]:g}, {position[2]:g})",
        f"绕 Z {rotation:g}°" if rotation is not None else None,
        f"挤出机 {item.get('extruder')}",
        f"顶点 {item.get('vertices')}",
    ]
    lines.append(indent + " · ".join(part for part in detail if part))

    matrix = item.get("matrix")
    if matrix and len(matrix) == 16:
        rows = [
            " ".join(f"{float(value):g}" for value in matrix[index : index + 4])
            for index in range(0, 16, 4)
        ]
        lines.append(f"{indent}矩阵 [{'] ['.join(rows)}]")

    for source in item.get("sources") or []:
        path = source.get("path", "?")
        object_id = source.get("objectId")
        note = ""
        sharers = shared_names.get((path, str(object_id))) or []
        if len(sharers) > 1:
            note = (
                f" · 被 {len(sharers)} 个对象共享："
                + " ".join("#" + str(other) for other in sharers)
            )
        lines.append(
            f"{indent}源 /{path}#{object_id} · {source.get('triangles', 0)} 面{note}"
        )

    parts = item.get("parts") or []
    if parts:
        described = ", ".join(
            f"id={part.get('id')}"
            + (f" {part.get('subtype')}" if part.get("subtype") else "")
            + (f" '{part.get('name')}'" if part.get("name") else "")
            for part in parts[:6]
        )
        more = "" if len(parts) <= 6 else f" … 共 {len(parts)} 个"
        lines.append(f"{indent}部件 {len(parts)} 个：{described}{more}")

    components = item.get("components") or []
    if len(components) > 1:
        described = ", ".join(
            f"[{comp.get('index')}] {comp.get('triangles')} 面 "
            f"{(comp.get('size') or [0, 0, 0])[0]:g}×{(comp.get('size') or [0, 0, 0])[1]:g}"
            f"×{(comp.get('size') or [0, 0, 0])[2]:g}"
            for comp in components[:8]
        )
        more = "" if len(components) <= 8 else f" … 共 {len(components)} 个"
        lines.append(f"{indent}连通壳: {described}{more}")
    elif components:
        lines.append(f"{indent}连通壳: 1 个（整体连通）")

    meta = item.get("settings") or {}
    if meta:
        pairs = ", ".join(f"{key}={value}" for key, value in list(meta.items())[:8])
        lines.append(f"{indent}对象设置: {pairs}")


def _shared_reference_names(data: dict) -> dict[tuple[str, str], list[int]]:
    """Map ``(object file path, mesh object id)`` to the root objects using it."""
    shared: dict[tuple[str, str], list[int]] = {}
    for item in data["objects"]:
        for source in item.get("sources") or []:
            shared.setdefault(
                (source.get("path", ""), str(source.get("objectId"))), []
            ).append(item["id"])
    return shared


def _format_full_blocks(
    data: dict, shared_names: dict, settings_keys, settings_limit: int
) -> list[str]:
    """Extra text sections that only make sense for ``detail=full``."""
    lines: list[str] = []

    names: dict[str, list[int]] = {}
    for item in data["objects"]:
        names.setdefault(item["name"], []).append(item["id"])
    duplicates = {name: ids for name, ids in names.items() if len(ids) > 1}
    if duplicates:
        lines.append("名称对照（存在重名，按名称选择会报 AMBIGUOUS_OBJECT，请用 id）：")
        for name, ids in sorted(duplicates.items()):
            lines.append(f"  {name} → {' '.join('#' + str(i) for i in ids)}")
        lines.append("")

    entries = data.get("entries") or []
    if entries:
        shown = ", ".join(entries[:30])
        more = "" if len(entries) <= 30 else f" … 共 {len(entries)} 个"
        lines.append(f"包内条目 {len(entries)} 个：{shown}{more}")
        lines.append("")

    settings = data.get("projectSettings")
    if settings is not None:
        keys = match_keys(settings, settings_keys or [])
        if keys:
            lines.append(f"项目设置（匹配 {len(keys)} 项）：")
            for key in keys[:settings_limit]:
                lines.append(f"  {key} = {settings[key]}")
            if len(keys) > settings_limit:
                lines.append(f"  … 其余 {len(keys) - settings_limit} 项被 settings_limit 截断")
        else:
            hint = (
                "传 settings_keys 可列出任意键（支持前缀与通配，例如 \"wipe_tower\" 或 \"*speed*\"）"
            )
            shown_keys = [key for key in COMMON_SETTING_KEYS if key in settings]
            lines.append(f"项目设置共 {len(settings)} 项；常用项：")
            for key in shown_keys[: max(0, settings_limit)]:
                lines.append(f"  {key} = {settings[key]}")
            if len(shown_keys) > settings_limit:
                lines.append(
                    f"  … 其余 {len(shown_keys) - settings_limit} 项常用键未显示"
                    "（提高 settings_limit 或用 settings_keys 查询）"
                )
            lines.append(f"  （{hint}）")
    return [line for line in lines] if lines else []


def cmd_inspect(args: dict) -> dict:
    path = _require_path(args)
    detail = str(args.get("detail") or "summary")
    if detail not in ("summary", "full"):
        raise ThreeMFError("INVALID_ARGS", "detail 必须是 summary 或 full")
    settings_keys = args.get("settings_keys")
    if isinstance(settings_keys, str):
        settings_keys = [settings_keys]
    if settings_keys is not None and not isinstance(settings_keys, list):
        raise ThreeMFError("INVALID_ARGS", "settings_keys 必须是字符串数组")
    settings_limit = int(args.get("settings_limit") or 80)
    if settings_limit < 1:
        raise ThreeMFError("INVALID_ARGS", "settings_limit 必须为正整数")
    loaded = load(path)
    data = describe(loaded, detail=detail, include_settings=bool(args.get("include_settings", True)))
    lines = []
    lines.append(
        f"{os.path.basename(path)} · {data['format']} · {data['stats']['objects']} 个对象 / "
        f"{data['stats']['plates']} 个盘 / {data['stats']['triangles']} 个三角面"
    )
    if data.get("printer"):
        bed = data["printer"].get("bed") or []
        lines.append(
            f"打印机：{data['printer'].get('model', '?')}  床面 {bed[0]:g}×{bed[1]:g}×{bed[2]:g} mm"
        )
    if data.get("filaments"):
        colors = ", ".join(
            f"#{i + 1} {item['type']} {item['color']}" for i, item in enumerate(data["filaments"])
        )
        lines.append(f"耗材：{colors}")
    if data.get("sliceInfo"):
        # The slicer's own numbers: an exported .gcode.3mf needs no slicing to
        # answer "how long does this take".
        lines.extend(format_slice_info(data["sliceInfo"]))
    elif data["format"] == "bambu-sliced":
        lines.append(
            "已切片，但包内缺少 Metadata/slice_info.config：读不到预估时长与耗材重量"
        )
    for plate in data["plates"]:
        lines.append(f"盘 {plate['id']}（{plate['name'] or '-'}）：对象 {plate['objectIds']}")
    shared_names = _shared_reference_names(data) if detail == "full" else {}
    for item in data["objects"]:
        size = [round(item["bbox"][3 + i] - item["bbox"][i], 2) for i in range(3)]
        lines.append(
            f"  #{item['id']} {item['name']} · {item['triangles']} 面 · 尺寸 {size[0]}×{size[1]}×{size[2]} mm"
            + (f" · 盘 {item['plate']}" if item["plate"] else "")
            + (" · 共享网格" if item["sharedMesh"] else "")
        )
        if detail == "full":
            _format_object_full(lines, item, shared_names)
    if detail == "full":
        lines.extend(_format_full_blocks(data, shared_names, settings_keys, settings_limit))
    data["summary"] = "\n".join(lines)
    data["changes"] = []
    data["validation"] = {"changedEntries": [], "preservedEntries": 0}
    return data


# ------------------------------------------------------------------------- edit
def cmd_edit(args: dict) -> dict:
    path = _require_path(args)
    loaded = load(path)
    snapshot = _snapshot(loaded)
    ops = args.get("ops")
    if not isinstance(ops, list):
        raise ThreeMFError("INVALID_ARGS", "ops 必须是数组")
    before_ids = set(loaded.scene.object_ids())
    log = apply_edit(loaded, ops)
    created_ids = [oid for oid in loaded.scene.object_ids() if oid not in before_ids]
    changes = []
    for raw, line in zip(ops, log):
        entry = {"kind": str(raw.get("op")), "detail": line}
        selector = raw.get("object", raw.get("objectId"))
        if selector is not None:
            try:
                entry["objectId"] = int(resolve_object(loaded, selector))
            except ThreeMFError:
                pass
        changes.append(entry)
    touched = {item["objectId"] for item in changes if "objectId" in item}
    # A duplicate creates an object that no change line points at; report the new
    # ids too, otherwise the caller only sees the source object echoed back.
    touched.update(int(oid) for oid in created_ids)
    objects = [
        _object_brief(loaded, str(oid))
        for oid in sorted(touched)
        if str(oid) in loaded.scene.root_objects
    ]
    result = _finish(
        loaded,
        f"编辑 {len(ops)} 个操作",
        changes,
        {"opsApplied": len(ops), "objects": objects},
        args.get("output"),
        bool(args.get("in_place")),
        bool(args.get("dry_run")),
        snapshot,
    )
    if objects:
        lines = [result["summary"], "改动后状态："]
        _describe_objects(lines, objects)
        result["summary"] = "\n".join(lines)
    return result


def _suppress_overlap(result: dict, object_ids: set[int], note: str) -> None:
    """Drop OBJECT_OVERLAP rows involving objects this call deliberately placed."""
    if not object_ids:
        return
    kept = []
    removed = 0
    for item in result.get("diagnostics") or []:
        ids = {int(value) for value in (item.get("objectIds") or []) if str(value).isdigit()}
        if item.get("code") == "OBJECT_OVERLAP" and ids & object_ids:
            removed += 1
            continue
        kept.append(item)
    if removed:
        kept.append(
            {
                "severity": "info",
                "code": "OVERLAP_SUPPRESSED",
                "message": f"按 allow_overlap 忽略了 {removed} 条与本次改动对象相关的包围盒重叠告警。{note}",
            }
        )
    result["diagnostics"] = kept


# ------------------------------------------------------------------- build
def cmd_build(args: dict) -> dict:
    """Constructive modelling: booleans, primitives, splitting and replacement.

    This is the command the previous session had to emulate in external Python:
    subtract a box, move a column, union it back. It operates in world
    millimetres and keeps the target object's identity, plate and extruder.
    """
    path = _require_path(args)
    loaded = load(path)
    snapshot = _snapshot(loaded)
    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ThreeMFError("INVALID_ARGS", "build 需要非空 ops 数组")
    before_ids = set(loaded.scene.object_ids())
    logs, stats = apply_build(loaded, ops)

    touched: set[int] = set()
    for raw, line, info in zip(ops, logs, stats):
        for key in ("objectId", "createdObjectId", "negativeObjectId", "positiveObjectId", "operandObjectId"):
            value = info.get(key)
            if isinstance(value, int):
                touched.add(value)
    for object_id in loaded.scene.object_ids():
        if object_id not in before_ids:
            touched.add(int(object_id))

    changes = []
    for raw, line, info in zip(ops, logs, stats):
        entry = {"kind": str(raw.get("op")), "detail": line}
        object_id = info.get("objectId") or info.get("createdObjectId")
        if isinstance(object_id, int):
            entry["objectId"] = object_id
        changes.append(entry)

    objects = [
        _object_brief(loaded, str(object_id))
        for object_id in sorted(touched)
        if str(object_id) in loaded.scene.root_objects
    ]
    extra = {
        "opsApplied": len(ops),
        "objects": objects,
        "operationStats": {"operations": stats},
        "detachedMesh": any(bool(item.get("detachedMesh")) for item in stats),
        "transformBaked": any(bool(item.get("transformBaked")) for item in stats),
        "partsCollapsed": max((int(item.get("partsCollapsed") or 0) for item in stats), default=0),
    }
    result = _finish(
        loaded,
        f"构建 {len(ops)} 个操作",
        changes,
        extra,
        args.get("output"),
        bool(args.get("in_place")),
        bool(args.get("dry_run")),
        snapshot,
    )

    warnings = []
    seen = set()
    for item in stats:
        for warning in item.get("warnings") or []:
            key = (warning.get("code"), warning.get("objectId"), warning.get("message"))
            if key in seen:
                continue
            seen.add(key)
            warnings.append(warning)
    if warnings:
        result["diagnostics"].extend(warnings)
        errors = [item for item in warnings if item.get("severity") == "error"]
        if errors:
            # A boolean result that is not closed is a hard failure: do not leave
            # a package the slicer cannot trust.
            if result.get("outputFile"):
                try:
                    os.remove(result["outputFile"])
                except OSError:
                    pass
            raise ThreeMFError(
                "VALIDATION_FAILED",
                "构建结果没有通过几何校验，已回滚",
                {"errors": errors},
            )
    if args.get("allow_overlap"):
        _suppress_overlap(result, touched, "布尔/替换/拆分通常会产生有意重叠。")

    if logs:
        lines = [result["summary"], "构建步骤："]
        lines.extend(f"  • {line}" for line in logs)
        if objects:
            lines.append("改动后状态：")
            _describe_objects(lines, objects)
        result["summary"] = "\n".join(lines)
    return result


def _analyze_result(loaded, result: dict) -> dict:
    """Render the printability report; the structured fields keep the numbers."""
    data = result["printability"]
    overhang = data["overhang"]
    object_id = int(result["objectId"])
    volume = data.get("volumeMm3")
    volume_text = (
        f"体积 {volume / 1000:.2f} cm³" if volume is not None else "体积不可靠（绕向不一致）"
    )
    lines = [
        f"对象 {object_id} · 可打印性：{data['triangles']} 面 · {data['shells']} 个连通壳 · "
        f"{volume_text} · 表面积 {data['surfaceAreaMm2'] / 100:.1f} cm²",
    ]
    size = data["size"]
    layer = data.get("layerHeight")
    layers = data.get("estimatedLayers")
    geometry = f"尺寸 {size[0]:g}×{size[1]:g}×{size[2]:g} mm"
    if layers and layer:
        geometry += f"（{layers} 层 @ {layer:g} mm）"
    lines.append(geometry)
    if not data["triangles"]:
        state = "没有网格数据"
    elif data["watertight"]:
        state = "水密"
    else:
        state = (
            f"不闭合：边界边 {data['boundaryEdges']} / "
            f"非流形边 {data['nonManifoldEdges']}"
        )
    if data.get("orientationConsistent"):
        state += " · 绕向一致"
    else:
        state += " · 绕向不一致"
    genus = data.get("genus")
    if genus is not None:
        state += f" · 通孔 {genus} 个（genus）"
    lines.append(state)
    if data["shells"] > 1:
        lines.append(
            f"提示：{data['shells']} 个壳彼此不连通，Bambu 可能把它识别成多个部件"
        )
    geometry_issues = (
        f"退化面 {data.get('degenerateTriangles', 0)} · "
        f"重复面 {data.get('duplicateFaces', 0)} · "
        f"反向面 {data.get('opposingFaces', 0)} · "
        f"零厚度薄片 {data.get('thinSheets', 0)}"
    )
    lines.append(geometry_issues)
    lines.append(
        f"悬垂 {overhang['angleDegrees']:g}°：{overhang['areaMm2'] / 100:.1f} cm²"
        f"（{overhang['percent']:.1f}%），贴床 {overhang['bedContactMm2'] / 100:.1f} cm²，"
        f"需支撑 {overhang['unsupportedMm2'] / 100:.1f} cm²（{overhang['unsupportedPercent']:.1f}%）"
    )
    diagnostics = []
    if not data["triangles"]:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "EMPTY_MESH",
                "message": f"对象 {object_id} 没有网格数据，无法评估可打印性",
                "objectId": object_id,
            }
        )
    elif not data["watertight"]:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "OPEN_MESH",
                "message": (
                    f"网格不闭合（边界边 {data['boundaryEdges']}、"
                    f"非流形边 {data['nonManifoldEdges']}）：面积统计仍然可用，"
                    "体积与通孔数不可靠"
                ),
                "objectId": object_id,
            }
        )
    if data["triangles"] and not data.get("orientationConsistent"):
        diagnostics.append(
            {
                "severity": "warning",
                "code": "INCONSISTENT_WINDING",
                "message": f"对象 {object_id} 的三角形绕向不一致，有符号体积会相互抵消；体积按不可靠处理",
                "objectId": object_id,
            }
        )
    if data["shells"] > 1:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "DISCONNECTED_SHELLS",
                "message": f"对象 {object_id} 含 {data['shells']} 个互不连通的壳，Bambu 可能拆成多个部件",
                "objectId": object_id,
            }
        )
    if data.get("negativeShells"):
        diagnostics.append(
            {
                "severity": "warning",
                "code": "NEGATIVE_SHELL",
                "message": f"对象 {object_id} 有 {data['negativeShells']} 个壳的法向朝内"
                "（体积符号为负）；切片前建议修正法向",
                "objectId": object_id,
            }
        )
    for code, count, label in (
        ("DEGENERATE_TRIANGLES", data.get("degenerateTriangles", 0), "退化三角形"),
        ("THIN_SHEET", data.get("thinSheets", 0), "零厚度薄片"),
        ("COINCIDENT_FACES", data.get("duplicateFaces", 0) + data.get("opposingFaces", 0), "重复/反向面"),
    ):
        if count:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": code,
                    "message": f"对象 {object_id} 含 {count} 个{label}",
                    "objectId": object_id,
                }
            )
    if genus is not None and genus >= 20:
        lines.append(
            f"提示：{genus} 个通孔意味着每层要多次起停与空驶，是打印时长的主要来源之一"
        )
        diagnostics.append(
            {
                "severity": "info",
                "code": "MANY_HOLES",
                "message": f"对象 {object_id} 有 {genus} 个通孔（genus），空驶与外墙时间会偏高",
                "objectId": object_id,
            }
        )
    if overhang["unsupportedPercent"] >= 5.0:
        lines.append(
            f"提示：需支撑面积占表面积 {overhang['unsupportedPercent']:.1f}%，"
            "支撑会明显增加时长与耗材"
        )
        diagnostics.append(
            {
                "severity": "info",
                "code": "HIGH_OVERHANG",
                "message": f"对象 {object_id} 有 {overhang['unsupportedMm2']:.0f} mm² 悬垂需要支撑",
                "objectId": object_id,
            }
        )
    return {
        "file": loaded.path,
        "outputFile": None,
        "summary": "\n".join(lines),
        "changes": [],
        "diagnostics": diagnostics,
        "validation": {"changedEntries": [], "preservedEntries": 0, "dryRun": True},
        "objectId": object_id,
        "op": "analyze",
        "trianglesBefore": int(data["triangles"]),
        "trianglesAfter": int(data["triangles"]),
        "printability": data,
    }

# ------------------------------------------------------------------------- mesh
def cmd_mesh(args: dict) -> dict:
    path = _require_path(args)
    loaded = load(path)
    snapshot = _snapshot(loaded)
    op = str(args.get("op") or "")
    if not op:
        raise ThreeMFError("INVALID_ARGS", "mesh 需要 op")
    params = {
        key: value
        for key, value in args.items()
        if key not in ("file", "output", "in_place", "dry_run")
    }
    selector = args.get("object", args.get("objectId"))
    if op != "import_stl" and (selector is None or str(selector).strip() == ""):
        raise ThreeMFError(
            "INVALID_ARGS",
            f"mesh op={op} 需要 object（对象 id 或唯一名称）；"
            "只有 import_stl 不需要 object",
        )
    if op == "diagnose":
        object_id = resolve_object(loaded, selector)
        result = apply_mesh(loaded, object_id, op, params)
        report = result["diagnosis"]
        state = "水密" if report["watertight"] else f"不闭合（开放边 {report['boundaryEdges']}）"
        winding = "绕向一致" if report["orientationConsistent"] else "绕向不一致"
        volume = (
            f"{report['volumeMm3'] / 1000:.3f} cm³"
            if report["volumeMm3"] is not None
            else "体积不可靠"
        )
        lines = [
            f"对象 {object_id} 诊断：{report['triangles']} 面 · {report['shells']} 个壳 · "
            f"{state} · {winding} · {volume}",
            f"退化面 {report['degenerateTriangles']} · 重复面 {report['duplicateFaces']} · "
            f"反向面 {report['opposingFaces']} · 零厚度薄片 {report['thinSheets']} · "
            f"共面内部面对 {report['coincidentFacePairs']}"
            + ("（已截断）" if report["coincidentTruncated"] else ""),
        ]
        for item in report["shellsReport"][:6]:
            lines.append(
                f"  壳 {item['index']}：{item['triangles']} 面 · "
                f"{item['size'][0]:g}×{item['size'][1]:g}×{item['size'][2]:g} mm · "
                f"体积 {item['signedVolumeMm3']} mm³"
                + (" · 反向" if item["negative"] else "")
                + (" · 薄片" if item["thin"] else "")
            )
        for item in report["coincidentSamples"][:3]:
            lines.append(
                f"  共面样本：面 {item['triangles'][0]} 与 {item['triangles'][1]} "
                f"@ ({item['center'][0]:g}, {item['center'][1]:g}, {item['center'][2]:g})"
            )
        return {
            "file": loaded.path,
            "outputFile": None,
            "summary": "\n".join(lines),
            "changes": [],
            "diagnostics": [],
            "validation": {"changedEntries": [], "preservedEntries": 0, "dryRun": True},
            "objectId": int(object_id),
            "op": op,
            "trianglesBefore": int(result["triangles"]),
            "trianglesAfter": int(result["triangles"]),
            "diagnosis": report,
            "operationStats": {"diagnosis": report},
        }
    if op == "section":
        object_id = resolve_object(loaded, selector)
        result = apply_mesh(loaded, object_id, op, params)
        section = result["section"]
        if params.get("format") == "svg" or params.get("out"):
            from threemf.geometry import section_svg

            target = params.get("out") or (
                os.path.splitext(loaded.path)[0] + "_section.svg"
            )
            if section.get("loops"):
                section["svgPath"] = section_svg(section, str(target))
        loops = section["loops"]
        closed = sum(1 for item in loops if item["closed"])
        lines = [
            f"对象 {object_id} 剖面：{len(loops)} 个环（闭合 {closed}）· "
            f"净面积 {section['areaMm2']:.2f} mm² · 周长 {section['perimeterMm']:.2f} mm"
        ]
        if section["basis"]:
            lines.append(
                "平面基：origin ({origin[0]:g}, {origin[1]:g}, {origin[2]:g})，"
                "u ({u[0]:g}, {u[1]:g}, {u[2]:g})，v ({v[0]:g}, {v[1]:g}, {v[2]:g})".format(
                    **section["basis"]
                )
            )
        if section.get("svgPath"):
            lines.append(f"SVG：{section['svgPath']}")
        for item in loops[:6]:
            lines.append(
                f"  环：{len(item['points'])} 点 · {'闭合' if item['closed'] else '开放'} · "
                f"面积 {item['areaMm2']} mm²"
                + ("（已截断）" if item["truncated"] else "")
            )
        return {
            "file": loaded.path,
            "outputFile": None,
            "summary": "\n".join(lines),
            "changes": [],
            "diagnostics": [],
            "validation": {"changedEntries": [], "preservedEntries": 0, "dryRun": True},
            "objectId": int(object_id),
            "op": op,
            "trianglesBefore": int(result["triangles"]),
            "trianglesAfter": int(result["triangles"]),
            "section": section,
            "operationStats": {"section": section},
        }
    if op == "measure":
        object_id = resolve_object(loaded, selector)
        result = apply_mesh(loaded, object_id, op, params)
        measurement = result["measurement"]
        mode = measurement["mode"]
        if mode == "heightmap" and params.get("out"):
            from threemf.render import heightmap_png

            measurement["png"] = heightmap_png(measurement["values"], str(params["out"]))
        if mode == "heightmap":
            lines = [
                f"对象 {object_id} 高度场（{measurement['axis']} 轴，"
                f"{measurement['grid']}×{measurement['grid']}）："
                f"min {measurement['min']} · max {measurement['max']} · "
                f"mean {measurement['mean']} · 有料单元 {measurement['cells']}"
            ]
            if measurement.get("png"):
                lines.append(f"PNG：{measurement['png']}")
        elif mode == "spans":
            spans = measurement["spans"]
            lines = [
                f"对象 {object_id} 材料区间（{measurement['axis']} 轴）："
                f"{len(spans)} 段 · 覆盖 {measurement['coverageMm']} mm · "
                f"{measurement['triangleCount']} 个三角形"
            ]
            for start, end in spans[:10]:
                lines.append(f"  [{start:g}, {end:g}]")
        else:
            lines = [
                f"对象 {object_id} 区域：{measurement['triangles']} 个三角形 · "
                f"bbox {measurement['bbox']} · 尺寸 {measurement['size']}"
            ]
        return {
            "file": loaded.path,
            "outputFile": None,
            "summary": "\n".join(lines),
            "changes": [],
            "diagnostics": [],
            "validation": {"changedEntries": [], "preservedEntries": 0, "dryRun": True},
            "objectId": int(object_id),
            "op": op,
            "trianglesBefore": int(result["triangles"]),
            "trianglesAfter": int(result["triangles"]),
            "measurement": measurement,
            "operationStats": {"measurement": measurement},
        }
    if op == "components":
        object_id = resolve_object(loaded, args.get("object", args.get("objectId")))
        result = apply_mesh(loaded, object_id, op, params)
        components = result["components"]
        lines = [
            f"对象 {object_id} 内有 {len(components)} 个连通体（按面数降序，"
            "可直接用于 select.component）："
        ]
        for item in components:
            lines.append(
                f"  #{item['index']} · {item['triangles']} 面 · "
                f"{item['size'][0]:.2f}×{item['size'][1]:.2f}×{item['size'][2]:.2f} mm · "
                f"中心 ({item['center'][0]:.2f}, {item['center'][1]:.2f}, "
                f"{item['center'][2]:.2f})"
            )
        return {
            "file": loaded.path,
            "outputFile": None,
            "summary": "\n".join(lines),
            "changes": [],
            "diagnostics": [],
            "validation": {"changedEntries": [], "preservedEntries": 0, "dryRun": True},
            "objectId": int(object_id),
            "op": op,
            "trianglesBefore": int(result["triangles"]),
            "trianglesAfter": int(result["triangles"]),
            "operationStats": {"components": components},
        }
    if op == "analyze":
        result = apply_mesh(loaded, args.get("object", args.get("objectId")), op, params)
        return _analyze_result(loaded, result)
    if op == "export_stl":
        object_id = resolve_object(loaded, args.get("object", args.get("objectId")))
        result = apply_mesh(loaded, object_id, op, params)
        return {
            "file": loaded.path,
            "outputFile": result["output"],
            "summary": f"导出 STL：{result['output']}（{result['triangles']} 个三角面）",
            "changes": [
                {
                    "kind": "export_stl",
                    "objectId": int(object_id),
                    "detail": f"对象 {object_id} → {result['output']}",
                }
            ],
            "diagnostics": [],
            "validation": {"changedEntries": [], "preservedEntries": 0},
            "objectId": int(object_id),
            "op": op,
            "trianglesBefore": int(result["triangles"]),
            "trianglesAfter": int(result["triangles"]),
            "operationStats": {"bbox": result["bbox"]},
        }
    if op == "import_stl":
        result = apply_mesh(loaded, None, op, params)
        changes = [
            {
                "kind": "import_stl",
                "objectId": int(result["objectId"]),
                "detail": (
                    f"导入 {os.path.basename(str(params.get('stl')))} → 对象 "
                    f"{result['objectId']}（{result['triangles']} 面）"
                ),
            }
        ]
        finished = _finish(
            loaded,
            "导入 STL",
            changes,
            {
                "objectId": int(result["objectId"]),
                "op": op,
                "trianglesBefore": 0,
                "trianglesAfter": int(result["triangles"]),
                "verticesBefore": 0,
                "verticesAfter": int(result["vertices"]),
            },
            args.get("output"),
            bool(args.get("in_place")),
            bool(args.get("dry_run")),
            snapshot,
        )
        if args.get("allow_overlap"):
            _suppress_overlap(
                finished,
                {int(result["objectId"])},
                "导入常用于替换旧几何，重叠是预期结果。",
            )
        return finished
    selector = args.get("object", args.get("objectId"))
    object_id = resolve_object(loaded, selector)
    result = apply_mesh(loaded, object_id, op, params)
    changes = [
        {
            "kind": op,
            "objectId": int(object_id),
            "detail": f"对象 {object_id}：{result['trianglesBefore']} → {result['trianglesAfter']} 面",
        }
    ]
    extra = {
        "objectId": int(object_id),
        "op": op,
        "trianglesBefore": int(result["trianglesBefore"]),
        "trianglesAfter": int(result["trianglesAfter"]),
        "verticesBefore": int(result["verticesBefore"]),
        "verticesAfter": int(result["verticesAfter"]),
        "qualityBefore": result["qualityBefore"],
        "qualityAfter": result["qualityAfter"],
        "operationStats": result["stats"],
        "detachedMesh": bool((result.get("install") or {}).get("detached")),
    }
    finished = _finish(
        loaded,
        f"网格操作 {op}（对象 {object_id}）",
        changes,
        extra,
        args.get("output"),
        bool(args.get("in_place")),
        bool(args.get("dry_run")),
        snapshot,
    )
    before = result.get("bboxBefore") or [0] * 6
    after = result.get("bboxAfter") or [0] * 6
    size_before = [round(before[3 + i] - before[i], 2) for i in range(3)]
    size_after = [round(after[3 + i] - after[i], 2) for i in range(3)]
    quality = result["qualityAfter"]
    detail = [
        f"对象 {object_id} · {op}",
        f"尺寸 {size_before[0]}×{size_before[1]}×{size_before[2]} → "
        f"{size_after[0]}×{size_after[1]}×{size_after[2]} mm",
        f"三角面 {result['trianglesBefore']} → {result['trianglesAfter']}",
        "水密" if quality["watertight"] else f"开放边 {quality['boundaryEdges']}",
    ]
    stats = result["stats"]
    selection = stats.get("selection")
    if selection:
        before = selection["size"]
        after = selection.get("sizeAfter") or before
        detail.append(
            f"仅改选区 {selection['triangles']} 面："
            f"{before[0]}×{before[1]}×{before[2]} → "
            f"{after[0]}×{after[1]}×{after[2]} mm"
        )
        if stats.get("pivotMode") == "selection":
            detail.append(
                f"轴心=选区中心 ({selection['center'][0]}, {selection['center'][1]}, "
                f"{selection['center'][2]})"
            )
        seam = int(selection.get("seamEdges") or 0)
        if seam:
            detail.append(
                f"选区内部接缝 {seam} 条边：选区与原网格在此处连成一体，"
                "变换后表面会沿这些边裂开（只想整体搬动某个壳时，请用 "
                "select.component 选中完整连通体）"
            )
    install = result.get("install") or {}
    if install.get("detached"):
        siblings = install.get("siblings") or []
        names = " ".join("#" + str(other) for other in siblings)
        detail.append(
            f"网格原被 {len(siblings) + 1} 个对象共用，已为对象 {object_id} 单独复制一份"
            f"（{install.get('targetFile')}），{names} 的几何保持原样"
        )
    if op == "plane_cut":
        detail.append(f"封盖 {stats.get('capTriangles', 0)} 面 / {stats.get('loops', 0)} 个环")
        if stats.get("planeShift"):
            detail.append(f"平面偏移 {stats['planeShift']} mm")
        if stats.get("capFallback"):
            detail.append("封盖使用了兜底三角化")
    stretch = stats.get("stretch")
    if stretch:
        band = stretch["band"]
        after_band = stretch["bandAfter"]
        detail.append(
            f"拉伸 {stretch['axis']} 轴区间 "
            f"[{band[0]:g}, {band[1]:g}] → [{after_band[0]:g}, {after_band[1]:g}]"
            f"（×{stretch['factor']:g}，两侧共移动 {stretch['delta']:g} mm，"
            f"外侧顶点刚性位移、形状不变）"
        )
        if stretch.get("coincidentWithFace"):
            detail.append("注意：拉伸边界与已有面共面，接缝处会有折痕")
    finished["summary"] = " · ".join(str(item) for item in detail) + "\n" + finished["summary"]
    return finished


# -------------------------------------------------------------------- preflight
def cmd_preflight(args: dict) -> dict:
    """Slice readiness report: overlapping objects, real surface contact, plate bounds.

    Advisory by default. Bambu accepts plates with neighbours a few millimetres
    apart and with interlocking parts, so a distance threshold is not a usable
    gate — 0.3.0 tried that and refused files Bambu sliced. Only genuine surface
    contact blocks; everything else is reported so a failed slice can be explained.
    ``strict`` also treats box overlap as blocking, for callers who want that.
    """
    from threemf.scene import _find_free_spot
    from threemf.validate import TOUCH_MM

    path = _require_path(args)
    loaded = load(path)
    try:
        plate_filter = args.get("plate")
        plate_filter = int(plate_filter) if plate_filter is not None else None
    except (TypeError, ValueError) as exc:
        raise ThreeMFError("INVALID_ARGS", f"plate 必须是整数：{exc}") from exc
    if plate_filter == 0:
        plate_filter = None
    strict = bool(args.get("strict"))
    diagnostics = validate(
        loaded.scene, loaded.settings, loaded.project, with_distances=True
    )
    overlaps = [
        item
        for item in diagnostics
        if item["code"] == "OBJECT_OVERLAP"
        and (plate_filter is None or item.get("plate") in (None, plate_filter))
        and str(item.get("objectId")) in loaded.scene.root_objects
    ]
    touching = [
        item
        for item in overlaps
        if float(item.get("nearestMm", float("inf"))) <= TOUCH_MM
    ]
    blocking = overlaps if strict else touching
    plate_reports = [
        item
        for item in diagnostics
        if item["code"] == "PLATE_CLEARANCE"
        and (plate_filter is None or item.get("plate") in (None, plate_filter))
    ]

    lines: list[str] = []
    suggestions: list[dict] = []
    infeasible: list[dict] = []
    if blocking:
        reason = "严格模式：存在包围盒重叠" if strict else "对象表面实际接触/干涉"
        lines.append(f"切片预检发现 {len(blocking)} 处问题（{reason}）")
        for item in blocking:
            lines.append(f"  [{item['code']}] {item['message']}")
    else:
        lines.append("切片预检通过：未发现对象接触，可以直接切片")
    for item in plate_reports:
        lines.append(f"  {item['message']}")

    if blocking:
        lines.append("建议（move 增量，单位 mm；可直接放进 ops）：")
        seen: set[int] = set()
        for item in blocking:
            plate_id = item.get("plate")
            for object_id in item.get("objectIds") or [item.get("objectId")]:
                object_id = int(object_id)
                if object_id in seen:
                    continue
                seen.add(object_id)
                spot = None
                if plate_id is not None and loaded.settings is not None:
                    try:
                        spot = _find_free_spot(loaded, str(object_id), plate_id)
                    except ThreeMFError:
                        spot = None
                if spot is None:
                    infeasible.append(
                        {
                            "objectId": object_id,
                            "plate": plate_id,
                            "reason": "盘内找不到能放下它的空位",
                        }
                    )
                    lines.append(
                        f"  #{object_id}：盘内没有空位，需先移走其他对象或换盘（无建议）"
                    )
                    continue
                # _find_free_spot works in plate-local coordinates; convert the
                # distance to a move delta, which is frame independent.
                lo, hi = loaded.scene.world_bbox(str(object_id))
                origin = loaded.plate_origins().get(plate_id, np.zeros(3))
                local_centre = (lo + hi) / 2.0 - origin
                delta = [spot[0] - float(local_centre[0]), spot[1] - float(local_centre[1])]
                build = loaded.scene.build_transform(str(object_id))
                suggestions.append(
                    {
                        "objectId": object_id,
                        "plate": plate_id,
                        "move": {"dx": round(delta[0], 3), "dy": round(delta[1], 3)},
                        "from": [round(float(build[3, 0]), 3), round(float(build[3, 1]), 3)],
                        "to": [
                            round(float(build[3, 0]) + delta[0], 3),
                            round(float(build[3, 1]) + delta[1], 3),
                        ],
                    }
                )
                lines.append(
                    f"  #{object_id}：move dx={delta[0]:.1f}, dy={delta[1]:.1f}"
                    f"（世界坐标 {build[3, 0]:.1f},{build[3, 1]:.1f} → "
                    f"{build[3, 0] + delta[0]:.1f},{build[3, 1] + delta[1]:.1f}）"
                )
        if infeasible:
            lines.append(
                f"  注意：{len(infeasible)} 个对象没有可用建议；"
                "各建议是逐对象独立求解，不能保证同时套用"
            )

    return {
        "file": loaded.path,
        "outputFile": None,
        "summary": "\n".join(lines),
        "changes": [],
        "diagnostics": [_diag(item) for item in diagnostics if item["severity"] != "info"],
        "validation": {"changedEntries": [], "preservedEntries": 0},
        "ready": not blocking,
        "strict": strict,
        "blocking": [_diag(item) for item in blocking],
        "warnings": [_diag(item) for item in overlaps if item not in blocking],
        "plateReports": [_diag(item) for item in plate_reports],
        "suggestions": suggestions,
        "infeasible": infeasible,
    }


# --------------------------------------------------------------------- settings
def cmd_settings(args: dict) -> dict:
    path = _require_path(args)
    loaded = load(path)
    snapshot = _snapshot(loaded)
    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ThreeMFError("INVALID_ARGS", "settings 需要非空 ops 数组")
    results = apply_settings(loaded, ops)
    changes = [
        {"kind": "setting", "detail": f"{item['key']}: {item['old']!r} → {item['new']!r}"}
        for item in results
    ]
    return _finish(
        loaded,
        f"修改 {len(results)} 项设置",
        changes,
        {"settings": results},
        args.get("output"),
        bool(args.get("in_place")),
        bool(args.get("dry_run")),
        snapshot,
    )


# ------------------------------------------------------------------------- diff
def cmd_diff(args: dict) -> dict:
    """Read-only comparison of two packages: settings, objects, plates, slice.

    This is the command that replaces a twenty-minute guessing session: one call
    answers which project settings actually differ, whether the geometry moved
    and what the slicer said about each side.
    """
    path = _require_path(args)
    against = str(args.get("against") or "").strip()
    if not against:
        raise ThreeMFError(
            "INVALID_ARGS", "diff 需要 against：要对比的另一个 3mf（绝对路径）"
        )
    other = os.path.abspath(against)
    if not os.path.isfile(other):
        raise ThreeMFError("NOT_FOUND", f"对比文件不存在：{other}")

    keys = args.get("keys")
    if isinstance(keys, str):
        keys = [keys]
    if keys is not None:
        if not isinstance(keys, list) or not all(isinstance(item, str) for item in keys):
            raise ThreeMFError("INVALID_ARGS", "keys 必须是字符串数组")
        keys = [item for item in keys if item.strip()] or None
    try:
        limit = int(args.get("limit") or 100)
    except (TypeError, ValueError) as exc:
        raise ThreeMFError("INVALID_ARGS", f"limit 必须是整数：{exc}") from exc
    if limit < 1:
        raise ThreeMFError("INVALID_ARGS", "limit 必须为正整数")

    left = load(path)
    right = load(other)
    geometry = bool(args.get("geometry"))
    try:
        geometry_limit = int(args.get("geometry_limit") or 2000000)
    except (TypeError, ValueError) as exc:
        raise ThreeMFError("INVALID_ARGS", f"geometry_limit 必须是整数：{exc}") from exc
    if geometry_limit < 1:
        raise ThreeMFError("INVALID_ARGS", "geometry_limit 必须为正整数")
    diff = compare(
        left,
        right,
        keys=keys,
        limit=limit,
        geometry=geometry,
        geometry_limit=geometry_limit,
    )
    return {
        "file": left.path,
        "outputFile": None,
        "summary": "\n".join(diff_lines(diff)),
        "changes": [],
        "diagnostics": [],
        "validation": {"changedEntries": [], "preservedEntries": 0},
        "diff": diff,
    }


if __name__ == "__main__":
    sys.exit(main())
