"""End-to-end self test for the Bambu 3MF kernel.

Runs against copies of the sample files, never the originals. Usage:

    python selftest/run_selftest.py [--samples D:\\下载] [--skip-slice]

The script prints one line per check and exits non-zero when anything fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

from threemf import cli  # noqa: E402
from threemf.geometry import (  # noqa: E402
    component_info,
    connected_components,
    mesh_quality,
    printability,
    primitive_box,
    section_mesh,
    topology_report,
)
from threemf.model import Mesh  # noqa: E402
from threemf.scene import apply_edit, apply_mesh, apply_settings, describe, load, save  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from fixture_packs import make_sliced_3mf  # noqa: E402

BAMBU = r"C:\Program Files\Bambu Studio\bambu-studio.exe"
RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy(src: str, workdir: str, name: str) -> str:
    target = os.path.join(workdir, name)
    shutil.copy2(src, target)
    return target


def entry_digests(path: str) -> dict:
    with zipfile.ZipFile(path) as zf:
        return {name: hashlib.sha256(zf.read(name)).hexdigest() for name in zf.namelist()}


def slice_project(path: str, plate: int, tag: str, workdir: str, timeout: int = 900) -> tuple[bool, str]:
    """Slice one plate with the Bambu CLI; returns (ok, human readable note)."""
    out_dir = os.path.join(workdir, f"sliced_{tag}")
    os.makedirs(out_dir, exist_ok=True)
    target = f"{tag}.gcode.3mf"
    started = time.time()
    process = subprocess.run(
        [
            BAMBU,
            "--slice",
            str(plate),
            "--export-3mf",
            target,
            "--outputdir",
            out_dir,
            "--allow-newer-file",
            path,
        ],
        capture_output=True,
        timeout=timeout,
    )
    ok = process.returncode == 0 and os.path.isfile(os.path.join(out_dir, target))
    return ok, f"plate {plate} exit {process.returncode}, {time.time() - started:.1f}s"


def multi_shell_object(path: str):
    """First single-part object of ``path`` made of several connected shells.

    Assembly objects (several <part> entries, i.e. several mesh sources) are
    skipped: shell-level selection is a single-mesh operation, and the kernel
    refuses to silently merge parts.
    """
    loaded = load(path)
    best = None
    for object_id in loaded.scene.object_ids():
        try:
            if len(loaded.scene.mesh_sources(object_id)) != 1:
                continue
            vertices, triangles = loaded.scene.world_triangles(object_id)
        except Exception:  # noqa: BLE001 - samples can be odd
            continue
        mesh = Mesh(vertices, triangles, [])
        labels = connected_components(mesh)
        if len(labels) and int(labels.max()) >= 1:
            if best is None or int(labels.max()) > best[2]:
                best = (object_id, mesh, int(labels.max()) + 1)
    return best


def project_with_assemble(samples: dict):
    """First sample whose model_settings carries an <assemble> block."""
    for name, path in samples.items():
        if name.lower().endswith(".gcode.3mf"):
            continue
        try:
            loaded = load(path)
        except Exception:  # noqa: BLE001
            continue
        if loaded.settings is None:
            continue
        if any(True for _ in loaded.settings.root.iter("assemble")):
            return name, path
    return None, None


# --------------------------------------------------------------------- fixtures
def make_core_3mf(path: str) -> None:
    """A minimal non-Bambu 3MF with one inline cube."""
    vertices = [
        (0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0),
        (0, 0, 10), (20, 0, 10), (20, 20, 10), (0, 20, 10),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2),
        (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6),
        (3, 0, 4), (3, 4, 7),
    ]
    vertex_xml = "".join(
        f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in vertices
    )
    triangle_xml = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "<resources><object id=\"1\" type=\"model\"><mesh><vertices>"
        + vertex_xml
        + "</vertices><triangles>"
        + triangle_xml
        + "</triangles></mesh></object></resources>"
        '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 20 0"/></build></model>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("3D/3dmodel.model", model)


# ----------------------------------------------------------------------- tests
def test_inspect(samples: dict, workdir: str) -> None:
    for name, path in samples.items():
        try:
            data = describe(load(path), "summary")
        except Exception as error:  # noqa: BLE001
            check(False, f"inspect {name}", f"{type(error).__name__}: {error}")
            continue
        sliced = os.path.basename(name).endswith(".gcode.3mf")
        if sliced:
            ok = data["format"] == "bambu-sliced" and data["stats"]["objects"] == 0
        else:
            ok = data["format"].startswith("bambu") and data["stats"]["objects"] >= 1
        check(ok, f"inspect {name}", f"{data['format']} · {data['stats']['objects']} objects")


def test_preservation(src: str, workdir: str) -> None:
    target = copy(src, workdir, "preserve.3mf")
    before = load(target).package.digests()
    loaded = load(target)
    apply_edit(loaded, [{"op": "rename", "object": 2, "name": "保留测试"}])
    saved = save(loaded)
    after = load(saved["output"]).package.digests()
    changed = set(saved["changedEntries"])
    untouched = [name for name in before if name not in changed]
    ok = all(before[name] == after.get(name) for name in untouched)
    check(ok, "untouched entries stay byte-identical", f"{len(untouched)} entries")


def test_move(src: str, workdir: str) -> None:
    target = copy(src, workdir, "move.3mf")
    loaded = load(target)
    before = describe(loaded)["objects"][0]["bbox"]
    apply_edit(loaded, [{"op": "move", "object": 2, "dx": 10, "dy": -5, "dz": 0}])
    saved = save(loaded)
    after = describe(load(saved["output"]))["objects"][0]["bbox"]
    ok = (
        abs((after[0] - before[0]) - 10) < 1e-3
        and abs((after[1] - before[1]) + 5) < 1e-3
        and abs((after[3] - before[3]) - 10) < 1e-3
    )
    check(ok, "move shifts world bbox", f"{before[:2]} → {after[:2]}")


def test_bake(src: str, workdir: str) -> None:
    target = copy(src, workdir, "bake.3mf")
    loaded = load(target)
    before = describe(loaded)["objects"][0]["bbox"]
    apply_mesh(loaded, "2", "bake", {})
    saved = save(loaded)
    after = describe(load(saved["output"]))["objects"][0]["bbox"]
    ok = max(abs(a - b) for a, b in zip(before, after)) < 1e-3
    check(ok, "bake keeps world position", f"max delta {max(abs(a - b) for a, b in zip(before, after)):.5f}")


def test_scale(src: str, workdir: str) -> None:
    target = copy(src, workdir, "scale.3mf")
    loaded = load(target)
    before = describe(loaded)["objects"][0]["bbox"]
    size_before = [before[3 + i] - before[i] for i in range(3)]
    apply_mesh(loaded, "2", "scale", {"scale": 1.1})
    saved = save(loaded)
    after = describe(load(saved["output"]))["objects"][0]["bbox"]
    size_after = [after[3 + i] - after[i] for i in range(3)]
    ratios = [size_after[i] / size_before[i] for i in range(3)]
    ok = all(abs(ratio - 1.1) < 0.01 for ratio in ratios)
    check(ok, "scale grows bbox by 1.1", ", ".join(f"{r:.3f}" for r in ratios))


def test_cut(src: str, workdir: str) -> None:
    target = copy(src, workdir, "cut.3mf")
    loaded = load(target)
    top = describe(loaded)["objects"][0]["bbox"][5]
    plan = top / 2.0
    result = apply_mesh(
        loaded,
        "2",
        "plane_cut",
        {"plane": {"axis": "z", "value": plan}, "keep": "negative", "cap": True},
    )
    saved = save(loaded)
    after = describe(load(saved["output"]))["objects"][0]
    quality = mesh_quality(
        Mesh(*load(saved["output"]).scene.world_triangles("2"))
    )
    ok = (
        abs(after["bbox"][5] - plan) < 0.01
        and quality["boundaryEdges"] == 0
        and not quality["nonManifoldEdges"]
    )
    check(ok, "plane cut caps watertight", f"zmax {after['bbox'][5]:.2f}, cap {result['stats'].get('capTriangles')}")


def test_settings(src: str, workdir: str) -> None:
    target = copy(src, workdir, "settings.3mf")
    loaded = load(target)
    apply_settings(loaded, [{"op": "set", "key": "layer_height", "value": "0.28"}])
    saved = save(loaded)
    reloaded = load(saved["output"])
    raw = reloaded.project.serialize()
    ok = reloaded.project.get("layer_height") == "0.28" and b'    "layer_height": "0.28"' in raw
    check(ok, "settings write keeps JSON shape", "layer_height=0.28")


def test_render(src: str, workdir: str, samples: dict) -> None:
    from threemf.render import cmd_render

    out_dir = os.path.join(workdir, "preview")
    result = cmd_render({"file": src, "views": ["iso", "plate"], "width": 500, "out_dir": out_dir}, {})
    ok = True
    details = []
    for view in result["views"]:
        exists = os.path.isfile(view["path"]) and os.path.getsize(view["path"]) > 2000
        ok = ok and exists
        details.append(f"{view['name']}={os.path.getsize(view['path']) if exists else 0}B")
    check(ok, "render writes previews", ", ".join(details))


def test_core_file(workdir: str) -> None:
    path = os.path.join(workdir, "core.3mf")
    make_core_3mf(path)
    data = describe(load(path), "summary")
    ok = data["format"] == "core" and data["stats"]["objects"] == 1
    loaded = load(path)
    apply_edit(loaded, [{"op": "move", "object": 1, "dx": 5, "dy": 0}])
    saved = save(loaded)
    moved = describe(load(saved["output"]))["objects"][0]["bbox"]
    ok = ok and abs(moved[0] - 25.0) < 1e-6
    check(ok, "core 3MF inspect + move", f"bbox {moved[:2]}")


def test_sliced_refusal(samples: dict) -> None:
    sliced = next((p for p in samples.values() if p.endswith(".gcode.3mf")), None)
    if sliced is None:
        check(True, "sliced file refusal", "no sample")
        return
    loaded = load(sliced)
    try:
        apply_edit(loaded, [{"op": "move", "object": 1, "dx": 1}])
        check(False, "sliced file refusal", "edit was allowed")
    except Exception as error:  # noqa: BLE001
        check(getattr(error, "code", "") == "SLICED_FILE", "sliced file refusal", getattr(error, "code", str(error)))


def test_stl_roundtrip(src: str, workdir: str) -> None:
    target = copy(src, workdir, "stl.3mf")
    loaded = load(target)
    stl_path = os.path.join(workdir, "exported.stl")
    apply_mesh(loaded, "2", "export_stl", {"stl": stl_path})
    ok = os.path.isfile(stl_path) and os.path.getsize(stl_path) > 84
    imported = copy(src, workdir, "stl_import.3mf")
    loaded = load(imported)
    result = apply_mesh(loaded, None, "import_stl", {"stl": stl_path, "name": "imported", "plate": 1})
    saved = save(loaded)
    data = describe(load(saved["output"]))
    ok = ok and any(item["name"] == "imported" for item in data["objects"])
    check(ok, "STL export + import", f"{result.get('triangles')} triangles")


def test_stretch(src: str, workdir: str, skip: bool) -> None:
    """op=stretch must lengthen the band, keep topology, and still slice."""
    target = copy(src, workdir, "stretch.3mf")
    loaded = load(target)
    before = describe(load(target), "summary")["objects"][0]
    lo, hi = before["bbox"][0], before["bbox"][3]
    centre = (lo + hi) / 2.0
    band_half = 14.0
    delta = 17.6
    triangles_before = before["triangles"]
    result = apply_mesh(
        loaded,
        "2",
        "stretch",
        {
            "axis": "x",
            "band": [centre - band_half, centre + band_half],
            "delta": delta,
            "negative_share": 0.5,
        },
    )
    saved = save(loaded)
    reloaded = load(saved["output"])
    after = describe(reloaded, "summary")["objects"][0]
    width_before = before["bbox"][3] - before["bbox"][0]
    width_after = after["bbox"][3] - after["bbox"][0]
    # Geometry must be untouched in y/z, and the triangle count must not change.
    ok = (
        abs(width_after - (width_before + delta)) < 1e-3
        and abs((after["bbox"][4] - after["bbox"][1]) - (before["bbox"][4] - before["bbox"][1])) < 1e-6
        and abs((after["bbox"][5] - after["bbox"][2]) - (before["bbox"][5] - before["bbox"][2])) < 1e-6
        and result["trianglesAfter"] == triangles_before
        and result["install"]["detached"]
    )
    check(
        ok,
        "stretch widens band, keeps topology",
        f"x {width_before:.2f}->{width_after:.2f}, tris {triangles_before}->{result['trianglesAfter']}",
    )
    if skip or not os.path.isfile(BAMBU):
        check(True, "slice a stretched object", "skipped")
        return
    out_dir = os.path.join(workdir, "sliced_stretch")
    os.makedirs(out_dir, exist_ok=True)
    process = subprocess.run(
        [
            BAMBU,
            "--slice",
            "1",
            "--export-3mf",
            "stretch.gcode.3mf",
            "--outputdir",
            out_dir,
            "--allow-newer-file",
            saved["output"],
        ],
        capture_output=True,
        timeout=600,
    )
    sliced = os.path.join(out_dir, "stretch.gcode.3mf")
    result_path = os.path.join(out_dir, "result.json")
    ok = process.returncode == 0 and os.path.isfile(sliced)
    detail = f"exit {process.returncode}"
    if os.path.isfile(result_path):
        with open(result_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        ok = ok and payload.get("return_code") == 0
        detail += f", return_code {payload.get('return_code')}"
    check(ok, "slice a stretched object", detail)


def test_preflight_advisory(src: str, workdir: str) -> None:
    """Real contact blocks; mere bounding-box overlap does not.

    Stacking object 2 exactly on object 6 makes surfaces touch (distance ~0), which
    is a genuine mistake and must block. Interlocking parts whose boxes overlap but
    whose surfaces stay millimetres apart must not — that is the case 0.3.0 broke.
    """
    from threemf import cli

    collided = copy(src, workdir, "preflight_collision.3mf")
    loaded = load(collided)
    build = loaded.scene.build_transform("6")
    target = loaded.scene.build_transform("2")
    apply_edit(
        loaded,
        [
            {
                "op": "move",
                "object": 2,
                "dx": float(build[3, 0] - target[3, 0]),
                "dy": float(build[3, 1] - target[3, 1]),
            }
        ],
    )
    saved = save(loaded)
    contact = cli.cmd_preflight({"file": saved["output"]})
    clean = cli.cmd_preflight({"file": src})
    check(
        contact["ready"] is False and clean["ready"] is True,
        "real contact blocks, spacing does not",
        f"stacked_ready={contact['ready']} clean_ready={clean['ready']}",
    )


def test_preflight_accepts_what_bambu_accepts(samples: dict, workdir: str) -> None:
    """Files Bambu slices must not be reported as blocking.

    监控支架.3mf slices with 5 mm gaps, and A1外挂密封盒 plate 2 has interlocking
    parts whose boxes overlap; both were sliced successfully by the CLI. A distance
    threshold therefore must never become a refusal (0.3.0 did exactly that).
    """
    from threemf import cli

    tight = next((path for name, path in samples.items() if "监控支架" in name), None)
    nested = next((path for name, path in samples.items() if "特氟龙管" in name), None)
    results = []
    if tight:
        report = cli.cmd_preflight({"file": tight})
        results.append(("监控支架", report["ready"], len(report["blocking"])))
    if nested:
        report = cli.cmd_preflight({"file": nested, "plate": 2})
        # Interlocking parts: boxes overlap but surfaces are millimetres apart.
        results.append(("特氟龙管 plate2", report["ready"], len(report["warnings"])))
    if not results:
        check(True, "preflight accepts slicable projects", "samples missing")
        return
    check(
        all(ready for _name, ready, _count in results),
        "preflight accepts slicable projects",
        ", ".join(f"{name}: ready={ready}" for name, ready, _count in results),
    )


def test_preflight_plate2_coordinates(samples: dict, workdir: str) -> None:
    """Suggestions must be world coordinates, correct on a plate whose origin != 0."""
    from threemf import cli
    from threemf.scene import load as reload_package

    candidate = None
    for name, path in samples.items():
        loaded = reload_package(path)
        if loaded.settings is None:
            continue
        origins = loaded.plate_origins()
        if any(float(origin[0]) > 1.0 for plate, origin in origins.items() if plate > 1):
            candidate = path
            break
    if candidate is None:
        check(True, "preflight suggestions on plate 2", "no multi-plate sample")
        return
    loaded = reload_package(candidate)
    origin = loaded.plate_origins().get(2)
    report = cli.cmd_preflight({"file": candidate, "plate": 2, "strict": True})
    created = report["suggestions"]
    if not created:
        check(True, "preflight suggestions on plate 2", "no suggestions on plate 2")
        return
    # A suggestion for plate 2 must land inside plate 2, not near the world origin.
    bad = [
        item
        for item in created
        if float(item["to"][0]) < float(origin[0]) - 1.0
        or float(item["to"][0]) > float(origin[0]) + float(loaded.bed()[0]) + 1.0
    ]
    check(
        not bad,
        "preflight suggestions on plate 2",
        f"plate origin x={float(origin[0]):.1f}, {len(created)} suggestions, {len(bad)} outside",
    )


def test_clearance_is_safe(src: str, workdir: str) -> None:
    """Empty vertex clouds must not crash the distance helper (either side)."""
    from threemf.validate import _nearest_distance

    empty = np.zeros((0, 3))
    points = np.ones((9, 3))
    left = _nearest_distance(empty, points)
    right = _nearest_distance(points, empty)
    check(
        not np.isfinite(left) and not np.isfinite(right),
        "clearance tolerates empty meshes",
        f"{left}, {right}",
    )


def test_stretch_band_bounds(src: str, workdir: str) -> None:
    """A band outside the part must be refused, not silently move the object."""
    from threemf.constants import ThreeMFError

    loaded = load(src)
    info = describe(loaded, "summary")["objects"]
    item = next(entry for entry in info if entry["id"] == 2)
    low, high = item["bbox"][0], item["bbox"][3]
    outside = [high + 50.0, high + 60.0]
    try:
        apply_mesh(
            loaded, "2", "stretch", {"axis": "x", "band": outside, "delta": 20.0}
        )
        rejected = False
        message = "accepted"
    except ThreeMFError as exc:
        rejected = True
        message = exc.code
    check(
        rejected,
        "stretch refuses a band outside the part",
        f"band {[round(v, 1) for v in outside]} vs x∈[{low:.1f}, {high:.1f}] → {message}",
    )


def test_clearance_noise(samples: dict, workdir: str) -> None:
    """Clearance must stay one line per plate, and validate must stay fast."""
    from threemf.validate import validate

    target = next((path for name, path in samples.items() if "YIBOX" in name), None)
    if not target:
        check(True, "clearance stays quiet on a full plate", "sample missing")
        return
    work = copy(target, workdir, "clearance_noise.3mf")
    loaded = load(work)
    started = time.time()
    items = validate(loaded.scene, loaded.settings, loaded.project)
    elapsed = time.time() - started
    clearance = [item for item in items if item["code"] in ("OBJECT_OVERLAP", "PLATE_CLEARANCE")]
    per_plate = sum(1 for item in clearance if item["code"] == "PLATE_CLEARANCE")
    check(
        per_plate <= len(loaded.settings.plates) and elapsed < 1.0,
        "clearance stays quiet on a full plate",
        f"{len(clearance)} lines ({per_plate} plate reports, {len(loaded.settings.plates)} plates), {elapsed * 1000:.0f} ms",
    )


def test_output_schema_contract(src: str, workdir: str) -> None:
    """Validate real kernel output against the JS tool schemas.

    The Python tests never touch the JS layer, which is how 0.3.0 shipped a key
    (`objects[].size`) that its own `additionalProperties: false` schema rejected.
    """
    node = os.environ.get("DSH_NODE") or shutil.which("node") or shutil.which("node.exe")
    if not node:
        check(True, "output schemas accept kernel output", "node not found, skipped")
        return
    fixtures = os.path.join(workdir, "fixtures")
    if os.path.isdir(fixtures):
        shutil.rmtree(fixtures)
    generated = subprocess.run(
        [sys.executable, os.path.join(ROOT, "selftest", "make_fixtures.py"),
         "--samples", os.path.dirname(src), "--out", fixtures],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if generated.returncode != 0:
        # The 0.4.x fixture set is tied to Cutter.3mf and its object ids (2/6).
        # Fall back to the 0.5.0 fixture set, which works from any project.
        generated = subprocess.run(
            [sys.executable, os.path.join(ROOT, "selftest", "make_fixtures_v05.py"),
             "--file", src, "--out", fixtures],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    if generated.returncode != 0:
        check(False, "output schemas accept kernel output", f"fixture generation failed: {generated.stderr[-300:]}")
        return
    checked = subprocess.run(
        [node, os.path.join(ROOT, "selftest", "schema_contract.mjs"), fixtures],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    tail = [line for line in checked.stdout.strip().splitlines() if line.strip()]
    failures = [line for line in tail if line.startswith("FAIL")]
    check(
        checked.returncode == 0,
        "output schemas accept kernel output",
        tail[-1] if tail else checked.stderr[-300:],
    )
    for line in failures:
        print(f"      {line}")


def _torus_mesh(rings: int = 48, sides: int = 24, major: float = 20.0, minor: float = 6.0) -> Mesh:
    """A closed genus-1 surface with known area/volume; no sample file needed."""
    vertices = []
    for i in range(rings):
        u = 2 * math.pi * i / rings
        for j in range(sides):
            v = 2 * math.pi * j / sides
            vertices.append(
                (
                    (major + minor * math.cos(v)) * math.cos(u),
                    (major + minor * math.cos(v)) * math.sin(u),
                    minor * math.sin(v),
                )
            )
    triangles = []
    for i in range(rings):
        for j in range(sides):
            a = i * sides + j
            b = i * sides + (j + 1) % sides
            c = ((i + 1) % rings) * sides + j
            d = ((i + 1) % rings) * sides + (j + 1) % sides
            triangles.append((a, b, d))
            triangles.append((a, d, c))
    return Mesh(
        np.array(vertices, dtype=np.float64),
        np.array(triangles, dtype=np.int64),
        [],
    )


def test_slice_info(workdir: str) -> None:
    """A sliced package must report the slicer's own numbers without slicing."""
    path = os.path.join(workdir, "sliced_report.3mf")
    make_sliced_3mf(path)
    data = describe(load(path), "summary")
    plates = (data.get("sliceInfo") or {}).get("plates") or [{}]
    plate = plates[0]
    summary = cli.cmd_inspect({"file": path})["summary"]
    ok = (
        data["format"] == "bambu-sliced"
        and plate.get("predictionSeconds") == 7200
        and abs(float(plate.get("weightGrams") or 0) - 42.5) < 1e-6
        and len(plate.get("filaments") or []) == 1
        and "已切片" in summary
        and "2 小时 0 分" in summary
    )
    check(
        ok,
        "sliced package reports slicer data",
        f"{plate.get('predictionSeconds')}s / {plate.get('weightGrams')} g",
    )


