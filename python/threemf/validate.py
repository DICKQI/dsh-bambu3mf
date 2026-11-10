"""Structural diagnostics for a loaded 3MF package."""

from __future__ import annotations

import numpy as np

from .constants import ThreeMFError
from .geometry import topology_report
from .model import Scene
from .settings import ModelSettings, ProjectSettings, plate_origins


def _diag(severity: str, code: str, message: str, object_id=None, **extra) -> dict:
    item = {"severity": severity, "code": code, "message": message}
    if object_id is not None:
        item["objectId"] = int(object_id)
    item.update(extra)
    return item


def validate(
    scene: Scene,
    settings: ModelSettings | None,
    project: ProjectSettings | None,
    deep: bool = False,
    with_distances: bool = False,
) -> list[dict]:
    diagnostics: list[dict] = []
    diagnostics.extend(_diagnose_components(scene))
    diagnostics.extend(_diagnose_relationships(scene))
    diagnostics.extend(_diagnose_uuids(scene))
    diagnostics.extend(_diagnose_geometry(scene, deep))
    if settings is not None:
        diagnostics.extend(_diagnose_assemble(scene, settings))
    if settings is not None and project is not None:
        diagnostics.extend(_diagnose_plates(scene, settings, project))
        diagnostics.extend(_diagnose_clearance(scene, settings, project, with_distances))
    return diagnostics


# Clearance is reported, not enforced.
#
# Measured against Bambu Studio: it slices plates whose objects sit ~5 mm apart
# (监控支架.3mf) and plates whose parts interlock so their bounding boxes overlap
# (A1外挂密封盒 plate 2, nearest surfaces 3.2 mm). A distance threshold therefore
# cannot predict a real failure, and 0.3.0's 10 mm gate refused files Bambu
# accepted. So: one diagnostic per plate naming the tightest pair (informational),
# plus OBJECT_OVERLAP for boxes that actually intersect. ``preflight`` turns only
# real surface contact into a blocker.
OVERLAP_MM = 0.1  # axis-aligned box penetration that counts as overlapping
TOUCH_MM = 0.1  # nearest sampled vertex distance that counts as contact


