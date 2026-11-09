#!/usr/bin/env python3
"""Serve the latest GPW pack from world_roads.py. Publish a new pack by atomically replacing the input file."""

import argparse
import hashlib
import mmap
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PACK_MAGIC = 0x47505731
INDEX_MAGIC = 0x47504931
HEADER = struct.Struct(">III")
INDEX_ENTRY = struct.Struct(">II16s")
MAX_TILES = 500_000


class Release:
    def __init__(self, pack):
        magic, zoom, count = HEADER.unpack_from(pack)
        payload = HEADER.size * (count + 1)
        if magic != PACK_MAGIC or not 1 <= zoom <= 11 or not 1 <= count <= MAX_TILES or payload > len(pack):
            raise ValueError("Invalid GPW header")
        self.pack = pack
        self.tiles = {}
        index = bytearray(HEADER.pack(INDEX_MAGIC, zoom, count))
        previous = -1
        size = payload
        with memoryview(pack) as data:
            for key, offset, length in HEADER.iter_unpack(data[HEADER.size:payload]):
                z, x, y = key >> 24, (key >> 12) & 4095, key & 4095
                if (key <= previous or z > zoom or x >= 1 << z or y >= 1 << z
                        or not 1 <= length <= 1024 * 1024 or offset < payload or offset + length > len(pack)):
                    raise ValueError("Invalid GPW tile entry")
                digest = hashlib.sha256(data[offset:offset + length]).digest()[:16]
                index.extend(INDEX_ENTRY.pack(key, length, digest))
                self.tiles[key] = offset, length
                previous = key
                size += length
        if size > 2**31 - 1:
            raise ValueError("GPW package is too large")
        self.index = bytes(index)
        self.etag = '"' + hashlib.sha256(self.index).hexdigest() + '"'


def serve(path, host, port):
    lock = threading.Lock()
    current = None
    stamp = None

    def snapshot():
        nonlocal stamp, current
        with lock, path.open("rb") as file:
            stat = os.fstat(file.fileno())
            version = stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size
            if version != stamp:
                pack = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
                current = Release(pack)
                stamp = version
            return current

    class Handler(BaseHTTPRequestHandler):
        wbufsize = 65536

        def release(self):
            try:
                return snapshot()
            except (OSError, ValueError, struct.error) as error:
                self.log_error("%s", error)
                self.send_error(503, "Overview unavailable")
                return None

        def send_data_headers(self, release, status, length):
            self.send_response(status)
            self.send_header("ETag", release.etag)
            self.send_header("Cache-Control", "no-cache, no-transform")
            if status == 200:
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def do_GET(self):
            if self.path != "/index":
                self.send_error(404)
                return
            release = self.release()
            if release is None:
                return
            if self.headers.get("If-None-Match") == release.etag:
                self.send_data_headers(release, 304, 0)
                return
            self.send_data_headers(release, 200, len(release.index))
            self.wfile.write(release.index)

        def do_POST(self):
            if self.path != "/tiles":
                self.send_error(404)
                return
            self.connection.settimeout(30)
            release = self.release()
            if release is None:
                return
            if self.headers.get("If-Match") != release.etag:
                self.send_error(412, "Overview changed, reload the index")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= len(release.tiles) * 4 or length % 4:
                    raise ValueError()
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError()
                tiles = []
                previous = -1
                for (key,) in struct.iter_unpack(">I", data):
                    if key <= previous:
                        raise ValueError()
                    tiles.append(release.tiles[key])
                    previous = key
            except (ValueError, KeyError, TimeoutError):
                self.send_error(400, "Invalid tile request")
                return
            size = sum(length for _, length in tiles)
            self.send_data_headers(release, 200, size)
            self.log_message("tiles=%d bytes=%d", len(tiles), size)
            for offset, length in tiles:
                self.wfile.write(release.pack[offset:offset + length])

    snapshot()
    with ThreadingHTTPServer((host, port), Handler) as server:
        print(f"http://{host}:{server.server_port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path, help="Path to the latest world-roads.gpw")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    serve(args.pack, args.bind, args.port)
