"""ZIP/OPC package access that preserves every untouched entry.

A 3MF is an OPC (ZIP) container. Rebuilding the whole archive from parsed
models silently drops settings, thumbnails and the production-extension
layout, so this module only replaces the entries a caller actually changed and
keeps every other entry's bytes, order, compression method and timestamp.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import zipfile
from dataclasses import dataclass, field

from .constants import CONTENT_TYPES, ThreeMFError


@dataclass
class Entry:
    info: zipfile.ZipInfo
    data: bytes

    @property
    def name(self) -> str:
        return self.info.filename

    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Package:
    path: str
    order: list[str] = field(default_factory=list)
    entries: dict[str, Entry] = field(default_factory=dict)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: str) -> "Package":
        if not os.path.isfile(path):
            raise ThreeMFError("NOT_FOUND", f"file not found: {path}")
        try:
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise ThreeMFError(
                        "INVALID_3MF",
                        f"corrupt zip entry: {bad}",
                        {"entry": bad},
                    )
                pkg = cls(path=path)
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    pkg.order.append(info.filename)
                    pkg.entries[info.filename] = Entry(info, zf.read(info.filename))
                if CONTENT_TYPES not in pkg.entries:
                    raise ThreeMFError(
                        "INVALID_3MF",
                        "not a 3MF package: [Content_Types].xml is missing",
                    )
                if not pkg.order or pkg.order[0] != CONTENT_TYPES:
                    # Keep the original order regardless; OPC only requires the
                    # part to exist, but readers are happier when it is first.
                    pass
                return pkg
        except zipfile.BadZipFile as exc:
            raise ThreeMFError("INVALID_3MF", f"not a zip archive: {exc}") from exc

    # ----------------------------------------------------------------- access
    def has(self, name: str) -> bool:
        return name in self.entries

    def read(self, name: str) -> bytes:
        try:
            return self.entries[name].data
        except KeyError as exc:
            raise ThreeMFError("MISSING_ENTRY", f"package entry not found: {name}") from exc

    def read_text(self, name: str) -> str:
        return decode_entry(name, self.read(name))

    def names(self) -> list[str]:
        return list(self.order)

    def digests(self) -> dict[str, str]:
        return {name: entry.digest() for name, entry in self.entries.items()}

    # ------------------------------------------------------------------ write
    def write(
        self,
        output_path: str,
        changes: dict[str, bytes] | None = None,
        additions: dict[str, bytes] | None = None,
        deletions: set[str] | None = None,
    ) -> dict:
        """Write a new package; returns per-entry preservation facts."""
        changes = changes or {}
        additions = additions or {}
        deletions = deletions or set()
        unknown = set(changes) - set(self.entries)
        if unknown:
            raise ThreeMFError(
                "INTERNAL",
                "attempted to replace entries that do not exist",
                {"entries": sorted(unknown)},
            )

        out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
        os.makedirs(out_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".3mf-edit-", suffix=".tmp", dir=out_dir)
        os.close(fd)
        preserved = 0
        changed: list[str] = []
        try:
            with zipfile.ZipFile(tmp_path, "w", allowZip64=True) as zf:
                for name in self.order:
                    if name in deletions:
                        continue
                    info = self.entries[name].info
                    if name in changes:
                        zf.writestr(_clone_info(info), changes[name])
                        changed.append(name)
                    else:
                        zf.writestr(info, self.entries[name].data)
                        preserved += 1
                for name in sorted(additions):
                    if name in self.entries and name not in deletions:
                        raise ThreeMFError(
                            "INTERNAL", f"entry already exists: {name}"
                        )
                    zf.writestr(_new_info(name), additions[name])
                    changed.append(name)
            os.replace(tmp_path, output_path)
        except BaseException:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

        return {
            "entries": len(self.order) - len(deletions) + len(additions),
            "changed": sorted(changed),
            "preserved": preserved,
        }

    def assert_preserved(
        self, output_path: str, changed: set[str], deletions: set[str] | None = None
    ) -> list[str]:
        """Verify every entry outside ``changed`` is content-identical."""
        deletions = deletions or set()
        problems: list[str] = []
        with zipfile.ZipFile(output_path) as zf:
            out_names = set(zf.namelist())
            for name, entry in self.entries.items():
                if name in changed or name in deletions:
                    continue
                if name not in out_names:
                    problems.append(f"entry disappeared: {name}")
                    continue
                if hashlib.sha256(zf.read(name)).hexdigest() != entry.digest():
                    problems.append(f"entry content changed unexpectedly: {name}")
        return problems


def _clone_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    copy.compress_type = info.compress_type
    copy.external_attr = info.external_attr
    copy.create_system = info.create_system
    copy.comment = info.comment
    return copy


def _new_info(name: str) -> zipfile.ZipInfo:
    import time

    info = zipfile.ZipInfo(name, date_time=time.localtime(time.time())[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def decode_entry(name: str, data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ThreeMFError("INVALID_3MF", f"entry is not UTF-8 text: {name}")


def encode_text(text: str) -> bytes:
    return text.encode("utf-8")


def read_zip_member(path: str, name: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name)


def zip_names(path: str) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return [n for n in zf.namelist() if not n.endswith("/")]


def bytes_io(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)
