#!/usr/bin/env python3
"""What a release's assets must hash to, read from the catalog that points at them.

Prints one JSON object {asset name: {"sha256": ..., "md5": ..., "size": ...}} (each key optional)
for the release tag given, so the mirror workflow can refuse an asset that does not match what
the catalog promises. Catalogs: components.json (Bottles manifests: md5 as file_checksum and
file_size per step), desktop.json / steam-seed.json (packages with sha256 and size), linuxfs.json
(one package with sha256 and size). For the recordings release, each <name>.snapshot.json names
its files archive with a sha256 and size; the snapshot files themselves have no outer checksum.
"""
import json
import sys
from pathlib import Path


def basename(url):
    return url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]


def under(url, tag):
    return "/releases/download/%s/" % tag in url


def add(out, name, **fields):
    """One candidate per catalog step: the Bottles manifests describe the vendor's original file
    (its md5 and size), which is what a mirrored .exe/.msi is but not what a repacked .tar.xz is,
    and two steps may describe the same asset differently. An asset passes when it matches any
    of its candidates; an asset with no usable candidate is mirrored on its own sha256 alone."""
    candidate = {}
    for key, value in fields.items():
        if value in (None, "", 0, "0"):
            continue
        if key == "size":
            value = int(value)
        elif key == "md5" and not (isinstance(value, str) and len(value) == 32):
            continue
        elif key == "sha256" and not (isinstance(value, str) and len(value) == 64):
            continue
        candidate[key] = value
    if not candidate or not ({"md5", "sha256"} & set(candidate)):
        out.setdefault(name, [])
        return
    candidates = out.setdefault(name, [])
    if candidate not in candidates:
        candidates.append(candidate)


def from_components(doc, tag, out):
    for comp in doc.get("components", []):
        for step in comp.get("steps", []):
            url = step.get("url") or (step.get("environment") or {}).get("url") or ""
            if not isinstance(url, str) or not under(url, tag):
                continue
            add(out, basename(url), md5=step.get("file_checksum") or (step.get("environment") or {}).get("file_checksum"),
                size=step.get("file_size") or (step.get("environment") or {}).get("file_size"))


def from_packages(doc, tag, out):
    packages = doc.get("packages") or ([doc] if doc.get("url") else [])
    for pkg in packages:
        url = pkg.get("url", "")
        if under(url, tag):
            add(out, basename(url), sha256=pkg.get("sha256"), size=pkg.get("size") or pkg.get("file_size"))


def from_snapshots(folder, out):
    for path in Path(folder).glob("*.snapshot.json"):
        doc = json.loads(path.read_text("utf-8"))
        archive = doc.get("files_archive")
        if archive and archive.get("name"):
            add(out, archive["name"], sha256=archive.get("sha256"), size=archive.get("size"))


def main():
    tag = sys.argv[1]
    out = {}
    for source in sys.argv[2:]:
        path = Path(source)
        if path.is_dir():
            from_snapshots(path, out)
            continue
        doc = json.loads(path.read_text("utf-8"))
        if "components" in doc:
            from_components(doc, tag, out)
        else:
            from_packages(doc, tag, out)
    json.dump(out, sys.stdout, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
