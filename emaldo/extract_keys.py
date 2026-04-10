#!/usr/bin/env python3
"""Extract APP_ID, APP_SECRET, and app version from an Emaldo APK.

Parses the DEX bytecode and binary AndroidManifest.xml directly.

Usage::

    python -m emaldo.extract_keys path/to/base.apk
    python -m emaldo.extract_keys base.apk --update
    python -m emaldo.extract_keys base.apk --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


# ── DEX helpers ───────────────────────────────────────────────────────────

@dataclass
class DexFile:
    """Lightweight read-only view into a DEX file's key tables."""

    raw: bytes

    # Populated by _parse_header
    string_ids_size: int = 0
    string_ids_off: int = 0
    type_ids_size: int = 0
    type_ids_off: int = 0
    proto_ids_off: int = 0
    field_ids_size: int = 0
    field_ids_off: int = 0
    method_ids_size: int = 0
    method_ids_off: int = 0
    class_defs_size: int = 0
    class_defs_off: int = 0

    # Caches
    _string_cache: dict[int, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.raw[:4] != b"dex\n":
            raise ValueError("Not a valid DEX file (bad magic)")
        self._parse_header()

    def _parse_header(self) -> None:
        u = self._u32
        self.string_ids_size = u(56)
        self.string_ids_off = u(60)
        self.type_ids_size = u(64)
        self.type_ids_off = u(68)
        self.proto_ids_off = u(76)
        self.field_ids_size = u(80)
        self.field_ids_off = u(84)
        self.method_ids_size = u(88)
        self.method_ids_off = u(92)
        self.class_defs_size = u(96)
        self.class_defs_off = u(100)

    # ── Primitive readers ──

    def _u32(self, off: int) -> int:
        return struct.unpack_from("<I", self.raw, off)[0]

    def _u16(self, off: int) -> int:
        return struct.unpack_from("<H", self.raw, off)[0]

    @staticmethod
    def _read_uleb128(data: bytes, pos: int) -> tuple[int, int]:
        result = shift = 0
        while True:
            b = data[pos]; pos += 1
            result |= (b & 0x7F) << shift
            if b & 0x80 == 0:
                break
            shift += 7
        return result, pos

    # ── String / type / method lookups ──

    def string(self, idx: int) -> str:
        if idx in self._string_cache:
            return self._string_cache[idx]
        off = self._u32(self.string_ids_off + idx * 4)
        _, data_start = self._read_uleb128(self.raw, off)
        try:
            end = self.raw.index(0, data_start)
            s = self.raw[data_start:end].decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            s = ""
        self._string_cache[idx] = s
        return s

    def type_name(self, idx: int) -> str:
        desc_idx = self._u32(self.type_ids_off + idx * 4)
        return self.string(desc_idx)

    def method_class_idx(self, method_idx: int) -> int:
        return self._u16(self.method_ids_off + method_idx * 8)

    def method_name(self, method_idx: int) -> str:
        name_idx = self._u32(self.method_ids_off + method_idx * 8 + 4)
        return self.string(name_idx)

    # ── Class / method iteration ──

    def iter_classes(self):
        """Yield ``(class_def_index, type_index, class_data_offset)``."""
        for i in range(self.class_defs_size):
            off = self.class_defs_off + i * 32
            yield i, self._u32(off), self._u32(off + 24)

    def iter_methods_in_class(self, class_data_off: int):
        """Yield ``(method_idx, code_offset)`` for every method in a class."""
        if class_data_off == 0:
            return
        pos = class_data_off
        sf, pos = self._read_uleb128(self.raw, pos)
        ifs, pos = self._read_uleb128(self.raw, pos)
        dm, pos = self._read_uleb128(self.raw, pos)
        vm, pos = self._read_uleb128(self.raw, pos)
        # skip fields
        fidx = 0
        for _ in range(sf + ifs):
            d, pos = self._read_uleb128(self.raw, pos)
            fidx += d
            _, pos = self._read_uleb128(self.raw, pos)
        # methods
        midx = 0
        for _ in range(dm + vm):
            d, pos = self._read_uleb128(self.raw, pos)
            midx += d
            _, pos = self._read_uleb128(self.raw, pos)  # access_flags
            code_off, pos = self._read_uleb128(self.raw, pos)
            yield midx, code_off

    def extract_const_strings(self, code_off: int) -> list[tuple[int, int]]:
        """Return ``[(register, string_index), ...]`` from const-string ops."""
        if code_off == 0:
            return []
        insns_size = self._u32(code_off + 12)
        insns_off = code_off + 16
        results = []
        ip = 0
        while ip < insns_size:
            word = self._u16(insns_off + ip * 2)
            opcode = word & 0xFF
            if opcode == 0x1A:  # const-string vAA, string@BBBB
                reg = (word >> 8) & 0xFF
                str_idx = self._u16(insns_off + ip * 2 + 2)
                results.append((reg, str_idx))
                ip += 2
            elif opcode == 0x1B:  # const-string/jumbo vAA, string@BBBBBBBB
                reg = (word >> 8) & 0xFF
                str_idx = self._u32(insns_off + ip * 2 + 2)
                results.append((reg, str_idx))
                ip += 3
            else:
                ip += 1
        return results


# ── Binary XML (AXML) version extraction ─────────────────────────────────

def _parse_axml_strings(data: bytes) -> list[str] | None:
    """Parse the string pool from a binary AndroidManifest.xml."""
    if len(data) < 16 or struct.unpack_from("<I", data, 0)[0] != 0x00080003:
        return None

    pos = 8
    sp_type = struct.unpack_from("<H", data, pos)[0]
    if sp_type != 0x0001:  # RES_STRING_POOL_TYPE
        return None

    sp_header_size = struct.unpack_from("<H", data, pos + 2)[0]
    sp_string_count = struct.unpack_from("<I", data, pos + 8)[0]
    sp_flags = struct.unpack_from("<I", data, pos + 16)[0]
    sp_strings_start = struct.unpack_from("<I", data, pos + 20)[0]
    is_utf8 = bool(sp_flags & 0x100)

    # Read string offsets
    offsets_pos = pos + sp_header_size
    string_offsets = [
        struct.unpack_from("<I", data, offsets_pos + i * 4)[0]
        for i in range(sp_string_count)
    ]

    strings_abs = pos + sp_strings_start
    strings: list[str] = []

    for i in range(sp_string_count):
        off = strings_abs + string_offsets[i]
        try:
            if is_utf8:
                # char_len (1-2 bytes), byte_len (1-2 bytes), data, null
                b = data[off]; off += 1
                if b & 0x80:
                    off += 1
                byte_len = data[off]; off += 1
                if byte_len & 0x80:
                    byte_len = ((byte_len & 0x7F) << 8) | data[off]
                    off += 1
                strings.append(data[off:off + byte_len].decode("utf-8", errors="replace"))
            else:
                # UTF-16LE: char_len (2 bytes), data, null
                char_len = struct.unpack_from("<H", data, off)[0]; off += 2
                if char_len & 0x8000:
                    char_len = ((char_len & 0x7FFF) << 16) | struct.unpack_from("<H", data, off)[0]
                    off += 2
                strings.append(data[off:off + char_len * 2].decode("utf-16-le", errors="replace"))
        except (IndexError, struct.error):
            strings.append("")

    return strings


def extract_version_from_manifest(data: bytes) -> str | None:
    """Extract ``versionName`` from a binary AndroidManifest.xml.

    Parses the AXML format directly — finds the ``android:versionName``
    attribute (resource ID ``0x0101021c``) in the ``<manifest>`` element.
    """
    strings = _parse_axml_strings(data)
    if strings is None:
        return None

    # Skip past string pool to the resource ID chunk
    pos = 8
    sp_chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
    pos += sp_chunk_size

    # --- Resource ID map (optional) ---
    VERNAME_RES = 0x0101021C  # android:versionName
    vername_str_idx: int | None = None

    if pos < len(data) - 8:
        chunk_type = struct.unpack_from("<H", data, pos)[0]
        if chunk_type == 0x0180:  # RES_XML_RESOURCE_MAP_TYPE
            res_header_size = struct.unpack_from("<H", data, pos + 2)[0]
            res_chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
            res_count = (res_chunk_size - res_header_size) // 4
            for i in range(res_count):
                rid = struct.unpack_from("<I", data, pos + res_header_size + i * 4)[0]
                if rid == VERNAME_RES:
                    vername_str_idx = i
                    break
            pos += res_chunk_size

    if vername_str_idx is None:
        return None

    # --- Scan XML tree for START_ELEMENT with versionName attribute ---
    while pos < len(data) - 8:
        chunk_type = struct.unpack_from("<H", data, pos)[0]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        if chunk_size < 8:
            break

        if chunk_type == 0x0102:  # RES_XML_START_ELEMENT_TYPE
            if chunk_size >= 36:
                attr_count = struct.unpack_from("<H", data, pos + 28)[0]
                attr_size = struct.unpack_from("<H", data, pos + 30)[0]
                if attr_size == 0:
                    attr_size = 20
                attr_start = pos + 36

                for i in range(attr_count):
                    attr_off = attr_start + i * attr_size
                    if attr_off + 20 > len(data):
                        break
                    attr_name_idx = struct.unpack_from("<I", data, attr_off + 4)[0]

                    if attr_name_idx == vername_str_idx:
                        # rawValue (string index, or 0xFFFFFFFF)
                        raw_value = struct.unpack_from("<i", data, attr_off + 8)[0]
                        # typedValue: size(2), res0(1), type(1), data(4)
                        typed_type = data[attr_off + 15]
                        typed_data = struct.unpack_from("<I", data, attr_off + 16)[0]

                        if 0 <= raw_value < len(strings):
                            return strings[raw_value]
                        if typed_type == 0x03 and typed_data < len(strings):
                            return strings[typed_data]
                        return str(typed_data)

        pos += chunk_size

    return None


# ── Extraction logic ──────────────────────────────────────────────────────

_32CHAR_ALNUM = re.compile(r"^[A-Za-z0-9]{32}$")


def extract_from_dex(dex: DexFile) -> dict[str, str | None]:
    """Find APP_ID and APP_SECRET from a single DEX file.

    Locates the ``DinSaferApplication`` class and scans its methods for
    32-char alphanumeric ``const-string`` values (APP_ID and APP_SECRET).

    Returns dict with keys ``app_id``, ``app_secret``, ``class_name``,
    ``method_name``.  Values are *None* when not found.
    """
    result: dict[str, str | None] = {
        "app_id": None,
        "app_secret": None,
        "class_name": None,
        "method_name": None,
    }

    # Step 1: find DinSaferApplication class(es)
    target_classes: list[tuple[int, int, str]] = []  # (type_idx, data_off, name)
    for _, type_idx, data_off in dex.iter_classes():
        name = dex.type_name(type_idx)
        if name.endswith("/DinSaferApplication;"):
            target_classes.append((type_idx, data_off, name))

    if not target_classes:
        return result

    # Step 2: scan methods for const-string instructions
    candidates: list[str] = []
    for type_idx, data_off, cls_name in target_classes:
        for method_idx, code_off in dex.iter_methods_in_class(data_off):
            if code_off == 0:
                continue
            strings = dex.extract_const_strings(code_off)
            method_32char = []
            for _reg, str_idx in strings:
                s = dex.string(str_idx)
                if _32CHAR_ALNUM.match(s):
                    method_32char.append(s)

            if len(method_32char) >= 2:
                result["class_name"] = cls_name
                result["method_name"] = dex.method_name(method_idx)
                result["app_id"] = method_32char[0]
                result["app_secret"] = method_32char[1]
                return result
            candidates.extend(method_32char)

    # Fallback: if we found exactly 2 across all methods
    if len(candidates) >= 2:
        result["app_id"] = candidates[0]
        result["app_secret"] = candidates[1]

    return result


def extract_from_apk(apk_path: str | Path) -> dict:
    """Extract keys and version from an Emaldo APK file.

    Returns a dict with:
    - ``app_id``: The 32-char APP_ID string
    - ``app_secret``: The 32-char APP_SECRET (RC4 key)
    - ``class_name``: DEX class where found
    - ``method_name``: Method where found
    - ``apk_path``: Input file path
    - ``dex_file``: Which DEX within the APK
    - ``version``: App version from AndroidManifest.xml
    """
    apk_path = Path(apk_path)
    if not apk_path.exists():
        raise FileNotFoundError(f"APK not found: {apk_path}")

    version = None

    with zipfile.ZipFile(apk_path) as z:
        # Extract version from binary AndroidManifest.xml
        try:
            manifest = z.read("AndroidManifest.xml")
            version = extract_version_from_manifest(manifest)
        except (KeyError, Exception):
            pass

        # Extract keys from DEX files
        dex_names = sorted(n for n in z.namelist() if n.endswith(".dex"))

        for dex_name in dex_names:
            raw = z.read(dex_name)
            try:
                dex = DexFile(raw)
            except ValueError:
                continue
            result = extract_from_dex(dex)
            if result["app_id"]:
                result["apk_path"] = str(apk_path)
                result["dex_file"] = dex_name
                result["version"] = version
                return result

    return {
        "app_id": None,
        "app_secret": None,
        "class_name": None,
        "method_name": None,
        "apk_path": str(apk_path),
        "dex_file": None,
        "version": version,
    }


# ── Params file management ───────────────────────────────────────────────

PARAMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".emaldo_params.json")


