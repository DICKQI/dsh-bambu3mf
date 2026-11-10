"""Bambu Studio's own slice report (Metadata/slice_info.config).

An exported .gcode.3mf carries the slicer's verdict next to the G-code: the
predicted print time, the filament weight and one row per plate. Reading it
turns "inspect a sliced package" from a geometry query into an actual answer,
which is what a print-time question needs.

The file is written by several Bambu Studio versions and is not schema-stable:
newer builds add metadata keys, older ones omit first_layer_time, and plate
entries may be absent entirely when only some plates were sliced. Everything
here is therefore optional and tolerant: a missing value becomes None rather
than an exception, and a broken file is reported by the caller.
"""

from __future__ import annotations

import math

from lxml import etree

from .constants import ThreeMFError

#: A sliced plate listing thousands of objects carries no extra meaning; the
#: report is about totals, so keep the payload small and predictable.
MAX_OBJECTS = 200
MAX_WARNINGS = 20


def _number(raw) -> float | None:
    """Parse a Bambu numeric attribute; None when absent or not a number."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _integer(raw) -> int | None:
    value = _number(raw)
    return None if value is None else int(round(value))


def _flag(raw) -> bool | None:
    """Bambu writes booleans as the strings true/false."""
    if raw is None:
        return None
    return str(raw).strip().lower() in ("1", "true", "yes")


def _metadata(element: etree._Element) -> dict:
    result = {}
    for meta in element.iterfind("metadata"):
        key = meta.get("key")
        if key:
            result[key] = meta.get("value", "")
    return result


def parse_slice_info(data: bytes) -> dict:
    """Parse slice_info.config into a plain dict.

    Raises ThreeMFError only when the entry is not XML at all; every field
    inside is optional, because a partially written report is still useful.
    """
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise ThreeMFError(
            "INVALID_3MF", f"slice_info.config 不是合法 XML：{exc}"
        ) from exc

    header = {}
    for item in root.iterfind("header/header_item"):
        key = item.get("key")
        if key:
            header[key] = item.get("value", "")

    plates = []
    for plate in root.iterfind("plate"):
        meta = _metadata(plate)
        filaments = []
        for entry in plate.iterfind("filament"):
            filaments.append(
                {
                    "id": _integer(entry.get("id")),
                    "type": entry.get("type") or "",
                    "color": entry.get("color") or "",
                    "trayInfoIdx": entry.get("tray_info_idx") or "",
                    "usedMeters": _number(entry.get("used_m")),
                    "usedGrams": _number(entry.get("used_g")),
                }
            )
        objects = [
            {
                "identifyId": _integer(entry.get("identify_id")),
                "name": entry.get("name") or "",
                "skipped": _flag(entry.get("skipped")),
            }
            for entry in plate.iterfind("object")
        ]
        warnings = [
            {
                "msg": item.get("msg") or "",
                "level": _integer(item.get("level")),
                "errorCode": (item.get("error_code") or "").strip(),
            }
            for item in plate.iterfind("warning")
        ]
        plates.append(
            {
                "index": _integer(meta.get("index")),
                "predictionSeconds": _integer(meta.get("prediction")),
                "weightGrams": _number(meta.get("weight")),
                "firstLayerSeconds": _number(meta.get("first_layer_time")),
                "supportUsed": _flag(meta.get("support_used")),
                "pauseCount": _integer(meta.get("pause_count")),
                "objects": objects[:MAX_OBJECTS],
                "filaments": filaments,
                "warnings": warnings[:MAX_WARNINGS],
            }
        )

    return {
        "clientVersion": header.get("X-BBL-Client-Version", ""),
        "plates": plates,
    }


def _duration(seconds: float | None) -> str:
    """Human duration: minutes below an hour, X 小时 Y 分 above."""
    if seconds is None or seconds <= 0:
        return ""
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f} 分钟"
    hours = int(minutes // 60)
    return f"{hours} 小时 {minutes - hours * 60:.0f} 分"


def format_slice_info(info: dict) -> list[str]:
    """One readable line per sliced plate, for the inspect summary."""
    if not info:
        return []
    lines: list[str] = []
    version = str(info.get("clientVersion") or "").strip()
    suffix = f"（Bambu {version}）" if version else ""
    for plate in info.get("plates") or []:
        fields: list[str] = []
        duration = _duration(plate.get("predictionSeconds"))
        if duration:
            fields.append(f"预估 {duration}")
        grams = plate.get("weightGrams")
        if grams is not None:
            fields.append(f"{grams:.2f} g")
        used = []
        for filament in plate.get("filaments") or []:
            label = f"#{filament.get('id')}" if filament.get("id") is not None else ""
            kind = str(filament.get("type") or "").strip()
            color = str(filament.get("color") or "").strip()
            head = " ".join(part for part in (label, kind, color) if part).strip()
            detail = []
            if filament.get("usedMeters") is not None:
                detail.append(f"{filament['usedMeters']:.2f} m")
            if filament.get("usedGrams") is not None:
                detail.append(f"{filament['usedGrams']:.2f} g")
            used.append(" ".join(part for part in [head, "/".join(detail)] if part))
        if used:
            fields.append("耗材 " + "、".join(used))
        if plate.get("supportUsed") is True:
            fields.append("使用支撑")
        if plate.get("pauseCount"):
            fields.append(f"暂停 {plate['pauseCount']} 次")
        index = plate.get("index")
        head = f"盘 {index}" if index is not None else "已切片"
        lines.append(f"已切片{suffix}：{head}" + (" · " + " · ".join(fields) if fields else ""))
        warnings = plate.get("warnings") or []
        if warnings:
            messages = []
            for item in warnings[:3]:
                message = str(item.get("msg") or "").strip()
                if message and message not in messages:
                    messages.append(message)
            if messages:
                more = "" if len(warnings) <= 3 else f" 等 {len(warnings)} 条"
                lines.append(f"  切片警告：{'、'.join(messages)}{more}")
    return lines