def _sample_vertices(points: np.ndarray, limit: int = 1200) -> np.ndarray:
    if len(points) <= limit:
        return points
    step = max(1, len(points) // limit)
    return points[::step][:limit]


def _nearest_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Nearest sampled vertex distance; chunked so the broadcast stays small.

    Returns ``inf`` when either cloud is empty — an object without a mesh sits at
    the world origin and used to make ``gaps.min()`` raise on an empty array.
    """
    if len(a) == 0 or len(b) == 0:
        return float("inf")
    best = np.inf
    for start in range(0, len(a), 256):
        block = a[start : start + 256]
        gaps = np.sqrt(((block[:, None, :] - b[None, :, :]) ** 2).sum(axis=2))
        best = min(best, float(gaps.min()))
        if best == 0.0:
            break
    return best


def _diagnose_clearance(
    scene: Scene,
    settings: ModelSettings,
    project: ProjectSettings,
    with_distances: bool = False,
) -> list[dict]:
    """Per plate: which object pairs overlap, and how tight the closest pair is.

    ``with_distances`` also samples vertices to get the true surface distance; it
    is off in the ordinary validate path because it dominates runtime on large
    projects. Preflight turns it on, since there the answer decides the advice.
    """
    items: list[dict] = []
    for plate in settings.plates:
        ids = [
            str(instance["object_id"])
            for instance in plate.instances
            if str(instance["object_id"]) in scene.root_objects
        ]
        boxes: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for object_id in ids:
            try:
                boxes[object_id] = scene.world_bbox(object_id)
            except ThreeMFError:
                continue
        clouds: dict[str, np.ndarray] = {}

        def cloud(object_id: str) -> np.ndarray:
            if object_id not in clouds:
                try:
                    clouds[object_id] = _sample_vertices(scene.world_triangles(object_id)[0])
                except ThreeMFError:
                    clouds[object_id] = np.zeros((0, 3))
            return clouds[object_id]

        pairs: list[dict] = []
        tightest = None
        for index, first in enumerate(ids):
            for second in ids[index + 1 :]:
                if first not in boxes or second not in boxes:
                    continue
                lo_a, hi_a = boxes[first]
                lo_b, hi_b = boxes[second]
                overlap = np.minimum(hi_a, hi_b) - np.maximum(lo_a, lo_b)
                gap = np.array(
                    [
                        max(float(lo_b[0] - hi_a[0]), float(lo_a[0] - hi_b[0]), 0.0),
                        max(float(lo_b[1] - hi_a[1]), float(lo_a[1] - hi_b[1]), 0.0),
                    ]
                )
                if float(overlap.min()) > OVERLAP_MM:
                    nearest = _nearest_distance(cloud(first), cloud(second)) if with_distances else None
                    declared = {
                        "objectIds": [int(first), int(second)],
                        "penetration": [round(float(v), 3) for v in overlap],
                    }
                    if nearest is not None and np.isfinite(nearest):
                        declared["nearestMm"] = round(float(nearest), 3)
                    items.append(
                        _diag(
                            "warning",
                            "OBJECT_OVERLAP",
                            f"对象 {first} 与 {second} 在盘 {plate.plater_id} 上包围盒重叠 "
                            f"{overlap[0]:.1f}×{overlap[1]:.1f}×{overlap[2]:.1f} mm"
                            + (
                                f"（最近曲面 {nearest:.2f} mm）"
                                if nearest is not None and np.isfinite(nearest)
                                else ""
                            )
                            + "；装配体可以合法地互相穿插，切片前确认一下即可",
                            int(first),
                            plate=plate.plater_id,
                            **declared,
                        )
                    )
                    pairs.append({"objectIds": [int(first), int(second)], "penetration": declared["penetration"]})
                    continue
                distance = float(np.hypot(gap[0], gap[1]))
                entry = {
                    "objectIds": [int(first), int(second)],
                    "gapMm": round(distance, 3),
                }
                if tightest is None or distance < tightest["gapMm"]:
                    tightest = entry
        if tightest is not None:
            first, second = tightest["objectIds"]
            nearest = None
            if with_distances:
                measured = _nearest_distance(cloud(str(first)), cloud(str(second)))
                if np.isfinite(measured):
                    nearest = round(float(measured), 3)
                    tightest["nearestMm"] = nearest
            detail = f"{tightest['gapMm']:.1f} mm"
            if nearest is not None:
                detail += f"，最近曲面 {nearest:.1f} mm"
            items.append(
                _diag(
                    "info",
                    "PLATE_CLEARANCE",
                    f"盘 {plate.plater_id} 上最紧的一对是 {first} 与 {second}：包围盒相距 "
                    f"{detail}（仅供判断，不影响切片）",
                    plate=plate.plater_id,
                    pair=tightest,
                    pairs=pairs,
                )
            )
    return items


def _diagnose_relationships(scene: Scene) -> list[dict]:
    """A relationship that points at a missing part makes Bambu refuse the file."""
    items: list[dict] = []
    for target in sorted(scene.rel_targets()):
        if not target or scene.pkg.has(target) or target in scene.added_entries:
            continue
        items.append(
            _diag(
                "error",
                "DANGLING_RELATIONSHIP",
                f"3dmodel.model.rels 指向不存在的条目：{target}",
                path=target,
            )
        )
    return items


def _diagnose_assemble(scene: Scene, settings: ModelSettings) -> list[dict]:
    """``<assemble>`` must not name objects that are gone (Bambu parses it eagerly)."""
    items: list[dict] = []
    known = {str(object_id) for object_id in scene.root_objects}
    missing: set[str] = set()
    for assemble in settings.root.iter("assemble"):
        for item in assemble.iterfind("assemble_item"):
            object_id = item.get("object_id")
            if object_id is None or object_id in known:
                continue
            missing.add(object_id)
    for object_id in sorted(missing, key=lambda value: (len(value), value)):
        items.append(
            _diag(
                "error",
                "ASSEMBLE_STALE_OBJECT",
                f"assemble 里仍引用了已删除的对象 {object_id}，Bambu Studio 会无法解析该 3MF",
                object_id if object_id.isdigit() else None,
            )
        )
    return items


def _diagnose_components(scene: Scene) -> list[dict]:
    items: list[dict] = []
    used: set[str] = set()
    for object_id, root_object in scene.root_objects.items():
        if root_object.inline_mesh is not None:
            used.add("3D/3dmodel.model")
        for component in root_object.components:
            path = component.path.lstrip("/")
            if not path:
                continue
            used.add(path)
            if not scene.pkg.has(path):
                items.append(
                    _diag(
                        "error",
                        "MISSING_OBJECT_FILE",
                        f"对象 {object_id} 引用的文件不存在：{path}",
                        object_id,
                        path=path,
                    )
                )
                continue
            try:
                object_file = scene.object_file(path)
            except ThreeMFError as exc:
                items.append(_diag("error", exc.code, exc.message, object_id, path=path))
                continue
            if object_file.objects and component.object_id not in object_file.objects:
                items.append(
                    _diag(
                        "error",
                        "MISSING_MESH_OBJECT",
                        f"对象 {object_id} 引用的 {path}#{component.object_id} 不存在",
                        object_id,
                        path=path,
                    )
                )
    for path in scene.object_file_paths():
        if path in used or path in scene.deleted_entries:
            continue
        items.append(
            _diag(
                "warning",
                "ORPHAN_OBJECT_FILE",
                f"包内存在未被引用的对象文件：{path}",
                path=path,
            )
        )
    return items


def _diagnose_uuids(scene: Scene) -> list[dict]:
    seen: dict[str, str] = {}
    items: list[dict] = []
    for object_id, root_object in scene.root_objects.items():
        for element, label in (
            (root_object.element, f"root object {object_id}"),
            *[(c.element, f"component of {object_id}") for c in root_object.components],
        ):
            uuid_value = element.get(
                "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}UUID"
            )
            if not uuid_value:
                continue
            if uuid_value in seen:
                items.append(
                    _diag(
                        "warning",
                        "DUPLICATE_UUID",
                        f"{label} 与 {seen[uuid_value]} 使用了同一个 UUID",
                        object_id,
                        uuid=uuid_value,
                    )
                )
            else:
                seen[uuid_value] = label
    for item in scene.build_items:
        uuid_value = item.uuid
        if not uuid_value:
            continue
        if uuid_value in seen:
            items.append(
                _diag(
                    "warning",
                    "DUPLICATE_UUID",
                    f"build item {item.object_id} 与 {seen[uuid_value]} 使用了同一个 UUID",
                    item.object_id,
                    uuid=uuid_value,
                )
            )
        else:
            seen[uuid_value] = f"build item {item.object_id}"
    return items


def _diagnose_geometry(scene: Scene, deep: bool) -> list[dict]:
    items: list[dict] = []
    for object_id in scene.object_ids():
        try:
            sources = scene.mesh_sources(object_id)
        except ThreeMFError as exc:
            items.append(_diag("error", exc.code, exc.message, object_id))
            continue
        if not sources:
            items.append(
                _diag("warning", "EMPTY_MESH", f"对象 {object_id} 没有任何网格数据", object_id)
            )
            continue
        total_triangles = 0
        for path, mesh_object_id, mesh, _matrix in sources:
            total_triangles += mesh.triangle_count
            if mesh.vertex_count == 0:
                items.append(
                    _diag("warning", "EMPTY_MESH", f"对象 {object_id} 的网格为空（{path}）", object_id)
                )
                continue
            if mesh.triangle_count and (
                mesh.triangles.min() < 0 or mesh.triangles.max() >= mesh.vertex_count
            ):
                items.append(
                    _diag(
                        "error",
                        "BAD_TRIANGLE_INDEX",
                        f"对象 {object_id} 的三角形索引越界（{path}#{mesh_object_id}）",
                        object_id,
                    )
                )
            if not np.all(np.isfinite(mesh.vertices)):
                items.append(
                    _diag("error", "BAD_VERTEX", f"对象 {object_id} 含非有限坐标", object_id)
                )
            if deep and mesh.triangle_count:
                report = topology_report(mesh)
                if not report["watertight"]:
                    items.append(
                        _diag(
                            "warning",
                            "NON_MANIFOLD_MESH",
                            f"对象 {object_id} 网格不闭合：边界边 {report['boundaryEdges']}，"
                            f"非流形边 {report['nonManifoldEdges']}",
                            object_id,
                            boundaryEdges=report["boundaryEdges"],
                            nonManifoldEdges=report["nonManifoldEdges"],
                        )
                    )
                if not report["orientationConsistent"]:
                    items.append(
                        _diag(
                            "warning",
                            "INCONSISTENT_WINDING",
                            f"对象 {object_id} 的三角形绕向不一致，体积不可靠",
                            object_id,
                        )
                    )
                if report["shells"] > 1:
                    items.append(
                        _diag(
                            "warning",
                            "DISCONNECTED_SHELLS",
                            f"对象 {object_id} 含 {report['shells']} 个互不连通的壳；"
                            "Bambu 可能把它识别为多个部件",
                            object_id,
                            shells=report["shells"],
                        )
                    )
                if report["negativeShells"]:
                    items.append(
                        _diag(
                            "warning",
                            "NEGATIVE_SHELL",
                            f"对象 {object_id} 有 {report['negativeShells']} 个壳的法向朝内",
                            object_id,
                            shells=report["negativeShells"],
                        )
                    )
                if report["degenerateTriangles"]:
                    items.append(
                        _diag(
                            "warning",
                            "DEGENERATE_TRIANGLES",
                            f"对象 {object_id} 含 {report['degenerateTriangles']} 个退化三角形",
                            object_id,
                            triangles=report["degenerateTriangles"],
                        )
                    )
                if report["thinSheets"]:
                    items.append(
                        _diag(
                            "warning",
                            "THIN_SHEET",
                            f"对象 {object_id} 含 {report['thinSheets']} 个零厚度薄片",
                            object_id,
                            shells=report["thinSheets"],
                        )
                    )
                coincident = report["duplicateFaces"] + report["opposingFaces"]
                if coincident:
                    items.append(
                        _diag(
                            "warning",
                            "COINCIDENT_FACES",
                            f"对象 {object_id} 含 {coincident} 个重复/反向面（内部夹层）",
                            object_id,
                            faces=coincident,
                        )
                    )
        if total_triangles == 0:
            items.append(
                _diag("warning", "EMPTY_MESH", f"对象 {object_id} 没有任何三角形", object_id)
            )
    return items


def _diagnose_plates(
    scene: Scene, settings: ModelSettings, project: ProjectSettings
) -> list[dict]:
    items: list[dict] = []
    placeable = set(scene.root_objects)
    plated: set[int] = set()
    for plate in settings.plates:
        for instance in plate.instances:
            plated.add(int(instance["object_id"]))
            if str(instance["object_id"]) not in placeable:
                items.append(
                    _diag(
                        "error",
                        "PLATE_INSTANCE_MISSING",
                        f"盘 {plate.plater_id} 引用了不存在的对象 {instance['object_id']}",
                    )
                )
    for object_id in scene.object_ids():
        if int(object_id) not in plated:
            items.append(
                _diag(
                    "warning",
                    "OBJECT_NOT_ON_PLATE",
                    f"对象 {object_id} 不在任何盘上，Bambu Studio 可能不会显示它",
                    object_id,
                )
            )
    bed = project.bed_size()
    origin = project.bed_origin()
    origins = plate_origins(
        [plate.plater_id for plate in settings.plates], bed, origin
    )
    for plate in settings.plates:
        plate_origin = origins.get(plate.plater_id)
        if plate_origin is None:
            continue
        for instance in plate.instances:
            object_id = str(instance["object_id"])
            if object_id not in placeable:
                continue
            try:
                lo, hi = scene.world_bbox(object_id)
            except ThreeMFError:
                continue
            local_lo = lo - plate_origin
            local_hi = hi - plate_origin
            if (
                local_lo[0] < -0.5
                or local_lo[1] < -0.5
                or local_hi[0] > bed[0] + 0.5
                or local_hi[1] > bed[1] + 0.5
            ):
                items.append(
                    _diag(
                        "warning",
                        "OBJECT_OUTSIDE_PLATE",
                        f"对象 {object_id} 超出盘 {plate.plater_id} 的打印区域",
                        object_id,
                        plate=plate.plater_id,
                        bounds=[round(float(v), 3) for v in np.concatenate([local_lo, local_hi])],
                    )
                )
    return items
