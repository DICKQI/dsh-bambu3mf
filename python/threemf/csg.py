"""Robust mesh booleans, backed by manifold3d.

manifold3d is an optional dependency: only ``bambu3mf_build`` op ``boolean``
needs it, while primitives, splitting, replacing, sections and measurements
work with numpy alone. Keeping the import inside this module means an
installation without the wheel still gets every other tool, and the error it
does get names the exact Python executable and install command.
"""

from __future__ import annotations

import importlib.metadata

import numpy as np

from .constants import ThreeMFError

INSTALL_PIN = "manifold3d==3.5.4"


def _engine():
    try:
        import manifold3d  # noqa: PLC0415 - optional dependency
    except Exception as exc:  # noqa: BLE001 - report any import failure
        import sys

        raise ThreeMFError(
            "BOOLEAN_ENGINE_MISSING",
            "布尔运算需要 manifold3d，但当前 Python 无法导入它",
            {
                "python": sys.executable,
                "install": f'"{sys.executable}" -m pip install {INSTALL_PIN}',
                "reason": f"{type(exc).__name__}: {exc}",
            },
        ) from exc
    return manifold3d


def engine_version() -> str | None:
    """Version string without importing the extension if it is absent."""
    try:
        return importlib.metadata.version("manifold3d")
    except importlib.metadata.PackageNotFoundError:
        return None


def available() -> bool:
    return engine_version() is not None


def _status_name(module, status) -> str:
    try:
        return str(status).split(".")[-1]
    except Exception:  # noqa: BLE001
        return "unknown"


def _to_manifold(vertices, triangles, label: str):
    module = _engine()
    vertices = np.ascontiguousarray(vertices, dtype=np.float32).reshape(-1, 3)
    triangles = np.ascontiguousarray(triangles, dtype=np.uint32).reshape(-1, 3)
    if len(vertices) == 0 or len(triangles) == 0:
        raise ThreeMFError("BOOLEAN_OPERAND_EMPTY", f"{label} 没有网格数据")
    if triangles.max(initial=0) >= len(vertices):
        raise ThreeMFError(
            "BOOLEAN_OPERAND_OPEN",
            f"{label} 的三角形索引越界，无法做布尔",
            {"vertices": int(len(vertices)), "triangles": int(len(triangles))},
        )
    try:
        manifold = module.Manifold(module.Mesh(vertices, triangles))
    except Exception as exc:  # noqa: BLE001 - nanobind raises various errors
        raise ThreeMFError(
            "BOOLEAN_FAILED",
            f"{label} 无法导入布尔内核：{type(exc).__name__}: {exc}",
        ) from exc
    status = manifold.status()
    if status != module.Error.NoError:
        from .geometry import topology_report
        from .model import Mesh

        report = topology_report(Mesh(vertices.astype(np.float64), triangles.astype(np.int64), []))
        raise ThreeMFError(
            "BOOLEAN_OPERAND_OPEN",
            f"{label} 不是流形实体（{_status_name(module, status)}）；"
            f"边界边 {report['boundaryEdges']}、非流形边 {report['nonManifoldEdges']}。"
            "请先修补网格，或改用 mesh op=weld/cleanup",
            {
                "status": _status_name(module, status),
                "boundaryEdges": int(report["boundaryEdges"]),
                "nonManifoldEdges": int(report["nonManifoldEdges"]),
            },
        )
    if manifold.is_empty():
        raise ThreeMFError("BOOLEAN_OPERAND_EMPTY", f"{label} 导入后为空")
    return manifold


def _from_manifold(manifold) -> tuple[np.ndarray, np.ndarray]:
    mesh = manifold.to_mesh()
    vertices = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    triangles = np.asarray(mesh.tri_verts, dtype=np.int64)
    return vertices, triangles


def _summary(manifold) -> dict:
    try:
        triangles = int(manifold.num_tri())
    except Exception:  # noqa: BLE001
        triangles = 0
    try:
        components = len(manifold.decompose()) if not manifold.is_empty() else 0
    except Exception:  # noqa: BLE001
        components = 0
    volume = 0.0
    if not manifold.is_empty():
        try:
            volume = float(manifold.volume())
        except Exception:  # noqa: BLE001
            volume = 0.0
    return {"triangles": triangles, "components": components, "volumeMm3": round(volume, 3)}


def boolean(
    vertices_a: np.ndarray,
    triangles_a: np.ndarray,
    vertices_b: np.ndarray,
    triangles_b: np.ndarray,
    operation: str,
) -> dict:
    """Run one union/subtract/intersect and return a plain numpy mesh."""
    module = _engine()
    if operation not in ("union", "subtract", "intersect"):
        raise ThreeMFError("INVALID_ARGS", "operation 必须是 union/subtract/intersect")
    target = _to_manifold(vertices_a, triangles_a, "目标对象")
    operand = _to_manifold(vertices_b, triangles_b, "操作数")

    intersection = target ^ operand
    intersection_volume = 0.0 if intersection.is_empty() else float(intersection.volume())

    if operation == "union":
        result = target + operand
    elif operation == "subtract":
        result = target - operand
    else:
        result = intersection

    if result.is_empty():
        raise ThreeMFError(
            "BOOLEAN_EMPTY_RESULT",
            f"{operation} 的结果为空（目标与操作数没有交叠）",
            {
                "operation": operation,
                "intersectionVolumeMm3": round(intersection_volume, 3),
            },
        )
    result_status = result.status()
    if result_status != module.Error.NoError:
        raise ThreeMFError(
            "BOOLEAN_FAILED",
            f"{operation} 失败：{_status_name(module, result_status)}",
            {"status": _status_name(module, result_status)},
        )

    vertices, triangles = _from_manifold(result)
    info = _summary(result)
    info.update(
        {
            "operation": operation,
            "intersectionVolumeMm3": round(intersection_volume, 3),
            "targetVolumeMm3": round(float(target.volume()), 3),
            "operandVolumeMm3": round(float(operand.volume()), 3),
        }
    )
    return {"vertices": vertices, "triangles": triangles, "stats": info}


def split_plane(vertices, triangles, point, normal) -> tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]] | None:
    """Optional robust split; ``None`` means the caller should fall back to plane_cut."""
    module = _engine()
    manifold = _to_manifold(vertices, triangles, "拆分对象")
    normal = np.asarray(normal, dtype=np.float64)
    length = float(np.linalg.norm(normal))
    if length < 1e-12:
        raise ThreeMFError("INVALID_ARGS", "拆分的平面法向不能为零")
    normal = normal / length
    offset = float(np.dot(np.asarray(point, dtype=np.float64), normal))
    try:
        positive, negative = manifold.split_by_plane(normal, offset)
    except Exception as exc:  # noqa: BLE001
        raise ThreeMFError(
            "SPLIT_FAILED",
            f"manifold 拆分失败：{type(exc).__name__}: {exc}",
        ) from exc
    if positive.is_empty() and negative.is_empty():
        return None
    return _from_manifold(negative), _from_manifold(positive)
