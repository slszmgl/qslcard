"""Streaming ADIF 3.x reader/writer (SRS section 4.2).

Performance notes:

* Records are produced by a generator that consumes the file in 1 MiB chunks,
  so memory stays flat regardless of log size (SRS SR-ADIF-004).
* The hot loop uses str.find plus slicing, never regular expressions.
* Writing batches 512 records per stream write to minimise syscalls.

Note on field length: ADIF 3 specifies the length as a character count, so
multi-byte UTF-8 values are counted in code points here.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterable, Iterator, Mapping
from typing import IO, Any, TextIO

from .model import COLUMNS, QSO, normalize_qso

__all__ = [
    "ADIF_ORDER",
    "AdifError",
    "dumps",
    "iter_qsos",
    "iter_records",
    "load_adif",
    "qso_from_fields",
    "qso_to_fields",
    "record_to_adif",
    "write_adif",
]

_CHUNK = 1 << 20  # 1 MiB
_WHITESPACE = " \t\r\n"
_CANONICAL = frozenset(COLUMNS)


class AdifError(ValueError):
    """Raised when a stream cannot be parsed as ADIF."""


#: Canonical tag order used when writing, mirroring the ADIF specification.
ADIF_ORDER: tuple[str, ...] = tuple(name.upper() for name in COLUMNS)


def _parse_tag(tag: str) -> tuple[str, int | None]:
    """Split NAME:len[:type] into an upper-cased name and a length."""
    name, _, rest = tag.partition(":")
    name = name.strip().upper()
    if not rest:
        return name, None
    length_text, _, _type = rest.partition(":")
    length_text = length_text.strip()
    if not length_text.isdigit():
        return name, None
    return name, int(length_text)


def iter_records(
    stream: TextIO,
    chunk_size: int = _CHUNK,
    errors: list[dict[str, Any]] | None = None,
) -> Iterator[dict[str, str]]:
    """Yield raw tag dictionaries from an ADIF text stream.

    Handles a BOM, EOH, whitespace between records, case-insensitive tags, the
    NAME:len:type form and values containing angle brackets or spaces.

    ADIF length prefixes are wrong surprisingly often in real logs.  When a
    value is not followed by the next tag, the delimiters are trusted instead
    and the repair is appended to errors (SR-ADIF-005).
    """
    buf = stream.read(chunk_size)
    eof = not buf
    pos = 0
    fields: dict[str, str] = {}

    while True:
        lt = buf.find("<", pos)
        if lt < 0:
            if eof:
                break
            buf = buf[pos:]
            pos = 0
            chunk = stream.read(chunk_size)
            if chunk:
                buf += chunk
            else:
                eof = True
            continue

        gt = buf.find(">", lt)
        if gt < 0:
            if eof:
                break
            chunk = stream.read(chunk_size)
            if chunk:
                buf += chunk
            else:
                eof = True
            continue

        name, length = _parse_tag(buf[lt + 1 : gt])

        if length is None:
            if name == "EOR":
                if fields:
                    yield fields
                    fields = {}
            elif name == "EOH":
                fields = {}
            pos = gt + 1
        else:
            start = gt + 1
            end = start + length
            if end > len(buf) or (end == len(buf) and not eof):
                if not eof:
                    chunk = stream.read(chunk_size)
                    if chunk:
                        buf += chunk
                        continue
                    eof = True
                if end > len(buf):
                    break
            scan = end
            while scan < len(buf) and buf[scan] in _WHITESPACE:
                scan += 1
            if scan == len(buf) and not eof:
                chunk = stream.read(chunk_size)
                if chunk:
                    buf += chunk
                else:
                    eof = True
                continue
            value = buf[start:end]
            pos = end
            if scan < len(buf) and buf[scan] != "<":
                next_tag = buf.find("<", start + 1)
                if next_tag > start:
                    value = buf[start:next_tag].rstrip()
                    pos = next_tag
                    if errors is not None:
                        errors.append({"tag": name, "declared": length, "value": value[:60]})
            fields[name] = value

        if pos > _CHUNK:
            buf = buf[pos:]
            pos = 0


def open_text(path: str | os.PathLike[str]) -> IO[str]:
    """Open an ADIF file as UTF-8 text, tolerating a byte order mark."""
    return open(path, encoding="utf-8-sig", newline="")


def _iter_adx(stream: TextIO) -> Iterator[dict[str, str]]:
    """Parse the XML form (.adx) using the standard library only."""
    import xml.etree.ElementTree as ET

    for _event, element in ET.iterparse(stream, events=("end",)):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag.lower() == "record":
            fields: dict[str, str] = {}
            for child in element:
                name = child.tag.rsplit("}", 1)[-1].upper()
                fields[name] = (child.text or "").strip()
            if fields:
                yield fields
            element.clear()


def _read_path(path: str, errors: list[dict[str, Any]] | None = None) -> Iterator[dict[str, str]]:
    def run(encoding: str) -> Iterator[dict[str, str]]:
        with open(path, encoding=encoding, newline="") as handle:
            if path.lower().endswith(".adx"):
                yield from _iter_adx(handle)
            else:
                yield from iter_records(handle, errors=errors)

    try:
        yield from run("utf-8-sig")
    except UnicodeDecodeError:
        yield from run("latin-1")


_ADX_MARKERS = ("<?xml", "<adx", "<!doctype")


def _looks_like_adx(text: str) -> bool:
    return text.lstrip()[:64].lower().startswith(_ADX_MARKERS)


class _PrefixStream:
    """Re-attach already-read text in front of a stream, keeping it streaming."""

    __slots__ = ("_prefix", "_stream")

    def __init__(self, prefix: str, stream: TextIO) -> None:
        self._prefix = prefix
        self._stream = stream

    def read(self, size: int = -1) -> str:
        if not self._prefix:
            return self._stream.read(size)
        if size is None or size < 0:
            data = self._prefix + self._stream.read()
            self._prefix = ""
            return data
        if len(self._prefix) >= size:
            head = self._prefix[:size]
            self._prefix = self._prefix[size:]
            return head
        head, self._prefix = self._prefix, ""
        return head + self._stream.read(size - len(head))


def _iter_source(
    source: str | os.PathLike[str] | TextIO,
    errors: list[dict[str, Any]] | None = None,
) -> Iterator[dict[str, str]]:
    if hasattr(source, "read"):
        stream = source  # type: ignore[assignment]
        if str(getattr(stream, "name", "")).lower().endswith(".adx"):
            yield from _iter_adx(stream)
            return
        prefix = stream.read(4096)
        wrapped = _PrefixStream(prefix, stream)
        if _looks_like_adx(prefix):
            yield from _iter_adx(wrapped)
        else:
            yield from iter_records(wrapped, errors=errors)
        return
    path = os.fspath(source)
    if path.lower().endswith(".adx"):
        with open(path, encoding="utf-8-sig", newline="") as handle:
            yield from _iter_adx(handle)
        return
    yield from _read_path(path, errors)


def qso_from_fields(fields: Mapping[str, str]) -> QSO:
    """Map a raw ADIF record onto the canonical model."""
    qso = QSO()
    extra: dict[str, str] = {}
    for tag, value in fields.items():
        attr = tag.lower()
        if attr in _CANONICAL:
            setattr(qso, attr, value)
        else:
            extra[tag.upper()] = value
    if extra:
        qso.extra = extra
    return normalize_qso(qso)


def iter_qsos(
    source: str | os.PathLike[str] | TextIO,
    errors: list[dict[str, Any]] | None = None,
) -> Iterator[QSO]:
    """Yield QSO objects from an ADIF or ADX source.

    Pass a list as errors to collect repaired length prefixes.
    """
    for fields in _iter_source(source, errors):
        yield qso_from_fields(fields)


def load_adif(source: str | os.PathLike[str] | TextIO) -> list[QSO]:
    """Convenience wrapper for small inputs; prefer iter_qsos for large logs."""
    return list(iter_qsos(source))


def qso_to_fields(qso: QSO) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name in COLUMNS:
        value = getattr(qso, name)
        if value:
            fields[name.upper()] = value
    for tag, value in qso.extra.items():
        fields.setdefault(tag.upper(), value)
    return fields


def record_to_adif(fields: Mapping[str, str]) -> str:
    """Serialize one record with length-prefixed values."""
    parts: list[str] = []
    seen: set[str] = set()
    for tag in ADIF_ORDER:
        value = fields.get(tag)
        if value:
            parts.append(f"<{tag}:{len(value)}>{value}")
            seen.add(tag)
    for tag in sorted(fields):
        if tag in seen:
            continue
        value = fields[tag]
        if value:
            parts.append(f"<{tag}:{len(value)}>{value}")
    parts.append("<EOR>")
    return "".join(parts)


def _write_stream(
    qsos: Iterable[QSO],
    stream: TextIO,
    program: str,
    version: str,
) -> int:
    stream.write(f"<ADIF_VER:{len(version)}>{version}")
    stream.write(f"<PROGRAMID:{len(program)}>{program}")
    stream.write("<EOH>\n")
    count = 0
    buf: list[str] = []
    for qso in qsos:
        buf.append(record_to_adif(qso_to_fields(qso)))
        buf.append("\n")
        count += 1
        if len(buf) >= 512:
            stream.write("".join(buf))
            buf.clear()
    if buf:
        stream.write("".join(buf))
    return count


def write_adif(
    qsos: Iterable[QSO],
    out: TextIO | str | os.PathLike[str],
    *,
    program: str = "qslcard",
    version: str = "3.1.7",
    buffer_size: int = 1 << 20,
) -> int:
    """Write QSOs as ADIF; returns the number of records written."""
    if hasattr(out, "write"):
        return _write_stream(qsos, out, program, version)  # type: ignore[arg-type]
    with open(out, "w", encoding="utf-8", newline="", buffering=buffer_size) as stream:
        return _write_stream(qsos, stream, program, version)


def dumps(qsos: Iterable[QSO]) -> str:
    buffer = io.StringIO()
    write_adif(qsos, buffer)
    return buffer.getvalue()