def load_params() -> dict:
    """Load app parameters from ``.emaldo_params.json``.

    Raises ``RuntimeError`` if the file is missing or incomplete.
    """
    if not os.path.exists(PARAMS_FILE):
        raise RuntimeError(
            f"Missing {PARAMS_FILE}\n"
            "Run:  python -m emaldo.extract_keys <path/to/base.apk> --update"
        )
    with open(PARAMS_FILE) as f:
        params = json.load(f)
    required = ("app_id", "app_secret", "app_version")
    missing = [k for k in required if k not in params]
    if missing:
        raise RuntimeError(
            f"Incomplete .emaldo_params.json — missing: {', '.join(missing)}\n"
            "Run:  python -m emaldo.extract_keys <path/to/base.apk> --update"
        )
    return params


def write_params(result: dict, output_path: str | None = None) -> str:
    """Write ``.emaldo_params.json`` from extraction results.

    Args:
        result: Dict from :func:`extract_from_apk`.
        output_path: Override output location (defaults to package dir).

    Returns:
        Absolute path to the written file.
    """
    if not result.get("app_id") or not result.get("app_secret"):
        raise ValueError("Cannot write params — extraction found no keys")
    if not result.get("version"):
        raise ValueError("Cannot write params — no version found in APK")

    path = output_path or PARAMS_FILE
    params = {
        "app_id": result["app_id"],
        "app_secret": result["app_secret"],
        "app_version": result["version"],
    }
    with open(path, "w") as f:
        json.dump(params, f, indent=2)
        f.write("\n")
    return os.path.abspath(path)