def test_printability(workdir: str) -> None:
    """analyze must report genus and overhang, and must not count the bed."""
    core = os.path.join(workdir, "core.3mf")
    if not os.path.isfile(core):
        make_core_3mf(core)
    cube = cli.cmd_mesh({"file": core, "object": 1, "op": "analyze"})["printability"]
    cube_ok = (
        cube["watertight"]
        and cube["genus"] == 0
        and abs(cube["overhang"]["bedContactMm2"] - 400.0) < 1.0
        and cube["overhang"]["unsupportedMm2"] == 0.0
    )
    torus = printability(_torus_mesh(), layer_height=0.2)
    torus_ok = (
        torus["watertight"]
        and torus["shells"] == 1
        and torus["genus"] == 1
        and torus["overhang"]["unsupportedMm2"] > 0
        and torus["estimatedLayers"] is not None
    )
    empty = printability(Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), []))
    empty_ok = empty["triangles"] == 0 and empty["genus"] is None
    check(
        cube_ok and torus_ok and empty_ok,
        "printability (cube + torus + empty)",
        f"genus {torus['genus']}, overhang {torus['overhang']['unsupportedPercent']}%",
    )


def test_build_and_probes(workdir: str) -> None:
    """Constructive geometry, unified topology, section and measurement."""
    core = os.path.join(workdir, "core_build.3mf")
    make_core_3mf(core)
    base = copy(core, workdir, "build_base.3mf")

    # Boolean subtract: a 5x5x5 corner notch must keep one watertight shell.
    boolean_out = os.path.join(workdir, "build_boolean.3mf")
    result = cli.cmd_build(
        {
            "file": base,
            "output": boolean_out,
            "ops": [
                {
                    "op": "boolean",
                    "object": 1,
                    "operation": "subtract",
                    "with": {
                        "primitive": {
                            "shape": "box",
                            "size": [5, 5, 5],
                            "center": [22.5, 22.5, 5],
                        }
                    },
                }
            ],
        }
    )
    diagnosis = cli.cmd_mesh({"file": boolean_out, "object": 1, "op": "diagnose"})["diagnosis"]
    ok = (
        diagnosis["watertight"]
        and diagnosis["orientationConsistent"]
        and diagnosis["shells"] == 1
        and abs(diagnosis["volumeMm3"] - 3875.0) < 1e-3
        and any(
            item.get("code") == "BOOLEAN_DISCONNECTED" for item in result["diagnostics"]
        )
        is False
    )
    check(
        ok,
        "build subtract keeps one watertight shell",
        f"{diagnosis['volumeMm3']} mm³, {diagnosis['triangles']} tris",
    )

    # Boolean union of an overlapping box must be one shell and not report touch-only.
    union_out = os.path.join(workdir, "build_union.3mf")
    union = cli.cmd_build(
        {
            "file": base,
            "output": union_out,
            "ops": [
                {
                    "op": "boolean",
                    "object": 1,
                    "operation": "union",
                    "with": {
                        "primitive": {
                            "shape": "box",
                            "size": [4, 6, 6],
                            "center": [39, 30, 5],
                        }
                    },
                }
            ],
        }
    )
    union_diag = cli.cmd_mesh({"file": union_out, "object": 1, "op": "diagnose"})["diagnosis"]
    codes = {item.get("code") for item in union["diagnostics"]}
    check(
        union_diag["shells"] == 1
        and union_diag["watertight"]
        and "BOOLEAN_TOUCH_ONLY" not in codes,
        "build union of overlapping solids is one shell",
        f"{union_diag['volumeMm3']} mm³, codes {sorted(codes)}",
    )

    # Split: two independent, watertight objects; original is gone.
    split_out = os.path.join(workdir, "build_split.3mf")
    split = cli.cmd_build(
        {
            "file": base,
            "output": split_out,
            "ops": [
                {
                    "op": "split",
                    "object": 1,
                    "plane": {"axis": "x", "value": 30},
                    "names": ["left", "right"],
                    "gap": 0.5,
                }
            ],
        }
    )
    split_info = cli.cmd_inspect({"file": split_out})
    halves = split_info["objects"]
    halves_ok = len(halves) == 2 and all(
        cli.cmd_mesh({"file": split_out, "object": item["id"], "op": "diagnose"})["diagnosis"][
            "watertight"
        ]
        for item in halves
    )
    split_ok = (
        halves_ok
        and abs(halves[0]["bbox"][3] - 29.75) < 0.01
        and abs(halves[1]["bbox"][0] - 30.25) < 0.01
        and "1" not in loaded_ids(split_out)
    )
    check(
        split_ok,
        "build split produces two watertight objects",
        f"{len(halves)} objects, ids {[item['id'] for item in halves]}",
    )

    # Replace: object count unchanged, geometry still watertight.
    stl = os.path.join(workdir, "build_export.stl")
    cli.cmd_mesh({"file": base, "object": 1, "op": "export_stl", "stl": stl})
    replace_out = os.path.join(workdir, "build_replace.3mf")
    replaced = cli.cmd_build(
        {
            "file": base,
            "output": replace_out,
            "ops": [{"op": "replace", "object": 1, "source": {"stl": stl}}],
        }
    )
    replace_info = cli.cmd_inspect({"file": replace_out})
    replace_diag = cli.cmd_mesh({"file": replace_out, "object": 1, "op": "diagnose"})["diagnosis"]
    check(
        len(replace_info["objects"]) == 1
        and replace_diag["watertight"]
        and replace_diag["shells"] == 1
        and replaced["opsApplied"] == 1,
        "build replace keeps object identity",
        f"{replace_diag['triangles']} tris",
    )

    # Primitive: creates one new watertight object.
    primitive_out = os.path.join(workdir, "build_primitive.3mf")
    cli.cmd_build(
        {
            "file": base,
            "output": primitive_out,
            "ops": [
                {
                    "op": "primitive",
                    "primitive": {
                        "shape": "cylinder",
                        "radius": 4,
                        "height": 10,
                        "center": [60, 20, 5],
                    },
                    "name": "pin",
                }
            ],
        }
    )
    primitive_info = cli.cmd_inspect({"file": primitive_out})
    primitive_ok = len(primitive_info["objects"]) == 2 and any(
        item["name"] == "pin" for item in primitive_info["objects"]
    )
    check(primitive_ok, "build primitive creates a named object", f"{len(primitive_info['objects'])} objects")

    # Section and measurement on the 20x20x10 core cube at world 20..40.
    section = cli.cmd_mesh(
        {"file": base, "object": 1, "op": "section", "plane": {"axis": "z", "value": 5}}
    )["section"]
    height = cli.cmd_mesh(
        {
            "file": base,
            "object": 1,
            "op": "measure",
            "bbox": [20, 20, 0, 40, 40, 10],
            "axis": "z",
            "mode": "heightmap",
            "grid": 8,
        }
    )["measurement"]
    spans = cli.cmd_mesh(
        {
            "file": base,
            "object": 1,
            "op": "measure",
            "bbox": [20, 20, 0, 40, 40, 10],
            "axis": "x",
            "mode": "spans",
        }
    )["measurement"]
    check(
        len(section["loops"]) == 1
        and abs(section["areaMm2"] - 400.0) < 1e-3
        and height["min"] == 10.0
        and height["max"] == 10.0
        and spans["spans"] == [[20.0, 40.0]],
        "section + measure (area/height/spans)",
        f"area {section['areaMm2']}, height {height['max']}, spans {spans['spans']}",
    )

    # Geometry diff: same file → no change; boolean result → changed faces near the cutter.
    same = cli.cmd_diff({"file": base, "against": base, "geometry": True})
    changed = cli.cmd_diff({"file": base, "against": boolean_out, "geometry": True})
    geometry = changed["diff"]["geometry"]["objects"][0]
    check(
        same["diff"]["geometry"]["objects"][0]["changed"] == 0
        and geometry["changed"] > 0
        and geometry["clusters"],
        "geometry diff locates changed faces",
        f"{geometry['added']} added / {geometry['removed']} removed",
    )

    # Winding: one flipped triangle makes the volume unreliable instead of a fake number.
    cube = primitive_box([10, 10, 10], [5, 5, 5])
    flipped = cube.triangles.copy()
    flipped[0] = flipped[0][[0, 2, 1]]
    broken = topology_report(Mesh(cube.vertices, flipped, []))
    check(
        broken["orientationConsistent"] is False
        and broken["volumeMm3"] is None
        and broken["volumeReliable"] is False,
        "mixed winding reports volume unreliable",
        f"consistent={broken['orientationConsistent']}, volume={broken['volumeMm3']}",
    )

    # Coplanar internal wall: two cubes sharing a face get flagged.
    a = primitive_box([10, 10, 10], [5, 5, 5])
    b = primitive_box([10, 10, 10], [15, 5, 5])
    joined = Mesh(
        np.vstack([a.vertices, b.vertices]),
        np.vstack([a.triangles, b.triangles + len(a.vertices)]),
        [],
    )
    wall = topology_report(joined, deep=True)
    check(
        wall["coincidentFacePairs"] > 0,
        "coplanar internal faces are detected",
        f"{wall['coincidentFacePairs']} pairs",
    )


