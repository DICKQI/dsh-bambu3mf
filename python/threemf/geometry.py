"""Mesh maths: transforms, weld/cleanup, plane cut with capping, STL in/out.

All meshes are plain numpy arrays: ``vertices`` is ``(N, 3)`` float64 and
``triangles`` is ``(M, 3)`` int64 in counter-clockwise winding (outward normals
for a closed mesh).
"""

from __future__ import annotations

import math
import os
import struct
import sys
from dataclasses import dataclass

import numpy as np

from .constants import ThreeMFError
from .model import Mesh, transform_points


def _debug_enabled() -> bool:
    return bool(os.environ.get("THREEMF_DEBUG"))


# -------------------------------------------------------------------- inspection
def signed_volume(vertices: np.ndarray, triangles: np.ndarray) -> float:
    if len(triangles) == 0:
        return 0.0
    v0 = vertices[triangles[:, 0]]
    v1 = vertices[triangles[:, 1]]
    v2 = vertices[triangles[:, 2]]
    return float(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum() / 6.0)


def edge_counts(triangles: np.ndarray) -> tuple[int, int, int]:
    """Return (boundary, non_manifold, interior) undirected edge counts."""
    if len(triangles) == 0:
        return 0, 0, 0
    edges = np.concatenate(
        [
            triangles[:, [0, 1]],
            triangles[:, [1, 2]],
            triangles[:, [2, 0]],
        ],
        axis=0,
    )
    edges = np.sort(edges, axis=1)
    _unique, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = int((counts == 1).sum())
    non_manifold = int((counts > 2).sum())
    interior = int((counts == 2).sum())
    return boundary, non_manifold, interior


def mesh_quality(mesh: Mesh, tolerance: float = 1e-6) -> dict:
    """One topology verdict for every caller.

    ``edge_counts`` alone compares raw vertex indices, so a mesh whose vertices
    are duplicated along seams looks open to one tool and watertight to another
    (``analyze`` welded, ``weld`` did not). Every quality report now starts from
    the same welded index, so the tools cannot disagree about the same mesh.
    """
    report = topology_report(mesh, tolerance=tolerance, deep=False)
    return {
        "triangles": report["triangles"],
        "vertices": report["vertices"],
        "boundaryEdges": report["boundaryEdges"],
        "nonManifoldEdges": report["nonManifoldEdges"],
        "interiorEdges": report["interiorEdges"],
        "watertight": report["watertight"],
        "orientationConsistent": report["orientationConsistent"],
        "volume": report["netVolumeMm3"] if report["volumeReliable"] else None,
        "volumeReliable": report["volumeReliable"],
    }


