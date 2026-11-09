#!/usr/bin/env python3
"""Build the offline overview from a Protomaps PMTiles extract (pmtiles, mapbox-vector-tile)."""

import argparse
import functools
import gzip
import os
import shutil
import struct
import tempfile
import zlib
from pathlib import Path

from mapbox_vector_tile.decoder import TileData
from pmtiles.reader import all_tiles
from pmtiles.tile import Compression, TileType, deserialize_header

EXTENT = 1024
MAGIC = 0x47505731
ROAD_GROUPS = {"primary": 2, "trunk": 3, "motorway": 4}
LAYERS = {"earth", "boundaries", "roads", "places"}
PROPERTIES = {"kind", "kind_detail", "name", "name:ru", "population", "ref", "shield_text"}


def varint(value, output):
    value = (value << 1) ^ (value >> 31)
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)


def parts(geometry):
    coords = geometry["coordinates"]
    match geometry["type"]:
        case "LineString":
            yield coords
        case "MultiLineString" | "Polygon":
            yield from coords
        case "MultiPolygon":
            for polygon in coords:
                yield from polygon


def quantized(line, extent, polygon):
    result = []
    for x, y in line:
        point = round(x * EXTENT / extent), round(y * EXTENT / extent)
        if not result or result[-1] != point:
            result.append(point)
    if polygon and len(result) > 1 and result[0] == result[-1]:
        result.pop()
    return result if len(result) >= (3 if polygon else 2) else []


def road_label(line):
    longest = 0
    point = None
    for (x, y), (next_x, next_y) in zip(line, line[1:]):
        length = abs(next_x - x) + abs(next_y - y)
        mid = (x + next_x) // 2, (y + next_y) // 2
        if length > longest and inside(*mid):
            longest, point = length, mid
    return longest, point


def inside(x, y):
    return 0 <= x < EXTENT and 0 <= y < EXTENT


def encode(groups, places, refs):
    data = bytearray()
    for group in groups:
        data.extend(struct.pack(">H", len(group)))
        for line in group:
            data.extend(struct.pack(">H", len(line)))
            last_x = last_y = 0
            for x, y in line:
                varint(x - last_x, data)
                varint(y - last_y, data)
                last_x, last_y = x, y
    labels = []
    for name, (_, point) in sorted(places.items(), key=lambda item: item[1][0], reverse=True)[:12]:
        labels.append((False, *point, name))
    for name, (_, point) in sorted(refs.items(), key=lambda item: item[1][0], reverse=True)[:12]:
        labels.append((True, *point, name))
    data.extend(struct.pack(">H", len(labels)))
    for route, x, y, name in labels:
        text = name.encode()
        data.extend(struct.pack(">?HHB", route, x, y, len(text)))
        data.extend(text)
    return zlib.compress(data, 4)


@functools.lru_cache(maxsize=256)
def tile(raw, zoom):
    source = TileData(gzip.decompress(raw))
    groups = [[] for _ in range(5)]
    places, refs = {}, {}
    for layer in source.tile.layers:
        if layer.name not in LAYERS:
            continue
        keys = {i: name for i, name in enumerate(layer.keys) if name in PROPERTIES}
        for feature in layer.features:
            props = {keys[key]: source.parse_value(layer.values[val])
                     for key, val in zip(feature.tags[::2], feature.tags[1::2]) if key in keys}
            kind = props.get("kind")
            if layer.name == "places":
                if kind not in ("country", "locality"):
                    continue
                name = props.get("name:ru") or props.get("name") or ""
                if not name or len(name.encode()) > 80:
                    continue
                geometry = source.parse_geometry(feature.geometry, feature.type, layer.extent, True, None)
                if geometry["type"] == "Point":
                    point = tuple(round(v * EXTENT / layer.extent) for v in geometry["coordinates"])
                    if inside(*point):
                        priority = 10**10 if kind == "country" else int(props.get("population") or 0)
                        places[name] = max((priority, point), places.get(name, (0, point)))
                continue
            if layer.name == "earth":
                group = 0
            elif layer.name == "boundaries":
                if kind != "country":
                    continue
                group = 1
            else:
                detail = props.get("kind_detail", "")
                group = ROAD_GROUPS.get(detail.removesuffix("_link"))
                if group is None:
                    continue
            geometry = source.parse_geometry(feature.geometry, feature.type, layer.extent, True, None)
            ref = (props.get("shield_text") or props.get("ref") or "").split(";", 1)[0].strip()
            for line in parts(geometry):
                points = quantized(line, layer.extent, group == 0)
                if not points:
                    continue
                groups[group].append(points)
                if group >= 2 and zoom >= 6 and ref and len(ref.encode()) <= 32:
                    length, point = road_label(points)
                    if point is not None and length > refs.get(ref, (0, None))[0]:
                        refs[ref] = length, point
    if not any(groups) and not places:
        return b""
    return encode(groups, places, refs)


def build(source, target, max_zoom):
    index = []
    common = {}
    with source.open("rb") as raw, tempfile.TemporaryFile() as payload:
        read = lambda offset, length: os.pread(raw.fileno(), length, offset)
        header = deserialize_header(read(0, 127))
        if header["tile_type"] != TileType.MVT or header["tile_compression"] != Compression.GZIP:
            raise ValueError("Expected a gzip-compressed Protomaps vector archive")
        for (z, x, y), data in all_tiles(read):
            if z > max_zoom:
                break
            data = tile(data, z)
            if not data:
                continue
            offset = common.get(data) if len(data) < 64 else None
            if offset is None:
                offset = payload.tell()
                payload.write(data)
                if len(data) < 64:
                    common[data] = offset
            index.append(((z << 24) | (x << 12) | y, offset, len(data)))
            if len(index) % 10_000 == 0:
                print(f"z{z}: {len(index)} tiles, {payload.tell()/2**20:.1f} MiB", flush=True)
        if not index or index[-1][0] >> 24 != max_zoom:
            raise ValueError("Archive does not contain the requested zoom levels")
        start = 12 + len(index) * 12
        temp = target.with_suffix(".part")
        with temp.open("wb") as output:
            output.write(struct.pack(">III", MAGIC, max_zoom, len(index)))
            for key, offset, length in sorted(index):
                output.write(struct.pack(">III", key, start + offset, length))
            payload.seek(0)
            shutil.copyfileobj(payload, output)
        temp.replace(target)
        print(f"{len(index)} tiles, {target.stat().st_size/2**20:.1f} MiB", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--max-zoom", default=9, type=int, choices=range(6, 12))
    args = parser.parse_args()
    build(args.source, args.target, args.max_zoom)
