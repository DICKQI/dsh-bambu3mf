"""High-level scene operations: load, describe, edit, mesh ops, settings, save."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

import numpy as np
from lxml import etree

from .constants import (
    LAYER_RANGES,
    MODEL_SETTINGS,
    PROJECT_SETTINGS,
    ROOT_MODEL,
    SLICE_INFO,
    ThreeMFError,
)
from .geometry import (
    CutResult,
    cleanup,
    component_info,
    connected_components,
    flip_normals,
    heightmap,
    isolate_triangles,
    material_spans,
    mesh_quality,
    plane_cut,
    primitive_box,
    primitive_cylinder,
    primitive_sphere,
    printability,
    read_stl,
    recentered,
    region_extent,
    section_mesh,
    stretch_band,
    topology_report,
    transformed,
    write_stl,
)
from .model import (
    IDENTITY,
    Mesh,
    Scene,
    about_pivot,
    build_mesh_element,
    format_transform,
    mirror_matrix,
    parse_transform,
    pqn,
    qn,
    rotation_z,
    scale_matrix,
    set_mesh,
    transform_points,
    translation,
    xml_bytes,
)
from .package import Package
from .settings import (
    ModelSettings,
    ProjectSettings,
    plate_origin,
    plate_origins,
)
from .slicing import parse_slice_info
from .validate import validate


# ---------------------------------------------------------------------- loading
@dataclass
class Loaded:
    path: str
    package: Package
    scene: Scene
    settings: ModelSettings | None
    project: ProjectSettings | None
    layer_root: etree._Element | None = None
    layer_dirty: bool = False
    extra_changes: dict[str, bytes] = field(default_factory=dict)

    @property
    def is_bambu(self) -> bool:
        return self.settings is not None or self.project is not None

    @property
    def is_sliced(self) -> bool:
        return self.scene.is_sliced

    def bed(self) -> tuple[float, float, float]:
        if self.project is not None:
            return self.project.bed_size()
        return self._bed_from_geometry()

    def _bed_from_geometry(self) -> tuple[float, float, float]:
        lo = np.array([np.inf, np.inf, np.inf])
        hi = np.array([-np.inf, -np.inf, -np.inf])
        for object_id in self.scene.object_ids():
            try:
                o_lo, o_hi = self.scene.world_bbox(object_id)
            except ThreeMFError:
                continue
            lo = np.minimum(lo, o_lo)
            hi = np.maximum(hi, o_hi)
        if not np.all(np.isfinite(lo)):
            return 256.0, 256.0, 256.0
        return float(hi[0] - lo[0]), float(hi[1] - lo[1]), float(hi[2] - lo[2])

    def bed_origin(self) -> tuple[float, float]:
        if self.project is not None:
            return self.project.bed_origin()
        return 0.0, 0.0

    def plate_origins(self) -> dict[int, np.ndarray]:
        if self.settings is None:
            return {}
        bed = self.bed()
        return plate_origins(
            [plate.plater_id for plate in self.settings.plates], bed, self.bed_origin()
        )

    def object_name(self, object_id: str) -> str:
        if self.settings is not None:
            meta = self.settings.object_meta(object_id)
            if meta.get("name"):
                return meta["name"]
        root = self.scene.root_objects.get(str(object_id))
        if root is not None:
            name = root.element.get("name")
            if name:
                return name
        return f"Object_{object_id}"

    def object_extruder(self, object_id: str) -> int:
        if self.settings is None:
            return 1
        meta = self.settings.object_meta(object_id)
        try:
            return int(meta.get("extruder", "1") or 1)
        except ValueError:
            return 1


def load(path: str) -> Loaded:
    package = Package.load(path)
    scene = Scene(package)
    settings = None
    project = None
    layer_root = None
    if package.has(MODEL_SETTINGS):
        settings = ModelSettings.parse(package.read(MODEL_SETTINGS))
    if package.has(PROJECT_SETTINGS):
        project = ProjectSettings.parse(package.read(PROJECT_SETTINGS))
    if package.has(LAYER_RANGES):
        try:
            layer_root = etree.fromstring(package.read(LAYER_RANGES))
        except etree.XMLSyntaxError:
            layer_root = None
    return Loaded(
        path=os.path.abspath(path),
        package=package,
        scene=scene,
        settings=settings,
        project=project,
        layer_root=layer_root,
    )


# --------------------------------------------------------------------- describe
def describe(loaded: Loaded, detail: str = "summary", include_settings: bool = True) -> dict:
    scene = loaded.scene
    diagnostics = validate(
        scene, loaded.settings, loaded.project, deep=(detail == "full")
    )
    if loaded.is_sliced:
        diagnostics.insert(
            0,
            {
                "severity": "info",
                "code": "SLICED_FILE",
                "message": "这是已切片的 .gcode.3mf：包含 G-code，几何已不可编辑",
            },
        )
    if not loaded.is_bambu:
        diagnostics.insert(
            0,
            {
                "severity": "info",
                "code": "NON_BAMBU_3MF",
                "message": "通用 3MF（不含 Bambu 工程元数据）：只能编辑几何与摆放",
            },
        )

    # The slicer's own verdict (predicted time, filament weight, per-object
    # rows). It only exists in an exported .gcode.3mf, and a broken one must not
    # make inspect fail: the geometry is still readable.
    slice_info = None
    if scene.pkg.has(SLICE_INFO):
        try:
            slice_info = parse_slice_info(scene.pkg.read(SLICE_INFO))
        except ThreeMFError as exc:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "BAD_SLICE_INFO",
                    "message": f"读不了 Metadata/slice_info.config：{exc.message}",
                }
            )

    refs = scene.component_references()
    plates = []
    if loaded.settings is not None:
        for plate in loaded.settings.plates:
            plates.append(
                {
                    "id": plate.plater_id,
                    "name": plate.name,
                    "objectIds": [int(item["object_id"]) for item in plate.instances],
                }
            )
    objects = []
    for object_id in scene.object_ids():
        try:
            sources = scene.mesh_sources(object_id)
        except ThreeMFError:
            sources = []
        triangles = sum(mesh.triangle_count for _p, _o, mesh, _m in sources)
        vertices = sum(mesh.vertex_count for _p, _o, mesh, _m in sources)
        try:
            lo, hi = scene.world_bbox(object_id)
            bbox = [float(v) for v in np.concatenate([lo, hi])]
        except ThreeMFError:
            bbox = [0.0] * 6
        build = scene.build_transform(object_id)
        shared = any(
            len(refs.get((path.lstrip("/"), oid), [])) > 1
            for path, oid, _mesh, _matrix in sources
        )
        entry = {
            "id": int(object_id),
            "name": loaded.object_name(object_id),
            "plate": loaded.settings.plate_of(object_id) if loaded.settings else None,
            "extruder": loaded.object_extruder(object_id),
            "triangles": triangles,
            "vertices": vertices,
            "bbox": [round(v, 4) for v in bbox],
            "position": [round(float(build[3, 0]), 4), round(float(build[3, 1]), 4), round(float(build[3, 2]), 4)],
            "sharedMesh": shared,
            "sources": [
                {"path": path.lstrip("/"), "objectId": int(oid), "triangles": mesh.triangle_count}
                for path, oid, mesh, _m in sources
            ],
        }
        if detail == "full":
            entry["rotationZ"] = round(
                float(np.degrees(np.arctan2(build[0, 1], build[0, 0]))), 4
            )
            entry["matrix"] = [round(float(v), 6) for v in build.ravel()]
            if loaded.settings is not None:
                entry["settings"] = loaded.settings.object_meta(object_id)
                entry["parts"] = loaded.settings.parts(object_id)
            try:
                world = scene.world_triangles(object_id)
                mesh = Mesh(world[0], world[1], [])
                labels = connected_components(mesh)
                if len(labels) and int(labels.max()) > 0:
                    entry["components"] = component_info(mesh, labels)[:20]
            except ThreeMFError:
                pass
        objects.append(entry)

    printer = {}
    process = {}
    filaments = []
    if loaded.project is not None:
        bed = loaded.project.bed_size()
        printer = {
            "model": loaded.project.printer_model(),
            "settingsId": loaded.project.settings_id(),
            "bed": [round(bed[0], 3), round(bed[1], 3), round(bed[2], 3)],
        }
        process = {
            "layerHeight": str(loaded.project.get("layer_height", "")),
            "walls": str(loaded.project.get("wall_loops", "")),
            "infill": str(loaded.project.get("sparse_infill_density", "")),
            "support": str(loaded.project.get("enable_support", "")),
        }
        colors = loaded.project.filament_colors()
        types = loaded.project.filament_types()
        for index in range(max(len(colors), len(types))):
            filaments.append(
                {
                    "index": index + 1,
                    "type": types[index] if index < len(types) else "",
                    "color": colors[index] if index < len(colors) else "",
                }
            )

    result = {
        "file": loaded.path,
        "sizeBytes": os.path.getsize(loaded.path),
        "format": "bambu-sliced" if loaded.is_sliced else ("bambu-project" if loaded.is_bambu else "core"),
        "application": _metadata_value(scene.root, "Application"),
        "title": _metadata_value(scene.root, "Title"),
        "unit": scene.unit,
        "filaments": filaments,
        "plates": plates,
        "objects": objects,
        "diagnostics": diagnostics,
        "stats": {
            "objects": len(objects),
            "triangles": sum(item["triangles"] for item in objects),
            "plates": len(plates),
            "entries": len(scene.pkg.order),
        },
        "entries": scene.pkg.names() if detail == "full" else [],
    }
    if printer:
        result["printer"] = printer
    if process:
        result["process"] = process
    if slice_info is not None:
        result["sliceInfo"] = slice_info
    if include_settings and loaded.project is not None and detail == "full":
        result["projectSettings"] = {
            key: loaded.project.get(key) for key in sorted(loaded.project.data)
        }
    return result


def _metadata_value(root: etree._Element, name: str) -> str:
    for meta in root.iterfind("m:metadata", {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}):
        if meta.get("name") == name:
            return meta.text or ""
    return ""


# ------------------------------------------------------------------ save/report
def output_path_for(loaded: Loaded, output: str | None, in_place: bool) -> tuple[str, str | None]:
    if output:
        return os.path.abspath(output), None
    if in_place:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base, ext = os.path.splitext(loaded.path)
        backup = f"{base}.bak-{stamp}{ext}"
        shutil.copy2(loaded.path, backup)
        return loaded.path, backup
    base, ext = os.path.splitext(loaded.path)
    return f"{base}_edited{ext or '.3mf'}", None


def save(
    loaded: Loaded,
    output: str | None = None,
    in_place: bool = False,
    baseline_errors: set | None = None,
) -> dict:
    target, backup = output_path_for(loaded, output, in_place)

    changes = loaded.scene.serialize_changes()
    changes.update(loaded.extra_changes)
    if loaded.settings is not None and loaded.settings.dirty:
        changes[MODEL_SETTINGS] = loaded.settings.serialize()
    if loaded.project is not None and loaded.project.dirty:
        changes[PROJECT_SETTINGS] = loaded.project.serialize()
    if loaded.layer_root is not None and loaded.layer_dirty:
        changes[LAYER_RANGES] = xml_bytes(loaded.layer_root)

    if not changes:
        raise ThreeMFError("NO_CHANGES", "没有任何改动，未写出文件")

    result = loaded.package.write(
        target,
        changes=changes,
        additions=loaded.scene.added_entries,
        deletions=loaded.scene.deleted_entries,
    )
    changed = set(changes) | set(loaded.scene.added_entries)
    problems = loaded.package.assert_preserved(target, changed, loaded.scene.deleted_entries)
    if problems:
        try:
            os.remove(target)
        except OSError:
            pass
        raise ThreeMFError(
            "VALIDATION_FAILED",
            "写出的包破坏了未改动的条目，已回滚",
            {"problems": problems},
        )

    reloaded = load(target)
    diagnostics = validate(reloaded.scene, reloaded.settings, reloaded.project)
    baseline = baseline_errors or set()
    errors = [
        item
        for item in diagnostics
        if item["severity"] == "error"
        and (item.get("code"), item.get("objectId"), item.get("message")) not in baseline
    ]
    if errors:
        try:
            os.remove(target)
        except OSError:
            pass
        raise ThreeMFError(
            "VALIDATION_FAILED",
            "编辑后的包没有通过结构校验，已回滚",
            {"errors": errors},
        )
    return {
        "output": target,
        "backup": backup,
        "entries": result["entries"],
        "changedEntries": result["changed"],
        "preservedEntries": result["preserved"],
        "warnings": [item for item in diagnostics if item["severity"] == "warning"],
    }


# ---------------------------------------------------------------- object lookup
def resolve_object(loaded: Loaded, selector) -> str:
    ids = list(loaded.scene.root_objects)
    if isinstance(selector, int) or (isinstance(selector, str) and selector.strip().isdigit()):
        target = str(int(selector))
        if target in loaded.scene.root_objects:
            return target
        raise ThreeMFError("MISSING_OBJECT", f"对象 {selector} 不存在", {"objectId": selector})
    text = str(selector).strip()
    exact = [
        object_id
        for object_id in ids
        if loaded.object_name(object_id) == text or object_id == text
    ]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [object_id for object_id in ids if text.lower() in loaded.object_name(object_id).lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if not fuzzy:
        raise ThreeMFError(
            "MISSING_OBJECT",
            f"找不到名称匹配 {text!r} 的对象",
            {"candidates": [loaded.object_name(i) for i in ids][:40]},
        )
    raise ThreeMFError(
        "AMBIGUOUS_OBJECT",
        f"名称 {text!r} 匹配到多个对象，请用对象 id",
        {"candidates": [{"id": int(i), "name": loaded.object_name(i)} for i in fuzzy]},
    )


def _pivot(
    loaded: Loaded,
    object_id: str,
    mode: str,
    explicit,
    selection_center: np.ndarray | None = None,
) -> np.ndarray:
    if isinstance(explicit, dict):
        return np.array(
            [float(explicit.get("x", 0)), float(explicit.get("y", 0)), float(explicit.get("z", 0))]
        )
    if mode == "selection":
        if selection_center is None:
            raise ThreeMFError(
                "INVALID_ARGS", "pivot=selection 只能在带 select 的网格变换里使用"
            )
        return np.asarray(selection_center, dtype=np.float64).copy()
    lo, hi = loaded.scene.world_bbox(object_id)
    center = (lo + hi) / 2.0
    if mode == "bed":
        return np.array([center[0], center[1], lo[2]])
    if mode == "origin":
        return np.zeros(3)
    return center


# -------------------------------------------------------------------- edit ops
def apply_edit(loaded: Loaded, ops: list[dict]) -> list[str]:
    if loaded.is_sliced:
        raise ThreeMFError(
            "SLICED_FILE",
            "这是已切片的 .gcode.3mf，几何与摆放不可编辑；请在项目 3mf 上修改后重新切片",
        )
    if not ops:
        raise ThreeMFError("INVALID_ARGS", "ops 不能为空")
    log: list[str] = []
    for raw in ops:
        if not isinstance(raw, dict) or "op" not in raw:
            raise ThreeMFError("INVALID_ARGS", "每个操作都必须是带 op 字段的对象")
        op = str(raw["op"])
        object_id = resolve_object(loaded, raw.get("object", raw.get("objectId")))
        build = loaded.scene.build_transform(object_id)
        if op == "move":
            delta = np.array(
                [float(raw.get("dx", 0)), float(raw.get("dy", 0)), float(raw.get("dz", 0))]
            )
            loaded.scene.set_build_transform(object_id, build @ translation(*delta))
            log.append(f"移动 {object_id}：{delta.tolist()}")
        elif op == "set_position":
            matrix = build.copy()
            z_mode = str(raw.get("z_mode") or "origin")
            for index, key in enumerate(("x", "y", "z")):
                if raw.get(key) is None:
                    continue
                value = float(raw[key])
                if key == "z" and z_mode == "bed":
                    # Ask for the bottom of the object, not the mesh origin: the
                    # stored z is an origin offset, which is rarely what is meant.
                    # local_bottom is where the mesh sits relative to that offset.
                    lo, _hi = loaded.scene.world_bbox(object_id)
                    local_bottom = float(lo[2]) - float(build[3, 2])
                    value -= local_bottom
                matrix[3, index] = value
            loaded.scene.set_build_transform(object_id, matrix)
            if z_mode == "bed" and raw.get("z") is not None:
                lo, hi = loaded.scene.world_bbox(object_id)
                log.append(
                    f"设置 {object_id} 位置：底面 z={float(lo[2]):.3f}"
                    f"（原点 z={float(matrix[3, 2]):.3f}，对象高 {float(hi[2] - lo[2]):.3f}）"
                )
            else:
                log.append(f"设置 {object_id} 位置：{matrix[3, :3].tolist()}")
        elif op == "rotate_z":
            angle = float(raw.get("angle", 0))
            pivot = _pivot(loaded, object_id, raw.get("pivot", "center"), raw.get("pivotPoint"))
            loaded.scene.set_build_transform(object_id, build @ about_pivot(rotation_z(angle), pivot))
            log.append(f"绕 Z 旋转 {object_id}：{angle}°，pivot={np.round(pivot, 3).tolist()}")
        elif op == "drop_to_bed":
            lo, _hi = loaded.scene.world_bbox(object_id)
            loaded.scene.set_build_transform(object_id, build @ translation(0, 0, -float(lo[2])))
            log.append(f"落到床面 {object_id}：z -= {round(float(lo[2]), 4)}")
        elif op == "set_printable":
            item = loaded.scene.build_item(object_id)
            if item is None:
                raise ThreeMFError("MISSING_OBJECT", f"对象 {object_id} 没有 build item")
            value = "1" if bool(raw.get("printable", True)) else "0"
            item.element.set("printable", value)
            loaded.scene._root_dirty = True
            log.append(f"printable={value}：{object_id}")
        elif op == "rename":
            name = str(raw.get("name", "")).strip()
            if not name:
                raise ThreeMFError("INVALID_ARGS", "rename 需要非空 name")
            if loaded.settings is None:
                raise ThreeMFError("NOT_BAMBU_PROJECT", "通用 3MF 没有对象名称表")
            loaded.settings.set_object_meta(object_id, "name", name)
            for part in loaded.settings.parts(object_id):
                loaded.settings.set_part_meta(object_id, part["id"], "name", name)
            log.append(f"重命名 {object_id} → {name}")
        elif op == "set_extruder":
            if loaded.settings is None:
                raise ThreeMFError("NOT_BAMBU_PROJECT", "通用 3MF 没有挤出机设置")
            extruder = int(raw.get("extruder", 1))
            loaded.settings.set_object_meta(object_id, "extruder", str(extruder))
            log.append(f"挤出机 {object_id} → {extruder}")
        elif op == "set_plate":
            if loaded.settings is None:
                raise ThreeMFError("NOT_BAMBU_PROJECT", "通用 3MF 没有盘概念")
            target_plate = int(raw.get("plate", 1))
            note = _move_to_plate(loaded, object_id, target_plate)
            log.append(f"换盘 {object_id} → 盘 {target_plate}{note}")
        elif op == "set_object_setting":
            if loaded.settings is None:
                raise ThreeMFError("NOT_BAMBU_PROJECT", "通用 3MF 没有对象设置")
            key = str(raw.get("key", "")).strip()
            if not key:
                raise ThreeMFError("INVALID_ARGS", "set_object_setting 需要 key")
            value = raw.get("value")
            text = "" if value is None else (str(value).lower() if isinstance(value, bool) else str(value))
            loaded.settings.set_object_meta(object_id, key, text)
            log.append(f"对象设置 {object_id}.{key} = {text!r}")
        elif op == "duplicate":
            new_id, note = _duplicate(loaded, object_id, raw)
            log.append(f"复制 {object_id} → {new_id}{note}")
        elif op == "delete":
            _delete(loaded, object_id)
            log.append(f"删除 {object_id}")
        else:
            raise ThreeMFError("INVALID_ARGS", f"未知操作：{op}")
    return log


def _move_to_plate(loaded: Loaded, object_id: str, target_plate: int) -> str:
    settings = loaded.settings
    if settings is None:
        raise ThreeMFError("NOT_BAMBU_PROJECT", "通用 3MF 没有盘概念")
    if settings.plate(target_plate) is None:
        settings.add_plate(target_plate)
    origins = loaded.plate_origins()
    current = settings.plate_of(object_id)
    source_origin = origins.get(current, np.zeros(3))
    target_origin = origins.get(target_plate)
    if target_origin is None:
        # Plate was just created; recompute with the new plate list.
        target_origin = loaded.plate_origins().get(target_plate, np.zeros(3))
    delta = target_origin - source_origin
    build = loaded.scene.build_transform(object_id) @ translation(*delta)
    loaded.scene.set_build_transform(object_id, build)
    settings.move_to_plate(int(object_id), target_plate)
    _assert_inside_plate(loaded, object_id, target_plate)
    moved = _avoid_overlap(loaded, object_id, target_plate)
    return "" if moved is None else f"，自动避让到 ({moved[0]:.1f}, {moved[1]:.1f})"


def _assert_inside_plate(loaded: Loaded, object_id: str, plate_id: int) -> None:
    if loaded.settings is None or loaded.project is None:
        return
    bed = loaded.bed()
    origin = loaded.plate_origins().get(plate_id, np.zeros(3))
    lo, hi = loaded.scene.world_bbox(object_id)
    local_lo, local_hi = lo - origin, hi - origin
    tolerance = 0.5
    if (
        local_lo[0] < -tolerance
        or local_lo[1] < -tolerance
        or local_hi[0] > bed[0] + tolerance
        or local_hi[1] > bed[1] + tolerance
    ):
        raise ThreeMFError(
            "PLATE_BOUNDS",
            f"对象 {object_id} 放到盘 {plate_id} 后会超出打印区域，已取消",
            {
                "plate": plate_id,
                "objectId": int(object_id),
                "bounds": [round(float(v), 3) for v in np.concatenate([local_lo, local_hi])],
                "bed": [round(bed[0], 3), round(bed[1], 3)],
            },
        )


# ------------------------------------------------------------------ placement
# The only spacing number in the plugin: how far an automatically placed object
# keeps from its neighbours. Clearance diagnostics do not use a threshold at all
# (see validate.py), so automatic placement can never trip its own warnings.
PLACEMENT_PADDING_MM = 1.5


def _plate_bboxes(loaded: Loaded, plate_id: int, exclude: str | None = None) -> list:
    if loaded.settings is None:
        return []
    plate = loaded.settings.plate(plate_id)
    if plate is None:
        return []
    boxes = []
    for instance in plate.instances:
        oid = str(instance["object_id"])
        if exclude is not None and oid == str(exclude):
            continue
        if oid not in loaded.scene.root_objects:
            continue
        try:
            lo, hi = loaded.scene.world_bbox(oid)
        except ThreeMFError:
            continue
        boxes.append((oid, lo, hi))
    return boxes


def _overlaps(box_a, box_b, padding: float = PLACEMENT_PADDING_MM) -> bool:
    (_, lo_a, hi_a), (_, lo_b, hi_b) = box_a, box_b
    return not (
        hi_a[0] + padding <= lo_b[0]
        or hi_b[0] + padding <= lo_a[0]
        or hi_a[1] + padding <= lo_b[1]
        or hi_b[1] + padding <= lo_a[1]
    )


def _avoid_overlap(loaded: Loaded, object_id: str, plate_id: int) -> tuple[float, float] | None:
    """Nudge an object on a plate until its footprint fits next to the others."""
    if loaded.settings is None or loaded.project is None:
        return None
    others = _plate_bboxes(loaded, plate_id, exclude=object_id)
    if not others:
        return None
    lo, hi = loaded.scene.world_bbox(object_id)
    if not any(_overlaps((object_id, lo, hi), box) for box in others):
        return None
    best = _find_free_spot(loaded, object_id, plate_id, padding=PLACEMENT_PADDING_MM)
    if best is None:
        raise ThreeMFError(
            "PLATE_FULL",
            f"盘 {plate_id} 上没有能放下对象 {object_id} 的空位；先移走或删除其他对象",
        )
    origin = loaded.plate_origins().get(plate_id, np.zeros(3))
    current = (lo + hi) / 2.0 - origin
    delta = np.array(best) - current[:2]
    build = loaded.scene.build_transform(object_id) @ translation(float(delta[0]), float(delta[1]), 0.0)
    loaded.scene.set_build_transform(object_id, build)
    return float(best[0]), float(best[1])


def _find_free_spot(
    loaded: Loaded,
    object_id: str,
    plate_id: int,
    padding: float = PLACEMENT_PADDING_MM,
    margin: float = 1.0,
) -> tuple[float, float] | None:
    """Plate-local centre of the closest spot where ``object_id`` clears the rest.

    Read-only: the object is not moved. Used by ``_avoid_overlap`` to place a
    duplicate and by the slice preflight to suggest coordinates to the caller.
    """
    if loaded.settings is None or loaded.project is None:
        return None
    others = _plate_bboxes(loaded, plate_id, exclude=object_id)
    lo, hi = loaded.scene.world_bbox(object_id)
    bed = loaded.bed()
    origin = loaded.plate_origins().get(plate_id, np.zeros(3))
    size = hi - lo
    half_x = (bed[0] - size[0]) / 2.0 - 2.0
    half_y = (bed[1] - size[1]) / 2.0 - 2.0
    if half_x < 0 or half_y < 0:
        raise ThreeMFError("PLATE_TOO_SMALL", "目标盘放不下这个对象")
    local = (lo + hi) / 2.0 - origin
    center = np.array([bed[0] / 2.0, bed[1] / 2.0])
    local_others = [(oid, box_lo - origin, box_hi - origin) for oid, box_lo, box_hi in others]
    best = None
    best_distance = np.inf
    step = 4.0
    for dy in np.arange(-half_y, half_y + 0.1, step):
        for dx in np.arange(-half_x, half_x + 0.1, step):
            candidate = center + np.array([dx, dy])
            shift = candidate - local[:2]
            if float(np.hypot(shift[0], shift[1])) >= best_distance:
                continue
            test_lo = lo - origin + np.array([candidate[0], candidate[1], 0.0]) - local
            test_hi = hi - origin + np.array([candidate[0], candidate[1], 0.0]) - local
            if (
                test_lo[0] < margin
                or test_lo[1] < margin
                or test_hi[0] > bed[0] - margin
                or test_hi[1] > bed[1] - margin
            ):
                continue
            if any(
                _overlaps((object_id, test_lo, test_hi), box, padding)
                for box in local_others
            ):
                continue
            best_distance = float(np.hypot(shift[0], shift[1]))
            best = candidate
    return None if best is None else (float(best[0]), float(best[1]))


def _duplicate(loaded: Loaded, object_id: str, op: dict) -> tuple[str, str]:
    scene = loaded.scene
    root = scene.root_object(object_id)
    if root.inline_mesh is not None:
        raise ThreeMFError("UNSUPPORTED", "内嵌网格的通用 3MF 暂不支持复制")
    entries = [(component.path, component.object_id) for component in root.components]
    new_root = scene.add_root_object(entries)
    build = scene.build_transform(object_id) @ translation(
        float(op.get("dx", 0)), float(op.get("dy", 0)), float(op.get("dz", 0))
    )
    scene.add_build_item(new_root.id, build)
    note = ""
    if loaded.settings is not None:
        plate_id = loaded.settings.plate_of(object_id)
        if plate_id is not None and not any(float(op.get(k, 0)) for k in ("dx", "dy", "dz")):
            spot = _avoid_overlap(loaded, new_root.id, plate_id)
            if spot is not None:
                note = f"，自动避让到 ({spot[0]:.1f}, {spot[1]:.1f})"
        name = loaded.object_name(object_id)
        new_name = str(op.get("name") or f"{name} copy")
        parts = loaded.settings.parts(object_id)
        part_specs = [
            {
                "id": part["id"],
                "subtype": part["subtype"],
                "matrix": "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1",
                "metadata": {
                    key: value
                    for key, value in part["metadata"].items()
                    if key in ("source_file", "source_object_id", "source_volume_id")
                },
            }
            for part in parts
        ] or [{"id": entries[0][1]}]
        loaded.settings.add_object(
            int(new_root.id),
            new_name,
            loaded.object_extruder(object_id),
            part_specs,
        )
        plate = loaded.settings.plate_of(object_id)
        if plate is not None:
            loaded.settings.add_instance(int(new_root.id), plate)
    return new_root.id, note


def _delete(loaded: Loaded, object_id: str) -> None:
    root = loaded.scene.root_objects.get(str(object_id))
    used_paths = {
        component.path.lstrip("/") for component in (root.components if root else [])
    }
    loaded.scene.remove_root_object(object_id)
    if loaded.settings is not None:
        loaded.settings.remove_object(object_id)
    if loaded.layer_root is not None:
        from .settings import remove_layer_range_object

        if remove_layer_range_object(loaded.layer_root, int(object_id)):
            loaded.layer_dirty = True
    # Drop only the object files this object owned and nobody references now.
    referenced = {path for path, _oid in loaded.scene.component_references()}
    for path in used_paths:
        if path in referenced:
            continue
        loaded.scene.drop_object_file(path)


# -------------------------------------------------------------------- mesh ops
def analyze_object(loaded: Loaded, object_selector, params: dict | None = None) -> dict:
    """Read-only printability report for one object; nothing is modified."""
    params = params or {}
    if object_selector is None or not str(object_selector).strip():
        raise ThreeMFError("INVALID_ARGS", "analyze 需要 object（对象 id 或唯一名称）")
    object_id = resolve_object(loaded, object_selector)
    vertices, triangles = loaded.scene.world_triangles(object_id)
    layer_height = None
    if loaded.project is not None:
        raw = loaded.project.get("layer_height")
        try:
            layer_height = float(str(raw).strip()) if raw not in (None, "") else None
        except (TypeError, ValueError):
            layer_height = None
        if layer_height is not None and layer_height <= 0:
            layer_height = None
    report = printability(
        Mesh(vertices, triangles, []),
        layer_height=layer_height,
        overhang_angle=params.get("overhang_angle", 45.0),
    )
    return {
        "objectId": int(object_id),
        "triangles": int(len(triangles)),
        "printability": report,
    }

def apply_mesh(loaded: Loaded, object_selector, op: str, params: dict) -> dict:
    if loaded.is_sliced:
        raise ThreeMFError(
            "SLICED_FILE",
            "这是已切片的 .gcode.3mf，几何不可编辑；请在项目 3mf 上修改后重新切片",
        )
    if op == "import_stl":
        return _import_stl(loaded, params)
    if op == "analyze":
        return analyze_object(loaded, object_selector, params)

    object_id = resolve_object(loaded, object_selector)
    if op == "diagnose":
        vertices, triangles = loaded.scene.world_triangles(object_id)
        report = topology_report(
            Mesh(vertices, triangles, []),
            thin_mm=float(params.get("thin_threshold", 0.01)),
            deep=True,
        )
        return {
            "objectId": int(object_id),
            "triangles": int(len(triangles)),
            "diagnosis": report,
        }
    if op == "section":
        vertices, triangles = loaded.scene.world_triangles(object_id)
        point, normal = _plane(params, vertices)
        result = section_mesh(
            Mesh(vertices, triangles, []),
            point,
            normal,
            max_points=int(params.get("max_points") or 2000),
        )
        return {
            "objectId": int(object_id),
            "triangles": int(len(triangles)),
            "section": result,
        }
    if op == "measure":
        vertices, triangles = loaded.scene.world_triangles(object_id)
        bbox = params.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 6:
            raise ThreeMFError(
                "INVALID_ARGS",
                "measure 需要 bbox: [xmin,ymin,zmin,xmax,ymax,zmax]（世界坐标）",
            )
        mode = str(params.get("mode") or "heightmap")
        if mode == "heightmap":
            result = heightmap(
                Mesh(vertices, triangles, []),
                bbox,
                axis=str(params.get("axis") or "z"),
                grid=int(params.get("grid") or 64),
            )
        elif mode == "spans":
            result = material_spans(
                Mesh(vertices, triangles, []), bbox, axis=str(params.get("axis") or "z")
            )
        elif mode == "extent":
            result = region_extent(Mesh(vertices, triangles, []), bbox)
        else:
            raise ThreeMFError("INVALID_ARGS", "measure 的 mode 必须是 heightmap/spans/extent")
        return {
            "objectId": int(object_id),
            "triangles": int(len(triangles)),
            "measurement": result,
        }
    if op == "components":
        vertices, triangles = loaded.scene.world_triangles(object_id)
        mesh = Mesh(vertices, triangles, [])
        info = component_info(mesh, connected_components(mesh))
        return {
            "objectId": int(object_id),
            "triangles": int(len(triangles)),
            "components": info,
        }
    if op == "export_stl":
        vertices, triangles = loaded.scene.world_triangles(object_id)
        target = params.get("stl") or params.get("output")
        if not target:
            raise ThreeMFError("INVALID_ARGS", "export_stl 需要 stl 路径")
        target = os.path.abspath(target)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(write_stl(Mesh(vertices, triangles, []), os.path.basename(target)))
        return {
            "output": target,
            "triangles": int(len(triangles)),
            "bbox": [float(v) for v in np.concatenate([vertices.min(0), vertices.max(0)])] if len(vertices) else [0.0] * 6,
        }

    sources = loaded.scene.mesh_sources(object_id)
    if not sources:
        raise ThreeMFError("EMPTY_MESH", f"对象 {object_id} 没有网格数据")
    if len(sources) > 1 and not params.get("merge_components"):
        raise ThreeMFError(
            "MULTI_COMPONENT",
            f"对象 {object_id} 由 {len(sources)} 个部件组成；几何编辑会合并它们，请显式传 merge_components=true",
            {"components": len(sources)},
        )

    world = loaded.scene.world_triangles(object_id)
    vertices, triangles = world[0].copy(), world[1].copy()
    bbox_before = (
        [float(v) for v in np.concatenate([vertices.min(0), vertices.max(0)])]
        if len(vertices)
        else [0.0] * 6
    )
    quality_before = mesh_quality(Mesh(vertices, triangles, []))
    triangles_before = int(len(triangles))
    vertices_before = int(len(vertices))
    stats: dict = {"op": op}

    transform_ops = ("scale", "rotate", "mirror", "translate")
    select_spec = params.get("select")
    triangle_mask: np.ndarray | None = None
    moved_vertices: np.ndarray | None = None
    selection_center: np.ndarray | None = None
    if select_spec:
        if op not in transform_ops:
            raise ThreeMFError(
                "INVALID_ARGS",
                f"select 只适用于 {'/'.join(transform_ops)}；{op} 会改写整块网格，"
                "请改用 scale/rotate/mirror/translate",
            )
        triangle_mask = _selection_mask(vertices, triangles, select_spec)
        picked = vertices[np.unique(triangles[triangle_mask].ravel())]
        sel_lo, sel_hi = picked.min(axis=0), picked.max(axis=0)
        selection_center = (sel_lo + sel_hi) / 2.0
        seam = selection_seam_edges(triangles, triangle_mask)
        vertices, triangles, moved_vertices = isolate_triangles(
            vertices, triangles, triangle_mask
        )
        stats["selection"] = {
            "triangles": int(triangle_mask.sum()),
            "size": [round(float(v), 4) for v in (sel_hi - sel_lo)],
            "bbox": [round(float(v), 4) for v in np.concatenate([sel_lo, sel_hi])],
            "center": [round(float(v), 4) for v in selection_center],
            "seamEdges": seam,
        }

    def movable() -> np.ndarray:
        if moved_vertices is None:
            return np.ones(len(vertices), dtype=bool)
        return moved_vertices

    pivot_mode = params.get("pivot") or ("selection" if selection_center is not None else "center")
    if selection_center is not None:
        stats["pivotMode"] = pivot_mode

    if op == "bake":
        pass
    elif op == "translate":
        delta = np.array([float(params.get("dx", 0)), float(params.get("dy", 0)), float(params.get("dz", 0))])
        moving = movable()
        vertices[moving] = vertices[moving] + delta
    elif op == "scale":
        factors = params.get("scale", params.get("factor", 1.0))
        if isinstance(factors, (int, float)):
            factors = [float(factors)] * 3
        factors = np.array([float(v) for v in factors], dtype=np.float64)
        if np.any(factors <= 0):
            raise ThreeMFError("INVALID_ARGS", "缩放系数必须为正数")
        pivot = _pivot(loaded, object_id, pivot_mode, params.get("pivotPoint"), selection_center)
        moving = movable()
        vertices[moving] = (vertices[moving] - pivot) * factors + pivot
        stats["scale"] = factors.tolist()
    elif op == "rotate":
        axis = str(params.get("axis", "z"))
        angle = float(params.get("angle", 0))
        from .model import rotation_axis

        pivot = _pivot(loaded, object_id, pivot_mode, params.get("pivotPoint"), selection_center)
        matrix = rotation_axis(axis, angle)
        moving = movable()
        vertices[moving] = (vertices[moving] - pivot) @ matrix + pivot
        stats["axis"], stats["angle"] = axis, angle
    elif op == "mirror":
        axis = str(params.get("axis", "x"))
        if selection_center is not None:
            center = selection_center
        else:
            lo, hi = vertices.min(0), vertices.max(0)
            center = (lo + hi) / 2.0
        matrix = mirror_matrix(axis)
        moving = movable()
        vertices[moving] = (vertices[moving] - center) @ matrix + center
        triangles = triangles.copy()
        if triangle_mask is None:
            triangles = triangles[:, [0, 2, 1]]
        else:
            triangles[triangle_mask] = triangles[triangle_mask][:, [0, 2, 1]]
        stats["axis"] = axis
    elif op == "weld":
        mesh = cleanup(Mesh(vertices, triangles, []), float(params.get("tolerance", 1e-5)))[0]
        vertices, triangles = mesh.vertices, mesh.triangles
    elif op == "cleanup":
        mesh, quality = cleanup(Mesh(vertices, triangles, []), float(params.get("tolerance", 1e-5)))
        vertices, triangles = mesh.vertices, mesh.triangles
        stats["quality"] = quality
    elif op == "plane_cut":
        point, normal = _plane(params, vertices)
        keep = str(params.get("keep", "negative"))
        cap = bool(params.get("cap", True))
        # Cutting exactly along existing geometry leaves coplanar slivers; nudge
        # the plane by a thousandth of a millimetre when that happens.
        attempts = [(0.0, point)]
        if cap:
            attempts += [(1e-3, point + normal * 1e-3), (-1e-3, point - normal * 1e-3)]
        chosen = None
        last_error = None
        for shift, shifted_point in attempts:
            try:
                candidate = plane_cut(
                    Mesh(vertices, triangles, []),
                    shifted_point,
                    normal,
                    keep=keep,
                    cap=cap,
                )
            except ThreeMFError as exc:
                last_error = exc
                continue
            if not cap or candidate.boundary_edges_after <= candidate.boundary_edges_before:
                chosen = (candidate, shift)
                break
            last_error = ThreeMFError(
                "CAP_FAILED",
                f"切割后残留 {candidate.boundary_edges_after} 条开放边（切割前 "
                f"{candidate.boundary_edges_before} 条），已放弃",
            )
        if chosen is None:
            raise last_error or ThreeMFError("CAP_FAILED", "平面切割失败")
        result, shift = chosen
        vertices, triangles = result.mesh.vertices, result.mesh.triangles
        stats.update(
            {
                "keep": keep,
                "planeShift": shift,
                "clippedTriangles": result.clipped_triangles,
                "capTriangles": result.cap_triangles,
                "loops": result.loops,
                "capFallback": bool(result.cap_fallback),
                "boundaryBefore": result.boundary_edges_before,
                "boundaryAfter": result.boundary_edges_after,
            }
        )
    elif op == "flip":
        triangles = triangles[:, [0, 2, 1]]
    elif op == "stretch":
        axis = str(params.get("axis", "x"))
        axis_index = {"x": 0, "y": 1, "z": 2}.get(axis)
        if axis_index is None:
            raise ThreeMFError("INVALID_ARGS", "stretch 的 axis 必须是 x/y/z")
        band = params.get("band") or params.get("planes")
        if not isinstance(band, (list, tuple)) or len(band) != 2:
            raise ThreeMFError(
                "INVALID_ARGS",
                "stretch 需要 band: [low, high]（世界坐标，单位 mm），区间内部按比例拉长",
            )
        low, high = float(band[0]), float(band[1])
        if params.get("delta") is not None:
            delta = float(params["delta"])
        elif params.get("amount") is not None:
            delta = float(params["amount"])
        else:
            raise ThreeMFError("INVALID_ARGS", "stretch 需要 delta（该轴方向增加的长度，mm）")
        share = params.get("negative_share")
        if share is None:
            share = 0.5
        # Vertices already sitting on a band plane mean the stretch lands on an
        # existing face; those edges become a crease rather than a clean run.
        before = vertices[:, axis_index]
        on_plane = bool(
            np.isclose(before, low, atol=1e-6).any() or np.isclose(before, high, atol=1e-6).any()
        )
        mesh_stretch, stretch_stats = stretch_band(
            Mesh(vertices, triangles, []), axis_index, low, high, delta, float(share)
        )
        vertices, triangles = mesh_stretch.vertices, mesh_stretch.triangles
        if on_plane:
            stretch_stats["coincidentWithFace"] = True
        stats["stretch"] = stretch_stats
    else:
        raise ThreeMFError("INVALID_ARGS", f"未知网格操作：{op}")
    if moved_vertices is not None:
        final = vertices[moved_vertices]
        final_lo, final_hi = final.min(axis=0), final.max(axis=0)
        stats["selection"]["sizeAfter"] = [
            round(float(v), 4) for v in (final_hi - final_lo)
        ]
        stats["selection"]["centerAfter"] = [
            round(float(v), 4) for v in ((final_lo + final_hi) / 2.0)
        ]

    mesh = Mesh(vertices, triangles, [])
    if op in ("weld", "cleanup"):
        mesh = cleanup(mesh, float(params.get("weldTolerance", 1e-5)))[0]
    normalized, offset = recentered(mesh)
    install = _install_mesh(loaded, object_id, normalized, offset)
    quality_after = mesh_quality(normalized)
    moved = normalized.vertices + offset
    bbox_after = (
        [float(v) for v in np.concatenate([moved.min(0), moved.max(0)])]
        if len(moved)
        else [0.0] * 6
    )
    return {
        "objectId": int(object_id),
        "bboxBefore": bbox_before,
        "bboxAfter": bbox_after,
        "op": op,
        "trianglesBefore": triangles_before,
        "trianglesAfter": int(len(normalized.triangles)),
        "verticesBefore": vertices_before,
        "verticesAfter": int(len(normalized.vertices)),
        "qualityBefore": quality_before,
        "qualityAfter": quality_after,
        "stats": stats,
        "install": install,
    }


# --------------------------------------------------------------- constructive ops
def _part_count(loaded: Loaded, object_id: str) -> int:
    try:
        return len(loaded.scene.mesh_sources(object_id))
    except ThreeMFError:
        return 0


def _require_single_part(loaded: Loaded, object_id: str, params: dict, op: str) -> int:
    parts = _part_count(loaded, object_id)
    if parts > 1 and not params.get("merge_parts"):
        raise ThreeMFError(
            "MULTI_COMPONENT",
            f"对象 {object_id} 由 {parts} 个部件组成；{op} 会把它们合并成一个部件。"
            "确认要合并请传 merge_parts=true",
            {"components": parts},
        )
    return parts


def _default_primitive_center(loaded: Loaded, mesh: Mesh) -> np.ndarray:
    lo, hi = mesh.bbox()
    bed = loaded.bed()
    origin = loaded.plate_origins().get(1, np.zeros(3))
    return np.array(
        [
            float(origin[0]) + float(bed[0]) / 2.0 - float(lo[0] + hi[0]) / 2.0,
            float(origin[1]) + float(bed[1]) / 2.0 - float(lo[1] + hi[1]) / 2.0,
            -float(lo[2]),
        ]
    )


def _primitive_mesh(loaded: Loaded, spec: dict) -> Mesh:
    if not isinstance(spec, dict):
        raise ThreeMFError("INVALID_ARGS", "primitive 必须是对象，例如 {\"shape\":\"box\"}")
    shape = str(spec.get("shape") or "")
    center = spec.get("center")
    if shape == "box":
        size = spec.get("size")
        if not isinstance(size, (list, tuple)) or len(size) != 3:
            raise ThreeMFError("INVALID_ARGS", "box 需要 size: [x,y,z]")
        base = primitive_box([float(value) for value in size], [0.0, 0.0, 0.0])
    elif shape == "cylinder":
        radius = spec.get("radius")
        height = spec.get("height")
        if radius is None or height is None:
            raise ThreeMFError("INVALID_ARGS", "cylinder 需要 radius 和 height")
        base = primitive_cylinder(
            float(radius),
            float(height),
            str(spec.get("axis") or "z"),
            [0.0, 0.0, 0.0],
            int(spec.get("segments") or 64),
        )
    elif shape == "sphere":
        radius = spec.get("radius")
        if radius is None:
            raise ThreeMFError("INVALID_ARGS", "sphere 需要 radius")
        base = primitive_sphere(
            float(radius), [0.0, 0.0, 0.0], int(spec.get("segments") or 48)
        )
    else:
        raise ThreeMFError("INVALID_ARGS", "primitive 的 shape 必须是 box/cylinder/sphere")
    if center is None:
        center = _default_primitive_center(loaded, base)
    else:
        center = np.asarray([float(value) for value in center], dtype=np.float64)
        if center.shape != (3,):
            raise ThreeMFError("INVALID_ARGS", "primitive.center 需要三个坐标")
    moved = Mesh(
        base.vertices + center,
        base.triangles.copy(),
        [dict(attrs) for attrs in base.tri_attrs],
    )
    return moved


def _add_object_from_mesh(
    loaded: Loaded,
    mesh: Mesh,
    *,
    name: str,
    plate: int = 1,
    extruder: int = 1,
    source_object: str | None = None,
) -> dict:
    """Create a root object from a world-space mesh, preserving its position."""
    normalized, offset = recentered(mesh)
    scene = loaded.scene
    path = _new_object_path(scene)
    embedded = _next_embedded_id(scene)
    _new_object_file(scene, path, normalized, embedded)
    root = scene.add_root_object([("/" + path, embedded)])
    root.element.set("name", str(name))
    scene.add_build_item(root.id, translation(float(offset[0]), float(offset[1]), float(offset[2])))
    scene.ensure_rels([path])
    if loaded.settings is not None:
        loaded.settings.add_object(
            int(root.id),
            str(name),
            int(extruder),
            [{"id": embedded, "mesh_stat": mesh.triangle_count}],
        )
        loaded.settings.add_instance(int(root.id), int(plate))
        if source_object is not None:
            meta = loaded.settings.object_meta(source_object)
            for key, value in meta.items():
                if key in ("name", "identify_id", "face_count"):
                    continue
                loaded.settings.set_object_meta(root.id, key, str(value))
    return {
        "objectId": int(root.id),
        "path": path,
        "embeddedId": embedded,
        "offset": [float(value) for value in offset],
    }


def _object_world_mesh(loaded: Loaded, selector) -> tuple[str, np.ndarray, np.ndarray]:
    object_id = resolve_object(loaded, selector)
    vertices, triangles = loaded.scene.world_triangles(object_id)
    return object_id, vertices, triangles


def _operand_mesh(loaded: Loaded, spec, target_id: str) -> dict:
    if not isinstance(spec, dict):
        raise ThreeMFError(
            "INVALID_ARGS",
            "with 必须是 {object} / {stl} / {primitive} 之一",
        )
    if spec.get("object") is not None:
        object_id, vertices, triangles = _object_world_mesh(loaded, spec.get("object"))
        return {
            "kind": "object",
            "objectId": object_id,
            "vertices": vertices,
            "triangles": triangles,
        }
    if spec.get("stl"):
        stl_path = os.path.abspath(str(spec.get("stl")))
        if not os.path.isfile(stl_path):
            raise ThreeMFError("NOT_FOUND", f"STL 不存在：{stl_path}")
        with open(stl_path, "rb") as handle:
            mesh = read_stl(handle.read())
        translate = spec.get("translate")
        if translate is not None:
            delta = np.asarray([float(value) for value in translate], dtype=np.float64)
            if delta.shape != (3,):
                raise ThreeMFError("INVALID_ARGS", "stl.translate 需要三个坐标")
            mesh = Mesh(mesh.vertices + delta, mesh.triangles.copy(), [dict(a) for a in mesh.tri_attrs])
        return {
            "kind": "stl",
            "path": stl_path,
            "vertices": mesh.vertices,
            "triangles": mesh.triangles,
        }
    if spec.get("primitive") is not None:
        mesh = _primitive_mesh(loaded, spec.get("primitive") or {})
        return {
            "kind": "primitive",
            "shape": str((spec.get("primitive") or {}).get("shape") or ""),
            "mesh": mesh,
            "vertices": mesh.vertices,
            "triangles": mesh.triangles,
        }
    raise ThreeMFError("INVALID_ARGS", "with 需要 object、stl 或 primitive 之一")


def _prepare_operand(vertices: np.ndarray, triangles: np.ndarray, label: str, repair: str) -> tuple[np.ndarray, np.ndarray, dict]:
    mesh = Mesh(vertices, triangles, [])
    quality_before = topology_report(mesh)
    if repair != "none":
        mesh, _changes = cleanup(mesh, float(1e-5))
    quality = topology_report(mesh)
    if not quality["watertight"]:
        raise ThreeMFError(
            "BOOLEAN_OPERAND_OPEN",
            f"{label} 不是闭合实体（边界边 {quality['boundaryEdges']}、"
            f"非流形边 {quality['nonManifoldEdges']}），无法做布尔",
            {
                "boundaryEdges": int(quality["boundaryEdges"]),
                "nonManifoldEdges": int(quality["nonManifoldEdges"]),
                "before": quality_before,
            },
        )
    if not quality["orientationConsistent"]:
        raise ThreeMFError(
            "BOOLEAN_OPERAND_OPEN",
            f"{label} 的三角形绕向不一致，无法做布尔；请先修复法向",
            {"orientationConsistent": False},
        )
    return mesh.vertices, mesh.triangles, quality


def _build_boolean(loaded: Loaded, raw: dict) -> dict:
    object_id = resolve_object(loaded, raw.get("object", raw.get("objectId")))
    operation = str(raw.get("operation") or "")
    if operation not in ("union", "subtract", "intersect"):
        raise ThreeMFError("INVALID_ARGS", "boolean.operation 必须是 union/subtract/intersect")
    parts = _require_single_part(loaded, object_id, raw, "布尔")
    _target_id, vertices, triangles = _object_world_mesh(loaded, object_id)
    operand = _operand_mesh(loaded, raw.get("with"), object_id)
    repair = str(raw.get("repair") or "weld")
    if repair not in ("none", "weld"):
        raise ThreeMFError("INVALID_ARGS", "boolean.repair 必须是 none 或 weld")
    target_v, target_t, target_quality = _prepare_operand(vertices, triangles, "目标对象", repair)
    operand_v, operand_t, operand_quality = _prepare_operand(
        operand["vertices"], operand["triangles"], "操作数", repair
    )

    from . import csg

    result = csg.boolean(target_v, target_t, operand_v, operand_t, operation)
    result_mesh, _cleanup_stats = cleanup(
        Mesh(result["vertices"], result["triangles"], []), 1e-5
    )
    result_report = topology_report(result_mesh, deep=True)
    normalized, offset = recentered(result_mesh)
    install = _install_mesh(loaded, object_id, normalized, offset)

    warnings: list[dict] = []
    intersection = float(result["stats"].get("intersectionVolumeMm3") or 0.0)
    if operation == "union" and intersection <= 1e-6:
        warnings.append(
            {
                "severity": "warning",
                "code": "BOOLEAN_TOUCH_ONLY",
                "message": "并集的两个实体没有真实体积重叠（交集体积为 0，可能只是贴面或分离）："
                "结果可能仍是两块，Bambu 可能把它识别为独立部件。"
                "让操作数嵌入目标 1–2 mm 后再并集",
                "objectId": int(object_id),
            }
        )
    if operation == "subtract" and intersection <= 1e-6:
        warnings.append(
            {
                "severity": "warning",
                "code": "BOOLEAN_NO_EFFECT",
                "message": "减法没有切到任何材料（交集体积为 0），几何未改变",
                "objectId": int(object_id),
            }
        )
    if result_report["shells"] > 1:
        warnings.append(
            {
                "severity": "warning",
                "code": "BOOLEAN_DISCONNECTED",
                "message": f"布尔结果包含 {result_report['shells']} 个互不连通的壳；"
                "Bambu 可能拆成多个部件",
                "objectId": int(object_id),
            }
        )
    internal_faces = (
        result_report["coincidentFacePairs"]
        + result_report["duplicateFaces"]
        + result_report["opposingFaces"]
    )
    if internal_faces:
        warnings.append(
            {
                "severity": "warning",
                "code": "COINCIDENT_FACES",
                "message": f"布尔结果含 {internal_faces} 处共面内部面/重复面；"
                "虽然可能水密，但 Bambu 可能把它视为两个部件",
                "objectId": int(object_id),
            }
        )
    if not result_report["orientationConsistent"] or not result_report["watertight"]:
        warnings.append(
            {
                "severity": "error",
                "code": "BOOLEAN_RESULT_INVALID",
                "message": "布尔结果不是闭合且绕向一致的实体",
                "objectId": int(object_id),
            }
        )

    operand_object = None
    if raw.get("keep_operand"):
        if operand["kind"] == "object":
            operand_object = int(operand["objectId"])
        else:
            created = _add_object_from_mesh(
                loaded,
                Mesh(operand_v, operand_t, []),
                name=str(raw.get("operand_name") or f"{loaded.object_name(object_id)}_operand"),
                plate=int(loaded.settings.plate_of(object_id) if loaded.settings else 1) or 1,
                extruder=loaded.object_extruder(object_id),
            )
            operand_object = created["objectId"]

    return {
        "op": "boolean",
        "objectId": int(object_id),
        "operation": operation,
        "operandKind": operand["kind"],
        "operandObjectId": operand_object,
        "intersectionVolumeMm3": round(intersection, 3),
        "targetVolumeMm3": result["stats"].get("targetVolumeMm3"),
        "operandVolumeMm3": result["stats"].get("operandVolumeMm3"),
        "resultVolumeMm3": result_report["volumeMm3"],
        "resultShells": result_report["shells"],
        "resultWatertight": result_report["watertight"],
        "resultOrientationConsistent": result_report["orientationConsistent"],
        "triangles": result_report["triangles"],
        "partsBefore": parts,
        "partsCollapsed": parts if parts > 1 else 0,
        "detachedMesh": bool(install.get("detached")),
        "transformBaked": True,
        "warnings": warnings,
    }


def _cut_side(mesh: Mesh, point: np.ndarray, normal: np.ndarray, keep: str) -> Mesh:
    attempts = [0.0, 1e-3, -1e-3]
    last_error = None
    for shift in attempts:
        try:
            candidate = plane_cut(mesh, point + normal * shift, normal, keep=keep, cap=True)
        except ThreeMFError as exc:
            last_error = exc
            continue
        if candidate.mesh.is_empty():
            continue
        if candidate.boundary_edges_after <= candidate.boundary_edges_before:
            return candidate.mesh
        last_error = ThreeMFError(
            "SPLIT_FAILED",
            f"拆分封盖后残留 {candidate.boundary_edges_after} 条开放边",
        )
    raise last_error or ThreeMFError("SPLIT_FAILED", "拆分失败")


def _build_split(loaded: Loaded, raw: dict) -> dict:
    object_id = resolve_object(loaded, raw.get("object", raw.get("objectId")))
    parts = _require_single_part(loaded, object_id, raw, "拆分")
    _target_id, vertices, triangles = _object_world_mesh(loaded, object_id)
    point, normal = _plane(raw, vertices)
    normal = np.asarray(normal, dtype=np.float64)
    length = float(np.linalg.norm(normal))
    if length < 1e-12:
        raise ThreeMFError("INVALID_ARGS", "拆分平面法向不能为零")
    normal = normal / length
    mesh = Mesh(vertices, triangles, [])

    negative = positive = None
    try:
        from . import csg

        split = csg.split_plane(vertices, triangles, point, normal)
        if split is not None:
            negative_v, negative_t = split[0]
            positive_v, positive_t = split[1]
            negative = Mesh(negative_v, negative_t, []) if len(negative_t) else None
            positive = Mesh(positive_v, positive_t, []) if len(positive_t) else None
    except ThreeMFError as exc:
        if exc.code not in (
            "BOOLEAN_ENGINE_MISSING",
            "SPLIT_FAILED",
            "BOOLEAN_OPERAND_OPEN",
            "BOOLEAN_FAILED",
        ):
            raise
        negative = positive = None
    if negative is None or positive is None:
        negative = _cut_side(mesh, point, normal, "negative")
        positive = _cut_side(mesh, point, normal, "positive")
    if negative.is_empty() or positive.is_empty():
        raise ThreeMFError(
            "SPLIT_EMPTY_SIDE",
            "拆分平面没有同时穿过两半（有一侧为空）；请检查平面位置",
            {"objectId": int(object_id)},
        )

    gap = float(raw.get("gap") or 0.0)
    if gap < 0:
        raise ThreeMFError("INVALID_ARGS", "split.gap 不能为负")
    if gap > 0:
        negative = Mesh(
            negative.vertices - normal * (gap / 2.0), negative.triangles.copy(), []
        )
        positive = Mesh(
            positive.vertices + normal * (gap / 2.0), positive.triangles.copy(), []
        )

    names = raw.get("names")
    base_name = loaded.object_name(object_id)
    if names is None:
        names = [f"{base_name}_A", f"{base_name}_B"]
    if not isinstance(names, (list, tuple)) or len(names) != 2:
        raise ThreeMFError("INVALID_ARGS", "split.names 需要两个名称：[负侧, 正侧]")
    plate = 1
    if loaded.settings is not None:
        plate = int(loaded.settings.plate_of(object_id) or 1)
    extruder = loaded.object_extruder(object_id)
    negative_object = _add_object_from_mesh(
        loaded,
        negative,
        name=str(names[0]),
        plate=plate,
        extruder=extruder,
        source_object=object_id,
    )
    positive_object = _add_object_from_mesh(
        loaded,
        positive,
        name=str(names[1]),
        plate=plate,
        extruder=extruder,
        source_object=object_id,
    )
    if not raw.get("keep_original"):
        _delete(loaded, object_id)

    warnings = []
    negative_report = topology_report(negative)
    positive_report = topology_report(positive)
    if negative_report["shells"] > 1 or positive_report["shells"] > 1:
        warnings.append(
            {
                "severity": "warning",
                "code": "SPLIT_MULTI_SHELL",
                "message": f"拆分结果含多个壳（负侧 {negative_report['shells']}、"
                f"正侧 {positive_report['shells']}）；Bambu 可能继续拆件",
                "objectId": int(object_id),
            }
        )
    if raw.get("keep_original"):
        warnings.append(
            {
                "severity": "info",
                "code": "SPLIT_ORIGINAL_KEPT",
                "message": "已保留原对象；两半与原对象在同一位置重叠，仅用于对比",
                "objectId": int(object_id),
            }
        )
    if gap == 0 and not raw.get("keep_original"):
        warnings.append(
            {
                "severity": "info",
                "code": "SPLIT_ZERO_GAP",
                "message": "两半切面重合（gap=0）；若同盘打印，Bambu 可能报重叠。"
                "需要分开时可传 gap（mm）",
            }
        )
    return {
        "op": "split",
        "objectId": int(object_id),
        "negativeObjectId": negative_object["objectId"],
        "positiveObjectId": positive_object["objectId"],
        "negativeVolumeMm3": negative_report["volumeMm3"],
        "positiveVolumeMm3": positive_report["volumeMm3"],
        "gap": round(gap, 4),
        "partsBefore": parts,
        "partsCollapsed": parts if parts > 1 else 0,
        "detachedMesh": False,
        "transformBaked": True,
        "warnings": warnings,
    }


def _build_replace(loaded: Loaded, raw: dict) -> dict:
    object_id = resolve_object(loaded, raw.get("object", raw.get("objectId")))
    parts = _require_single_part(loaded, object_id, raw, "替换")
    target_vertices, target_triangles = loaded.scene.world_triangles(object_id)
    target_quality = topology_report(
        Mesh(target_vertices, target_triangles, [])
    )
    operand = _operand_mesh(loaded, raw.get("source"), object_id)
    vertices, triangles = operand["vertices"], operand["triangles"]
    mesh = Mesh(vertices, triangles, [])
    normalized, offset = recentered(mesh)
    install = _install_mesh(loaded, object_id, normalized, offset)
    quality_after = topology_report(normalized)
    warnings = []
    if not quality_after["watertight"]:
        warnings.append(
            {
                "severity": "warning",
                "code": "OPEN_MESH",
                "message": f"替换后的网格不闭合（边界边 {quality_after['boundaryEdges']}），"
                "体积与切片结果可能不可靠",
                "objectId": int(object_id),
            }
        )
    return {
        "op": "replace",
        "objectId": int(object_id),
        "sourceKind": operand["kind"],
        "triangles": quality_after["triangles"],
        "volumeMm3": quality_after["volumeMm3"],
        "watertight": quality_after["watertight"],
        "qualityBefore": target_quality,
        "partsBefore": parts,
        "partsCollapsed": parts if parts > 1 else 0,
        "detachedMesh": bool(install.get("detached")),
        "transformBaked": True,
        "warnings": warnings,
    }


def apply_build(loaded: Loaded, ops: list[dict]) -> tuple[list[str], list[dict]]:
    if loaded.is_sliced:
        raise ThreeMFError(
            "SLICED_FILE",
            "这是已切片的 .gcode.3mf，几何不可编辑；请在项目 3mf 上修改后重新切片",
        )
    if not ops:
        raise ThreeMFError("INVALID_ARGS", "build 的 ops 不能为空")
    logs: list[str] = []
    stats: list[dict] = []
    for index, raw in enumerate(ops):
        if not isinstance(raw, dict) or "op" not in raw:
            raise ThreeMFError("INVALID_ARGS", "每个 build 操作都必须是带 op 字段的对象")
        op = str(raw.get("op"))
        if op == "primitive":
            spec = raw.get("primitive") or raw
            mesh = _primitive_mesh(loaded, spec)
            quality = topology_report(mesh)
            created = _add_object_from_mesh(
                loaded,
                mesh,
                name=str(raw.get("name") or f"Primitive_{index + 1}"),
                plate=int(raw.get("plate") or 1),
                extruder=int(raw.get("extruder") or 1),
            )
            info = {
                "op": "primitive",
                "index": index,
                "createdObjectId": created["objectId"],
                "shape": str(spec.get("shape") or ""),
                "triangles": quality["triangles"],
                "volumeMm3": quality["volumeMm3"],
                "watertight": quality["watertight"],
                "warnings": [],
            }
            logs.append(
                f"基本体 {info['shape']} → 对象 {created['objectId']}"
                f"（{quality['triangles']} 面，体积 {quality['volumeMm3']} mm³）"
            )
        elif op == "boolean":
            info = _build_boolean(loaded, raw)
            logs.append(
                f"布尔 {info['operation']}：对象 {info['objectId']} "
                f"{info['targetVolumeMm3']} → {info['resultVolumeMm3']} mm³"
                f"（交集体积 {info['intersectionVolumeMm3']} mm³）"
            )
        elif op == "split":
            info = _build_split(loaded, raw)
            info["index"] = index
            logs.append(
                f"拆分对象 {info['objectId']} → {info['negativeObjectId']} / "
                f"{info['positiveObjectId']}（gap {info['gap']} mm）"
            )
        elif op == "replace":
            info = _build_replace(loaded, raw)
            logs.append(
                f"替换对象 {info['objectId']} 的几何：{info['triangles']} 面，"
                f"体积 {info['volumeMm3']} mm³"
            )
        else:
            raise ThreeMFError("INVALID_ARGS", f"未知 build 操作：{op}")
        info["index"] = index
        stats.append(info)
    return logs, stats


def _selection_mask(vertices: np.ndarray, triangles: np.ndarray, spec) -> np.ndarray:
    """Per-triangle mask for ``select``: connected-component index and/or world bbox."""
    if not isinstance(spec, dict):
        raise ThreeMFError("INVALID_ARGS", 'select 必须是对象，例如 {"component": 0}')
    mask = np.zeros(len(triangles), dtype=bool)
    matched = False
    if spec.get("component") is not None:
        wanted = spec.get("component")
        if isinstance(wanted, (int, float, str)):
            wanted = [wanted]
        try:
            indices = sorted({int(item) for item in wanted})
        except (TypeError, ValueError) as exc:
            raise ThreeMFError(
                "INVALID_ARGS", "select.component 必须是整数或整数数组"
            ) from exc
        labels = connected_components(Mesh(vertices, triangles, []))
        known = sorted(int(value) for value in np.unique(labels)) if len(labels) else []
        missing = [index for index in indices if index not in known]
        if missing:
            raise ThreeMFError(
                "MISSING_COMPONENT",
                f"对象没有连通体 {missing}；可用索引 0..{known[-1] if known else -1}"
                f"（共 {len(known)} 个）",
                {"components": known},
            )
        for index in indices:
            mask |= labels == index
        matched = True
    box = spec.get("bbox")
    if isinstance(box, dict):
        low = np.array([float(v) for v in (box.get("min") or [-np.inf] * 3)])
        high = np.array([float(v) for v in (box.get("max") or [np.inf] * 3)])
        if len(low) != 3 or len(high) != 3:
            raise ThreeMFError("INVALID_ARGS", "select.bbox.min/max 需要三个坐标")
        centroids = vertices[triangles].mean(axis=1)
        mask |= np.all((centroids >= low) & (centroids <= high), axis=1)
        matched = True
    if not matched:
        raise ThreeMFError("INVALID_ARGS", "select 需要 component 或 bbox")
    if not mask.any():
        raise ThreeMFError(
            "EMPTY_SELECTION", "选区没有命中任何三角形，请检查 component 索引或 bbox 范围"
        )
    return mask


def selection_seam_edges(triangles: np.ndarray, mask: np.ndarray) -> int:
    """Edges shared by a selected and an unselected triangle.

    Moving a selection that shares edges with the rest of the shell must tear the
    surface open along those edges: the two sides stop coinciding. Counting them
    lets the caller say so instead of returning a silently broken mesh.
    """
    if not mask.any() or mask.all():
        return 0

    def edges_of(subset: np.ndarray) -> set[tuple[int, int]]:
        found: set[tuple[int, int]] = set()
        for triangle in subset:
            for first, second in (
                (triangle[0], triangle[1]),
                (triangle[1], triangle[2]),
                (triangle[2], triangle[0]),
            ):
                found.add((min(int(first), int(second)), max(int(first), int(second))))
        return found

    return len(edges_of(triangles[mask]) & edges_of(triangles[~mask]))


def _plane(params: dict, vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    plane = params.get("plane")
    if isinstance(plane, dict) and "point" in plane and "normal" in plane:
        point = np.array([float(v) for v in plane["point"]], dtype=np.float64)
        normal = np.array([float(v) for v in plane["normal"]], dtype=np.float64)
        return point, normal
    axis = None
    value = None
    if isinstance(plane, dict):
        axis = str(plane.get("axis", "z"))
        value = plane.get("value", plane.get("offset"))
    else:
        axis = str(params.get("axis", "z"))
        value = params.get("planeZ", params.get("value"))
    index = {"x": 0, "y": 1, "z": 2}.get(axis)
    if index is None:
        raise ThreeMFError("INVALID_ARGS", "切割平面轴必须是 x/y/z")
    if value is None:
        raise ThreeMFError("INVALID_ARGS", "平面切割需要 value（世界坐标）")
    normal = np.zeros(3)
    normal[index] = 1.0
    point = np.zeros(3)
    point[index] = float(value)
    return point, normal


def _install_mesh(
    loaded: Loaded, object_id: str, mesh: Mesh, offset: np.ndarray
) -> dict:
    """Write a new mesh for one object and report whether it had to be detached.

    A mesh object shared by several root objects is never edited in place: the
    target object gets its own new object file, so siblings keep their original
    geometry. That is the safe default, but it is invisible unless reported, so
    the result carries ``detached`` and the sibling ids.
    """
    scene = loaded.scene
    root = scene.root_object(object_id)
    if root.inline_mesh is not None:
        # A core 3MF can keep the mesh inside the root object. Rewiring it to a
        # component that points back at 3D/3dmodel.model would be a self
        # reference; edit the inline mesh in place instead.
        set_mesh(root.element, mesh)
        root.inline_mesh = mesh
        scene._root_dirty = True
        scene.set_build_transform(
            object_id,
            translation(float(offset[0]), float(offset[1]), float(offset[2])),
        )
        if loaded.settings is not None:
            _reset_parts(loaded, object_id, int(object_id), mesh.triangle_count)
        return {
            "detached": False,
            "siblings": [],
            "targetFile": ROOT_MODEL,
            "targetObjectId": int(object_id),
        }
    refs = scene.component_references()
    sources = scene.mesh_sources(object_id)
    old_paths = {path.lstrip("/") for path, _oid, _m, _mat in sources}

    target_path = None
    target_oid = None
    siblings: list[int] = []
    if len(sources) == 1:
        path, oid, _m, _mat = sources[0]
        sharers = refs.get((path.lstrip("/"), oid), [])
        if len(sharers) > 1:
            siblings = [int(other) for other in sharers if str(other) != str(object_id)]
        else:
            target_path, target_oid = path.lstrip("/"), str(oid)

    if target_path is None:
        target_path = _new_object_path(scene)
        target_oid = _next_embedded_id(scene)
        object_file = _new_object_file(scene, target_path, mesh, target_oid)
    else:
        object_file = scene.object_file(target_path)
        target = object_file.objects.get(str(target_oid))
        if target is None:
            raise ThreeMFError("MISSING_MESH_OBJECT", f"{target_path}#{target_oid} 不存在")
        set_mesh(target.element, mesh)
        target.mesh = mesh
        object_file.dirty = True
        scene.mark_file_dirty(target_path)

    # Point the root object at exactly one component and remember the offset.
    for component in list(root.components):
        component.element.getparent().remove(component.element)
    root.components.clear()
    components_el = root.element.find(qn("components"))
    if components_el is None:
        components_el = etree.SubElement(root.element, qn("components"))
    component_el = etree.SubElement(components_el, qn("component"))
    component_el.set(pqn("path"), "/" + target_path)
    component_el.set("objectid", str(target_oid))
    component_el.set(pqn("UUID"), _uuid())
    component_el.set("transform", format_transform(IDENTITY))
    from .model import ComponentRef

    root.components.append(ComponentRef(component_el, str(target_oid), "/" + target_path, IDENTITY.copy()))
    scene._root_dirty = True
    scene.ensure_rels([target_path])

    # Drop the object files this rewiring orphaned (never pre-existing orphans).
    still_used = {path for path, _oid in scene.component_references()}
    for path in old_paths:
        if path in still_used:
            continue
        scene.drop_object_file(path)

    # Keep the world position: the normalized mesh is centred, so translate back.
    build = translation(float(offset[0]), float(offset[1]), float(offset[2]))
    scene.set_build_transform(object_id, build)

    if loaded.settings is not None:
        _reset_parts(loaded, object_id, int(target_oid), mesh.triangle_count)

    return {
        "detached": bool(siblings),
        "siblings": siblings,
        "targetFile": target_path,
        "targetObjectId": int(target_oid),
    }


def _reset_parts(loaded: Loaded, object_id: str, part_id: int, triangles: int) -> None:
    settings = loaded.settings
    element = settings.object_element(object_id)
    if element is None:
        return
    for part in list(element.iterfind("part")):
        element.remove(part)
    part = etree.SubElement(element, "part")
    part.set("id", str(part_id))
    part.set("subtype", "normal_part")
    part.set("uuid", _uuid())
    from .settings import _set_metadata

    _set_metadata(part, "name", loaded.object_name(object_id))
    _set_metadata(part, "matrix", "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1")
    mesh_stat = etree.SubElement(part, "mesh_stat")
    mesh_stat.set("face_count", str(triangles))
    for key in ("edges_fixed", "degenerate_facets", "facets_removed", "facets_reversed", "backwards_edges"):
        mesh_stat.set(key, "0")
    settings.dirty = True


def _next_embedded_id(scene: Scene) -> str:
    """A package-unique mesh object id; Bambu's loader keys volumes globally."""
    highest = 0
    for path in scene.object_file_paths():
        try:
            object_file = scene.object_file(path)
        except ThreeMFError:
            continue
        for object_id in object_file.objects:
            if str(object_id).isdigit():
                highest = max(highest, int(object_id))
    return str(highest + 1)


