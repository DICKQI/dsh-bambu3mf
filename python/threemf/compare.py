"""Key-by-key comparison of two loaded packages.

The question this answers is "why do these two projects behave differently?":
most of the time it is a handful of project_settings keys, the rest is geometry
or placement. Comparing is cheap next to guessing, so the tool compares
settings, objects, plates and the slicer report in one pass.
"""

from __future__ import annotations

import difflib
import json
import os

import numpy as np

from .constants import ThreeMFError
from .geometry import topology_report
from .model import Mesh
from .scene import Loaded, describe
from .settings import match_keys


# ---------------------------------------------------------------- value compare
def _canonical(value):
    """Comparison key that treats 0.2 and "0.2" as the same setting.

    Bambu writes every project setting as a string in some versions and as a
    JSON scalar in others, so a raw type check would report differences that do
    not exist for the slicer.
    """
    if isinstance(value, bool) or value is None:
        return (type(value).__name__, value)
    if isinstance(value, (int, float)):
        return ("num", float(value))
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                return ("num", float(text))
            except ValueError:
                pass
        return ("str", text)
    if isinstance(value, (list, tuple)):
        return ("list", tuple(_canonical(item) for item in value))
    if isinstance(value, dict):
        return ("dict", json.dumps(value, sort_keys=True, ensure_ascii=False))
    return ("str", str(value))


