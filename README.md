# TileDelta

TileDelta publishes an offline PMTiles world map and deltas for changed tiles. Clients can rebuild the latest archive from their local copy, even after skipping releases.

[![Publish world overview](https://github.com/brlumen/tile-delta/actions/workflows/world-overview.yml/badge.svg)](https://github.com/brlumen/tile-delta/actions/workflows/world-overview.yml)

## Download

The world overview contains gzip-compressed vector tiles at zooms 0–9. It includes land, country boundaries, major roads, place names, and road labels. Open `world-roads.pmtiles` with a PMTiles v3 reader. [Latest release](https://github.com/brlumen/tile-delta/releases/latest)

| Release file | Purpose |
| --- | --- |
| `world-roads.pmtiles` | Complete offline map |
| `world-index.pmi` | PMTiles header and directories, plus fingerprints of the compressed tile payloads |
| `world-release.json` | Release tag, index SHA-256, and base versions with ready-made deltas |
| `tiles-<base-sha256>.bin` | Changed tile payloads for a specific base index |
| `world-base-indexes.zip` | Prior indexes used by the workflow to build future deltas |

The upgrade to the November 9, 2026 snapshot from the preceding release transferred 1,104 distinct tile payloads (6.56 MiB), plus the new index. The complete archive was 66.74 MiB. [Release example](https://github.com/brlumen/tile-delta/releases/tag/world-pmtiles-20261109)

## How updates work

1. Resolve the latest release and use its assets for the entire update. Verify the PMI index against its SHA-256.
2. Compare the target fingerprints with fingerprints computed from the local archive. Reuse matching payloads by content.
3. Fetch the ready-made delta for the local index SHA-256 when available. Otherwise, request the missing payloads by HTTP byte range from the new PMTiles.
4. Write the new archive prefix and payloads in the order specified by the target index. Verify the downloaded payloads before atomically replacing the local file.

A PMI index starts with the four bytes `PMI1` and two big-endian unsigned 32-bit integers giving the PMTiles prefix length and the number of distinct payloads. The prefix is everything before the tile data section. It is followed by the first 16 bytes of each compressed payload's SHA-256 in archive offset order. The SHA-256 of the entire index identifies a version.

A delta concatenates the target payloads absent from its base in the same offset order. Read offsets and lengths from the target PMTiles directories. Tile removals are represented by the new directories.

GitHub Actions checks for a new Protomaps snapshot weekly. It retains the two latest published PMTiles releases and carries forward up to eight previous indexes for delta generation. [Workflow](.github/workflows/world-overview.yml)

## Build locally

Use Python 3.11 or later on Linux or macOS and the `pmtiles` CLI. The example uses a known snapshot date and a fresh output directory. [go-pmtiles](https://github.com/protomaps/go-pmtiles)

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install pmtiles==3.8.1 mapbox-vector-tile==2.2.0

snapshot=20261109
mkdir -p build
pmtiles extract "https://build.protomaps.com/$snapshot.pmtiles" build/source.pmtiles --maxzoom=9
python tools/world_roads.py build/source.pmtiles build/world-roads.pmtiles
pmtiles verify build/world-roads.pmtiles
python tools/world_road_release.py release build/world-roads.pmtiles \
  "build/release-$snapshot" "world-pmtiles-$snapshot" seeds/*.pmi
```

To use another PMTiles as a delta base, export its index with `python tools/world_road_release.py index base.pmtiles base.pmi` and pass `base.pmi` to the `release` command.

## Licenses

The code is MIT-licensed. The OpenStreetMap-derived map data is available under ODbL 1.0. [Code license](LICENSE), [map data and attribution](DATA-LICENSE.md)