# -------------------------------------------------------------------- transforms
def printability(
    mesh: Mesh,
    layer_height: float | None = None,
    overhang_angle: float = 45.0,
    bed_mm: float = 0.2,
) -> dict:
    """Print-time drivers that need no slicer run.

    Four numbers answer most "why is this slow?" questions: the surface area,
    the overhang area that actually needs support (the bottom resting on the
    bed is excluded, otherwise every part reports a huge overhang), the number
    of through-holes (topological genus: each hole adds one handle, and each
    hole costs start/stop + travel on every layer) and the layer count.

    Genus is only meaningful on a closed surface; an open mesh reports None
    instead of a number that looks authoritative and is wrong.
    """
    try:
        angle = float(overhang_angle)
    except (TypeError, ValueError) as exc:
        raise ThreeMFError("INVALID_ARGS", f"overhang_angle 必须是数字：{exc}") from exc
    if not 0.0 < angle < 90.0:
        raise ThreeMFError("INVALID_ARGS", "overhang_angle 必须在 0~90 度之间（不含端点）")

    zero_overhang = {
        "angleDegrees": round(angle, 2),
        "areaMm2": 0.0,
        "percent": 0.0,
        "bedContactMm2": 0.0,
        "unsupportedMm2": 0.0,
        "unsupportedPercent": 0.0,
    }
    if mesh.is_empty():
        return {
            "triangles": 0,
            "vertices": 0,
            "watertight": False,
            "boundaryEdges": 0,
            "nonManifoldEdges": 0,
            "shells": 0,
            "genus": None,
            "orientationConsistent": False,
            "volumeReliable": False,
            "negativeShells": 0,
            "volumeMm3": None,
            "netVolumeMm3": None,
            "degenerateTriangles": 0,
            "duplicateFaces": 0,
            "opposingFaces": 0,
            "thinSheets": 0,
            "surfaceAreaMm2": 0.0,
            "size": [0.0, 0.0, 0.0],
            "heightMm": 0.0,
            "layerHeight": layer_height,
            "estimatedLayers": None,
            "overhang": zero_overhang,
        }

    vertices = mesh.vertices
    triangles = mesh.triangles
    report = topology_report(mesh, tolerance=1e-6, deep=False)

    lo = vertices.min(axis=0)
    hi = vertices.max(axis=0)
    size = [round(float(value), 3) for value in (hi - lo)]
    height = float(size[2])

    v0 = vertices[triangles[:, 0]]
    v1 = vertices[triangles[:, 1]]
    v2 = vertices[triangles[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(cross, axis=1)
    area = lengths / 2.0
    total_area = float(area.sum())

    faces = int(report["triangles"])
    used_vertices = int(report["vertices"])
    boundary = int(report["boundaryEdges"])
    non_manifold = int(report["nonManifoldEdges"])
    watertight = bool(report["watertight"])
    shells = int(report["shells"])
    genus = report["genus"]
    volume = report["volumeMm3"]

    normal_z = cross[:, 2] / np.maximum(lengths, 1e-12)
    centre_z = (v0[:, 2] + v1[:, 2] + v2[:, 2]) / 3.0
    floor_z = float(vertices[:, 2].min())
    threshold = -math.cos(math.radians(angle))
    downward = normal_z <= threshold
    # A face resting on the build plate is supported by the plate, not by the
    # part's own overhang handling.
    bed = downward & (centre_z <= floor_z + max(float(bed_mm), 0.0))
    unsupported = downward & ~bed
    overhang_area = float(area[downward].sum())
    bed_area = float(area[bed].sum())
    unsupported_area = float(area[unsupported].sum())
    def percent(value: float) -> float:
        return round(value / total_area * 100.0, 2) if total_area > 0 else 0.0


    layers = None
    if layer_height and layer_height > 0 and height > 0:
        layers = int(math.ceil(height / float(layer_height) - 1e-9))
    return {
        "triangles": faces,
        "vertices": used_vertices,
        "watertight": watertight,
        "boundaryEdges": boundary,
        "nonManifoldEdges": non_manifold,
        "shells": shells,
        "genus": genus,
        "orientationConsistent": bool(report["orientationConsistent"]),
        "volumeReliable": bool(report["volumeReliable"]),
        "negativeShells": int(report["negativeShells"]),
        "volumeMm3": None if volume is None else round(volume, 2),
        "netVolumeMm3": None
        if report["netVolumeMm3"] is None
        else round(float(report["netVolumeMm3"]), 2),
        "degenerateTriangles": int(report["degenerateTriangles"]),
        "duplicateFaces": int(report["duplicateFaces"]),
        "opposingFaces": int(report["opposingFaces"]),
        "thinSheets": int(report["thinSheets"]),
        "surfaceAreaMm2": round(total_area, 2),
        "size": size,
        "heightMm": round(height, 3),
        "layerHeight": layer_height,
        "estimatedLayers": layers,
        "overhang": {
            "angleDegrees": round(angle, 2),
            "areaMm2": round(overhang_area, 2),
            "percent": percent(overhang_area),
            "bedContactMm2": round(bed_area, 2),
            "unsupportedMm2": round(unsupported_area, 2),
            "unsupportedPercent": percent(unsupported_area),
        },
    }

def transformed(mesh: Mesh, matrix: np.ndarray) -> Mesh:
    return Mesh(
        transform_points(mesh.vertices, matrix),
        mesh.triangles.copy(),
        [dict(a) for a in mesh.tri_attrs],
    )


def recentered(mesh: Mesh) -> tuple[Mesh, np.ndarray]:
    """Move the mesh so XY is centred on the origin and the lowest Z is 0."""
    if mesh.is_empty():
        return mesh.clone(), np.zeros(3)
    lo, hi = mesh.bbox()
    offset = np.array([(lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, lo[2]])
    moved = Mesh(
        mesh.vertices - offset,
        mesh.triangles.copy(),
        [dict(a) for a in mesh.tri_attrs],
    )
    return moved, offset


def stretch_band(
    mesh: Mesh,
    axis: int,
    low: float,
    high: float,
    delta: float,
    negative_share: float = 0.5,
) -> tuple[Mesh, dict]:
    """Lengthen the band ``low..high`` by ``delta`` and shift what is outside it.

    Vertices inside the band are remapped so the band grows by ``delta``; the
    parts on either side keep their exact shape and move out by their share of
    the stretch. No triangle is added, removed or re-cut, so topology, triangle
    count and surface quality are unchanged: this is how a part gets longer
    without touching either end.

    ``delta`` may be negative (compress). ``negative_share`` says how much of the
    growth goes to the low side: 0.5 (default) grows symmetrically, 0 grows only
    toward +axis, 1 grows only toward -axis.
    """
    if mesh.is_empty():
        raise ThreeMFError("EMPTY_MESH", "网格为空，无法拉伸")
    if not (high > low):
        raise ThreeMFError("INVALID_ARGS", "stretch 需要 high > low（拉伸区间）")
    if not (0.0 <= negative_share <= 1.0):
        raise ThreeMFError("INVALID_ARGS", "negative_share 必须在 0..1 之间")
    coordinates = mesh.vertices[:, axis]
    axis_min = float(coordinates.min())
    axis_max = float(coordinates.max())
    # A band outside the mesh moves everything rigidly and stretches nothing, so
    # the caller would get a shifted-but-unchanged part while the summary claimed
    # it grew. Refuse instead of doing something surprising.
    slack = 1e-6
    if high < axis_min - slack or low > axis_max + slack:
        raise ThreeMFError(
            "INVALID_ARGS",
            f"band [{low:g}, {high:g}] 完全落在物体之外（该轴实际范围 "
            f"[{axis_min:g}, {axis_max:g}]），拉伸不会改变尺寸；请把 band 放到物体内部",
            {"axis": "xyz"[axis], "range": [axis_min, axis_max]},
        )
    if low < axis_min - slack or high > axis_max + slack:
        raise ThreeMFError(
            "INVALID_ARGS",
            f"band [{low:g}, {high:g}] 超出物体在该轴的范围 [{axis_min:g}, {axis_max:g}]；"
            "两端越界会让实际增长量小于 delta，请把 band 收进物体内部",
            {"axis": "xyz"[axis], "range": [axis_min, axis_max]},
        )
    length = high - low
    target = length + delta
    if target <= 1e-6:
        raise ThreeMFError(
            "INVALID_ARGS",
            f"拉伸后区间长度 {target:.4f} mm 非正；delta 不能小于区间长度 {-length:.3f}",
        )
    factor = target / length
    low_shift = -delta * negative_share
    high_shift = delta * (1.0 - negative_share)
    new_low = low + low_shift
    vertices = mesh.vertices.copy()
    inside = (coordinates >= low - 1e-9) & (coordinates <= high + 1e-9)
    below = coordinates < low - 1e-9
    above = coordinates > high + 1e-9

    vertices[inside, axis] = new_low + (coordinates[inside] - low) * factor
    vertices[below, axis] = coordinates[below] + low_shift
    vertices[above, axis] = coordinates[above] + high_shift

    stats = {
        "axis": "xyz"[axis],
        "band": [round(float(low), 4), round(float(high), 4)],
        "bandAfter": [round(float(new_low), 4), round(float(new_low + target), 4)],
        "delta": round(float(delta), 4),
        "factor": round(float(factor), 6),
        "negativeShare": round(float(negative_share), 4),
        "verticesInside": int(inside.sum()),
        "verticesBelow": int(below.sum()),
        "verticesAbove": int(above.sum()),
    }
    stretched = Mesh(vertices, mesh.triangles.copy(), [dict(a) for a in mesh.tri_attrs])
    return stretched, stats


# ---------------------------------------------------------------------- cleanup
def weld_vertices(mesh: Mesh, tolerance: float = 1e-5) -> Mesh:
    if mesh.is_empty():
        return mesh.clone()
    quantized = np.round(mesh.vertices / max(tolerance, 1e-9)).astype(np.int64)
    _unique, inverse = np.unique(quantized, axis=0, return_inverse=True)
    new_vertices = np.zeros((int(inverse.max()) + 1, 3), dtype=np.float64)
    np.add.at(new_vertices, inverse, mesh.vertices)
    counts = np.bincount(inverse).astype(np.float64)
    new_vertices /= counts[:, None]
    triangles = inverse[mesh.triangles]
    attrs = [dict(a) for a in mesh.tri_attrs]
    return Mesh(new_vertices, triangles, attrs)


def remove_degenerate(mesh: Mesh, area_epsilon: float = 1e-12) -> Mesh:
    if mesh.is_empty():
        return mesh.clone()
    v0 = mesh.vertices[mesh.triangles[:, 0]]
    v1 = mesh.vertices[mesh.triangles[:, 1]]
    v2 = mesh.vertices[mesh.triangles[:, 2]]
    area = np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1) / 2.0
    same = (
        (mesh.triangles[:, 0] == mesh.triangles[:, 1])
        | (mesh.triangles[:, 1] == mesh.triangles[:, 2])
        | (mesh.triangles[:, 0] == mesh.triangles[:, 2])
    )
    keep = (area > area_epsilon) & ~same
    if keep.all():
        return mesh.clone()
    return Mesh(
        mesh.vertices.copy(),
        mesh.triangles[keep],
        [a for a, k in zip(mesh.tri_attrs, keep.tolist()) if k],
    )


def remove_duplicate_faces(mesh: Mesh) -> Mesh:
    if mesh.is_empty():
        return mesh.clone()
    canonical = np.sort(mesh.triangles, axis=1)
    _unique, index = np.unique(canonical, axis=0, return_index=True)
    index = np.sort(index)
    attrs = [mesh.tri_attrs[i] for i in index.tolist()] if mesh.tri_attrs else []
    return Mesh(mesh.vertices.copy(), mesh.triangles[index], attrs)


def flip_normals(mesh: Mesh) -> Mesh:
    return Mesh(mesh.vertices.copy(), mesh.triangles[:, [0, 2, 1]], [dict(a) for a in mesh.tri_attrs])


def cleanup(mesh: Mesh, weld_tolerance: float = 1e-5) -> tuple[Mesh, dict]:
    before = mesh_quality(mesh)
    current = weld_vertices(mesh, weld_tolerance)
    current = remove_degenerate(current)
    current = remove_duplicate_faces(current)
    current = compact_vertices(current)
    after = mesh_quality(current)
    return current, {"before": before, "after": after}


def compact_vertices(mesh: Mesh) -> Mesh:
    """Drop vertices that no triangle references (keeps bbox/position honest)."""
    if mesh.is_empty():
        return mesh.clone()
    used = np.unique(mesh.triangles.ravel())
    if len(used) == len(mesh.vertices):
        return mesh
    remap = -np.ones(len(mesh.vertices), dtype=np.int64)
    remap[used] = np.arange(len(used), dtype=np.int64)
    return Mesh(mesh.vertices[used], remap[mesh.triangles], [dict(a) for a in mesh.tri_attrs])


# --------------------------------------------------------------------- plane cut
@dataclass
class CutResult:
    mesh: Mesh
    kept_triangles: int
    clipped_triangles: int
    cap_triangles: int
    boundary_edges_before: int
    boundary_edges_after: int
    loops: int
    cap_fallback: bool = False


def _plane_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    helper = np.array([1.0, 0.0, 0.0])
    if abs(float(np.dot(helper, normal))) > 0.9:
        helper = np.array([0.0, 1.0, 0.0])
    u = np.cross(helper, normal)
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    return u, v


def _polygon_area(points: np.ndarray) -> float:
    x = points[:, 0]
    y = points[:, 1]
    return float(0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _point_in_polygon(point: np.ndarray, polygon: np.ndarray) -> bool:
    x, y = float(point[0]), float(point[1])
    inside = False
    count = len(polygon)
    for i in range(count):
        x1, y1 = float(polygon[i][0]), float(polygon[i][1])
        x2, y2 = float(polygon[(i + 1) % count][0]), float(polygon[(i + 1) % count][1])
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def _segments_intersect(a, b, c, d) -> bool:
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1 = cross(c, d, a)
    d2 = cross(c, d, b)
    d3 = cross(a, b, c)
    d4 = cross(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _bridge_hole(ring_points, ring_indices, hole_points, hole_indices):
    """Splice one hole ring into the outer ring with the shortest visible bridge."""
    best = None
    best_distance = np.inf
    for hi, hpoint in enumerate(hole_points):
        for oi, opoint in enumerate(ring_points):
            distance = float((hpoint[0] - opoint[0]) ** 2 + (hpoint[1] - opoint[1]) ** 2)
            if distance >= best_distance:
                continue
            if not _visible(hpoint, opoint, ring_points, hole_points, hi, oi):
                continue
            best_distance = distance
            best = (hi, oi)
    if best is None:
        raise ThreeMFError("CAP_FAILED", "no visible bridge between cut rings")
    hi, oi = best
    merged_points = np.vstack(
        [ring_points[: oi + 1], hole_points[hi:], hole_points[: hi + 1], ring_points[oi:]]
    )
    merged_indices = (
        list(ring_indices[: oi + 1])
        + list(hole_indices[hi:])
        + list(hole_indices[: hi + 1])
        + list(ring_indices[oi:])
    )
    return merged_points, merged_indices


def _visible(hpoint, opoint, ring, hole, hi, oi) -> bool:
    for points, skip in ((ring, oi), (hole, hi)):
        count = len(points)
        for i in range(count):
            j = (i + 1) % count
            if i in (skip, (skip - 1) % count) or j == skip:
                continue
            if _segments_intersect(hpoint, opoint, points[i], points[j]):
                return False
    midpoint = (hpoint + opoint) / 2.0
    if not _point_in_polygon(midpoint, ring):
        return False
    if _point_in_polygon(midpoint, hole):
        return False
    return True


def _sign(p, a, b) -> float:
    return (p[0] - b[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (p[1] - b[1])


def _strictly_inside(p, a, b, c, eps: float = 1e-12) -> bool:
    d1 = _sign(p, a, b)
    d2 = _sign(p, b, c)
    d3 = _sign(p, c, a)
    return (d1 > eps and d2 > eps and d3 > eps) or (d1 < -eps and d2 < -eps and d3 < -eps)


def _ear_clip(points: np.ndarray) -> tuple[list[tuple[int, int, int]], list[int]]:
    count = len(points)
    if count < 3:
        return [], []
    indices = list(range(count))
    triangles: list[tuple[int, int, int]] = []
    guard = 0
    while len(indices) > 3 and guard < 8 * count * count:
        guard += 1
        found = False
        for position in range(len(indices)):
            i0 = indices[(position - 1) % len(indices)]
            i1 = indices[position]
            i2 = indices[(position + 1) % len(indices)]
            a, b, c = points[i0], points[i1], points[i2]
            area = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            # Collinear vertices (area ~ 0) still have to be consumed: their two
            # boundary edges belong to side-wall triangles and must be paired.
            if area < -1e-12:
                continue
            if _ear_blocked(points, indices, (i0, i1, i2), position):
                continue
            triangles.append((i0, i1, i2))
            indices.pop(position)
            found = True
            break
        if not found:
            break
    if len(indices) == 3:
        a, b, c = points[indices[0]], points[indices[1]], points[indices[2]]
        area = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if area >= -1e-12:
            triangles.append((indices[0], indices[1], indices[2]))
            indices = []
    return triangles, indices


def _ear_blocked(points, indices, ear, position) -> bool:
    i0, i1, i2 = ear
    a, b, c = points[i0], points[i1], points[i2]
    skip = {position, (position - 1) % len(indices), (position + 1) % len(indices)}
    for k in range(len(indices)):
        if k in skip:
            continue
        p = points[indices[k]]
        if _strictly_inside(p, a, b, c):
            return True
    return False


def _loop_edges(segments: list[tuple[int, int]]) -> list[list[int]]:
    adjacency: dict[int, list[int]] = {}
    for a, b in segments:
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    for key, value in adjacency.items():
        if len(value) != 2:
            raise ThreeMFError(
                "CAP_FAILED",
                f"cut boundary is not a clean loop at vertex {key}",
                {"degree": len(value)},
            )
    loops: list[list[int]] = []
    visited: set[tuple[int, int]] = set()
    for start in adjacency:
        for neighbour in adjacency[start]:
            key = (min(start, neighbour), max(start, neighbour))
            if key in visited:
                continue
            loop = [start]
            previous = start
            current = neighbour
            visited.add(key)
            while current != start:
                loop.append(current)
                candidates = adjacency[current]
                nxt = candidates[0] if candidates[1] == previous else candidates[1]
                edge = (min(current, nxt), max(current, nxt))
                if edge in visited:
                    raise ThreeMFError("CAP_FAILED", "cut boundary loop self-intersects")
                visited.add(edge)
                previous, current = current, nxt
                if len(loop) > len(segments) + 5:
                    raise ThreeMFError("CAP_FAILED", "cut boundary loop does not close")
            loops.append(loop)
    return loops


def plane_cut(
    mesh: Mesh,
    point: np.ndarray,
    normal: np.ndarray,
    keep: str = "positive",
    cap: bool = True,
) -> CutResult:
    """Clip a mesh against a plane and (optionally) cap the opening."""
    normal = np.asarray(normal, dtype=np.float64)
    length = float(np.linalg.norm(normal))
    if length < 1e-12:
        raise ThreeMFError("INVALID_ARGS", "plane normal must not be zero")
    normal = normal / length
    point = np.asarray(point, dtype=np.float64)
    if keep not in ("positive", "negative"):
        raise ThreeMFError("INVALID_ARGS", "keep must be 'positive' or 'negative'")

    scale = max(1.0, float(np.abs(mesh.vertices).max()) if len(mesh.vertices) else 1.0)
    if not mesh.is_empty():
        # Seamed surfaces carry duplicate vertices at identical positions; weld
        # them so the cut boundary chains into clean loops.
        mesh = weld_vertices(mesh, 1e-6 * scale)
    epsilon = 1e-9 * scale
    snap_epsilon = 1e-7 * scale
    distance = (mesh.vertices - point) @ normal
    if keep == "negative":
        distance = -distance
        normal = -normal
    on_plane_vertex = np.abs(distance) <= snap_epsilon
    distance = np.where(on_plane_vertex, 0.0, distance)
    inside = distance >= -epsilon

    base_count = len(mesh.vertices)
    extras: list[np.ndarray] = []
    intersection_cache: dict[tuple[int, int], int] = {}
    position_cache: dict[tuple[int, int, int], int] = {}

    def vertex(index: int) -> np.ndarray:
        return mesh.vertices[index] if index < base_count else extras[index - base_count]

    def intersection(a: int, b: int) -> int:
        if on_plane_vertex[a]:
            return a
        if on_plane_vertex[b]:
            return b
        key = (a, b) if a < b else (b, a)
        cached = intersection_cache.get(key)
        if cached is not None:
            return cached
        da, db = float(distance[a]), float(distance[b])
        denominator = da - db
        t = 0.0 if abs(denominator) < 1e-15 else da / denominator
        t = min(1.0, max(0.0, t))
        position = vertex(a) + t * (vertex(b) - vertex(a))
        position = position - float(np.dot(position - point, normal)) * normal
        position_key = tuple(int(round(float(value) / (1e-7 * scale))) for value in position)
        existing = position_cache.get(position_key)
        if existing is not None:
            intersection_cache[key] = existing
            return existing
        index = base_count + len(extras)
        extras.append(position)
        position_cache[position_key] = index
        intersection_cache[key] = index
        return index

    kept: list[tuple[int, int, int]] = []
    kept_attrs: list[dict] = []
    segments: list[tuple[int, int]] = []
    clipped = 0

    for index, triangle in enumerate(mesh.triangles.tolist()):
        flags = [bool(inside[t]) for t in triangle]
        if all(flags):
            kept.append(tuple(triangle))
            if index < len(mesh.tri_attrs):
                kept_attrs.append(dict(mesh.tri_attrs[index]))
            else:
                kept_attrs.append({})
            continue
        if not any(flags):
            continue
        clipped += 1
        polygon: list[int] = []
        for offset in range(3):
            a = triangle[offset]
            b = triangle[(offset + 1) % 3]
            if inside[a] and (not polygon or polygon[-1] != a):
                polygon.append(a)
            if inside[a] != inside[b]:
                polygon.append(intersection(a, b))
        cleaned: list[int] = []
        for v in polygon:
            if not cleaned or cleaned[-1] != v:
                cleaned.append(v)
        if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
            cleaned.pop()
        if len(cleaned) < 3:
            continue
        for i in range(1, len(cleaned) - 1):
            kept.append((cleaned[0], cleaned[i], cleaned[i + 1]))
            kept_attrs.append({})
        on_plane = [v for v in cleaned if v >= base_count or bool(on_plane_vertex[v])]
        if len(on_plane) == 2:
            segments.append((on_plane[0], on_plane[1]))

    if extras:
        vertices = np.vstack([mesh.vertices] + extras)
    else:
        vertices = mesh.vertices.copy()
    kept_array = np.array(kept, dtype=np.int64).reshape(-1, 3)
    cap_triangles = 0
    cap_array = np.zeros((0, 3), dtype=np.int64)
    loop_count = 0
    cap_fallback = False
    if cap and segments:
        cap_array, loop_count, cap_fallback = _cap(vertices, segments, point, normal)
        cap_triangles = int(len(cap_array))
    if len(cap_array):
        triangles = np.vstack([kept_array, cap_array])
        attrs = kept_attrs + [{} for _ in range(len(cap_array))]
    else:
        triangles = kept_array
        attrs = kept_attrs
    result = compact_vertices(Mesh(vertices, triangles, attrs))
    boundary_before, _nm, _inner = edge_counts(mesh.triangles)
    boundary_after, _nm2, _inner2 = edge_counts(result.triangles)
    return CutResult(
        mesh=result,
        kept_triangles=len(kept),
        clipped_triangles=clipped,
        cap_triangles=cap_triangles,
        boundary_edges_before=boundary_before,
        boundary_edges_after=boundary_after,
        loops=loop_count,
        cap_fallback=cap_fallback,
    )


def _cap(vertices: np.ndarray, segments: list[tuple[int, int]], point: np.ndarray, normal: np.ndarray) -> tuple[np.ndarray, int]:
    loops = _loop_edges(segments)
    if not loops:
        return np.zeros((0, 3), dtype=np.int64), 0
    u, v = _plane_basis(normal)
    rings: list[tuple[np.ndarray, list[int]]] = []
    for loop in loops:
        points = np.array(
            [[float(np.dot(vertices[idx] - point, u)), float(np.dot(vertices[idx] - point, v))] for idx in loop]
        )
        if len(loop) >= 3:
            rings.append((points, loop))
    if _debug_enabled():
        print(
            f"[cap] loops={len(loops)} areas={[round(_polygon_area(r[0]), 2) for r in rings]} "
            f"sizes={[len(r[0]) for r in rings]}",
            file=sys.stderr,
        )
    if not rings:
        return np.zeros((0, 3), dtype=np.int64), len(loops)

    # Nesting depth decides which rings are solids and which are holes: a ring
    # inside an even number of other rings bounds material, an odd depth is a hole.
    depths = []
    for index, (points, _loop) in enumerate(rings):
        probe = points[0]
        depth = 0
        for other_index, (other_points, _other_loop) in enumerate(rings):
            if other_index == index:
                continue
            if _point_in_polygon(probe, other_points):
                depth += 1
        depths.append(depth)

    groups: dict[int, list[int]] = {}
    for index, depth in enumerate(depths):
        if depth % 2 == 0:
            groups.setdefault(index, [])
    if _debug_enabled():
        print(f"[cap] depths={depths}", file=sys.stderr)
    for index, depth in enumerate(depths):
        if depth % 2 == 1:
            # Attach the hole to the smallest enclosing solid.
            best = None
            best_area = np.inf
            for outer_index in groups:
                if _point_in_polygon(rings[index][0][0], rings[outer_index][0]):
                    area = abs(_polygon_area(rings[outer_index][0]))
                    if area < best_area:
                        best_area = area
                        best = outer_index
            if best is not None:
                groups[best].append(index)
            else:
                raise ThreeMFError("CAP_FAILED", "cut hole is outside every solid ring")

    faces: list[tuple[int, int, int]] = []
    fallback = False
    for outer_index, hole_indices in groups.items():
        outer_points, outer_loop = rings[outer_index]
        if _polygon_area(outer_points) < 0:
            outer_points = outer_points[::-1]
            outer_loop = list(reversed(outer_loop))
        merged_points = outer_points
        merged_indices = list(outer_loop)
        for hole_index in sorted(hole_indices, key=lambda i: abs(_polygon_area(rings[i][0])), reverse=True):
            hole_points, hole_loop = rings[hole_index]
            if _polygon_area(hole_points) > 0:
                hole_points = hole_points[::-1]
                hole_loop = list(reversed(hole_loop))
            merged_points, merged_indices = _bridge_hole(
                merged_points, merged_indices, hole_points, hole_loop
            )
        triangles, leftover = _ear_clip(merged_points)
        if leftover:
            # Last resort: a fan over the stubborn remainder still pairs every
            # boundary edge, which keeps the cap closed. It may overlap locally.
            for i in range(1, len(leftover) - 1):
                triangles.append((leftover[0], leftover[i], leftover[i + 1]))
            fallback = True
        if _debug_enabled():
            print(
                f"[cap] group outer={outer_index} holes={hole_indices} "
                f"merged={len(merged_points)} tris={len(triangles)} expected={len(merged_points) - 2}",
                file=sys.stderr,
            )
        for a, b, c in triangles:
            ia, ib, ic = merged_indices[a], merged_indices[b], merged_indices[c]
            if len({ia, ib, ic}) < 3:
                continue
            pa, pb, pc = vertices[ia], vertices[ib], vertices[ic]
            face_normal = np.cross(pb - pa, pc - pa)
            if float(np.dot(face_normal, normal)) < 0:
                ia, ib, ic = ia, ic, ib
            faces.append((ia, ib, ic))
        if _debug_enabled():
            ring_edges = set()
            for loop_index in [outer_index, *hole_indices]:
                loop = rings[loop_index][1]
                for i in range(len(loop)):
                    a, b = loop[i], loop[(i + 1) % len(loop)]
                    ring_edges.add((min(a, b), max(a, b)))
            cap_edges = {}
            for tri in faces:
                for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                    key = (min(a, b), max(a, b))
                    cap_edges[key] = cap_edges.get(key, 0) + 1
            missing = [edge for edge in ring_edges if cap_edges.get(edge, 0) == 0]
            duplicate = [edge for edge, count in cap_edges.items() if count > 2]
            print(
                f"[cap] group outer={outer_index} ringEdges={len(ring_edges)} "
                f"missing={len(missing)} overused={len(duplicate)}",
                file=sys.stderr,
            )
    if not faces:
        raise ThreeMFError("CAP_FAILED", "cap triangulation produced no faces")
    return np.array(faces, dtype=np.int64), len(loops), fallback


# -------------------------------------------------------------------------- STL
def read_stl(data: bytes) -> Mesh:
    if len(data) < 84:
        raise ThreeMFError("INVALID_STL", "STL file is too small")
    ascii_candidate = data[:5].lower() == b"solid"
    if ascii_candidate:
        try:
            return _read_ascii_stl(data)
        except ThreeMFError:
            pass
    count = struct.unpack("<I", data[80:84])[0]
    expected = 84 + count * 50
    if count == 0 or expected > len(data) + 1:
        if ascii_candidate:
            return _read_ascii_stl(data)
        raise ThreeMFError("INVALID_STL", "binary STL header is inconsistent")
    array = np.frombuffer(data[84 : 84 + count * 50], dtype=np.uint8).reshape(count, 50)
    floats = array[:, :48].copy().view("<f4").reshape(count, 12)
    triangles = floats[:, 3:].reshape(count, 3, 3).astype(np.float64)
    vertices = triangles.reshape(-1, 3)
    faces = np.arange(count * 3, dtype=np.int64).reshape(count, 3)
    return remove_degenerate(Mesh(vertices, faces, []), 1e-14)


def _read_ascii_stl(data: bytes) -> Mesh:
    text = data.decode("utf-8", "replace")
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    current: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            current.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif parts and parts[0].lower() == "endloop":
            if len(current) == 3:
                base = len(vertices)
                vertices.extend(current)
                faces.append((base, base + 1, base + 2))
            current = []
    if not vertices:
        raise ThreeMFError("INVALID_STL", "no triangles found in STL")
    return Mesh(np.array(vertices, dtype=np.float64), np.array(faces, dtype=np.int64), [])


# ------------------------------------------------------------------ components
def welded_index(vertices: np.ndarray, tolerance: float = 1e-6) -> np.ndarray:
    """Map every vertex to a shared index, merging positions within ``tolerance``."""
    if len(vertices) == 0:
        return np.zeros(0, dtype=np.int64)
    quantized = np.round(vertices / max(tolerance, 1e-9)).astype(np.int64)
    _unique, inverse = np.unique(quantized, axis=0, return_inverse=True)
    return inverse.astype(np.int64).ravel()


def connected_components(mesh: Mesh, tolerance: float = 1e-6) -> np.ndarray:
    """Label every triangle with a connected-component index.

    Shells that only touch at a vertex still count as one component (that is how
    a slicer sees them). Labels are ordered largest-shell-first and the ordering
    is deterministic, so an index stays meaningful across calls.
    """
    if mesh.is_empty():
        return np.zeros(0, dtype=np.int64)
    inverse = welded_index(mesh.vertices, tolerance)
    size = int(inverse.max()) + 1
    triangles = mesh.triangles
    edges = np.concatenate(
        [
            triangles[:, [0, 1]],
            triangles[:, [1, 2]],
            triangles[:, [2, 0]],
        ],
        axis=0,
    )
    edges = np.unique(np.sort(inverse[edges], axis=1), axis=0)
    parent = np.arange(size, dtype=np.int64)
    if len(edges):
        while True:
            left = parent[edges[:, 0]]
            right = parent[edges[:, 1]]
            low = np.minimum(left, right)
            high = np.maximum(left, right)
            pending = low != high
            if not pending.any():
                break
            np.minimum.at(parent, high[pending], low[pending])
            parent = parent[parent]
    labels = parent[inverse[triangles[:, 0]]]

    unique, counts = np.unique(labels, return_counts=True)
    centroids = mesh.vertices[triangles].mean(axis=1)
    sums = np.zeros((len(unique), 3), dtype=np.float64)
    np.add.at(sums, np.searchsorted(unique, labels), centroids)
    means = sums / counts[:, None]
    order = np.lexsort(
        (means[:, 2], means[:, 1], means[:, 0], -counts.astype(np.float64))
    )
    ranked = np.empty(len(unique), dtype=np.int64)
    ranked[order] = np.arange(len(unique), dtype=np.int64)
    return ranked[np.searchsorted(unique, labels)]


def component_info(
    mesh: Mesh, labels: np.ndarray | None = None, tolerance: float = 1e-6
) -> list[dict]:
    """Per-component summary: triangle count, bbox, size, centre and volume."""
    if mesh.is_empty():
        return []
    if labels is None:
        labels = connected_components(mesh, tolerance)
    v0 = mesh.vertices[mesh.triangles[:, 0]]
    v1 = mesh.vertices[mesh.triangles[:, 1]]
    v2 = mesh.vertices[mesh.triangles[:, 2]]
    volumes = np.einsum("ij,ij->i", v0, np.cross(v1, v2)) / 6.0
    result: list[dict] = []
    for index in range(int(labels.max()) + 1 if len(labels) else 0):
        mask = labels == index
        if not mask.any():
            continue
        points = mesh.vertices[np.unique(mesh.triangles[mask].ravel())]
        lo, hi = points.min(axis=0), points.max(axis=0)
        result.append(
            {
                "index": index,
                "triangles": int(mask.sum()),
                "bbox": [float(v) for v in np.concatenate([lo, hi])],
                "size": [float(v) for v in (hi - lo)],
                "center": [float(v) for v in ((lo + hi) / 2.0)],
                "volume": round(abs(float(volumes[mask].sum())), 3),
            }
        )
    return result


def isolate_triangles(
    vertices: np.ndarray, triangles: np.ndarray, mask: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Give the selected triangles their own copy of the vertices they use.

    Returns ``(vertices, triangles, moved)`` where ``moved`` marks the vertices
    that belong to the selection, so a transform can be applied to exactly those
    and leave every other shell untouched (no shared vertex can drag along).
    """
    if not mask.any():
        raise ThreeMFError("EMPTY_SELECTION", "选区没有命中任何三角形")
    selected = triangles[mask]
    used = np.unique(selected.ravel())
    remap = np.arange(len(vertices), dtype=np.int64)
    copies = len(vertices) + np.arange(len(used), dtype=np.int64)
    remap[used] = copies
    moved = np.zeros(len(vertices) + len(used), dtype=bool)
    moved[copies] = True
    new_triangles = triangles.copy()
    new_triangles[mask] = remap[selected]
    new_vertices = np.vstack([vertices, vertices[used]])
    return new_vertices, new_triangles, moved


def write_stl(mesh: Mesh, name: str = "bambu3mf") -> bytes:
    triangles = mesh.triangles
    vertices = mesh.vertices
    header = f"bambu3mf {name}".encode("utf-8")[:80].ljust(80, b" ")
    buffer = bytearray(header)
    buffer.extend(struct.pack("<I", len(triangles)))
    if len(triangles):
        v0 = vertices[triangles[:, 0]]
        v1 = vertices[triangles[:, 1]]
        v2 = vertices[triangles[:, 2]]
        normals = np.cross(v1 - v0, v2 - v0)
        lengths = np.linalg.norm(normals, axis=1)
        lengths[lengths == 0] = 1.0
        normals = normals / lengths[:, None]
        for index in range(len(triangles)):
            record = np.concatenate([normals[index], v0[index], v1[index], v2[index]])
            buffer.extend(np.asarray(record, dtype="<f4").tobytes())
            buffer.extend(b"\x00\x00")
    return bytes(buffer)


# --------------------------------------------------------------- topology facts
def topology_report(
    mesh: Mesh,
    tolerance: float = 1e-6,
    thin_mm: float = 0.01,
    deep: bool = False,
    max_shells: int = 32,
) -> dict:
    """The one topology report every caller shares.

    ``watertight`` alone is not enough to trust a mesh: a union can leave
    coplanar internal faces, a boolean can leave a zero-thickness sheet, and a
    mesh with mixed triangle orientation reports a signed volume that is pure
    cancellation. This report separates those facts so ``analyze`` can say
    "volume unreliable" instead of printing 1.7 cm^3 for a 5.9 cm^3 part.
    """
    if mesh.is_empty():
        return {
            "triangles": 0,
            "vertices": 0,
            "boundaryEdges": 0,
            "nonManifoldEdges": 0,
            "interiorEdges": 0,
            "watertight": False,
            "orientationConsistent": False,
            "volumeReliable": False,
            "shells": 0,
            "genus": None,
            "negativeShells": 0,
            "mixedWinding": False,
            "volumeMm3": None,
            "netVolumeMm3": None,
            "degenerateTriangles": 0,
            "duplicateFaces": 0,
            "opposingFaces": 0,
            "thinSheets": 0,
            "shellsReport": [],
            "thinSheetDetails": [],
            "nestedShells": [],
            "coincidentFacePairs": 0,
            "coincidentSamples": [],
            "coincidentTruncated": False,
        }

    vertices = mesh.vertices
    triangles = mesh.triangles
    inverse = welded_index(vertices, tolerance)
    welded = inverse[triangles]
    faces = int(len(welded))
    used_vertices = int(len(np.unique(welded)))
    boundary, non_manifold, interior = edge_counts(welded)
    unique_edges = int(boundary + non_manifold + interior)
    watertight = boundary == 0 and non_manifold == 0

    labels = connected_components(mesh, tolerance)
    shells = int(labels.max()) + 1 if len(labels) else 0

    # Direction conflicts: a consistently oriented closed surface traverses
    # every interior edge once in each direction. Two same-direction uses mean
    # the triangle winding is mixed, so signed volume cannot be trusted.
    directed = np.concatenate(
        [welded[:, [0, 1]], welded[:, [1, 2]], welded[:, [2, 0]]], axis=0
    )
    tri_index = np.tile(np.arange(faces, dtype=np.int64), 3)
    signs = np.where(directed[:, 0] < directed[:, 1], 1, -1)
    keys = np.sort(directed, axis=1)
    _unique_edges, edge_inverse = np.unique(keys, axis=0, return_inverse=True)
    counts = np.bincount(edge_inverse)
    sign_sum = np.bincount(edge_inverse, weights=signs)
    conflict = (counts == 2) & (np.abs(sign_sum) == 2)
    conflict_per_shell = np.zeros(shells, dtype=np.int64)
    if conflict.any():
        order = np.argsort(edge_inverse, kind="stable")
        sorted_inverse = edge_inverse[order]
        conflict_entries = order[conflict[sorted_inverse]]
        conflict_per_shell = (
            np.bincount(labels[tri_index[conflict_entries]], minlength=shells) // 2
        )
    orientation_consistent = not bool(conflict.any())

    v0 = vertices[triangles[:, 0]]
    v1 = vertices[triangles[:, 1]]
    v2 = vertices[triangles[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(cross, axis=1)
    triangle_volume = np.einsum("ij,ij->i", v0, np.cross(v1, v2)) / 6.0
    shell_signed = np.bincount(labels, weights=triangle_volume, minlength=shells)
    negative_shells = int((shell_signed < -1e-9).sum())
    mixed_winding = bool(negative_shells and (shell_signed > 1e-9).any())
    volume_reliable = bool(watertight and orientation_consistent)
    volume = float(np.abs(shell_signed).sum()) if volume_reliable else None
    net_volume = float(np.abs(shell_signed.sum())) if volume_reliable else None

    degenerate_mask = lengths <= 2e-12
    repeated = (
        (welded[:, 0] == welded[:, 1])
        | (welded[:, 1] == welded[:, 2])
        | (welded[:, 0] == welded[:, 2])
    )
    degenerate = int((degenerate_mask | repeated).sum())

    canonical = np.sort(welded, axis=1)
    _canon, canon_inverse, canon_counts = np.unique(
        canonical, axis=0, return_inverse=True, return_counts=True
    )
    duplicate_faces = int((canon_counts - 1).sum())

    # Orientation parity within a face's sorted vertex order: two triangles
    # using the same three vertices with opposite parity are an internal wall.
    order = np.argsort(welded, axis=1, kind="stable")
    inversions = (
        (order[:, 0] > order[:, 1]).astype(np.int8)
        + (order[:, 0] > order[:, 2]).astype(np.int8)
        + (order[:, 1] > order[:, 2]).astype(np.int8)
    )
    parity = (inversions % 2).astype(np.int8)
    even = np.bincount(canon_inverse, weights=(parity == 0).astype(np.float64))
    odd = np.bincount(canon_inverse, weights=(parity == 1).astype(np.float64))
    opposing_faces = int(np.minimum(even, odd).sum())

    # Per-shell geometry via vectorised vertex aggregation; a Python loop over
    # every shell would be quadratic on lattice-like models.
    shell_lo = np.full((shells, 3), np.inf)
    shell_hi = np.full((shells, 3), -np.inf)
    vertex_shell = np.full(len(vertices), -1, dtype=np.int64)
    for corner in range(3):
        vertex_shell[triangles[:, corner]] = labels
    valid = vertex_shell >= 0
    if valid.any():
        np.minimum.at(shell_lo, vertex_shell[valid], vertices[valid])
        np.maximum.at(shell_hi, vertex_shell[valid], vertices[valid])
    shell_sizes = shell_hi - shell_lo
    shell_triangles = np.bincount(labels, minlength=shells)
    thin_mask = np.zeros(shells, dtype=bool)
    if shells:
        thin_mask = (shell_sizes.min(axis=1) <= float(thin_mm)) | (
            watertight & (np.abs(shell_signed) <= float(thin_mm) ** 3)
        )
    thin_sheets = int(thin_mask.sum())

    shells_report = []
    for index in range(min(shells, max_shells)):
        shells_report.append(
            {
                "index": index,
                "triangles": int(shell_triangles[index]),
                "bbox": [round(float(value), 4) for value in np.concatenate([shell_lo[index], shell_hi[index]])],
                "size": [round(float(value), 4) for value in shell_sizes[index]],
                "center": [
                    round(float(value), 4) for value in ((shell_lo[index] + shell_hi[index]) / 2.0)
                ],
                "signedVolumeMm3": round(float(shell_signed[index]), 3),
                "negative": bool(shell_signed[index] < -1e-9),
                "orientationConflicts": int(conflict_per_shell[index]),
                "thin": bool(thin_mask[index]),
            }
        )

    thin_details = []
    for index in np.nonzero(thin_mask)[0][:20]:
        thin_details.append(
            {
                "shell": int(index),
                "triangles": int(shell_triangles[index]),
                "bbox": [round(float(value), 4) for value in np.concatenate([shell_lo[index], shell_hi[index]])],
                "size": [round(float(value), 4) for value in shell_sizes[index]],
                "signedVolumeMm3": round(float(shell_signed[index]), 3),
            }
        )

    nested = []
    limit = min(shells, 400)
    for outer in range(limit):
        outer_lo, outer_hi = shell_lo[outer], shell_hi[outer]
        for inner in range(limit):
            if inner == outer:
                continue
            if abs(shell_signed[inner]) >= abs(shell_signed[outer]):
                continue
            if (
                np.all(shell_lo[inner] >= outer_lo - 1e-6)
                and np.all(shell_hi[inner] <= outer_hi + 1e-6)
            ):
                nested.append({"outer": int(outer), "inner": int(inner)})
                if len(nested) >= 20:
                    break
        if len(nested) >= 20:
            break

    coincident_pairs = 0
    coincident_samples = []
    coincident_truncated = False
    if deep:
        coincident_pairs, coincident_samples, coincident_truncated = _coincident_pairs(
            vertices, triangles, welded
        )

    genus = None
    if watertight and orientation_consistent and faces and shells:
        chi = used_vertices - unique_edges + faces
        numerator = 2 * shells - chi
        if numerator >= 0 and numerator % 2 == 0:
            genus = numerator // 2

    return {
        "triangles": faces,
        "vertices": used_vertices,
        "boundaryEdges": int(boundary),
        "nonManifoldEdges": int(non_manifold),
        "interiorEdges": int(interior),
        "watertight": bool(watertight),
        "orientationConsistent": bool(orientation_consistent),
        "volumeReliable": bool(volume_reliable),
        "shells": int(shells),
        "genus": genus,
        "negativeShells": negative_shells,
        "mixedWinding": mixed_winding,
        "volumeMm3": None if volume is None else round(volume, 3),
        "netVolumeMm3": None if net_volume is None else round(net_volume, 3),
        "degenerateTriangles": degenerate,
        "duplicateFaces": duplicate_faces,
        "opposingFaces": opposing_faces,
        "thinSheets": thin_sheets,
        "shellsReport": shells_report,
        "thinSheetDetails": thin_details,
        "nestedShells": nested,
        "coincidentFacePairs": int(coincident_pairs),
        "coincidentSamples": coincident_samples,
        "coincidentTruncated": bool(coincident_truncated),
    }


def _point_in_triangle_2d(point: np.ndarray, a, b, c) -> bool:
    def cross(origin, first, second):
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (
            first[1] - origin[1]
        ) * (second[0] - origin[0])

    d1 = cross(a, b, point)
    d2 = cross(b, c, point)
    d3 = cross(c, a, point)
    has_neg = d1 < -1e-12 or d2 < -1e-12 or d3 < -1e-12
    has_pos = d1 > 1e-12 or d2 > 1e-12 or d3 > 1e-12
    return not (has_neg and has_pos)


def _segments_intersect_2d(a, b, c, d) -> bool:
    def cross(origin, first, second):
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (
            first[1] - origin[1]
        ) * (second[0] - origin[0])

    d1 = cross(c, d, a)
    d2 = cross(c, d, b)
    d3 = cross(a, b, c)
    d4 = cross(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _triangles_overlap_2d(a, b, c, d, e, f) -> bool:
    for point, (p, q, r) in (
        (a, (d, e, f)),
        (b, (d, e, f)),
        (c, (d, e, f)),
        (d, (a, b, c)),
        (e, (a, b, c)),
        (f, (a, b, c)),
    ):
        if _point_in_triangle_2d(point, p, q, r):
            return True
    for first, second in ((a, b), (b, c), (c, a)):
        for third, fourth in ((d, e), (e, f), (f, d)):
            if _segments_intersect_2d(first, second, third, fourth):
                return True
    return False


def _coincident_pairs(vertices, triangles, welded, group_cap: int = 300, check_cap: int = 200000):
    """Coplanar, oppositely oriented triangle pairs that overlap in area.

    These are the internal walls a robust boolean should remove but a naive
    mesh union or an external tool can leave behind: the model is still
    watertight, yet the slicer sees two parts glued face to face.
    """
    v0 = vertices[triangles[:, 0]]
    v1 = vertices[triangles[:, 1]]
    v2 = vertices[triangles[:, 2]]
    normals = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(normals, axis=1)
    keep = lengths > 1e-12
    if int(keep.sum()) < 2:
        return 0, [], False
    unit = normals[keep] / lengths[keep, None]
    original_unit = unit.copy()
    first = v0[keep]
    offset = np.einsum("ij,ij->i", first, unit)
    # Canonical plane orientation: opposite-facing triangles must land in the
    # same group, so flip every normal into a fixed hemisphere.
    flip = (
        (unit[:, 0] < 0)
        | ((np.abs(unit[:, 0]) <= 1e-12) & (unit[:, 1] < 0))
        | (
            (np.abs(unit[:, 0]) <= 1e-12)
            & (np.abs(unit[:, 1]) <= 1e-12)
            & (unit[:, 2] < 0)
        )
    )
    unit = np.where(flip[:, None], -unit, unit)
    offset = np.where(flip, -offset, offset)
    plane = np.column_stack(
        [np.round(unit, 4), np.round(offset, 3)]
    )
    _planes, plane_inverse = np.unique(plane, axis=0, return_inverse=True)
    indices = np.nonzero(keep)[0]
    order = np.argsort(plane_inverse, kind="stable")
    sorted_inverse = plane_inverse[order]
    starts = np.searchsorted(sorted_inverse, np.arange(int(plane_inverse.max()) + 1))
    ends = np.searchsorted(
        sorted_inverse, np.arange(int(plane_inverse.max()) + 1), side="right"
    )
    pairs = 0
    samples = []
    truncated = False
    checked = 0
    for group in range(len(starts)):
        members = indices[order[starts[group] : ends[group]]]
        if len(members) < 2:
            continue
        if len(members) > group_cap:
            members = members[:group_cap]
            truncated = True
        points = [v0[members], v1[members], v2[members]]
        group_normals = original_unit[members]
        basis_u, basis_v = _plane_basis(group_normals[0])
        projected = [
            np.column_stack([points[k] @ basis_u, points[k] @ basis_v])
            for k in range(3)
        ]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                checked += 1
                if checked > check_cap:
                    return pairs, samples, True
                if float(np.dot(group_normals[i], group_normals[j])) > -0.9:
                    continue
                if _triangles_overlap_2d(
                    projected[0][i],
                    projected[1][i],
                    projected[2][i],
                    projected[0][j],
                    projected[1][j],
                    projected[2][j],
                ):
                    pairs += 1
                    if len(samples) < 5:
                        centre = (
                            v0[members[i]] + v1[members[i]] + v2[members[i]]
                        ) / 3.0
                        samples.append(
                            {
                                "triangles": [int(members[i]), int(members[j])],
                                "center": [round(float(value), 3) for value in centre],
                            }
                        )
    return pairs, samples, truncated


# ------------------------------------------------------------------- primitives
def primitive_box(size, center) -> Mesh:
    """Axis-aligned closed box with outward winding."""
    size = np.asarray(size, dtype=np.float64).reshape(3)
    center = np.asarray(center, dtype=np.float64).reshape(3)
    if np.any(size <= 0):
        raise ThreeMFError("INVALID_ARGS", "box 的 size 三个分量都必须为正")
    half = size / 2.0
    corners = np.array(
        [
            [-1, -1, -1],
            [1, -1, -1],
            [1, 1, -1],
            [-1, 1, -1],
            [-1, -1, 1],
            [1, -1, 1],
            [1, 1, 1],
            [-1, 1, 1],
        ],
        dtype=np.float64,
    )
    vertices = corners * half + center
    triangles = np.array(
        [
            [0, 2, 1],
            [0, 3, 2],
            [4, 5, 6],
            [4, 6, 7],
            [0, 1, 5],
            [0, 5, 4],
            [1, 2, 6],
            [1, 6, 5],
            [2, 3, 7],
            [2, 7, 6],
            [3, 0, 4],
            [3, 4, 7],
        ],
        dtype=np.int64,
    )
    return Mesh(vertices, triangles, [])


def _axis_rotation(axis: str) -> np.ndarray:
    if axis == "z":
        return np.eye(3)
    if axis == "x":
        # Cylinder built along +Z, rotated to +X.
        return np.array([[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]])
    if axis == "y":
        return np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])
    raise ThreeMFError("INVALID_ARGS", "axis 必须是 x/y/z")


def primitive_cylinder(radius, height, axis, center, segments: int = 64) -> Mesh:
    radius = float(radius)
    height = float(height)
    if radius <= 0 or height <= 0:
        raise ThreeMFError("INVALID_ARGS", "cylinder 的 radius 和 height 必须为正")
    segments = max(8, min(512, int(segments)))
    angles = np.linspace(0.0, 2.0 * math.pi, segments, endpoint=False)
    ring = np.column_stack([np.cos(angles) * radius, np.sin(angles) * radius])
    vertices = []
    for z in (-height / 2.0, height / 2.0):
        for x, y in ring:
            vertices.append([x, y, z])
    vertices.extend([[0.0, 0.0, -height / 2.0], [0.0, 0.0, height / 2.0]])
    bottom_center = 2 * segments
    top_center = bottom_center + 1
    triangles = []
    for index in range(segments):
        nxt = (index + 1) % segments
        b0, b1 = index, nxt
        t0, t1 = segments + index, segments + nxt
        triangles.append([b0, b1, t1])
        triangles.append([b0, t1, t0])
        triangles.append([bottom_center, b1, b0])
        triangles.append([top_center, t0, t1])
    array = np.asarray(vertices, dtype=np.float64) @ _axis_rotation(axis).T
    array = array + np.asarray(center, dtype=np.float64).reshape(3)
    return Mesh(array, np.asarray(triangles, dtype=np.int64), [])


def primitive_sphere(radius, center, segments: int = 48) -> Mesh:
    radius = float(radius)
    if radius <= 0:
        raise ThreeMFError("INVALID_ARGS", "sphere 的 radius 必须为正")
    segments = max(12, min(256, int(segments)))
    rings = max(3, segments // 2)
    vertices = [[0.0, 0.0, radius]]
    for ring in range(1, rings):
        phi = math.pi * ring / rings
        z = radius * math.cos(phi)
        r = radius * math.sin(phi)
        for step in range(segments):
            theta = 2.0 * math.pi * step / segments
            vertices.append([r * math.cos(theta), r * math.sin(theta), z])
    bottom = len(vertices)
    vertices.append([0.0, 0.0, -radius])
    triangles = []
    for step in range(segments):
        nxt = (step + 1) % segments
        triangles.append([0, 1 + step, 1 + nxt])
    for ring in range(rings - 2):
        base = 1 + ring * segments
        nxt_base = base + segments
        for step in range(segments):
            nxt = (step + 1) % segments
            triangles.append([base + step, nxt_base + step, nxt_base + nxt])
            triangles.append([base + step, nxt_base + nxt, base + nxt])
    last = 1 + (rings - 2) * segments
    for step in range(segments):
        nxt = (step + 1) % segments
        triangles.append([bottom, last + nxt, last + step])
    array = np.asarray(vertices, dtype=np.float64)
    array = array + np.asarray(center, dtype=np.float64).reshape(3)
    return Mesh(array, np.asarray(triangles, dtype=np.int64), [])


# --------------------------------------------------------------------- sections
def section_mesh(
    mesh: Mesh,
    point: np.ndarray,
    normal: np.ndarray,
    max_points: int = 2000,
) -> dict:
    """Intersect a mesh with a plane and chain the segments into 2D loops."""
    normal = np.asarray(normal, dtype=np.float64)
    length = float(np.linalg.norm(normal))
    if length < 1e-12:
        raise ThreeMFError("INVALID_ARGS", "section 的 normal 不能为零")
    normal = normal / length
    point = np.asarray(point, dtype=np.float64)
    if mesh.is_empty():
        return {"loops": [], "areaMm2": 0.0, "perimeterMm": 0.0, "bbox": None, "basis": None}

    distance = (mesh.vertices - point) @ normal
    scale = max(1.0, float(np.abs(mesh.vertices).max()))
    on_plane = np.abs(distance) <= 1e-7 * scale
    segments: list[tuple[int, int]] = []
    points: list[np.ndarray] = []
    lookup: dict[tuple[int, int, int], int] = {}

    def node(position: np.ndarray) -> int:
        key = tuple(int(round(float(value) / (1e-6 * scale))) for value in position)
        existing = lookup.get(key)
        if existing is not None:
            return existing
        index = len(points)
        points.append(np.asarray(position, dtype=np.float64))
        lookup[key] = index
        return index

    for triangle in mesh.triangles.tolist():
        flags = [bool(on_plane[index]) for index in triangle]
        if all(flags):
            continue
        distances = [float(distance[index]) for index in triangle]
        if all(value > 0 for value in distances) or all(value < 0 for value in distances):
            continue
        hits: list[int] = []
        for offset in range(3):
            a = triangle[offset]
            b = triangle[(offset + 1) % 3]
            da, db = distances[offset], distances[(offset + 1) % 3]
            if abs(da) <= 1e-9 * scale:
                hits.append(node(mesh.vertices[a]))
            elif da * db < 0:
                t = da / (da - db)
                hits.append(node(mesh.vertices[a] + t * (mesh.vertices[b] - mesh.vertices[a])))
        unique: list[int] = []
        for index in hits:
            if index not in unique:
                unique.append(index)
        if len(unique) == 2:
            segments.append((unique[0], unique[1]))

    if not segments:
        return {"loops": [], "areaMm2": 0.0, "perimeterMm": 0.0, "bbox": None, "basis": None}

    adjacency: dict[int, list[int]] = {}
    for a, b in segments:
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    basis_u, basis_v = _plane_basis(normal)
    projected = [
        np.array([float(np.dot(position - point, basis_u)), float(np.dot(position - point, basis_v))])
        for position in points
    ]

    used: set[tuple[int, int]] = set()
    loops = []
    for start in list(adjacency):
        for neighbour in adjacency[start]:
            key = (min(start, neighbour), max(start, neighbour))
            if key in used:
                continue
            chain = [start]
            previous, current = start, neighbour
            used.add(key)
            opened = False
            while current != start:
                chain.append(current)
                candidates = adjacency.get(current, [])
                if len(candidates) != 2:
                    opened = True
                    break
                nxt = candidates[0] if candidates[1] == previous else candidates[1]
                edge = (min(current, nxt), max(current, nxt))
                if edge in used:
                    opened = True
                    break
                used.add(edge)
                previous, current = current, nxt
                if len(chain) > len(segments) + 5:
                    opened = True
                    break
            coordinates = np.array([projected[index] for index in chain])
            area = float(abs(_polygon_area(coordinates))) if len(coordinates) >= 3 else 0.0
            perimeter = 0.0
            if len(coordinates) >= 2:
                if opened:
                    perimeter = float(
                        np.linalg.norm(np.diff(coordinates, axis=0), axis=1).sum()
                    )
                else:
                    perimeter = float(
                        np.linalg.norm(
                            np.roll(coordinates, -1, axis=0) - coordinates, axis=1
                        ).sum()
                    )
            output = coordinates
            truncated = False
            if len(output) > max_points:
                step = max(1, len(output) // max_points)
                output = output[::step][:max_points]
                truncated = True
            loops.append(
                {
                    "closed": not opened,
                    "points": [[round(float(value), 4) for value in pair] for pair in output],
                    "areaMm2": round(area, 3),
                    "perimeterMm": round(perimeter, 3),
                    "truncated": truncated,
                }
            )

    if not loops:
        return {"loops": [], "areaMm2": 0.0, "perimeterMm": 0.0, "bbox": None, "basis": None}

    # Net area uses nesting parity: an even depth is material, an odd depth is a hole.
    net_area = 0.0
    for index, loop in enumerate(loops):
        coordinates = np.array(loop["points"], dtype=np.float64)
        probe = coordinates[0] if len(coordinates) else np.zeros(2)
        depth = 0
        for other in range(len(loops)):
            if other == index:
                continue
            polygon = np.array(loops[other]["points"], dtype=np.float64)
            if len(polygon) >= 3 and _point_in_polygon(probe, polygon):
                depth += 1
        net_area += loop["areaMm2"] if depth % 2 == 0 else -loop["areaMm2"]
    intersection_points = np.array([points[index] for index in lookup.values()])
    bbox = (
        [round(float(value), 3) for value in np.concatenate([intersection_points.min(0), intersection_points.max(0)])]
        if len(intersection_points)
        else None
    )
    return {
        "loops": loops,
        "areaMm2": round(float(net_area), 3),
        "perimeterMm": round(float(sum(loop["perimeterMm"] for loop in loops)), 3),
        "bbox": bbox,
        "basis": {
            "origin": [round(float(value), 6) for value in point],
            "u": [round(float(value), 6) for value in basis_u],
            "v": [round(float(value), 6) for value in basis_v],
            "normal": [round(float(value), 6) for value in normal],
        },
    }


# ------------------------------------------------------------------ measurement
def _region_triangles(vertices, triangles, bbox) -> np.ndarray:
    lower = np.asarray(bbox[:3], dtype=np.float64)
    upper = np.asarray(bbox[3:], dtype=np.float64)
    if np.any(upper <= lower):
        raise ThreeMFError("INVALID_ARGS", "bbox 必须是 [xmin,ymin,zmin,xmax,ymax,zmax] 且 max>min")
    tri_lo = vertices[triangles].min(axis=1)
    tri_hi = vertices[triangles].max(axis=1)
    mask = np.all(tri_lo <= upper, axis=1) & np.all(tri_hi >= lower, axis=1)
    return np.nonzero(mask)[0]


def heightmap(
    mesh: Mesh,
    bbox,
    axis: str = "z",
    grid: int = 64,
    max_triangles: int = 200000,
) -> dict:
    """Maximum surface coordinate along ``axis`` on a regular 2D grid."""
    axis_index = {"x": 0, "y": 1, "z": 2}.get(str(axis))
    if axis_index is None:
        raise ThreeMFError("INVALID_ARGS", "heightmap 的 axis 必须是 x/y/z")
    grid = max(8, min(256, int(grid)))
    lower = np.asarray(bbox[:3], dtype=np.float64)
    upper = np.asarray(bbox[3:], dtype=np.float64)
    selected = _region_triangles(mesh.vertices, mesh.triangles, bbox)
    if len(selected) > max_triangles:
        raise ThreeMFError(
            "MEASURE_TOO_LARGE",
            f"bbox 内有 {len(selected)} 个三角形（上限 {max_triangles}）；请缩小 bbox 或降低精度",
            {"triangles": int(len(selected)), "limit": int(max_triangles)},
        )
    plane_axes = [index for index in range(3) if index != axis_index]
    rows, cols = plane_axes[0], plane_axes[1]
    row_edges = np.linspace(lower[rows], upper[rows], grid + 1)
    col_edges = np.linspace(lower[cols], upper[cols], grid + 1)
    row_centers = (row_edges[:-1] + row_edges[1:]) / 2.0
    col_centers = (col_edges[:-1] + col_edges[1:]) / 2.0
    values = np.full((grid, grid), -np.inf, dtype=np.float64)

    for triangle_index in selected.tolist():
        triangle = mesh.triangles[triangle_index]
        coordinates = mesh.vertices[triangle]
        r = coordinates[:, rows]
        c = coordinates[:, cols]
        r0, r1 = float(r.min()), float(r.max())
        c0, c1 = float(c.min()), float(c.max())
        ri = np.nonzero((row_centers >= r0) & (row_centers <= r1))[0]
        ci = np.nonzero((col_centers >= c0) & (col_centers <= c1))[0]
        if len(ri) == 0 or len(ci) == 0:
            continue
        rr, cc = np.meshgrid(row_centers[ri], col_centers[ci], indexing="ij")
        p0, p1, p2 = coordinates
        denominator = (p1[rows] - p0[rows]) * (p2[cols] - p0[cols]) - (
            p2[rows] - p0[rows]
        ) * (p1[cols] - p0[cols])
        if abs(denominator) < 1e-15:
            continue
        a = ((rr - p0[rows]) * (p2[cols] - p0[cols]) - (p2[rows] - p0[rows]) * (cc - p0[cols])) / denominator
        b = ((p1[rows] - p0[rows]) * (cc - p0[cols]) - (rr - p0[rows]) * (p1[cols] - p0[cols])) / denominator
        gamma = 1.0 - a - b
        inside = (a >= -1e-9) & (b >= -1e-9) & (gamma >= -1e-9)
        if not inside.any():
            continue
        surface = (
            a * p0[axis_index] + b * p1[axis_index] + gamma * p2[axis_index]
        )
        block = values[np.ix_(ri, ci)]
        np.maximum(block, np.where(inside, surface, -np.inf), out=block)
        values[np.ix_(ri, ci)] = block

    finite = values[np.isfinite(values)]
    serialised = [
        [None if not np.isfinite(value) else round(float(value), 4) for value in row]
        for row in values
    ]
    return {
        "mode": "heightmap",
        "axis": str(axis),
        "rowAxis": "xyz"[rows],
        "colAxis": "xyz"[cols],
        "grid": grid,
        "bbox": [round(float(value), 4) for value in np.concatenate([lower, upper])],
        "values": serialised,
        "min": round(float(finite.min()), 4) if len(finite) else None,
        "max": round(float(finite.max()), 4) if len(finite) else None,
        "mean": round(float(finite.mean()), 4) if len(finite) else None,
        "cells": int(len(finite)),
        "triangleCount": int(len(selected)),
    }


def section_svg(section: dict, path: str, width: int = 800) -> str:
    """Write a section as an SVG (millimetre viewBox) and return its path."""
    import os

    loops = section.get("loops") or []
    points = [point for loop in loops for point in loop.get("points") or []]
    if not points:
        raise ThreeMFError("SECTION_EMPTY", "剖面没有可写出的环")
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    lo_x, hi_x = min(xs), max(xs)
    lo_y, hi_y = min(ys), max(ys)
    span_x = max(hi_x - lo_x, 1e-6)
    span_y = max(hi_y - lo_y, 1e-6)
    margin = 8.0
    scale = (width - 2 * margin) / span_x
    height = int(round(span_y * scale + 2 * margin))
    body = []
    for loop in loops:
        pairs = loop.get("points") or []
        if len(pairs) < 3:
            continue
        commands = []
        for index, (x, y) in enumerate(pairs):
            px = margin + (float(x) - lo_x) * scale
            py = height - margin - (float(y) - lo_y) * scale
            commands.append(f"{'M' if index == 0 else 'L'}{px:.3f},{py:.3f}")
        body.append('<path d="' + " ".join(commands) + ' Z" />')
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="#1a1c20"/>'
        '<g fill="#e8e8e8" fill-opacity="0.85" fill-rule="evenodd" '
        'stroke="#f0a040" stroke-width="1">'
        + "".join(body)
        + "</g></svg>"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(svg)
    return os.path.abspath(path)


def material_spans(mesh: Mesh, bbox, axis: str = "z") -> dict:
    """Merged triangle spans along one axis inside a region.

    This is a locator, not a solid-inside test: a triangle whose vertices cross
    the region contributes its whole span. It answers "where is material along
    this axis" in one cheap pass.
    """
    axis_index = {"x": 0, "y": 1, "z": 2}.get(str(axis))
    if axis_index is None:
        raise ThreeMFError("INVALID_ARGS", "spans 的 axis 必须是 x/y/z")
    selected = _region_triangles(mesh.vertices, mesh.triangles, bbox)
    if len(selected) == 0:
        return {
            "mode": "spans",
            "axis": str(axis),
            "spans": [],
            "coverageMm": 0.0,
            "triangleCount": 0,
            "extent": None,
        }
    coordinates = mesh.vertices[mesh.triangles[selected]][:, :, axis_index]
    low = coordinates.min(axis=1)
    high = coordinates.max(axis=1)
    order = np.argsort(low)
    merged: list[list[float]] = []
    for index in order.tolist():
        start, end = float(low[index]), float(high[index])
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    coverage = float(sum(end - start for start, end in merged))
    return {
        "mode": "spans",
        "axis": str(axis),
        "spans": [[round(float(start), 3), round(float(end), 3)] for start, end in merged],
        "coverageMm": round(coverage, 3),
        "triangleCount": int(len(selected)),
        "extent": [round(float(low.min()), 3), round(float(high.max()), 3)],
    }


def region_extent(mesh: Mesh, bbox) -> dict:
    selected = _region_triangles(mesh.vertices, mesh.triangles, bbox)
    if len(selected) == 0:
        return {"mode": "extent", "triangles": 0, "bbox": None, "size": None}
    points = mesh.vertices[np.unique(mesh.triangles[selected].ravel())]
    lo, hi = points.min(axis=0), points.max(axis=0)
    return {
        "mode": "extent",
        "triangles": int(len(selected)),
        "bbox": [round(float(value), 3) for value in np.concatenate([lo, hi])],
        "size": [round(float(value), 3) for value in (hi - lo)],
    }