def _display(value, limit: int = 120) -> str:
    """Compact one-value rendering; long gcode blobs are truncated.

    Lists are rendered item by item so a truncated value still looks like a
    value (a json.dumps cut in the middle leaves brackets dangling).
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and not value.strip():
        return '""'
    if isinstance(value, list):
        parts = [_display(item, 40) for item in value[:6]]
        text = "[" + ", ".join(parts) + "]"
        if len(value) > 6:
            text = text[:-1] + f", … 共 {len(value)} 项]"
        return text if len(text) <= limit else text[:limit] + "…"
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    if len(text) > limit:
        return text[:limit] + f"…（共 {len(text)} 字符）"
    return text


def _clip(value, limit: int = 2000):
    """Keep one value bounded: machine gcode blobs are kilobytes each.

    The text layer already truncates for display; this also keeps the structured
    payload (and the session record) from growing by hundreds of kilobytes when
    two unrelated projects are compared.
    """
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…（共 {len(value)} 字符）"
    if isinstance(value, list) and len(value) > 200:
        return value[:200] + [f"…（共 {len(value)} 项）"]
    return value


def _round_list(values, digits: int = 3):
    if not isinstance(values, (list, tuple)):
        return values
    rounded = []
    for item in values:
        try:
            rounded.append(round(float(item), digits))
        except (TypeError, ValueError):
            rounded.append(item)
    return rounded


def _human_bytes(size) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "?"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value:.0f} B"


# --------------------------------------------------------------------- settings
def compare_settings(left: dict, right: dict, keys, limit: int) -> dict:
    left = left or {}
    right = right or {}
    union = sorted(set(left) | set(right))
    filtered = bool(keys)
    if filtered:
        considered = match_keys(union, keys)
        if not considered:
            raise ThreeMFError(
                "INVALID_ARGS",
                f"keys 没有匹配到任何项目设置：{list(keys)}",
                {"suggestions": difflib.get_close_matches(str(keys[0]), union, n=8)},
            )
    else:
        considered = union

    entries: list[dict] = []
    same = 0
    for key in considered:
        in_left = key in left
        in_right = key in right
        if in_left and in_right:
            if _canonical(left[key]) == _canonical(right[key]):
                same += 1
                if filtered:
                    entries.append(
                        {"key": key, "status": "same", "left": left[key], "right": right[key]}
                    )
                continue
            entries.append(
                {
                    "key": key,
                    "status": "changed",
                    "left": _clip(left[key]),
                    "right": _clip(right[key]),
                }
            )
        elif in_left:
            entries.append({"key": key, "status": "onlyLeft", "left": _clip(left[key])})
        else:
            entries.append({"key": key, "status": "onlyRight", "right": _clip(right[key])})

    # Differences first: a limit must never hide the answer, only long lists of
    # identical keys.
    entries.sort(key=lambda item: 0 if item["status"] != "same" else 1)
    different = sum(1 for item in entries if item["status"] != "same")
    truncated = len(entries) > limit
    return {
        "left": len(left),
        "right": len(right),
        "compared": len(considered),
        "different": different,
        "same": same,
        "filtered": filtered,
        "entries": entries[:limit],
        "truncated": truncated,
    }


# ---------------------------------------------------------------------- objects
OBJECT_FIELDS = (
    ("name", "名称"),
    ("plate", "盘"),
    ("extruder", "挤出机"),
    ("triangles", "三角面"),
    ("vertices", "顶点"),
    ("bbox", "包围盒"),
    ("position", "位置"),
    ("sharedMesh", "共享网格"),
)


def _object_map(data: dict) -> dict:
    return {int(item["id"]): item for item in data.get("objects") or []}


def compare_objects(left: dict, right: dict, limit: int) -> dict:
    left_map = _object_map(left)
    right_map = _object_map(right)
    only_left = [
        {"id": object_id, "name": left_map[object_id]["name"]}
        for object_id in sorted(set(left_map) - set(right_map))
    ]
    only_right = [
        {"id": object_id, "name": right_map[object_id]["name"]}
        for object_id in sorted(set(right_map) - set(left_map))
    ]
    changed = []
    for object_id in sorted(set(left_map) & set(right_map)):
        first, second = left_map[object_id], right_map[object_id]
        fields = []
        for key, label in OBJECT_FIELDS:
            value_a, value_b = first.get(key), second.get(key)
            if key in ("bbox", "position"):
                value_a, value_b = _round_list(value_a), _round_list(value_b)
            if _canonical(value_a) != _canonical(value_b):
                fields.append(
                    {"field": key, "label": label, "left": value_a, "right": value_b}
                )
        if fields:
            changed.append(
                {"id": object_id, "name": first.get("name") or "", "changes": fields}
            )
    total = len(changed) + len(only_left) + len(only_right)
    return {
        "left": len(left_map),
        "right": len(right_map),
        "onlyLeft": only_left[:limit],
        "onlyRight": only_right[:limit],
        "changed": changed[:limit],
        "different": total,
        "truncated": total > limit,
    }


# ----------------------------------------------------------------------- plates
def _plate_map(data: dict) -> dict:
    return {int(plate["id"]): plate for plate in data.get("plates") or []}


def compare_plates(left: dict, right: dict, limit: int) -> dict:
    left_map = _plate_map(left)
    right_map = _plate_map(right)
    only_left = [
        {"id": plate_id, "name": left_map[plate_id].get("name") or ""}
        for plate_id in sorted(set(left_map) - set(right_map))
    ]
    only_right = [
        {"id": plate_id, "name": right_map[plate_id].get("name") or ""}
        for plate_id in sorted(set(right_map) - set(left_map))
    ]
    changed = []
    for plate_id in sorted(set(left_map) & set(right_map)):
        first, second = left_map[plate_id], right_map[plate_id]
        fields = []
        if (first.get("name") or "") != (second.get("name") or ""):
            fields.append(
                {"field": "name", "label": "名称", "left": first.get("name") or "", "right": second.get("name") or ""}
            )
        ids_a = sorted(int(item) for item in first.get("objectIds") or [])
        ids_b = sorted(int(item) for item in second.get("objectIds") or [])
        if ids_a != ids_b:
            fields.append({"field": "objectIds", "label": "对象", "left": ids_a, "right": ids_b})
        if fields:
            changed.append(
                {"id": plate_id, "name": first.get("name") or "", "changes": fields}
            )
    total = len(changed) + len(only_left) + len(only_right)
    return {
        "left": len(left_map),
        "right": len(right_map),
        "onlyLeft": only_left[:limit],
        "onlyRight": only_right[:limit],
        "changed": changed[:limit],
        "different": total,
        "truncated": total > limit,
    }


# ------------------------------------------------------------------------ slice
def _slice_side(data: dict) -> dict:
    info = data.get("sliceInfo") or {}
    plates = []
    seconds_total = 0.0
    grams_total = 0.0
    timed = 0
    weighed = 0
    for plate in info.get("plates") or []:
        seconds = plate.get("predictionSeconds")
        grams = plate.get("weightGrams")
        if seconds:
            seconds_total += float(seconds)
            timed += 1
        if grams is not None:
            grams_total += float(grams)
            weighed += 1
        plates.append(
            {
                "index": plate.get("index"),
                "predictionMinutes": round(float(seconds) / 60.0, 1) if seconds else None,
                "weightGrams": grams,
            }
        )
    return {
        "sliced": data.get("format") == "bambu-sliced",
        "clientVersion": info.get("clientVersion") or "",
        "plates": plates,
        "totalPredictionMinutes": round(seconds_total / 60.0, 1) if timed else None,
        "totalWeightGrams": round(grams_total, 2) if weighed else None,
    }


def _delta(first: dict, second: dict) -> dict:
    minutes = (
        round(second["totalPredictionMinutes"] - first["totalPredictionMinutes"], 1)
        if first.get("totalPredictionMinutes") is not None
        and second.get("totalPredictionMinutes") is not None
        else None
    )
    grams = (
        round(second["totalWeightGrams"] - first["totalWeightGrams"], 2)
        if first.get("totalWeightGrams") is not None
        and second.get("totalWeightGrams") is not None
        else None
    )
    return {"predictionMinutes": minutes, "weightGrams": grams}


# --------------------------------------------------------------- geometry diff
def _fingerprint(vertices, triangles, decimals: int = 3):
    """Position-canonical triangle keys plus winding parity.

    Vertex order and triangle order are irrelevant, so the comparison survives
    a re-export even when ids and mesh order changed. Keys are packed from the
    coordinates themselves, not from a per-file vertex index: index numbers
    shift as soon as one side has an extra vertex, which would make almost
    every triangle look changed.
    """
    quantized = np.round(vertices * (10**decimals)).astype(np.int64)
    shifted = quantized + (1 << 20)
    if np.any(shifted < 0) or np.any(shifted >= (1 << 21)):
        raise ThreeMFError("INVALID_ARGS", "几何 diff 的坐标超出可编码范围")
    packed = (
        shifted[:, 0].astype(np.uint64)
        | (shifted[:, 1].astype(np.uint64) << np.uint64(21))
        | (shifted[:, 2].astype(np.uint64) << np.uint64(42))
    )
    triangle_keys = packed[triangles]
    canonical = np.sort(triangle_keys, axis=1)
    structured = np.ascontiguousarray(canonical).view(
        np.dtype([("a", "<u8"), ("b", "<u8"), ("c", "<u8")])
    ).reshape(-1)
    order = np.argsort(triangle_keys, axis=1, kind="stable")
    inversions = (
        (order[:, 0] > order[:, 1]).astype(np.int8)
        + (order[:, 0] > order[:, 2]).astype(np.int8)
        + (order[:, 1] > order[:, 2]).astype(np.int8)
    )
    parity = (inversions % 2).astype(np.int8)
    return structured, parity


def _cluster(points, size: float = 5.0, limit: int = 8) -> list[dict]:
    if len(points) == 0:
        return []
    cells = np.floor(points / size).astype(np.int64)
    unique, inverse = np.unique(cells, axis=0, return_inverse=True)
    counts = np.bincount(inverse)
    order = np.argsort(-counts)[:limit]
    clusters = []
    for index in order.tolist():
        mask = inverse == index
        subset = points[mask]
        clusters.append(
            {
                "count": int(counts[index]),
                "bbox": [round(float(value), 2) for value in np.concatenate([subset.min(0), subset.max(0)])],
                "center": [round(float(value), 2) for value in subset.mean(0)],
            }
        )
    return clusters


def compare_geometry(
    left: Loaded,
    right: Loaded,
    left_data: dict,
    right_data: dict,
    limit: int = 100,
    max_triangles: int = 2000000,
) -> dict:
    """Triangle-set comparison for objects that exist on both sides."""
    left_objects = {int(item["id"]): item for item in left_data.get("objects") or []}
    right_objects = {int(item["id"]): item for item in right_data.get("objects") or []}
    common = sorted(set(left_objects) & set(right_objects))
    entries = []
    skipped = []
    for object_id in common[:limit]:
        left_entry = left_objects[object_id]
        right_entry = right_objects[object_id]
        if (
            int(left_entry.get("triangles") or 0) > max_triangles
            or int(right_entry.get("triangles") or 0) > max_triangles
        ):
            skipped.append(object_id)
            continue
        try:
            left_vertices, left_triangles = left.scene.world_triangles(str(object_id))
            right_vertices, right_triangles = right.scene.world_triangles(str(object_id))
        except ThreeMFError:
            skipped.append(object_id)
            continue
        left_keys, left_parity = _fingerprint(left_vertices, left_triangles)
        right_keys, right_parity = _fingerprint(right_vertices, right_triangles)
        combined = np.concatenate([left_keys, right_keys])
        unique, inverse = np.unique(combined, return_inverse=True)
        left_inverse = inverse[: len(left_keys)]
        right_inverse = inverse[len(left_keys) :]
        left_counts = np.bincount(left_inverse, minlength=len(unique))
        right_counts = np.bincount(right_inverse, minlength=len(unique))
        removed = int(np.maximum(left_counts - right_counts, 0).sum())
        added = int(np.maximum(right_counts - left_counts, 0).sum())
        left_even = np.bincount(left_inverse, weights=(left_parity == 0), minlength=len(unique))
        right_even = np.bincount(right_inverse, weights=(right_parity == 0), minlength=len(unique))
        left_odd = left_counts - left_even
        right_odd = right_counts - right_even
        common_keys = (left_counts > 0) & (right_counts > 0)
        flipped = int(
            np.sum(
                np.where(
                    common_keys,
                    np.minimum(left_even, right_odd) + np.minimum(left_odd, right_even),
                    0.0,
                )
            )
        )

        left_centroids = left_vertices[left_triangles].mean(axis=1)
        right_centroids = right_vertices[right_triangles].mean(axis=1)
        left_extra = left_counts[left_inverse] > right_counts[left_inverse]
        right_extra = right_counts[right_inverse] > left_counts[right_inverse]
        differing = np.vstack([left_centroids[left_extra], right_centroids[right_extra]])

        left_report = topology_report(Mesh(left_vertices, left_triangles, []))
        right_report = topology_report(Mesh(right_vertices, right_triangles, []))
        entries.append(
            {
                "id": object_id,
                "name": left_entry.get("name") or right_entry.get("name") or "",
                "leftTriangles": int(len(left_triangles)),
                "rightTriangles": int(len(right_triangles)),
                "added": added,
                "removed": removed,
                "flipped": flipped,
                "unchanged": int(max(0, len(left_triangles) - removed - flipped)),
                "changed": added + removed + flipped,
                "leftVolumeMm3": left_report["volumeMm3"],
                "rightVolumeMm3": right_report["volumeMm3"],
                "volumeDeltaMm3": (
                    round(float(right_report["volumeMm3"]) - float(left_report["volumeMm3"]), 3)
                    if left_report["volumeMm3"] is not None
                    and right_report["volumeMm3"] is not None
                    else None
                ),
                "bboxDelta": [
                    round(float(right_entry["bbox"][index]) - float(left_entry["bbox"][index]), 3)
                    for index in range(6)
                ],
                "clusters": _cluster(differing),
            }
        )
    return {
        "objects": entries,
        "onlyLeft": sorted(set(left_objects) - set(right_objects))[:limit],
        "onlyRight": sorted(set(right_objects) - set(left_objects))[:limit],
        "skipped": skipped,
        "maxTriangles": max_triangles,
    }


# ------------------------------------------------------------------------- main
def _side(loaded: Loaded, data: dict) -> dict:
    stats = data.get("stats") or {}
    return {
        "path": loaded.path,
        "format": data.get("format") or "",
        "sizeBytes": int(data.get("sizeBytes") or 0),
        "objects": int(stats.get("objects") or 0),
        "plates": int(stats.get("plates") or 0),
        "triangles": int(stats.get("triangles") or 0),
    }


def compare(
    left: Loaded,
    right: Loaded,
    keys=None,
    limit: int = 100,
    geometry: bool = False,
    geometry_limit: int = 2000000,
) -> dict:
    left_data = describe(left, "summary")
    right_data = describe(right, "summary")
    left_slice = _slice_side(left_data)
    right_slice = _slice_side(right_data)
    result = {
        "left": _side(left, left_data),
        "right": _side(right, right_data),
        "settings": compare_settings(
            left.project.data if left.project is not None else {},
            right.project.data if right.project is not None else {},
            keys,
            limit,
        ),
        "objects": compare_objects(left_data, right_data, limit),
        "plates": compare_plates(left_data, right_data, limit),
        "slice": {"left": left_slice, "right": right_slice, "delta": _delta(left_slice, right_slice)},
    }
    if geometry:
        result["geometry"] = compare_geometry(
            left, right, left_data, right_data, limit=limit, max_triangles=geometry_limit
        )
    return result


# ------------------------------------------------------------------------- text
def _side_line(side: dict) -> str:
    return (
        f"{side.get('format') or '?'} · {side.get('objects', 0)} 对象 / "
        f"{side.get('plates', 0)} 盘 / {side.get('triangles', 0):,} 面 · "
        f"{_human_bytes(side.get('sizeBytes'))}"
    )


def _settings_line(settings: dict) -> str:
    if settings.get("filtered"):
        head = f"设置（匹配 {settings['compared']} 项）：{settings['different']} 项不同"
        if settings.get("same"):
            head += f"，{settings['same']} 项相同"
        return head + "。"
    if not settings.get("different"):
        return f"设置：{settings['compared']} 项，全部相同。"
    counts: list[str] = []
    statuses = [entry["status"] for entry in settings.get("entries") or []]
    changed = statuses.count("changed")
    only_left = statuses.count("onlyLeft")
    only_right = statuses.count("onlyRight")
    if changed:
        counts.append(f"{changed} 项值不同")
    if only_left:
        counts.append(f"{only_left} 项仅左")
    if only_right:
        counts.append(f"{only_right} 项仅右")
    suffix = f"（{'、'.join(counts)}）" if counts else ""
    return f"设置：{settings['compared']} 项中 {settings['different']} 项不同{suffix}"


def diff_lines(diff: dict, limit: int = 30) -> list[str]:
    left = diff.get("left") or {}
    right = diff.get("right") or {}
    left_name = os.path.basename(left.get("path") or "?")
    right_name = os.path.basename(right.get("path") or "?")
    lines = [
        f"3MF 对比：{left_name} ↔ {right_name}",
        f"左：{_side_line(left)}",
        f"右：{_side_line(right)}",
        "",
        _settings_line(diff.get("settings") or {}),
    ]
    settings = diff.get("settings") or {}
    shown_same = 0
    for entry in (settings.get("entries") or [])[:limit]:
        status = entry.get("status")
        key = entry.get("key")
        if status == "changed":
            lines.append(f"  ~ {key}: {_display(entry.get('left'))} → {_display(entry.get('right'))}")
        elif status == "onlyLeft":
            lines.append(f"  - {key}: {_display(entry.get('left'))}（仅左）")
        elif status == "onlyRight":
            lines.append(f"  + {key}: {_display(entry.get('right'))}（仅右）")
        else:
            # A pattern like "filam*" can match a hundred keys; listing every
            # identical one buries the answer.
            if shown_same >= 12:
                continue
            shown_same += 1
            lines.append(f"  = {key}: {_display(entry.get('left'))}")
    same_total = int(settings.get("same") or 0)
    # Unfiltered output only lists differences; the header already carries the
    # "N of M" fact, so a second "554 identical" line is just noise.
    if settings.get("filtered") and same_total > shown_same:
        lines.append(f"  … 其余 {same_total - shown_same} 项相同（未列出）")
    if settings.get("truncated"):
        hidden = settings.get("different", 0) - len(settings.get("entries") or [])
        lines.append(f"  … 另有 {max(hidden, 0)} 项未列出（用 keys 过滤或调大 limit）")

    objects = diff.get("objects") or {}
    lines.append("")
    lines.append(
        f"对象：{objects.get('left', 0)} ↔ {objects.get('right', 0)}；"
        f"{len(objects.get('onlyLeft') or [])} 个仅左 / {len(objects.get('onlyRight') or [])} 个仅右 / "
        f"{len(objects.get('changed') or [])} 个有差异"
    )
    for item in (objects.get("onlyLeft") or [])[:limit]:
        lines.append(f"  - #{item['id']} {item.get('name') or ''}（仅左）")
    for item in (objects.get("onlyRight") or [])[:limit]:
        lines.append(f"  + #{item['id']} {item.get('name') or ''}（仅右）")
    for item in (objects.get("changed") or [])[:limit]:
        detail = "；".join(
            f"{change.get('label') or change.get('field')} "
            f"{_display(change.get('left'))} → {_display(change.get('right'))}"
            for change in item.get("changes") or []
        )
        lines.append(f"  #{item['id']} {item.get('name') or ''}：{detail}")
    if objects.get("truncated"):
        lines.append("  … 对象差异过多，已截断（调大 limit）")

    geometry = diff.get("geometry")
    if geometry:
        entries = geometry.get("objects") or []
        changed = [item for item in entries if item.get("changed")]
        lines.append("")
        lines.append(
            f"几何：{len(entries)} 个共有对象中 {len(changed)} 个有差异"
            + (f"，{len(geometry.get('onlyLeft') or [])} 个仅左 / "
               f"{len(geometry.get('onlyRight') or [])} 个仅右"
               if geometry.get("onlyLeft") or geometry.get("onlyRight") else "")
            + (f"；{len(geometry.get('skipped') or [])} 个超过面数上限已跳过"
               if geometry.get("skipped") else "")
        )
        for item in changed[:limit]:
            bits = []
            if item.get("added"):
                bits.append(f"新增 {item['added']}")
            if item.get("removed"):
                bits.append(f"删除 {item['removed']}")
            if item.get("flipped"):
                bits.append(f"翻转 {item['flipped']}")
            detail = f"对象 #{item['id']} {item.get('name') or ''}：{' / '.join(bits)} 面"
            if item.get("volumeDeltaMm3") is not None:
                detail += f"；体积 {item['leftVolumeMm3'] / 1000:.3f}→{item['rightVolumeMm3'] / 1000:.3f} cm³"
            lines.append(detail)
            clusters = item.get("clusters") or []
            if clusters:
                cluster = clusters[0]
                lines.append(
                    f"  集中区：{cluster['count']} 面 @ "
                    f"X {cluster['bbox'][0]:g}–{cluster['bbox'][3]:g}, "
                    f"Y {cluster['bbox'][1]:g}–{cluster['bbox'][4]:g}, "
                    f"Z {cluster['bbox'][2]:g}–{cluster['bbox'][5]:g}"
                )
        if len(changed) > limit:
            lines.append(f"  … 另有 {len(changed) - limit} 个对象未列出")

    plates = diff.get("plates") or {}
    lines.append("")
    lines.append(
        f"盘：{plates.get('left', 0)} ↔ {plates.get('right', 0)}；"
        f"{len(plates.get('onlyLeft') or [])} 个仅左 / {len(plates.get('onlyRight') or [])} 个仅右 / "
        f"{len(plates.get('changed') or [])} 个有差异"
    )
    for item in (plates.get("changed") or [])[:limit]:
        detail = "；".join(
            f"{change.get('label') or change.get('field')} "
            f"{_display(change.get('left'))} → {_display(change.get('right'))}"
            for change in item.get("changes") or []
        )
        lines.append(f"  #{item['id']} {item.get('name') or ''}：{detail}")

    slice_info = diff.get("slice") or {}
    left_slice = slice_info.get("left") or {}
    right_slice = slice_info.get("right") or {}
    lines.append("")
    lines.append(f"切片：左{_slice_text(left_slice)}；右{_slice_text(right_slice)}")
    delta = slice_info.get("delta") or {}
    if delta.get("predictionMinutes") is not None:
        bits = [f"时长 {delta['predictionMinutes']:+.1f} 分钟"]
        if delta.get("weightGrams") is not None:
            bits.append(f"耗材 {delta['weightGrams']:+.2f} g")
        lines.append(f"  差值（右 - 左）：{' / '.join(bits)}")

    if _no_difference(diff):
        lines.append("")
        lines.append("结论：设置、对象、盘与切片结果均无差异。")
    return lines


def _slice_text(side: dict) -> str:
    if not side.get("sliced"):
        return "未切片"
    minutes = side.get("totalPredictionMinutes")
    grams = side.get("totalWeightGrams")
    bits = []
    plates = side.get("plates") or []
    if len(plates) == 1:
        index = plates[0].get("index")
        bits.append(f"盘 {index}" if index is not None else "已切片")
    elif plates:
        bits.append(f"{len(plates)} 个盘")
    if minutes is not None:
        bits.append(f"{minutes:.1f} 分钟")
    if grams is not None:
        bits.append(f"{grams:.2f} g")
    return " · ".join(bits) if bits else "已切片（无 slice_info 数据）"


def _no_difference(diff: dict) -> bool:
    settings = diff.get("settings") or {}
    objects = diff.get("objects") or {}
    plates = diff.get("plates") or {}
    geometry = diff.get("geometry") or {}
    geometry_changed = bool(
        geometry.get("onlyLeft")
        or geometry.get("onlyRight")
        or any(item.get("changed") for item in geometry.get("objects") or [])
    )
    return (
        not settings.get("different")
        and not objects.get("different")
        and not plates.get("different")
        and not geometry_changed
        and not (diff.get("slice", {}).get("delta", {}).get("predictionMinutes") or 0)
    )