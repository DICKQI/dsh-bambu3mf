"""3MF model documents: the root model, production-extension object files, meshes.

The production extension splits one logical scene into a root index
(``3D/3dmodel.model``) plus one file per part (``3D/Objects/object_N.model``).
A build item references a root object, whose component carries
``p:path`` + ``objectid``. All geometry is stored in the object file.

Coordinates follow the 3MF core spec: a transform is a 3x4 matrix applied to a
*row* vector, so the last three numbers are the translation.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

import numpy as np
from lxml import etree

from .constants import (
    CORE_NS,
    NS,
    PROD_NS,
    ROOT_MODEL,
    ROOT_RELS,
    RELS_NS,
    ThreeMFError,
)
from .package import Package

_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>\s*", re.IGNORECASE)
_INDENT_RE = re.compile(r"(?m)^((?:  )+)")


# --------------------------------------------------------------------------- xml
def parse_xml(data: bytes) -> etree._Element:
    parser = etree.XMLParser(
        remove_blank_text=True,
        resolve_entities=False,
        huge_tree=True,
        recover=False,
    )
    return etree.fromstring(data, parser=parser)


def xml_bytes(root: etree._Element, indent: int = 1) -> bytes:
    text = etree.tostring(
        root, encoding="UTF-8", xml_declaration=True, pretty_print=True
    ).decode("utf-8")
    if indent == 1:
        # Bambu Studio writes one-space indentation; keep diffs quiet.
        text = _INDENT_RE.sub(lambda m: " " * (len(m.group(1)) // 2), text)
    return text.encode("utf-8")


def qn(tag: str) -> str:
    """Qualify a core-namespace tag."""
    return f"{{{CORE_NS}}}{tag}"


def pqn(tag: str) -> str:
    return f"{{{PROD_NS}}}{tag}"


def num(value: float) -> str:
    return f"{float(value):.9g}"


# ---------------------------------------------------------------------- matrices
IDENTITY = np.eye(4, dtype=np.float64)


def parse_transform(text: str | None) -> np.ndarray:
    """Parse a 3MF transform (12 numbers, row-vector convention) into 4x4."""
    if not text:
        return IDENTITY.copy()
    parts = text.split()
    if len(parts) != 12:
        raise ThreeMFError(
            "INVALID_3MF",
            f"transform must have 12 numbers, got {len(parts)}",
            {"transform": text},
        )
    a = [float(v) for v in parts]
    m = np.zeros((4, 4), dtype=np.float64)
    m[0, :3] = a[0:3]
    m[1, :3] = a[3:6]
    m[2, :3] = a[6:9]
    m[3, :] = [a[9], a[10], a[11], 1.0]
    return m


def format_transform(m: np.ndarray) -> str:
    values = [
        m[0, 0], m[0, 1], m[0, 2],
        m[1, 0], m[1, 1], m[1, 2],
        m[2, 0], m[2, 1], m[2, 2],
        m[3, 0], m[3, 1], m[3, 2],
    ]
    return " ".join(num(v) for v in values)


def is_identity(m: np.ndarray, tol: float = 1e-9) -> bool:
    return bool(np.allclose(m, IDENTITY, atol=tol, rtol=0))


def transform_points(points: np.ndarray, m: np.ndarray) -> np.ndarray:
    if points.size == 0:
        return points.reshape(0, 3)
    homogeneous = np.hstack([points, np.ones((len(points), 1), dtype=np.float64)])
    return (homogeneous @ m)[:, :3]


def translation(x: float, y: float, z: float) -> np.ndarray:
    m = IDENTITY.copy()
    m[3, 0], m[3, 1], m[3, 2] = float(x), float(y), float(z)
    return m


def rotation_z(degrees: float) -> np.ndarray:
    rad = np.radians(float(degrees))
    c, s = np.cos(rad), np.sin(rad)
    m = IDENTITY.copy()
    m[0, 0], m[0, 1] = c, s
    m[1, 0], m[1, 1] = -s, c
    return m


def rotation_axis(axis: str, degrees: float) -> np.ndarray:
    rad = np.radians(float(degrees))
    c, s = np.cos(rad), np.sin(rad)
    m = IDENTITY.copy()
    if axis == "x":
        m[1, 1], m[1, 2] = c, s
        m[2, 1], m[2, 2] = -s, c
    elif axis == "y":
        m[0, 0], m[0, 2] = c, -s
        m[2, 0], m[2, 2] = s, c
    elif axis == "z":
        m[0, 0], m[0, 1] = c, s
        m[1, 0], m[1, 1] = -s, c
    else:
        raise ThreeMFError("INVALID_ARGS", f"unknown axis: {axis}")
    return m


def scale_matrix(factors: np.ndarray) -> np.ndarray:
    m = IDENTITY.copy()
    m[0, 0], m[1, 1], m[2, 2] = (float(v) for v in factors)
    return m


def mirror_matrix(axis: str) -> np.ndarray:
    m = IDENTITY.copy()
    index = {"x": 0, "y": 1, "z": 2}.get(axis)
    if index is None:
        raise ThreeMFError("INVALID_ARGS", f"unknown mirror axis: {axis}")
    m[index, index] = -1.0
    return m


def about_pivot(m: np.ndarray, pivot: np.ndarray) -> np.ndarray:
    """Return ``T(-p) @ M @ T(p)`` so a row-vector point rotates around pivot."""
    return translation(-pivot[0], -pivot[1], -pivot[2]) @ m @ translation(
        pivot[0], pivot[1], pivot[2]
    )


# -------------------------------------------------------------------------- mesh
@dataclass
class Mesh:
    vertices: np.ndarray
    triangles: np.ndarray
    tri_attrs: list[dict] = field(default_factory=list)

    def clone(self) -> "Mesh":
        return Mesh(
            self.vertices.copy(),
            self.triangles.copy(),
            [dict(a) for a in self.tri_attrs],
        )

    @property
    def triangle_count(self) -> int:
        return int(len(self.triangles))

    @property
    def vertex_count(self) -> int:
        return int(len(self.vertices))

    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        if len(self.vertices) == 0:
            zero = np.zeros(3)
            return zero, zero
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    def is_empty(self) -> bool:
        return len(self.vertices) == 0 or len(self.triangles) == 0


def empty_mesh() -> Mesh:
    return Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), [])


def read_mesh(object_element: etree._Element) -> Mesh | None:
    mesh_el = object_element.find("m:mesh", NS)
    if mesh_el is None:
        return None
    verts: list[tuple[float, float, float]] = []
    for v in mesh_el.iterfind("m:vertices/m:vertex", NS):
        verts.append((float(v.get("x")), float(v.get("y")), float(v.get("z"))))
    vertices = np.array(verts, dtype=np.float64).reshape(-1, 3)
    tris: list[tuple[int, int, int]] = []
    attrs: list[dict] = []
    for t in mesh_el.iterfind("m:triangles/m:triangle", NS):
        tris.append((int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))))
        extra = {
            k: v
            for k, v in t.attrib.items()
            if k not in ("v1", "v2", "v3")
        }
        attrs.append(extra)
    triangles = np.array(tris, dtype=np.int64).reshape(-1, 3)
    return Mesh(vertices, triangles, attrs)


def build_mesh_element(mesh: Mesh) -> etree._Element:
    element = etree.Element(qn("mesh"))
    vertices_el = etree.SubElement(element, qn("vertices"))
    for x, y, z in mesh.vertices.tolist():
        etree.SubElement(
            vertices_el,
            qn("vertex"),
            x=num(x),
            y=num(y),
            z=num(z),
        )
    triangles_el = etree.SubElement(element, qn("triangles"))
    attrs = mesh.tri_attrs
    for index, (a, b, c) in enumerate(mesh.triangles.tolist()):
        attrib = {"v1": str(a), "v2": str(b), "v3": str(c)}
        if index < len(attrs):
            attrib.update(attrs[index])
        etree.SubElement(triangles_el, qn("triangle"), **attrib)
    return element


def set_mesh(object_element: etree._Element, mesh: Mesh) -> None:
    old = object_element.find("m:mesh", NS)
    new = build_mesh_element(mesh)
    if old is not None:
        object_element.replace(old, new)
    else:
        object_element.insert(0, new)


def mesh_stats(mesh: Mesh) -> dict:
    lo, hi = mesh.bbox()
    return {
        "vertices": mesh.vertex_count,
        "triangles": mesh.triangle_count,
        "bbox": [float(v) for v in np.concatenate([lo, hi])],
        "size": [float(v) for v in (hi - lo)],
    }


# ----------------------------------------------------------------- object files
@dataclass
class MeshObject:
    id: str
    element: etree._Element
    mesh: Mesh | None
    components: list[dict]


@dataclass
class ObjectFile:
    path: str
    root: etree._Element
    objects: dict[str, MeshObject]
    dirty: bool = False

    @classmethod
    def parse(cls, path: str, data: bytes) -> "ObjectFile":
        root = parse_xml(data)
        objects: dict[str, MeshObject] = {}
        for obj in root.iterfind("m:resources/m:object", NS):
            oid = obj.get("id")
            if oid is None:
                continue
            components = []
            for comp in obj.iterfind("m:components/m:component", NS):
                components.append(
                    {
                        "objectid": comp.get("objectid"),
                        "path": (comp.get("{%s}path" % PROD_NS) or "").lstrip("/"),
                        "transform": parse_transform(comp.get("transform")),
                        "element": comp,
                    }
                )
            objects[oid] = MeshObject(oid, obj, read_mesh(obj), components)
        return cls(path=path, root=root, objects=objects)

    def serialize(self) -> bytes:
        return xml_bytes(self.root)

    def add_mesh_object(self, object_id: str, mesh: Mesh, uuid_value: str | None = None) -> etree._Element:
        resources = self.root.find("m:resources", NS)
        if resources is None:
            resources = etree.SubElement(self.root, qn("resources"))
        obj = etree.SubElement(resources, qn("object"))
        obj.set("id", str(object_id))
        obj.set(pqn("UUID"), uuid_value or new_uuid())
        obj.set("type", "model")
        obj.append(build_mesh_element(mesh))
        self.objects[str(object_id)] = MeshObject(str(object_id), obj, mesh, [])
        self.dirty = True
        return obj


# ------------------------------------------------------------------ root objects
@dataclass
class ComponentRef:
    element: etree._Element
    object_id: str
    path: str
    transform: np.ndarray

    @property
    def zip_path(self) -> str:
        return self.path.lstrip("/")


@dataclass
class RootObject:
    id: str
    element: etree._Element
    components: list[ComponentRef]
    inline_mesh: Mesh | None

    @property
    def uuid(self) -> str | None:
        return self.element.get(pqn("UUID"))


@dataclass
class BuildItem:
    object_id: str
    element: etree._Element
    transform: np.ndarray

    @property
    def uuid(self) -> str | None:
        return self.element.get(pqn("UUID"))

    @property
    def printable(self) -> bool:
        return self.element.get("printable", "1") not in ("0", "false")


def new_uuid() -> str:
    return str(uuid.uuid4())


class Scene:
    """Parsed view over one package: root model + object files + metadata."""

    def __init__(self, package: Package):
        self.pkg = package
        if not package.has(ROOT_MODEL):
            raise ThreeMFError(
                "INVALID_3MF",
                "not a 3MF model: 3D/3dmodel.model is missing",
            )
        self.root = parse_xml(package.read(ROOT_MODEL))
        self._root_dirty = False
        self._files: dict[str, ObjectFile] = {}
        self._file_dirty: set[str] = set()
        self.deleted_entries: set[str] = set()
        self.added_entries: dict[str, bytes] = {}
        self.extra_changes: dict[str, bytes] = {}
        self._index()

    # ------------------------------------------------------------------ parse
    def _index(self) -> None:
        self.root_objects: dict[str, RootObject] = {}
        self.build_items: list[BuildItem] = []
        for obj in self.root.iterfind("m:resources/m:object", NS):
            oid = obj.get("id")
            if oid is None:
                continue
            components: list[ComponentRef] = []
            for comp in obj.iterfind("m:components/m:component", NS):
                components.append(
                    ComponentRef(
                        element=comp,
                        object_id=str(comp.get("objectid")),
                        path=comp.get("{%s}path" % PROD_NS) or "",
                        transform=parse_transform(comp.get("transform")),
                    )
                )
            self.root_objects[str(oid)] = RootObject(
                id=str(oid),
                element=obj,
                components=components,
                inline_mesh=read_mesh(obj),
            )
        self.build_element = self.root.find("m:build", NS)
        for item in self.root.iterfind("m:build/m:item", NS):
            self.build_items.append(
                BuildItem(
                    object_id=str(item.get("objectid")),
                    element=item,
                    transform=parse_transform(item.get("transform")),
                )
            )

    @property
    def is_bambu(self) -> bool:
        return bool(
            self.pkg.has("Metadata/project_settings.config")
            or self.pkg.has("Metadata/model_settings.config")
            or self.root.nsmap.get("BambuStudio")
        )

    @property
    def is_sliced(self) -> bool:
        return any(
            name.startswith("Metadata/plate_") and name.endswith(".gcode")
            for name in self.pkg.names()
        )

    @property
    def unit(self) -> str:
        return self.root.get("unit", "millimeter")

    # ------------------------------------------------------------- object files
    def object_file(self, path: str) -> ObjectFile:
        zip_path = path.lstrip("/")
        if zip_path not in self._files:
            if not self.pkg.has(zip_path):
                raise ThreeMFError(
                    "MISSING_OBJECT_FILE",
                    f"object file referenced by the model is missing: {zip_path}",
                    {"path": zip_path},
                )
            self._files[zip_path] = ObjectFile.parse(zip_path, self.pkg.read(zip_path))
        return self._files[zip_path]

    def mesh_sources(self, object_id: str) -> list[tuple[str, str, Mesh, np.ndarray]]:
        """Flatten a root object into ``(file, objectId, mesh, local transform)``.

        ``local transform`` maps object-file mesh coordinates into the root
        object's local space (nested components compose in order).
        """
        root_object = self.root_objects.get(str(object_id))
        if root_object is None:
            raise ThreeMFError(
                "MISSING_OBJECT",
                f"root object {object_id} does not exist",
                {"objectId": object_id},
            )
        sources: list[tuple[str, str, Mesh, np.ndarray]] = []
        if root_object.inline_mesh is not None:
            sources.append((ROOT_MODEL, str(object_id), root_object.inline_mesh, IDENTITY.copy()))
            return sources
        for component in root_object.components:
            self._collect(
                component.path,
                component.object_id,
                component.transform,
                sources,
                depth=0,
            )
        return sources

    def _collect(
        self,
        path: str,
        object_id: str,
        transform: np.ndarray,
        out: list[tuple[str, str, Mesh, np.ndarray]],
        depth: int,
    ) -> None:
        if depth > 8:
            raise ThreeMFError("INVALID_3MF", "component nesting is too deep")
        if not path:
            # Component pointing inside the root model itself.
            target = self.root_objects.get(str(object_id))
            if target is None:
                return
            if target.inline_mesh is not None:
                out.append((ROOT_MODEL, str(object_id), target.inline_mesh, transform))
            for nested in target.components:
                self._collect(
                    nested.path,
                    nested.object_id,
                    nested.transform @ transform,
                    out,
                    depth + 1,
                )
            return
        object_file = self.object_file(path)
        target = object_file.objects.get(str(object_id))
        if target is None:
            # Bambu leaves empty placeholder files behind; treat as empty.
            return
        if target.mesh is not None:
            out.append((path, str(object_id), target.mesh, transform))
        for nested in target.components:
            self._collect(
                nested.path,
                nested.object_id,
                nested["transform"] @ transform,
                out,
                depth + 1,
            )

    def world_sources(self, object_id: str) -> list[tuple[str, str, Mesh, np.ndarray]]:
        build = self.build_transform(object_id)
        return [
            (path, oid, mesh, local @ build)
            for path, oid, mesh, local in self.mesh_sources(object_id)
        ]

    # --------------------------------------------------------------- transforms
    def build_item(self, object_id: str) -> BuildItem | None:
        for item in self.build_items:
            if item.object_id == str(object_id):
                return item
        return None

    def build_transform(self, object_id: str) -> np.ndarray:
        item = self.build_item(object_id)
        return item.transform.copy() if item else IDENTITY.copy()

    def set_build_transform(self, object_id: str, matrix: np.ndarray) -> None:
        item = self.build_item(object_id)
        if item is None:
            raise ThreeMFError(
                "MISSING_OBJECT", f"object {object_id} has no build item"
            )
        item.transform = matrix
        item.element.set("transform", format_transform(matrix))
        self._root_dirty = True

    def set_component_transform(self, root_object_id: str, path: str, object_id: str, matrix: np.ndarray) -> None:
        root_object = self.root_objects[str(root_object_id)]
        for component in root_object.components:
            if component.path.lstrip("/") == path.lstrip("/") and component.object_id == str(object_id):
                component.transform = matrix
                component.element.set("transform", format_transform(matrix))
                self._root_dirty = True
                return
        raise ThreeMFError(
            "MISSING_OBJECT",
            f"component {path}#{object_id} not found on object {root_object_id}",
        )

    # ------------------------------------------------------------------- bbox
    def world_bbox(self, object_id: str) -> tuple[np.ndarray, np.ndarray]:
        lo = np.array([np.inf, np.inf, np.inf])
        hi = np.array([-np.inf, -np.inf, -np.inf])
        for _path, _oid, mesh, matrix in self.world_sources(object_id):
            if mesh.is_empty():
                continue
            points = transform_points(mesh.vertices, matrix)
            lo = np.minimum(lo, points.min(axis=0))
            hi = np.maximum(hi, points.max(axis=0))
        if not np.all(np.isfinite(lo)):
            zero = np.zeros(3)
            return zero, zero
        return lo, hi

    def world_triangles(self, object_id: str) -> tuple[np.ndarray, np.ndarray]:
        """Return all world-space triangles for one root object."""
        verts: list[np.ndarray] = []
        tris: list[np.ndarray] = []
        offset = 0
        for _path, _oid, mesh, matrix in self.world_sources(object_id):
            if mesh.is_empty():
                continue
            world = transform_points(mesh.vertices, matrix)
            verts.append(world)
            tris.append(mesh.triangles + offset)
            offset += len(world)
        if not verts:
            return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
        return np.vstack(verts), np.vstack(tris)

    # ----------------------------------------------------------------- editing
    def object_ids(self) -> list[str]:
        return [item.object_id for item in self.build_items]

    def add_build_item(
        self,
        object_id: str,
        matrix: np.ndarray,
        printable: bool = True,
    ) -> BuildItem:
        if self.build_element is None:
            self.build_element = etree.SubElement(self.root, qn("build"))
        element = etree.SubElement(self.build_element, qn("item"))
        element.set("objectid", str(object_id))
        element.set(pqn("UUID"), new_uuid())
        element.set("transform", format_transform(matrix))
        element.set("printable", "1" if printable else "0")
        item = BuildItem(str(object_id), element, matrix)
        self.build_items.append(item)
        self._root_dirty = True
        return item

    def remove_build_item(self, object_id: str) -> None:
        item = self.build_item(object_id)
        if item is None:
            return
        self.build_items.remove(item)
        item.element.getparent().remove(item.element)
        self._root_dirty = True

    def remove_root_object(self, object_id: str) -> None:
        root_object = self.root_objects.pop(str(object_id), None)
        if root_object is not None:
            parent = root_object.element.getparent()
            if parent is not None:
                parent.remove(root_object.element)
            self._root_dirty = True
        self.remove_build_item(object_id)

    def root_object(self, object_id: str) -> RootObject:
        root_object = self.root_objects.get(str(object_id))
        if root_object is None:
            raise ThreeMFError(
                "MISSING_OBJECT",
                f"root object {object_id} does not exist",
                {"objectId": object_id},
            )
        return root_object

    def add_root_object(self, mesh_entries: list[tuple[str, str]], uuid_value: str | None = None) -> RootObject:
        """Create a root object whose components point at ``(path, objectId)``."""
        resources = self.root.find("m:resources", NS)
        if resources is None:
            resources = etree.SubElement(self.root, qn("resources"))
        new_id = str(self.next_object_id())
        element = etree.SubElement(resources, qn("object"))
        element.set("id", new_id)
        element.set(pqn("UUID"), uuid_value or new_uuid())
        element.set("type", "model")
        components_el = etree.SubElement(element, qn("components"))
        refs: list[ComponentRef] = []
        for path, oid in mesh_entries:
            comp = etree.SubElement(components_el, qn("component"))
            comp.set(pqn("path"), path)
            comp.set("objectid", str(oid))
            comp.set(pqn("UUID"), new_uuid())
            comp.set("transform", format_transform(IDENTITY))
            refs.append(ComponentRef(comp, str(oid), path, IDENTITY.copy()))
        obj = RootObject(new_id, element, refs, None)
        self.root_objects[new_id] = obj
        self._root_dirty = True
        return obj

    def next_object_id(self) -> int:
        ids = [int(k) for k in self.root_objects if str(k).lstrip("-").isdigit()]
        return (max(ids) + 1) if ids else 1

    # ------------------------------------------------------------------- files
    def mark_file_dirty(self, path: str) -> None:
        self._file_dirty.add(path.lstrip("/"))

    def serialize_changes(self) -> dict[str, bytes]:
        changes: dict[str, bytes] = dict(self.extra_changes)
        if self._root_dirty:
            changes[ROOT_MODEL] = xml_bytes(self.root)
        for path in sorted(self._file_dirty):
            object_file = self._files.get(path)
            if object_file is not None:
                changes[path] = object_file.serialize()
        return changes

    def ensure_rels(self, paths: list[str]) -> None:
        """Add a relationship for every object file that lacks one."""
        if not self.pkg.has(ROOT_RELS):
            return
        rel_root = parse_xml(self.pkg.read(ROOT_RELS))
        existing = {
            (rel.get("Target") or "").lstrip("/")
            for rel in rel_root.iterfind("{%s}Relationship" % RELS_NS)
        }
        next_index = len(existing) + 1
        added = False
        for path in paths:
            clean = path.lstrip("/")
            if clean in existing:
                continue
            rel = etree.SubElement(rel_root, "{%s}Relationship" % RELS_NS)
            rel.set("Target", "/" + clean)
            rel.set("Id", f"rel-{next_index}")
            rel.set(
                "Type",
                "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
            )
            next_index += 1
            added = True
        if added:
            self.extra_changes[ROOT_RELS] = xml_bytes(rel_root)

    def prune_rels(self, paths: list[str]) -> list[str]:
        """Drop relationships pointing at entries that no longer exist.

        A dangling ``<Relationship>`` makes Bambu Studio reject the whole package
        ("The input model file to the slicer can not be parsed"), so every place
        that removes an object file has to prune its relationship too.
        """
        targets = {path.lstrip("/") for path in paths if path}
        if not targets or not self.pkg.has(ROOT_RELS):
            return []
        rel_root = parse_xml(self.extra_changes.get(ROOT_RELS) or self.pkg.read(ROOT_RELS))
        removed: list[str] = []
        for rel in list(rel_root.iterfind("{%s}Relationship" % RELS_NS)):
            target = (rel.get("Target") or "").lstrip("/")
            if target in targets:
                rel_root.remove(rel)
                removed.append(target)
        if removed:
            self.extra_changes[ROOT_RELS] = xml_bytes(rel_root)
        return removed

    def drop_object_file(self, path: str) -> str | None:
        """Remove one ``3D/Objects/*.model`` entry and its relationship."""
        clean = path.lstrip("/")
        if clean in self.added_entries:
            self.added_entries.pop(clean, None)
        elif self.pkg.has(clean):
            self.deleted_entries.add(clean)
        else:
            return None
        self._files.pop(clean, None)
        self.prune_rels([clean])
        return clean

    def rel_targets(self) -> set[str]:
        if not self.pkg.has(ROOT_RELS):
            return set()
        data = self.extra_changes.get(ROOT_RELS) or self.pkg.read(ROOT_RELS)
        rel_root = parse_xml(data)
        return {
            (rel.get("Target") or "").lstrip("/")
            for rel in rel_root.iterfind("{%s}Relationship" % RELS_NS)
        }

    def object_file_paths(self) -> list[str]:
        # Added files live in ``_files``/``added_entries`` until save; a second
        # object created in the same call must see the first one, otherwise both
        # roots point at the same path/id and end up sharing one mesh.
        names = (
            set(self.pkg.names())
            | set(self._files)
            | set(self.added_entries)
        ) - set(self.deleted_entries)
        return sorted(
            name
            for name in names
            if name.startswith("3D/Objects/") and name.endswith(".model")
        )

    # ---------------------------------------------------------------- analysis
    def component_references(self) -> dict[tuple[str, str], list[str]]:
        """Map ``(object file, objectId)`` to the root object ids using it."""
        refs: dict[tuple[str, str], list[str]] = {}
        for object_id, root_object in self.root_objects.items():
            for component in root_object.components:
                key = (component.path.lstrip("/"), component.object_id)
                refs.setdefault(key, []).append(object_id)
        return refs
