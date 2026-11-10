#!/usr/bin/env python3
"""Build the offline overview from a Protomaps PMTiles extract (pmtiles, mapbox-vector-tile)."""

import argparse
import functools
import gzip
import os
from pathlib import Path

from mapbox_vector_tile.decoder import TileData
from mapbox_vector_tile.Mapbox.vector_tile_pb2 import tile as VectorTile
from pmtiles.reader import all_tiles
from pmtiles.tile import Compression, TileType, deserialize_header, zxy_to_tileid
from pmtiles.writer import write

EXTENT = 1024
GROUPS = ("land", "boundaries", "primary", "trunk", "motorway")
ROAD_GROUPS = {"primary": 2, "trunk": 3, "motorway": 4}
LAYERS = {"earth", "boundaries", "roads", "places"}
PROPERTIES = {"kind", "kind_detail", "name", "name:ru", "population", "ref", "shield_text"}


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


def quantized_polygon(polygon, extent):
    rings = []
    for i, ring in enumerate(polygon):
        points = quantized(ring, extent, True)
        area = sum(x * yy - xx * y for (x, y), (xx, yy) in zip(points, points[1:] + points[:1]))
        if area:
            if (area > 0) != (i == 0):
                points.reverse()
            rings.append(points)
        elif i == 0:
            break
    return rings


def geometry(parts, polygon=False):
    commands = []
    last_x = last_y = 0
    for points in parts:
        commands.append(9)  # MoveTo(1)
        for i, (x, y) in enumerate(points):
            if i == 1:
                commands.append(((len(points) - 1) << 3) | 2)  # LineTo
            dx, dy = x - last_x, y - last_y
            commands.extend(((dx << 1) ^ (dx >> 31), (dy << 1) ^ (dy >> 31)))
            last_x, last_y = x, y
        if polygon:
            commands.append(15)  # ClosePath(1)
    return commands


def encode(groups, places, refs):
    tile = VectorTile()
    for name, group in zip(GROUPS, groups):
        if not group:
            continue
        layer = tile.layers.add(name=name, version=2, extent=EXTENT)
        if name == "land":
            for polygon in group:
                layer.features.add(type=3, geometry=geometry(polygon, polygon=True))
        else:
            layer.features.add(type=2, geometry=geometry(group))
    for layer_name, labels in (("places", places), ("road_labels", refs)):
        if not labels:
            continue
        layer = tile.layers.add(name=layer_name, version=2, extent=EXTENT)
        layer.keys.append("name")
        for name, (_, point) in sorted(labels.items(), key=lambda item: item[1][0], reverse=True)[:12]:
            layer.features.add(type=1, tags=(0, len(layer.values)), geometry=geometry([[point]]))
            layer.values.add(string_value=name)
    compressed = gzip.compress(tile.SerializeToString(), compresslevel=4, mtime=0)
    # Python 3.11–3.12 can put a platform-specific OS byte in the gzip header.
    return compressed[:9] + b"\xff" + compressed[10:]


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
            if layer.name == "earth":
                if feature.type != 3:
                    continue
                shape = source.parse_geometry(feature.geometry, feature.type, layer.extent, True, None)
                polygons = [shape["coordinates"]] if shape["type"] == "Polygon" else shape["coordinates"]
                for polygon in polygons:
                    rings = quantized_polygon(polygon, layer.extent)
                    if rings:
                        groups[0].append(rings)
                continue
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
            if layer.name == "boundaries":
                if kind != "country":
                    continue
                group = 1
            else:
                detail = props.get("kind_detail", "")
                group = ROAD_GROUPS.get(detail.removesuffix("_link"))
                if group is None:
                    continue
            if feature.type != 2:
                continue
            shape = source.parse_geometry(feature.geometry, feature.type, layer.extent, True, None)
            lines = [shape["coordinates"]] if shape["type"] == "LineString" else shape["coordinates"]
            ref = (props.get("shield_text") or props.get("ref") or "").split(";", 1)[0].strip()
            for line in lines:
                points = quantized(line, layer.extent, False)
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
    temp = target.with_suffix(".part")
    try:
        with source.open("rb") as raw, write(temp) as output:
            def read(offset, length):
                return os.pread(raw.fileno(), length, offset)

            header = deserialize_header(read(0, 127))
            if header["tile_type"] != TileType.MVT or header["tile_compression"] != Compression.GZIP:
                raise ValueError("Expected a gzip-compressed Protomaps vector archive")
            last_zoom = -1
            for (z, x, y), data in all_tiles(read):
                if z > max_zoom:
                    break
                data = tile(data, z)
                if data:
                    output.write_tile(zxy_to_tileid(z, x, y), data)
                    last_zoom = z
                    if output.addressed_tiles % 10_000 == 0:
                        print(f"z{z}: {output.addressed_tiles} tiles, {output.offset/2**20:.1f} MiB", flush=True)
            if last_zoom != max_zoom:
                raise ValueError("Archive does not contain the requested zoom levels")
            output.finalize(
                {"tile_type": TileType.MVT, "tile_compression": Compression.GZIP,
                 "min_lat_e7": -850511288, "max_lat_e7": 850511288},
                {"name": "TileDelta world overview", "format": "pbf",
                 "license": "https://opendatacommons.org/licenses/odbl/1-0/",
                 "attribution": '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
                                ' · <a href="https://protomaps.com/">Protomaps</a>',
                 "vector_layers": [{"id": name, "fields": {"name": "String"} if name in ("places", "road_labels") else {},
                                    "minzoom": 6 if name == "road_labels" else 0, "maxzoom": max_zoom}
                                   for name in (*GROUPS, "places", "road_labels")]},
            )
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    print(f"{output.addressed_tiles} tiles, {target.stat().st_size/2**20:.1f} MiB", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--max-zoom", default=9, type=int, choices=range(6, 12))
    args = parser.parse_args()
    build(args.source, args.target, args.max_zoom)