def _new_object_path(scene: Scene) -> str:
    existing = scene.object_file_paths()
    highest = 0
    for path in existing:
        stem = os.path.basename(path)
        digits = "".join(ch for ch in stem if ch.isdigit())
        if digits:
            highest = max(highest, int(digits))
    return f"3D/Objects/object_{highest + 1}.model"


def _new_object_file(scene: Scene, path: str, mesh: Mesh, object_id: str):
    template = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
        'requiredextensions="p">\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        " <resources>\n"
        " </resources>\n"
        " <build/>\n"
        "</model>\n"
    )
    from .model import ObjectFile, parse_xml

    object_file = ObjectFile.parse(path, template.encode("utf-8"))
    object_file.add_mesh_object(object_id, mesh, _uuid())
    scene._files[path.lstrip("/")] = object_file
    scene.added_entries[path.lstrip("/")] = object_file.serialize()
    return object_file


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def _import_stl(loaded: Loaded, params: dict) -> dict:
    stl_path = params.get("stl") or params.get("file")
    if not stl_path:
        raise ThreeMFError("INVALID_ARGS", "import_stl 需要 stl 路径")
    stl_path = os.path.abspath(stl_path)
    if not os.path.isfile(stl_path):
        raise ThreeMFError("NOT_FOUND", f"STL 不存在：{stl_path}")
    with open(stl_path, "rb") as handle:
        mesh = read_stl(handle.read())
    if mesh.is_empty():
        raise ThreeMFError("INVALID_STL", "STL 没有三角形")
    normalized, offset = recentered(mesh)
    scene = loaded.scene
    path = _new_object_path(scene)
    object_id = _next_embedded_id(scene)
    _new_object_file(scene, path, normalized, object_id)
    root = scene.add_root_object([("/" + path, object_id)])
    scene.add_build_item(root.id, translation(float(offset[0]), float(offset[1]), float(offset[2])))
    scene.ensure_rels([path])
    plate_id = int(params.get("plate", 1) or 1)
    if loaded.settings is not None:
        loaded.settings.add_object(int(root.id), str(params.get("name") or os.path.basename(stl_path)), int(params.get("extruder", 1)), [{"id": object_id, "mesh_stat": mesh.triangle_count}])
        loaded.settings.add_instance(int(root.id), plate_id)
    return {
        "objectId": int(root.id),
        "path": path,
        "triangles": mesh.triangle_count,
        "vertices": mesh.vertex_count,
        "bbox": [float(v) for v in np.concatenate([normalized.vertices.min(0), normalized.vertices.max(0)])],
    }