# ── CLI ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract APP_ID, APP_SECRET, and version from an Emaldo APK",
        epilog="Parses DEX bytecode and AndroidManifest.xml directly.",
    )
    parser.add_argument("apk", help="Path to base.apk")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--update", action="store_true",
        help="Write extracted values to .emaldo_params.json (default location: package dir)",
    )
    parser.add_argument(
        "--output", metavar="PATH", default=None,
        help="Custom output path for .emaldo_params.json (used with --update)",
    )
    parser.add_argument("--compare", metavar="APK2",
                        help="Compare keys with another APK version")
    args = parser.parse_args()

    result = extract_from_apk(args.apk)

    if not result["app_id"]:
        print(f"Could not find APP_ID/APP_SECRET in {args.apk}", file=sys.stderr)
        sys.exit(1)

    if args.compare:
        result2 = extract_from_apk(args.compare)
        if args.json:
            print(json.dumps({"apk1": result, "apk2": result2}, indent=2))
        else:
            print(f"APK 1: {result['apk_path']}")
            print(f"  APP_ID:     {result['app_id'] or '(not found)'}")
            print(f"  APP_SECRET: {result['app_secret'] or '(not found)'}")
            print(f"  Version:    {result['version'] or '?'}")
            print()
            print(f"APK 2: {result2['apk_path']}")
            print(f"  APP_ID:     {result2['app_id'] or '(not found)'}")
            print(f"  APP_SECRET: {result2['app_secret'] or '(not found)'}")
            print(f"  Version:    {result2['version'] or '?'}")
            print()
            id_match = result["app_id"] == result2["app_id"]
            sec_match = result["app_secret"] == result2["app_secret"]
            if id_match and sec_match:
                print("Keys are IDENTICAL across both versions.")
            else:
                if not id_match:
                    print(f"APP_ID CHANGED: {result['app_id']} -> {result2['app_id']}")
                if not sec_match:
                    print(f"APP_SECRET CHANGED: {result['app_secret']} -> {result2['app_secret']}")
        return

    if args.update:
        path = write_params(result, output_path=args.output)
        if not args.json:
            print(f"Updated {path}")

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        ver = f" (v{result['version']})" if result["version"] else ""
        print(f"Emaldo APK{ver}: {result['apk_path']}")
        print(f"  APP_ID:     {result['app_id']}")
        print(f"  APP_SECRET: {result['app_secret']}")
        print(f"  Version:    {result['version'] or '?'}")
        print(f"  Source:     {result['dex_file']} -> {result['class_name']}.{result['method_name']}()")


if __name__ == "__main__":
    main()
