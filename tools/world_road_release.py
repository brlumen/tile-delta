#!/usr/bin/env python3
"""Prepare immutable GPW assets and tile deltas for a public GitHub Release."""

import argparse
import hashlib
import json
import mmap
import re
from pathlib import Path

from world_road_updates import HEADER, INDEX_ENTRY, INDEX_MAGIC, MAX_TILES, PACK_MAGIC, Release


def read_base(path):
    if not HEADER.size < path.stat().st_size <= HEADER.size + MAX_TILES * INDEX_ENTRY.size:
        raise ValueError(f"Invalid index size: {path}")
    index = path.read_bytes()
    magic, zoom, count = HEADER.unpack_from(index)
    if (magic != INDEX_MAGIC or not 1 <= zoom <= 11 or not 1 <= count <= MAX_TILES
            or len(index) != HEADER.size + count * INDEX_ENTRY.size):
        raise ValueError(f"Invalid index: {path}")
    tiles = {key: (length, digest) for key, length, digest in INDEX_ENTRY.iter_unpack(index[HEADER.size:])}
    if len(tiles) != count:
        raise ValueError(f"Duplicate tile in index: {path}")
    return hashlib.sha256(index).hexdigest(), tiles


def publish(release, output, tag, bases):
    if not re.fullmatch(r"world-[0-9]{8}", tag):
        raise ValueError("Expected a world-YYYYMMDD tag")
    output.mkdir(parents=True)
    version = release.etag.strip('"')
    (output / "world-index.gpi").write_bytes(release.index)
    _, zoom, count = HEADER.unpack_from(release.index)
    tiles = list(INDEX_ENTRY.iter_unpack(release.index[HEADER.size:]))

    # A sorted, contiguous payload lets a client reconstruct offsets from the index.
    with memoryview(release.pack) as data, (output / "world-roads.gpw").open("wb") as out:
        out.write(HEADER.pack(PACK_MAGIC, zoom, count))
        offset = HEADER.size * (count + 1)
        for key, length, _ in tiles:
            out.write(HEADER.pack(key, offset, length))
            offset += length
        for key, length, _ in tiles:
            offset, _ = release.tiles[key]
            out.write(data[offset:offset + length])

    patches = set()
    with memoryview(release.pack) as data:
        for base in bases:
            base_version, previous = read_base(base)
            if base_version == version or base_version in patches:
                continue
            with (output / f"tiles-{base_version}.bin").open("wb") as out:
                for key, length, digest in tiles:
                    if previous.get(key) != (length, digest):
                        offset, _ = release.tiles[key]
                        out.write(data[offset:offset + length])
            patches.add(base_version)

    manifest = {"format": 1, "tag": tag, "version": version, "patches": sorted(patches)}
    (output / "world-release.json").write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{tag}: {count} tiles, {len(patches)} patches, version {version}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="Export an index for an installed or bundled pack")
    index.add_argument("pack", type=Path)
    index.add_argument("output", type=Path)
    release = commands.add_parser("release", help="Prepare all assets for a release")
    release.add_argument("pack", type=Path)
    release.add_argument("output", type=Path)
    release.add_argument("tag")
    release.add_argument("bases", type=Path, nargs="*")
    args = parser.parse_args()
    with args.pack.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as pack:
        overview = Release(pack)
        if args.command == "index":
            args.output.write_bytes(overview.index)
        else:
            publish(overview, args.output, args.tag, args.bases)
