"""Walk media folders from another machine and post the file list to reclaim.

Use this when reclaim can't mount your media itself (e.g. the share is only reachable
from a Windows box). Each --map pairs a folder this machine can read with the same
folder as Plex sees it, so the paths join to Plex's index.

  python remote_walk.py --url http://reclaim:8892 \\
      --map "//nas/media/Movies=/data/movies" --map "//nas/media/TV=/data/tv"

Standard library only. Add --user/--password if reclaim has basic auth on.
"""
import argparse
import base64
import gzip
import json
import os
import sys
import time
import urllib.request


def walk(local, plex_root, out, errors):
    def rec(p):
        try:
            with os.scandir(p) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        rec(e.path)
                    else:
                        rel = e.path[len(local):].replace("\\", "/")
                        out.append([plex_root + rel, e.stat().st_size])
        except OSError as ex:
            errors.append(f"{p}: {ex}")
    rec(local)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="reclaim base URL, e.g. http://localhost:8892")
    ap.add_argument("--map", action="append", required=True, metavar="LOCAL=PLEX",
                    help="folder readable here = the same folder as Plex sees it (repeatable)")
    ap.add_argument("--user")
    ap.add_argument("--password")
    args = ap.parse_args()

    pairs = []
    for m in args.map:
        if "=" not in m:
            ap.error(f"--map needs LOCAL=PLEX, got {m!r}")
        local, plex = m.split("=", 1)
        pairs.append((local.rstrip("/\\"), plex.rstrip("/")))

    files, errors, t0 = [], [], time.time()
    for local, plex in pairs:
        n = len(files)
        walk(local, plex, files, errors)
        print(f"{local}: {len(files) - n} files", flush=True)
    body = gzip.compress(json.dumps({"at": time.time(), "roots": [p for _, p in pairs], "files": files,
                                     "errors": errors[:50], "seconds": round(time.time() - t0)}).encode())
    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip", "X-Reclaim": "1"}
    if args.user:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{args.user}:{args.password or ''}".encode()).decode()
    req = urllib.request.Request(args.url.rstrip("/") + "/api/walk", data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        print(r.read().decode(), f"({round(time.time() - t0)} s, {len(errors)} errors)")
    return 1 if errors and not files else 0


if __name__ == "__main__":
    sys.exit(main())
