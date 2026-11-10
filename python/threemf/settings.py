"""Bambu metadata: ``model_settings.config``, ``project_settings.config``, plates.

``model_settings.config`` maps root object ids to parts, per-object settings and
plate membership. ``project_settings.config`` is the flattened print profile as
a JSON object.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
from lxml import etree

from .constants import (
    LAYER_RANGES,
    MODEL_SETTINGS,
    PLATE_COLUMNS,
    PLATE_PITCH,
    PROJECT_SETTINGS,
    ThreeMFError,
)
from .model import format_transform, parse_xml, xml_bytes

IDENTITY_4X4 = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"


# ------------------------------------------------------------------ model settings
@dataclass
class PlateInfo:
    element: etree._Element
    plater_id: int
    name: str
    locked: bool
    instances: list[dict]

    def object_ids(self) -> list[int]:
        return [int(item["object_id"]) for item in self.instances]


@dataclass
class ModelSettings:
    root: etree._Element
    dirty: bool = False
    plates: list[PlateInfo] = field(default_factory=list)

    @classmethod
    def parse(cls, data: bytes) -> "ModelSettings":
        root = parse_xml(data)
        settings = cls(root=root)
        settings._index()
        return settings

    def _index(self) -> None:
        self.plates = []
        for plate in self.root.iterfind("plate"):
            meta = _metadata(plate)
            try:
                plater_id = int(meta.get("plater_id", "0") or 0)
            except ValueError:
                plater_id = 0
            instances = []
            for instance in plate.iterfind("model_instance"):
                instance_meta = _metadata(instance)
                if "object_id" not in instance_meta:
                    continue
                instances.append(
                    {
                        "object_id": int(instance_meta["object_id"]),
                        "instance_id": int(instance_meta.get("instance_id", "0") or 0),
                        "identify_id": int(instance_meta.get("identify_id", "0") or 0),
                        "element": instance,
                    }
                )
            self.plates.append(
                PlateInfo(
                    element=plate,
                    plater_id=plater_id,
                    name=meta.get("plater_name", ""),
                    locked=meta.get("locked", "false") == "true",
                    instances=instances,
                )
            )

    # ------------------------------------------------------------------ objects
    def object_element(self, object_id: int | str) -> etree._Element | None:
        for obj in self.root.iterfind("object"):
            if obj.get("id") == str(object_id):
                return obj
        return None

    def object_meta(self, object_id: int | str) -> dict:
        element = self.object_element(object_id)
        return _metadata(element) if element is not None else {}

    def parts(self, object_id: int | str) -> list[dict]:
        element = self.object_element(object_id)
        if element is None:
            return []
        result = []
        for part in element.iterfind("part"):
            meta = _metadata(part)
            result.append(
                {
                    "id": part.get("id"),
                    "subtype": part.get("subtype"),
                    "uuid": part.get("uuid"),
                    "metadata": meta,
                }
            )
        return result

    def set_object_meta(self, object_id: int | str, key: str, value: str) -> None:
        element = self.object_element(object_id)
        if element is None:
            raise ThreeMFError(
                "MISSING_OBJECT",
                f"model_settings has no object {object_id}",
                {"objectId": object_id},
            )
        _set_metadata(element, key, value)
        self.dirty = True

    def set_part_meta(self, object_id: int | str, part_id: str, key: str, value: str) -> None:
        element = self.object_element(object_id)
        if element is None:
            raise ThreeMFError("MISSING_OBJECT", f"model_settings has no object {object_id}")
        for part in element.iterfind("part"):
            if part.get("id") == str(part_id):
                _set_metadata(part, key, value)
                self.dirty = True
                return
        raise ThreeMFError("MISSING_OBJECT", f"object {object_id} has no part {part_id}")

    def add_object(
        self,
        object_id: int,
        name: str,
        extruder: int,
        parts: list[dict],
        face_count: int | None = None,
    ) -> etree._Element:
        if self.object_element(object_id) is not None:
            raise ThreeMFError("DUPLICATE", f"model_settings already has object {object_id}")
        element = etree.SubElement(self.root, "object")
        element.set("id", str(object_id))
        _set_metadata(element, "name", name)
        _set_metadata(element, "extruder", str(extruder))
        if face_count is not None:
            meta = etree.SubElement(element, "metadata")
            meta.set("face_count", str(face_count))
        for part in parts:
            part_element = etree.SubElement(element, "part")
            part_element.set("id", str(part["id"]))
            part_element.set("subtype", part.get("subtype", "normal_part"))
            part_element.set("uuid", part.get("uuid") or "")
            _set_metadata(part_element, "name", name)
            _set_metadata(
                part_element,
                "matrix",
                part.get("matrix", IDENTITY_4X4),
            )
            for key, value in (part.get("metadata") or {}).items():
                _set_metadata(part_element, key, str(value))
            if part.get("mesh_stat") is not None:
                mesh_stat = etree.SubElement(part_element, "mesh_stat")
                mesh_stat.set("face_count", str(part["mesh_stat"]))
                for key in (
                    "edges_fixed",
                    "degenerate_facets",
                    "facets_removed",
                    "facets_reversed",
                    "backwards_edges",
                ):
                    mesh_stat.set(key, "0")
        self.dirty = True
        return element

    def remove_object(self, object_id: int | str) -> None:
        element = self.object_element(object_id)
        if element is not None:
            element.getparent().remove(element)
            self.dirty = True
        for plate in self.plates:
            for instance in list(plate.instances):
                if int(instance["object_id"]) == int(object_id):
                    plate.instances.remove(instance)
                    instance["element"].getparent().remove(instance["element"])
                    self.dirty = True
        self.prune_assemble(object_id)

    def prune_assemble(self, object_id: int | str) -> int:
        """Drop ``<assemble_item>`` entries that point at a removed object.

        Bambu Studio refuses to open a package whose ``<assemble>`` still names a
        deleted object, so removal has to clean both the ``instance`` and the
        ``volume`` item (they are two separate elements per object).
        """
        target = str(object_id)
        removed = 0
        for assemble in self.root.iter("assemble"):
            for item in list(assemble.iterfind("assemble_item")):
                if item.get("object_id") == target:
                    assemble.remove(item)
                    removed += 1
        if removed:
            self.dirty = True
        return removed

    # ------------------------------------------------------------------- plates
    def plate_of(self, object_id: int | str) -> int | None:
        for plate in self.plates:
            if int(object_id) in plate.object_ids():
                return plate.plater_id
        return None

    def plate(self, plater_id: int) -> PlateInfo | None:
        for plate in self.plates:
            if plate.plater_id == int(plater_id):
                return plate
        return None

    def add_plate(self, plater_id: int, name: str = "") -> PlateInfo:
        if self.plate(plater_id) is not None:
            raise ThreeMFError("DUPLICATE", f"plate {plater_id} already exists")
        element = etree.SubElement(self.root, "plate")
        _set_metadata(element, "plater_id", str(plater_id))
        _set_metadata(element, "plater_name", name)
        _set_metadata(element, "locked", "false")
        _set_metadata(element, "filament_map_mode", "Auto For Flush")
        _set_metadata(element, "filament_maps", "1")
        _set_metadata(element, "filament_volume_maps", "0")
        plate = PlateInfo(element, plater_id, name, False, [])
        self.plates.append(plate)
        self.dirty = True
        return plate

    def next_identify_id(self) -> int:
        highest = 0
        for plate in self.plates:
            for instance in plate.instances:
                highest = max(highest, int(instance["identify_id"]))
        return highest + 1

    def add_instance(self, object_id: int, plater_id: int, identify_id: int | None = None) -> None:
        plate = self.plate(plater_id)
        if plate is None:
            plate = self.add_plate(plater_id)
        for existing in plate.instances:
            if int(existing["object_id"]) == int(object_id):
                return
        element = etree.SubElement(plate.element, "model_instance")
        new_identify = identify_id if identify_id is not None else self.next_identify_id()
        _set_metadata(element, "object_id", str(object_id))
        _set_metadata(element, "instance_id", "0")
        _set_metadata(element, "identify_id", str(new_identify))
        plate.instances.append(
            {
                "object_id": int(object_id),
                "instance_id": 0,
                "identify_id": int(new_identify),
                "element": element,
            }
        )
        self.dirty = True

    def move_to_plate(self, object_id: int, plater_id: int) -> None:
        current = self.plate_of(object_id)
        if current == plater_id:
            return
        if current is not None:
            plate = self.plate(current)
            if plate is not None:
                for instance in list(plate.instances):
                    if int(instance["object_id"]) == int(object_id):
                        plate.instances.remove(instance)
                        instance["element"].getparent().remove(instance["element"])
                        self.dirty = True
        self.add_instance(object_id, plater_id)

    def serialize(self) -> bytes:
        return xml_bytes(self.root, indent=2)

    # ------------------------------------------------------------------- layer
    def remove_layer_ranges(self, object_id: int | str) -> bool:
        """Best-effort cleanup of ``layer_config_ranges.xml`` is done by caller."""
        return False


def _metadata(element: etree._Element) -> dict:
    result = {}
    for meta in element.iterfind("metadata"):
        key = meta.get("key")
        if key:
            result[key] = meta.get("value", "")
    return result


def _set_metadata(element: etree._Element, key: str, value: str) -> None:
    for meta in element.iterfind("metadata"):
        if meta.get("key") == key:
            meta.set("value", value)
            return
    meta = etree.SubElement(element, "metadata")
    meta.set("key", key)
    meta.set("value", value)


# ------------------------------------------------------------------- project cfg
@dataclass
class ProjectSettings:
    data: dict
    dirty: bool = False

    @classmethod
    def parse(cls, data: bytes) -> "ProjectSettings":
        try:
            payload = json.loads(data.decode("utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ThreeMFError(
                "INVALID_3MF", f"project_settings.config is not JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ThreeMFError("INVALID_3MF", "project_settings.config is not an object")
        return cls(data=payload)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> tuple[object, object]:
        old = self.data.get(key, None)
        self.data[key] = value
        self.dirty = True
        return old, value

    def unset(self, key: str) -> tuple[object, object]:
        old = self.data.pop(key, None)
        self.dirty = True
        return old, None

    def serialize(self) -> bytes:
        text = json.dumps(self.data, indent=4, ensure_ascii=False)
        return (text + "\n").encode("utf-8")

    # ------------------------------------------------------------------ derived
    def printable_area(self) -> list[list[float]]:
        raw = self.data.get("printable_area") or []
        points = []
        for item in raw:
            if isinstance(item, str) and "x" in item:
                x, y = item.split("x", 1)
                points.append([float(x), float(y)])
        return points

    def bed_size(self) -> tuple[float, float, float]:
        points = self.printable_area()
        if not points:
            return 256.0, 256.0, float(self.data.get("printable_height") or 256)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        height = float(self.data.get("printable_height") or 256)
        return max(xs) - min(xs), max(ys) - min(ys), height

    def bed_origin(self) -> tuple[float, float]:
        points = self.printable_area()
        if not points:
            return 0.0, 0.0
        return min(p[0] for p in points), min(p[1] for p in points)

    def filament_colors(self) -> list[str]:
        colors = self.data.get("filament_colour") or []
        return [str(c) for c in colors]

    def filament_types(self) -> list[str]:
        types = self.data.get("filament_type") or []
        return [str(t) for t in types]

    def printer_model(self) -> str:
        return str(self.data.get("printer_model") or "")

    def settings_id(self) -> str:
        return str(self.data.get("printer_settings_id") or "")


# ------------------------------------------------------------------- key match
def match_keys(keys, patterns) -> list[str]:
    """Resolve exact, prefix or fnmatch patterns against a key collection.

    Shared by inspect's settings_keys filter and the diff tool, so both agree on
    what a pattern means (a typo still matches nothing; a prefix is the common
    case for wipe_tower_* style families).
    """
    import fnmatch

    known = sorted(keys)
    lookup = set(known)
    wanted: list[str] = []
    for pattern in patterns or []:
        text = str(pattern).strip()
        if not text:
            continue
        if "*" in text or "?" in text:
            hits = [key for key in known if fnmatch.fnmatchcase(key, text)]
        elif text in lookup:
            hits = [text]
        else:
            hits = [key for key in known if key.startswith(text)]
        for key in hits:
            if key not in wanted:
                wanted.append(key)
    return wanted


# ------------------------------------------------------------------ plate origin
def plate_origin(
    plate_index: int,
    bed: tuple[float, float, float],
    origin: tuple[float, float] = (0.0, 0.0),
    columns: int = PLATE_COLUMNS,
) -> np.ndarray:
    """Scene-space origin of plate ``plate_index`` (0-based).

    Bambu Studio lays plates out on a grid whose pitch is 1.2x the bed size,
    three per row, with rows going towards negative Y.
    """
    columns = max(1, int(columns))
    stride_x = bed[0] * PLATE_PITCH
    stride_y = bed[1] * PLATE_PITCH
    col = plate_index % columns
    row = plate_index // columns
    return np.array(
        [origin[0] + col * stride_x, origin[1] - row * stride_y, 0.0],
        dtype=np.float64,
    )


def plate_origins(
    plater_ids: list[int],
    bed: tuple[float, float, float],
    origin: tuple[float, float] = (0.0, 0.0),
    columns: int = PLATE_COLUMNS,
) -> dict[int, np.ndarray]:
    ordered = sorted(plater_ids)
    return {
        plater_id: plate_origin(index, bed, origin, columns)
        for index, plater_id in enumerate(ordered)
    }


# ------------------------------------------------------------------- layer ranges
def remove_layer_range_object(root: etree._Element, object_id: int) -> bool:
    removed = False
    for obj in list(root.iterfind("object")):
        if int(obj.get("id", "-1")) == int(object_id):
            obj.getparent().remove(obj)
            removed = True
    return removed