def loaded_ids(path: str) -> set[str]:
    return set(load(path).scene.object_ids())


def test_diff(samples: dict, workdir: str) -> None:
    """diff must name the changed setting and the moved object, and honour keys."""
    candidate = None
    for path in samples.values():
        if path.lower().endswith(".gcode.3mf"):
            continue
        try:
            loaded = load(path)
        except Exception:  # noqa: BLE001 - samples can be odd
            continue
        if (
            loaded.project is not None
            and loaded.settings is not None
            and loaded.scene.object_ids()
            and (candidate is None or os.path.getsize(path) < os.path.getsize(candidate))
        ):
            candidate = path
    if candidate is None:
        check(True, "diff two projects", "no suitable sample")
        return
    left = copy(candidate, workdir, "diff_left.3mf")
    right = copy(candidate, workdir, "diff_right.3mf")
    loaded = load(right)
    apply_settings(loaded, [{"op": "set", "key": "layer_height", "value": "0.16"}])
    first = loaded.scene.object_ids()[0]
    apply_edit(loaded, [{"op": "move", "object": first, "dx": 3}])
    saved = save(loaded, os.path.join(workdir, "diff_right_out.3mf"))
    result = cli.cmd_diff({"file": left, "against": saved["output"]})
    entries = {item["key"]: item for item in result["diff"]["settings"]["entries"]}
    entry = entries.get("layer_height") or {}
    ok = (
        entry.get("status") == "changed"
        and str(entry.get("right")) == "0.16"
        and bool(result["diff"]["objects"]["changed"])
    )
    filtered = cli.cmd_diff(
        {"file": left, "against": saved["output"], "keys": ["layer_height", "printer_model"]}
    )
    statuses = {
        item["key"]: item["status"] for item in filtered["diff"]["settings"]["entries"]
    }
    ok = ok and statuses.get("layer_height") == "changed" and statuses.get("printer_model") == "same"
    rejected = False
    try:
        cli.cmd_diff({"file": left, "against": saved["output"], "keys": ["layer_heigth"]})
    except Exception as error:  # noqa: BLE001
        rejected = getattr(error, "code", "") == "INVALID_ARGS"
    ok = ok and rejected
    check(
        ok,
        "diff reports setting + object changes",
        f"{len(result['diff']['settings']['entries'])} setting entries",
    )


