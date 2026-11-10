"""Namespaces, entry names and small shared constants."""

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
BAMBU_NS = "http://schemas.bambulab.com/package/2021"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

NS = {"m": CORE_NS, "p": PROD_NS, "b": BAMBU_NS}

ROOT_MODEL = "3D/3dmodel.model"
ROOT_RELS = "3D/_rels/3dmodel.model.rels"
CONTENT_TYPES = "[Content_Types].xml"
RELS = "_rels/.rels"
MODEL_SETTINGS = "Metadata/model_settings.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
LAYER_RANGES = "Metadata/layer_config_ranges.xml"
CUT_INFO = "Metadata/cut_information.xml"
SLICE_INFO = "Metadata/slice_info.config"
FILAMENT_SEQ = "Metadata/filament_sequence.json"

#: Plate grid used by Bambu Studio's scene: bed pitch multiplier and columns.
PLATE_PITCH = 1.2
PLATE_COLUMNS = 3

#: Objects smaller than this (triangles) are still valid; empty meshes are not.
MAX_SAFE_INT = 2**31 - 1


class ThreeMFError(Exception):
    """A handled failure carrying a stable machine code."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
