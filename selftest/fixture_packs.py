"""Synthetic 3MF packages shared by the self tests.

The showpiece is a minimal "already sliced" package: the same cube as
make_core_3mf plus model/project settings and a slice_info.config. That lets
the tests exercise the slicer-report path without owning a real .gcode.3mf and
without invoking Bambu Studio.
"""

from __future__ import annotations

import zipfile

CUBE_VERTICES = [
    (0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0),
    (0, 0, 10), (20, 0, 10), (20, 20, 10), (0, 20, 10),
]
CUBE_FACES = [
    (0, 2, 1), (0, 3, 2),
    (4, 5, 6), (4, 6, 7),
    (0, 1, 5), (0, 5, 4),
    (1, 2, 6), (1, 6, 5),
    (2, 3, 7), (2, 7, 6),
    (3, 0, 4), (3, 4, 7),
]

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)

RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
)


def _model_xml() -> str:
    vertex_xml = "".join(
        f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in CUBE_VERTICES
    )
    triangle_xml = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in CUBE_FACES
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<resources><object id="1" type="model"><mesh><vertices>'
        + vertex_xml
        + "</vertices><triangles>"
        + triangle_xml
        + "</triangles></mesh></object></resources>"
        '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 120 120 0"/></build></model>'
    )


MODEL_SETTINGS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<config>"
    '  <object id="1">'
    '    <metadata key="name" value="cube.stl"/>'
    '    <metadata key="extruder" value="1"/>'
    '    <part id="1" subtype="normal_part" uuid="11111111-2222-3333-4444-555555555555">'
    '      <metadata key="name" value="cube.stl"/>'
    '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>'
    "    </part>"
    "  </object>"
    "  <plate>"
    '    <metadata key="plater_id" value="1"/>'
    '    <metadata key="plater_name" value="测试盘"/>'
    '    <metadata key="locked" value="false"/>'
    "    <model_instance>"
    '      <metadata key="object_id" value="1"/>'
    '      <metadata key="instance_id" value="0"/>'
    '      <metadata key="identify_id" value="7"/>'
    "    </model_instance>"
    "  </plate>"
    "</config>"
)

PROJECT_SETTINGS = (
    '{"printer_model": "Bambu Lab A1", "printer_settings_id": "selftest", '
    '"layer_height": "0.2", "wall_loops": "2", '
    '"printable_area": ["0x0", "256x0", "256x256", "0x256"], '
    '"printable_height": "256", "filament_colour": ["#FF0000"], '
    '"filament_type": ["PLA"]}'
)


def slice_info_xml(prediction: int = 7200, weight: str = "42.50") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<config>"
        "  <header>"
        '    <header_item key="X-BBL-Client-Type" value="slicer"/>'
        '    <header_item key="X-BBL-Client-Version" value="02.99.00.01"/>'
        "  </header>"
        "  <plate>"
        '    <metadata key="index" value="1"/>'
        f'    <metadata key="prediction" value="{prediction}"/>'
        f'    <metadata key="weight" value="{weight}"/>'
        '    <metadata key="support_used" value="false"/>'
        '    <metadata key="pause_count" value="1"/>'
        '    <metadata key="first_layer_time" value="12.0"/>'
        '    <object identify_id="7" name="cube.stl" skipped="false"/>'
        '    <filament id="1" tray_info_idx="GFA00" type="PLA" color="#FF0000" '
        'used_m="14.00" used_g="42.50"/>'
        '    <warning msg="synth_warning" level="2" error_code="1000"/>'
        "  </plate>"
        "</config>"
    )


def make_sliced_3mf(path: str, prediction: int = 7200, weight: str = "42.50") -> None:
    """Write a minimal sliced .gcode.3mf: cube + settings + slice_info + gcode."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("3D/3dmodel.model", _model_xml())
        zf.writestr("Metadata/model_settings.config", MODEL_SETTINGS)
        zf.writestr("Metadata/project_settings.config", PROJECT_SETTINGS)
        zf.writestr("Metadata/slice_info.config", slice_info_xml(prediction, weight))
        zf.writestr("Metadata/plate_1.gcode", "; synthetic gcode\nG1 X0 Y0\n")