def test_slice_result_js(workdir: str) -> None:
    """The JS result.json parser has no Python equivalent; run it under node."""
    node = os.environ.get("DSH_NODE") or shutil.which("node") or shutil.which("node.exe")
    if not node:
        check(True, "slice result parsing (node)", "node not found, skipped")
        return
    script = os.path.join(ROOT, "selftest", "slice_result.mjs")
    # The child prints Chinese; decode as UTF-8 explicitly, because the default
    # text mode uses the console locale (GBK here) and kills the reader thread.
    process = subprocess.run(
        [node, script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180
    )
    output = (process.stdout or process.stderr or "").strip().splitlines()
    check(
        process.returncode == 0,
        "slice result parsing (node)",
        output[-1] if output else "no output",
    )
    for line in output:
        if line.startswith("FAIL"):
            print(f"      {line}")

def test_z_mode(src: str, workdir: str) -> None:
    """z_mode=bed must place the object's bottom at the requested height."""
    target = copy(src, workdir, "z_mode.3mf")
    loaded = load(target)
    apply_edit(loaded, [{"op": "set_position", "object": 2, "z": 5.0, "z_mode": "bed"}])
    saved = save(loaded)
    bbox = describe(load(saved["output"]), "summary")["objects"]
    item = next(entry for entry in bbox if entry["id"] == 2)
    ok = abs(item["bbox"][2] - 5.0) < 1e-3
    check(ok, "z_mode=bed puts bottom at z", f"bottom {item['bbox'][2]:.3f}")


def test_seam_report(src: str, workdir: str) -> None:
    """A box selection that cuts through a shell must report seam edges."""
    loaded = load(src)
    item = describe(loaded, "summary")["objects"][0]
    lo, hi = item["bbox"][0], item["bbox"][3]
    result = apply_mesh(
        loaded,
        "2",
        "translate",
        {"dx": 0.0, "dy": 0.0, "dz": 0.001, "select": {"bbox": {"min": [lo, -200, -200], "max": [lo + 3, 200, 200]}}},
    )
    seam = int(result["stats"]["selection"].get("seamEdges") or 0)
    check(seam > 0, "seam edges reported for split selection", f"{seam} edges")


def test_slice(src: str, workdir: str, skip: bool) -> None:
    if skip or not os.path.isfile(BAMBU):
        check(True, "slice edited project", "skipped")
        return
    target = copy(src, workdir, "slice_me.3mf")
    loaded = load(target)
    apply_edit(loaded, [{"op": "move", "object": 2, "dx": 10, "dy": 10}])
    saved = save(loaded)
    out_dir = os.path.join(workdir, "sliced")
    os.makedirs(out_dir, exist_ok=True)
    started = time.time()
    process = subprocess.run(
        [
            BAMBU,
            "--slice",
            "1",
            "--export-3mf",
            "selftest.gcode.3mf",
            "--outputdir",
            out_dir,
            "--allow-newer-file",
            saved["output"],
        ],
        capture_output=True,
        timeout=600,
    )
    result_path = os.path.join(out_dir, "result.json")
    ok = process.returncode == 0 and os.path.isfile(os.path.join(out_dir, "selftest.gcode.3mf"))
    detail = f"exit {process.returncode}, {time.time() - started:.1f}s"
    if os.path.isfile(result_path):
        with open(result_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        objects = payload.get("sliced_plates", [{}])[0].get("objects", [])
        shifted = any(abs(obj["bbox"]["x"] - 150.0) < 0.5 for obj in objects if isinstance(obj.get("bbox"), dict))
        ok = ok and payload.get("return_code") == 0 and shifted
        detail += f", moved object x={[round(o['bbox']['x'], 1) for o in objects if 'bbox' in o]}"
    check(ok, "slice edited project", detail)


def test_cut_slice(src: str, workdir: str, skip: bool) -> None:
    """Regression: cutting a shared-mesh object must still slice."""
    if skip or not os.path.isfile(BAMBU):
        check(True, "slice a cut object", "skipped")
        return
    target = copy(src, workdir, "cut_slice.3mf")
    loaded = load(target)
    top = describe(loaded)["objects"][0]["bbox"][5]
    apply_mesh(
        loaded,
        "2",
        "plane_cut",
        {"plane": {"axis": "z", "value": top / 2.0}, "keep": "negative", "cap": True},
    )
    saved = save(loaded)
    out_dir = os.path.join(workdir, "sliced_cut")
    os.makedirs(out_dir, exist_ok=True)
    process = subprocess.run(
        [
            BAMBU,
            "--slice",
            "1",
            "--export-3mf",
            "cut.gcode.3mf",
            "--outputdir",
            out_dir,
            "--allow-newer-file",
            saved["output"],
        ],
        capture_output=True,
        timeout=600,
    )
    result_path = os.path.join(out_dir, "result.json")
    return_code = None
    if os.path.isfile(result_path):
        with open(result_path, "r", encoding="utf-8") as handle:
            return_code = json.load(handle).get("return_code")
    ok = process.returncode == 0 and return_code == 0 and os.path.isfile(
        os.path.join(out_dir, "cut.gcode.3mf")
    )
    check(ok, "slice a cut object", f"exit {process.returncode}, return {return_code}")


def test_delete_cleans_package(name: str, path: str, workdir: str, skip: bool) -> None:
    """Regression: deleting an object used to leave a stale ``<assemble_item>``
    and a dangling relationship, both of which make Bambu Studio refuse to parse
    the package ("The input model file to the slicer can not be parsed")."""
    target = copy(path, workdir, "delete_clean.3mf")
    loaded = load(target)
    plates = [plate for plate in loaded.settings.plates if plate.instances]
    plate = max(plates, key=lambda item: len(item.instances))
    victim = int(plate.instances[-1]["object_id"])
    apply_edit(loaded, [{"op": "delete", "object": victim}])
    saved = save(loaded)
    out = saved["output"]
    with zipfile.ZipFile(out) as zf:
        model_settings = zf.read("Metadata/model_settings.config").decode("utf-8")
        names = set(zf.namelist())
        rels = zf.read("3D/_rels/3dmodel.model.rels").decode("utf-8")
    stale = re.findall(rf'<assemble_item object_id="{victim}"', model_settings)
    dangling = [item for item in re.findall(r'Target="/([^"]+)"', rels) if item not in names]
    ok = not stale and not dangling
    detail = f"删除 {victim}：残留 assemble {len(stale)}，悬空关系 {len(dangling)}"
    if not skip and os.path.isfile(BAMBU):
        sliced, note = slice_project(out, plate.plater_id, "delete", workdir)
        ok = ok and sliced
        detail += f"，{note}"
    check(ok, f"delete keeps the package openable [{name}]", detail)


def test_component_select(name: str, path: str, workdir: str, skip: bool) -> None:
    """A multi-shell object can be reshaped one shell at a time (shrink just the
    centre shaft without exporting STL, deleting and re-importing)."""
    found = multi_shell_object(path)
    if found is None:
        check(True, f"connected shells listed [{name}]", "no multi-shell sample")
        return
    object_id, _mesh, count = found
    components = apply_mesh(load(path), object_id, "components", {})["components"]
    check(
        len(components) == count,
        f"connected shells listed [{name}]",
        f"对象 {object_id}: {len(components)} 个",
    )

    target = components[1] if len(components) > 1 else components[0]
    before = {item["index"]: item["size"] for item in components}
    work = copy(path, workdir, "select.3mf")
    loaded = load(work)
    plate = loaded.settings.plate_of(object_id) if loaded.settings else 1
    apply_mesh(
        loaded,
        object_id,
        "scale",
        {"scale": [0.9, 0.9, 1.0], "select": {"component": target["index"]}},
    )
    saved = save(loaded)
    after = {
        item["index"]: item["size"]
        for item in apply_mesh(load(saved["output"]), object_id, "components", {})["components"]
    }
    ratios = [round(after[target["index"]][i] / target["size"][i], 4) for i in range(3)]
    untouched = all(
        max(abs(after[index][i] - before[index][i]) for i in range(3)) < 1e-3
        for index in before
        if index != target["index"]
    )
    ok = (
        abs(ratios[0] - 0.9) < 1e-3
        and abs(ratios[1] - 0.9) < 1e-3
        and abs(ratios[2] - 1.0) < 1e-3
        and untouched
    )
    check(
        ok,
        f"select scales only that shell [{name}]",
        f"#{target['index']} ×{ratios}，其余 {len(before) - 1} 个不动",
    )
    if not skip and os.path.isfile(BAMBU):
        sliced, note = slice_project(saved["output"], plate or 1, "select", workdir)
        check(sliced, f"slice a selected-shell edit [{name}]", note)


def test_yibox_edit(src: str, workdir: str) -> None:
    """Regression: a big project with -0.0 transforms must still be JSON-clean."""
    target = copy(src, workdir, "yibox.3mf")
    loaded = load(target)
    before = next(item for item in describe(load(target))["objects"] if item["id"] == 16)["bbox"]
    apply_edit(
        loaded,
        [
            {"op": "rename", "object": 16, "name": "回归测试"},
            {"op": "move", "object": 16, "dx": 15, "dy": 10},
        ],
    )
    saved = save(loaded)
    data = describe(load(saved["output"]))
    item = next((entry for entry in data["objects"] if entry["id"] == 16), None)
    ok = (
        item is not None
        and item["name"] == "回归测试"
        and abs((item["bbox"][0] - before[0]) - 15) < 1e-3
    )
    check(ok, "large project edit stays valid", f"objects {data['stats']['objects']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default=r"D:\下载")
    parser.add_argument("--workdir", default=os.path.join(ROOT, "..", "3mf-sandbox", "selftest"))
    parser.add_argument("--skip-slice", action="store_true")
    args = parser.parse_args()
    if not os.path.isdir(args.samples):
        fallback = r"H:\bambu"
        if os.path.isdir(fallback):
            print(f"samples dir {args.samples} missing; falling back to {fallback}", flush=True)
            args.samples = fallback
        else:
            print(f"samples dir not found: {args.samples}")
            return 2

    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    samples = {}
    for name in sorted(os.listdir(args.samples)):
        if name.lower().endswith(".3mf"):
            samples[name] = os.path.join(args.samples, name)
    if not samples:
        print(f"no samples under {args.samples}")
        return 2

    source_hashes = {name: sha256(path) for name, path in samples.items()}
    print(f"samples: {len(samples)}  workdir: {workdir}\n")

    test_inspect(samples, workdir)
    cutter = next((p for n, p in samples.items() if n == "Cutter.3mf"), None)
    monitor = next((p for n, p in samples.items() if "监控支架" in n), None)
    if cutter:
        test_preservation(cutter, workdir)
        test_move(cutter, workdir)
        test_bake(cutter, workdir)
        test_scale(cutter, workdir)
        test_cut(cutter, workdir)
        test_stretch(cutter, workdir, args.skip_slice)
        test_preflight_advisory(cutter, workdir)
        test_z_mode(cutter, workdir)
        test_clearance_is_safe(cutter, workdir)
        test_stretch_band_bounds(cutter, workdir)
        test_seam_report(cutter, workdir)
        test_settings(cutter, workdir)
        test_render(cutter, workdir, samples)
        test_stl_roundtrip(cutter, workdir)
        test_slice(cutter, workdir, args.skip_slice)
        test_cut_slice(cutter, workdir, args.skip_slice)
    if monitor:
        test_bake(monitor, workdir)
    yibox = next((p for n, p in samples.items() if "YIBOX" in n), None)
    if yibox:
        test_yibox_edit(yibox, workdir)
    assemble_name, assemble_path = project_with_assemble(samples)
    if assemble_path:
        test_delete_cleans_package(assemble_name, assemble_path, workdir, args.skip_slice)
    shell_path = next(
        (p for p in samples.values() if multi_shell_object(p) is not None), None
    )
    if shell_path:
        test_component_select(
            os.path.basename(shell_path), shell_path, workdir, args.skip_slice
        )
    test_core_file(workdir)
    test_slice_info(workdir)
    test_printability(workdir)
    test_build_and_probes(workdir)
    test_diff(samples, workdir)
    test_slice_result_js(workdir)
    test_sliced_refusal(samples)
    test_preflight_accepts_what_bambu_accepts(samples, workdir)
    test_preflight_plate2_coordinates(samples, workdir)
    test_clearance_noise(samples, workdir)
    if cutter:
        test_output_schema_contract(cutter, workdir)

    unchanged = all(sha256(path) == source_hashes[name] for name, path in samples.items())
    check(unchanged, "source samples unchanged")

    failures = [item for item in RESULTS if not item[0]]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
