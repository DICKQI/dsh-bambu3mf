"""Software preview renderer: orthographic views drawn with Pillow.

This is a sanity-check renderer, not a photoreal renderer: painter's-algorithm
triangle fill with flat two-sided lighting. It is fast enough for a 100k-triangle
project at preview resolution and needs no GPU, OpenGL or extra packages.
"""

from __future__ import annotations

import math
import os
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .constants import ThreeMFError
from .scene import describe, load
from .settings import plate_origin

DEFAULT_COLORS = [
    (240, 240, 240),
    (255, 120, 90),
    (90, 170, 255),
    (120, 220, 140),
    (250, 200, 80),
    (200, 130, 250),
    (255, 170, 210),
    (150, 220, 220),
]

VIEW_DIRECTIONS = {
    "iso": (1.0, 1.0, 0.9),
    "top": (0.0, 0.0, 1.0),
    "front": (0.0, -1.0, 0.0),
    "right": (1.0, 0.0, 0.0),
}


def _hex_color(value: str) -> tuple[int, int, int] | None:
    text = (value or "").strip().lstrip("#")
    if len(text) == 8:
        text = text[:6]
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        if os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _camera(view: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    direction = np.array(VIEW_DIRECTIONS[view], dtype=np.float64)
    direction /= np.linalg.norm(direction)
    up_world = np.array([0.0, 0.0, 1.0])
    if view == "top":
        right = np.array([1.0, 0.0, 0.0])
        up = np.array([0.0, 1.0, 0.0])
    else:
        right = np.cross(up_world, direction)
        if np.linalg.norm(right) < 1e-9:
            right = np.array([1.0, 0.0, 0.0])
        right /= np.linalg.norm(right)
        up = np.cross(direction, right)
        up /= np.linalg.norm(up)
    return direction, right, up


def _collect(loaded, only_objects: list[str] | None):
    objects = []
    project = loaded.project
    colors = project.filament_colors() if project is not None else []
    for object_id in loaded.scene.object_ids():
        if only_objects and object_id not in only_objects:
            continue
        try:
            vertices, triangles = loaded.scene.world_triangles(object_id)
        except ThreeMFError:
            continue
        if len(triangles) == 0:
            continue
        extruder = loaded.object_extruder(object_id) - 1
        color = None
        if 0 <= extruder < len(colors):
            color = _hex_color(colors[extruder])
        if color is None:
            color = DEFAULT_COLORS[int(object_id) % len(DEFAULT_COLORS)]
        objects.append(
            {
                "id": object_id,
                "name": loaded.object_name(object_id),
                "vertices": vertices,
                "triangles": triangles,
                "color": color,
            }
        )
    return objects


def _camera_from_angles(azimuth: float, elevation: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    azimuth = math.radians(float(azimuth))
    elevation = math.radians(float(elevation))
    direction = np.array(
        [
            math.cos(elevation) * math.cos(azimuth),
            math.cos(elevation) * math.sin(azimuth),
            math.sin(elevation),
        ],
        dtype=np.float64,
    )
    direction /= max(float(np.linalg.norm(direction)), 1e-12)
    up_world = np.array([0.0, 0.0, 1.0])
    right = np.cross(up_world, direction)
    if np.linalg.norm(right) < 1e-9:
        right = np.array([1.0, 0.0, 0.0])
    right /= np.linalg.norm(right)
    up = np.cross(direction, right)
    up /= np.linalg.norm(up)
    return direction, right, up


def _cull_objects(objects, bbox):
    """Drop triangles whose AABB does not intersect ``bbox``."""
    lower = np.asarray(bbox[:3], dtype=np.float64)
    upper = np.asarray(bbox[3:], dtype=np.float64)
    kept = []
    for obj in objects:
        vertices = obj["vertices"]
        triangles = obj["triangles"]
        if len(triangles) == 0:
            continue
        tri_lo = vertices[triangles].min(axis=1)
        tri_hi = vertices[triangles].max(axis=1)
        mask = np.all(tri_lo <= upper, axis=1) & np.all(tri_hi >= lower, axis=1)
        if not mask.any():
            continue
        kept.append(
            {
                **obj,
                "triangles": triangles[mask],
                "bbox": [float(value) for value in np.concatenate([tri_lo[mask].min(0), tri_hi[mask].max(0)])],
            }
        )
    return kept


def render_view(
    view,
    objects,
    bed_rects,
    width,
    max_triangles,
    label,
    *,
    framing: str = "scene",
    bbox=None,
    zoom: float = 1.0,
    camera=None,
    height: int | None = None,
    crop: bool = False,
):
    if camera is not None:
        direction, right, up = _camera_from_angles(camera["azimuth"], camera["elevation"])
    else:
        direction, right, up = _camera(view)
    height = int(height or round(width * 0.78))
    margin = max(16, int(width * 0.05))
    image = Image.new("RGB", (width, height), (26, 28, 32))
    draw = ImageDraw.Draw(image)

    if crop and bbox is not None:
        objects = _cull_objects(objects, bbox)

    # World-space bounds of what is actually drawn. ``framing`` decides whether
    # the 256 mm bed sets the scale: a 40 mm part on a full bed used to occupy
    # barely a hundred pixels, which hid exactly the small bumps being checked.
    if framing == "bbox" and bbox is not None:
        points_lo = np.asarray(bbox[:3], dtype=np.float64)
        points_hi = np.asarray(bbox[3:], dtype=np.float64)
    else:
        points_lo = np.array([np.inf, np.inf, np.inf])
        points_hi = np.array([-np.inf, -np.inf, -np.inf])
        for obj in objects:
            lo = obj["vertices"].min(0)
            hi = obj["vertices"].max(0)
            points_lo = np.minimum(points_lo, lo)
            points_hi = np.maximum(points_hi, hi)
        if framing == "scene":
            for rect in bed_rects:
                points_lo = np.minimum(points_lo, np.array([rect["lo"][0], rect["lo"][1], 0.0]))
                points_hi = np.maximum(points_hi, np.array([rect["hi"][0], rect["hi"][1], 0.0]))
    if not np.all(np.isfinite(points_lo)):
        points_lo = np.array([0.0, 0.0, 0.0])
        points_hi = np.array([1.0, 1.0, 1.0])

    corners = np.array(
        [
            [x, y, z]
            for x in (points_lo[0], points_hi[0])
            for y in (points_lo[1], points_hi[1])
            for z in (points_lo[2], points_hi[2])
        ]
    )
    u = corners @ right
    v = corners @ up
    span_u = max(float(u.max() - u.min()), 1e-6)
    span_v = max(float(v.max() - v.min()), 1e-6)
    scale = min((width - 2 * margin) / span_u, (height - 2 * margin) / span_v)
    scale *= max(0.05, min(20.0, float(zoom)))
    center_u = float((u.min() + u.max()) / 2.0)
    center_v = float((v.min() + v.max()) / 2.0)

    def project(points):
        pu = (points @ right - center_u) * scale + width / 2.0
        pv = -(points @ up - center_v) * scale + height / 2.0
        return np.column_stack([pu, pv])

    # Bed plates only belong in the scene framing.
    if framing == "scene":
        for rect in bed_rects:
            lo, hi = rect["lo"], rect["hi"]
            quad = np.array(
                [
                    [lo[0], lo[1], 0.0],
                    [hi[0], lo[1], 0.0],
                    [hi[0], hi[1], 0.0],
                    [lo[0], hi[1], 0.0],
                ]
            )
            projected = project(quad)
            draw.polygon([tuple(p) for p in projected.tolist()], fill=(44, 47, 53))
            draw.line(
                [tuple(p) for p in np.vstack([projected, projected[:1]]).tolist()],
                fill=(90, 96, 108),
                width=2,
            )
            label_pos = projected.mean(axis=0)
            draw.text(
                (float(label_pos[0]), float(label_pos[1])),
                rect["label"],
                fill=(150, 156, 168),
                font=_font(max(12, width // 55)),
                anchor="mm",
            )

    total = sum(len(obj["triangles"]) for obj in objects)
    sampled = False
    light = direction + 0.45 * up + 0.25 * right
    light /= np.linalg.norm(light)

    for obj in objects:
        vertices = obj["vertices"]
        triangles = obj["triangles"]
        screen = project(vertices)
        depths = vertices @ direction
        centroids = depths[triangles].mean(axis=1)
        order = np.argsort(centroids)
        if total > max_triangles and len(order) > 0:
            keep = max(1, int(max_triangles * len(order) / total))
            # Deterministic stride keeps the preview representative.
            stride = max(1, len(order) // keep)
            order = order[::stride]
            sampled = True
        base = np.array(obj["color"], dtype=np.float64)
        for index in order.tolist():
            triangle = triangles[index]
            a, b, c = vertices[triangle[0]], vertices[triangle[1]], vertices[triangle[2]]
            normal = np.cross(b - a, c - a)
            length = float(np.linalg.norm(normal))
            if length < 1e-12:
                continue
            normal = normal / length
            strength = abs(float(np.dot(normal, light)))
            shade = 0.38 + 0.62 * strength
            color = tuple(int(min(255, max(0, channel * shade))) for channel in base)
            pts = screen[triangle]
            draw.polygon(
                [tuple(p) for p in pts.tolist()],
                fill=color,
            )

    draw.text(
        (margin, height - margin + 4),
        label,
        fill=(170, 176, 188),
        font=_font(max(12, width // 60)),
        anchor="ls",
    )
    return image, {
        "triangles": total,
        "sampled": sampled,
        "drawn": min(total, max_triangles) if sampled else total,
        "objects": len(objects),
        "bbox": [round(float(value), 3) for value in np.concatenate([points_lo, points_hi])],
    }


def cmd_render(args: dict, config: dict) -> dict:
    path = args.get("file")
    if not path:
        raise ThreeMFError("INVALID_ARGS", "render 需要 file")
    started = time.time()
    loaded = load(str(path))
    views = args.get("views") or ["iso", "top"]
    if isinstance(views, str):
        views = [views]
    views = [str(view) for view in views]
    unknown = [view for view in views if view not in ("iso", "top", "front", "right", "plate")]
    if unknown:
        raise ThreeMFError("INVALID_ARGS", f"未知视图：{unknown}")
    width = int(args.get("width") or config.get("renderWidth") or 900)
    width = max(320, min(2000, width))
    height = int(args.get("height") or round(width * 0.78))
    height = max(240, min(2400, height))
    max_triangles = int(args.get("max_triangles") or config.get("maxTriangles") or 250000)
    out_dir = args.get("out_dir") or os.path.join(os.path.dirname(os.path.abspath(str(path))), ".bambu3mf-preview")
    os.makedirs(out_dir, exist_ok=True)

    framing = str(args.get("framing") or "scene")
    if framing not in ("scene", "objects", "bbox"):
        raise ThreeMFError("INVALID_ARGS", "framing 必须是 scene/objects/bbox")
    bbox = args.get("bbox")
    if bbox is not None:
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 6:
            raise ThreeMFError("INVALID_ARGS", "bbox 需要 6 个数字：[xmin,ymin,zmin,xmax,ymax,zmax]")
        bbox = [float(value) for value in bbox]
        if any(bbox[index + 3] <= bbox[index] for index in range(3)):
            raise ThreeMFError("INVALID_ARGS", "bbox 的 max 必须大于 min")
    elif framing == "bbox":
        raise ThreeMFError("INVALID_ARGS", "framing=bbox 需要 bbox")
    zoom = float(args.get("zoom") or 1.0)
    if not 0.05 <= zoom <= 20.0:
        raise ThreeMFError("INVALID_ARGS", "zoom 必须在 0.05~20 之间")
    crop = bool(args.get("crop", framing == "bbox"))
    camera = args.get("camera")
    if camera is not None:
        if not isinstance(camera, dict) or camera.get("azimuth") is None or camera.get("elevation") is None:
            raise ThreeMFError("INVALID_ARGS", "camera 需要 azimuth 和 elevation（度）")
        camera = {
            "azimuth": float(camera["azimuth"]),
            "elevation": max(-89.0, min(89.0, float(camera["elevation"]))),
        }

    only = args.get("objects")
    only_objects = None
    if isinstance(only, list) and only:
        from .scene import resolve_object

        only_objects = [resolve_object(loaded, item) for item in only]

    objects = _collect(loaded, only_objects)
    if not objects and "plate" not in views:
        raise ThreeMFError("EMPTY_MESH", "没有可渲染的网格")

    bed_rects = []
    if loaded.settings is not None and loaded.project is not None:
        bed = loaded.bed()
        origin = loaded.bed_origin()
        for plate in loaded.settings.plates:
            plate_origin_point = plate_origin(
                sorted(p.plater_id for p in loaded.settings.plates).index(plate.plater_id),
                bed,
                origin,
            )
            bed_rects.append(
                {
                    "lo": plate_origin_point,
                    "hi": plate_origin_point + np.array([bed[0], bed[1], 0.0]),
                    "label": f"{plate.name or plate.plater_id}",
                }
            )

    results = []
    stats = []
    info = describe(loaded, "summary", include_settings=False)
    total_objects = int(info["stats"]["objects"])
    for view in views:
        if view == "plate":
            image = _plate_view(loaded, width)
            view_stats = {
                "triangles": 0,
                "sampled": False,
                "drawn": 0,
                "objects": 0,
                "bbox": None,
            }
        else:
            image, view_stats = render_view(
                view,
                objects,
                bed_rects,
                width,
                max_triangles,
                f"{os.path.basename(str(path))} · {view} · "
                f"{len(objects)}/{total_objects} objects"
                + (f" · {framing}" if framing != "scene" else ""),
                framing=framing,
                bbox=bbox,
                zoom=zoom,
                camera=camera,
                height=height,
                crop=crop,
            )
        tag = view
        if view != "plate":
            tag = f"{view}_{framing}"
        target = os.path.join(out_dir, f"{tag}.png")
        image.save(target, "PNG", optimize=True)
        results.append({"name": view, "path": target, "width": image.width, "height": image.height})
        stats.append(view_stats)

    total_triangles = int(sum(len(item["triangles"]) for item in objects))
    summary_lines = [
        f"预览：{len(results)} 个视图 · {len(objects)}/{total_objects} 个对象 · "
        f"{total_triangles} 个三角面 · {width}×{height}px · "
        f"取景 {framing} · {round(time.time() - started, 2)}s"
    ]
    if framing == "objects":
        summary_lines.append("取景=objects：只按绘制对象定标，床面不参与比例（适合放大看细节）")
    if framing == "bbox" and bbox:
        summary_lines.append(
            "取景=bbox：{}".format("、".join(f"{value:g}" for value in bbox))
        )
    if any(item["sampled"] for item in stats):
        summary_lines.append("注意：三角面超过上限，预览做了抽样，细节可能缺失")
    first = next(
        (item for item in info["objects"] if item["id"] in {int(o["id"]) for o in objects}),
        None,
    )
    bbox_info = first["bbox"] if first else (bbox or [0, 0, 0, 0, 0, 0])
    return {
        "file": loaded.path,
        "summary": "\n".join(summary_lines),
        "views": results,
        "renderStats": {
            "objects": len(objects),
            "totalObjects": total_objects,
            "triangles": total_triangles,
            "sampled": any(item["sampled"] for item in stats),
            "seconds": round(time.time() - started, 2),
            "bbox": bbox_info,
            "framing": framing,
            "requestedBbox": bbox,
            "zoom": zoom,
            "camera": camera,
        },
        "changes": [],
        "diagnostics": [],
        "validation": {"changedEntries": [], "preservedEntries": 0},
    }


def heightmap_png(values, path: str, scale: int = 12) -> str:
    """Blue-to-red heat map for ``mesh op=measure mode=heightmap``."""
    grid = len(values)
    if grid == 0 or any(len(row) == 0 for row in values):
        raise ThreeMFError("MEASURE_TOO_LARGE", "高度场为空，无法写图")
    finite = [float(value) for row in values for value in row if value is not None]
    if not finite:
        raise ThreeMFError("MEASURE_TOO_LARGE", "高度场没有有料单元，无法写图")
    lo, hi = min(finite), max(finite)
    span = max(hi - lo, 1e-9)
    cell = max(4, int(scale))
    size = grid * cell
    image = Image.new("RGB", (size, size), (26, 28, 32))
    draw = ImageDraw.Draw(image)
    for row_index, row in enumerate(values):
        for col_index, value in enumerate(row):
            if value is None:
                color = (38, 40, 46)
            else:
                t = (float(value) - lo) / span
                red = int(255 * min(1.0, 2.0 * t))
                blue = int(255 * min(1.0, 2.0 * (1.0 - t)))
                green = int(180 * (1.0 - abs(t - 0.5) * 2.0))
                color = (red, green, blue)
            x0 = col_index * cell
            y0 = size - (row_index + 1) * cell
            draw.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=color)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    image.save(path, "PNG", optimize=True)
    return os.path.abspath(path)


def _plate_view(loaded, width: int) -> Image.Image:
    height = int(round(width * 0.78))
    margin = max(20, int(width * 0.06))
    image = Image.new("RGB", (width, height), (26, 28, 32))
    draw = ImageDraw.Draw(image)
    settings = loaded.settings
    if settings is None or loaded.project is None:
        draw.text((margin, margin), "通用 3MF：没有盘信息", fill=(200, 200, 200), font=_font(18))
        return image
    bed = loaded.bed()
    origin = loaded.bed_origin()
    plates = sorted(settings.plates, key=lambda p: p.plater_id)
    if not plates:
        return image
    cols = 3
    rows = (len(plates) + cols - 1) // cols
    gap = 18
    cell_w = (width - 2 * margin - gap * (cols - 1)) / cols
    cell_h = min((height - 2 * margin - gap * (rows - 1)) / max(rows, 1), cell_w * bed[1] / max(bed[0], 1))
    scale = min(cell_w / max(bed[0], 1), cell_h / max(bed[1], 1))
    cheap_font = _font(max(12, int(scale * 10)))
    for index, plate in enumerate(plates):
        col = index % cols
        row = index // cols
        x0 = margin + col * (cell_w + gap)
        y0 = margin + row * (cell_h + gap)
        x1 = x0 + bed[0] * scale
        y1 = y0 + bed[1] * scale
        draw.rectangle([x0, y0, x1, y1], fill=(44, 47, 53), outline=(110, 116, 128), width=2)
        draw.text(
            (x0 + 6, y0 + 4),
            f"盘 {plate.plater_id} {plate.name or ''}",
            fill=(200, 205, 215),
            font=_font(max(13, int(width / 60))),
        )
        plate_origin_point = plate_origin(
            index, bed, origin
        )
        for instance in plate.instances:
            object_id = str(instance["object_id"])
            if object_id not in loaded.scene.root_objects:
                continue
            lo, hi = loaded.scene.world_bbox(object_id)
            local_lo = (lo - plate_origin_point) * scale + np.array([x0, y0, 0])
            local_hi = (hi - plate_origin_point) * scale + np.array([x0, y0, 0])
            color = DEFAULT_COLORS[int(object_id) % len(DEFAULT_COLORS)]
            draw.rectangle(
                [
                    float(local_lo[0]),
                    float(local_lo[1]),
                    float(local_hi[0]),
                    float(local_hi[1]),
                ],
                outline=color,
                width=2,
            )
            draw.text(
                (float(local_lo[0]) + 3, float(local_lo[1]) + 2),
                f"#{object_id}",
                fill=color,
                font=cheap_font,
            )
    return image
