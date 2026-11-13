#!/usr/bin/env python3
"""Export CRC requirements from retained .ko files using pyelftools."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

from elftools.elf.elffile import ELFFile


def read_module(path):
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        versions = elf.get_section_by_name("__versions")
        symbols = elf.get_section_by_name(".symtab")
        modinfo = elf.get_section_by_name(".modinfo")
        if elf.elfclass != 64 or versions is None or symbols is None or modinfo is None:
            raise ValueError(f"Expected a 64-bit versioned kernel module: {path.name}")
        data = versions.data()
        if len(data) % 64:
            raise ValueError(f"Unexpected modversion_info layout: {path.name}")
        imports = {}
        endian = "<" if elf.little_endian else ">"
        for offset in range(0, len(data), 64):
            crc = struct.unpack_from(endian + "Q", data, offset)[0]
            name = data[offset + 8:offset + 64].split(b"\0", 1)[0].decode()
            if crc > 0xffffffff or not name:
                raise ValueError(f"Invalid module CRC record: {path.name}")
            imports[name] = f"0x{crc:08x}"
        exports = {
            symbol.name[len("__ksymtab_"):]
            for symbol in symbols.iter_symbols()
            if symbol.name.startswith("__ksymtab_") and symbol.name != "__ksymtab_strings"
        }
        info = modinfo.data().split(b"\0")
        vermagic = next(
            (entry[len(b"vermagic="):].decode() for entry in info if entry.startswith(b"vermagic=")),
            None,
        )
    return imports, exports, vermagic


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "inspect":
        imports, exports, vermagic = read_module(Path(sys.argv[2]))
        print(json.dumps({"imports": imports, "exports": sorted(exports), "vermagic": vermagic}))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", type=Path, action="append", required=True)
    parser.add_argument("--kernel-ref", required=True)
    parser.add_argument("--kernel-release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    modules = {}
    for root in args.modules:
        for path in sorted(root.rglob("*.ko")):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest not in modules:
                imports, exports, vermagic = read_module(path)
                modules[digest] = {
                    "name": path.name, "sha256": digest, "vermagic": vermagic,
                    "imports": imports, "exports": sorted(exports),
                }
    if not modules:
        parser.error("No .ko files found.")
    retained_exports = {symbol for module in modules.values() for symbol in module["exports"]}
    required = {}
    for module in modules.values():
        for symbol, crc in module["imports"].items():
            if symbol in retained_exports:
                continue
            if symbol in required and required[symbol] != crc:
                parser.error(f"Retained modules require conflicting kernel CRCs for {symbol}.")
            required[symbol] = crc
    result = {
        "schema": 1, "kernel_ref": args.kernel_ref, "kernel_release": args.kernel_release,
        "unique_modules": len(modules),
        "versioned_modules": sum(bool(module["imports"]) for module in modules.values()),
        "modules_without_version_records": sorted(
            module["name"] for module in modules.values() if not module["imports"]
        ),
        "kernel_required_crcs": dict(sorted(required.items())),
        "retained_module_exports": sorted(retained_exports),
        "modules": [
            {"name": module["name"], "sha256": module["sha256"], "vermagic": module["vermagic"],
             "imports": len(module["imports"]), "exports": len(module["exports"])}
            for module in sorted(modules.values(), key=lambda module: (module["name"], module["sha256"]))
        ],
        "scope": "vendor_dlkm, vendor_boot DLKM ramdisk and current boot kernelsu.ko",
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Exported {len(modules)} unique modules and {len(required)} kernel CRC requirements.")


if __name__ == "__main__":
    main()