# --------------------------------------------------------------------- settings
def apply_settings(loaded: Loaded, ops: list[dict]) -> list[dict]:
    if loaded.project is None:
        raise ThreeMFError("NOT_BAMBU_PROJECT", "这个 3MF 不含 Bambu 工程设置")
    results: list[dict] = []
    for raw in ops:
        if not isinstance(raw, dict) or "op" not in raw:
            raise ThreeMFError("INVALID_ARGS", "每个操作都必须是带 op 字段的对象")
        op = str(raw["op"])
        if op == "set":
            key = str(raw.get("key", "")).strip()
            if not key:
                raise ThreeMFError("INVALID_ARGS", "set 需要 key")
            value = raw.get("value")
            if isinstance(value, list):
                value = [str(item) for item in value]
            elif value is not None and not isinstance(value, (str, int, float, bool)):
                raise ThreeMFError("INVALID_ARGS", f"{key} 的值必须是标量或字符串数组")
            if value is not None and not isinstance(value, list):
                value = str(value).lower() if isinstance(value, bool) else str(value)
            old, new = loaded.project.set(key, value)
            results.append({"key": key, "old": old, "new": new})
        elif op == "unset":
            key = str(raw.get("key", "")).strip()
            old, new = loaded.project.unset(key)
            results.append({"key": key, "old": old, "new": new})
        elif op == "set_filament_color":
            index = int(raw.get("index", 1)) - 1
            color = str(raw.get("color", ""))
            if color and not color.startswith("#"):
                color = "#" + color
            if not color:
                raise ThreeMFError("INVALID_ARGS", "set_filament_color 需要 color，例如 #FF0000")
            colors = loaded.project.filament_colors()
            while len(colors) <= index:
                colors.append("#FFFFFF")
            old = colors[index]
            colors[index] = color
            loaded.project.set("filament_colour", colors)
            results.append({"key": f"filament_colour[{index + 1}]", "old": old, "new": color})
        elif op == "set_object_setting":
            if loaded.settings is None:
                raise ThreeMFError("NOT_BAMBU_PROJECT", "没有 model_settings")
            object_id = resolve_object(loaded, raw.get("object", raw.get("objectId")))
            key = str(raw.get("key", "")).strip()
            value = raw.get("value")
            text = "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
            loaded.settings.set_object_meta(object_id, key, text)
            results.append({"key": f"object {object_id}.{key}", "old": None, "new": text})
        else:
            raise ThreeMFError("INVALID_ARGS", f"未知设置操作：{op}")
    if not results:
        raise ThreeMFError("INVALID_ARGS", "ops 不能为空")
    return results
