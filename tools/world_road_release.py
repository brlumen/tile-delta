#!/usr/bin/env python3
"""Build the update index and changed tile payloads for a PMTiles release."""

import argparse
import hashlib
import json
import mmap
import re
import shutil
import struct
from pathlib import Path

from pmtiles.tile import Compression, TileType, deserialize_directory, deserialize_header

INDEX_MAGIC = b"PMI1"
INDEX_HEADER = struct.Struct(">4sII")
MAX_TILES = 500_000
MAX_PREFIX = 8 * 1024 * 1024
MAX_TILE_BYTES = 1024 * 1024
HASH_BYTES = 16


def archive_index(pack):
    header = deserialize_header(pack[:127])
    start = header["tile_data_offset"]
    if (header["internal_compression"] != Compression.GZIP or header["tile_compression"] != Compression.GZIP
            or header["tile_type"] != TileType.MVT or not 127 < start <= MAX_PREFIX
            or start + header["tile_data_length"] != len(pack) or len(pack) > 2**31 - 1):
        raise ValueError("Unsupported world PMTiles archive")

    def entries(offset, length, root=False):
        for entry in deserialize_directory(pack[offset:offset + length]):
            if entry.run_length:
                yield entry
            elif root:
                yield from entries(header["leaf_directory_offset"] + entry.offset, entry.length)
            else:
                raise ValueError("Unexpected nested leaf directory")

    spans = {}
    for entry in entries(header["root_offset"], header["root_length"], root=True):
        if spans.setdefault(entry.offset, entry.length) != entry.length:
            raise ValueError("Conflicting PMTiles payload sizes")
    if len(spans) != header["tile_contents_count"] or not 1 <= len(spans) <= MAX_TILES:
        raise ValueError("Invalid PMTiles content count")

    offset = 0
    blobs = []
    with memoryview(pack) as data:
        for position, length in sorted(spans.items()):
            if position != offset or not 1 <= length <= MAX_TILE_BYTES or start + position + length > len(pack):
                raise ValueError("Non-contiguous or oversized PMTiles payload")
            blob = data[start + position:start + position + length]
            digest = hashlib.sha256(blob).digest()[:HASH_BYTES]
            blob.release()
            blobs.append((start + position, length, digest))
            offset += length
    if start + offset != len(pack):
        raise ValueError("Incomplete PMTiles payload index")
    index = INDEX_HEADER.pack(INDEX_MAGIC, start, len(blobs)) + pack[:start] + b"".join(b[2] for b in blobs)
    return index, blobs


def read_base(path):
    index = path.read_bytes()
    magic, prefix, count = INDEX_HEADER.unpack_from(index)
    start = INDEX_HEADER.size + prefix
    if (magic != INDEX_MAGIC or not 127 < prefix <= MAX_PREFIX or not 1 <= count <= MAX_TILES
            or len(index) != start + count * HASH_BYTES):
        raise ValueError(f"Invalid PMTiles index: {path}")
    hashes = {index[i:i + HASH_BYTES] for i in range(start, len(index), HASH_BYTES)}
    return hashlib.sha256(index).hexdigest(), hashes


def publish(pack, source, index, blobs, output, tag, bases):
    if not re.fullmatch(r"world-pmtiles-[0-9]{8}", tag):
        raise ValueError("Expected a world-pmtiles-YYYYMMDD tag")
    output.mkdir(parents=True)
    version = hashlib.sha256(index).hexdigest()
    (output / "world-index.pmi").write_bytes(index)
    shutil.copyfile(source, output / "world-roads.pmtiles")

    patches = []
    seen = {version}
    with memoryview(pack) as data:
        for base in bases:
            base_version, previous = read_base(base)
            if base_version in seen:
                continue
            seen.add(base_version)
            delta = output / f"tiles-{base_version}.bin"
            with delta.open("wb") as out:
                for offset, length, digest in blobs:
                    if digest not in previous:
                        out.write(data[offset:offset + length])
            if delta.stat().st_size:
                patches.append(base_version)
                print(f"{base_version}: {delta.stat().st_size} bytes")
            else:
                delta.unlink()

    manifest = {"format": 2, "tag": tag, "version": version, "patches": sorted(patches)}
    (output / "world-release.json").write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{tag}: {len(blobs)} unique payloads, {len(patches)} deltas, version {version}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="Export an index for an installed or bundled archive")
    index.add_argument("pack", type=Path)
    index.add_argument("output", type=Path)
    release = commands.add_parser("release", help="Prepare all assets for a release")
    release.add_argument("pack", type=Path)
    release.add_argument("output", type=Path)
    release.add_argument("tag")
    release.add_argument("bases", type=Path, nargs="*")
    args = parser.parse_args()
    with args.pack.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as pack:
        index, blobs = archive_index(pack)
        if args.command == "index":
            args.output.write_bytes(index)
        else:
            publish(pack, args.pack, index, blobs, args.output, args.tag, args.bases)
