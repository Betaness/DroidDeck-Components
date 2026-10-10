#!/usr/bin/env python3
"""upstream.py <components.json> <bottles manifests dir> <tag>: the vendor's own download link
for every asset of the release that a Bottles dependency manifest describes with the same md5.
Prints {asset name: url}. The manifests are checked out at a pinned commit by the workflow, so
the link is the one the catalog's checksum was taken from, not whatever the manifest says today."""
import glob
import json
import os
import re
import sys


def manifest_steps(root):
    """md5 -> (component, url) over every step of every manifest (plain line scan: the files are
    simple YAML lists of steps)."""
    found = {}
    for path in glob.glob(os.path.join(root, "**", "*.yml"), recursive=True):
        name = os.path.basename(path)[:-4]
        current = {}

        def flush():
            if current.get("url") and current.get("md5"):
                found.setdefault(current["md5"].lower(), (name, current["url"]))

        for line in open(path, encoding="utf-8", errors="replace"):
            if re.match(r"\s*-\s*action:", line):
                flush()
                current = {}
            match = re.match(r"\s*url:\s*(\S+)", line)
            if match:
                current["url"] = match.group(1)
            match = re.match(r"\s*file_checksum:\s*(\S+)", line)
            if match:
                current["md5"] = match.group(1)
        flush()
    return found


def main():
    catalog, root, tag = sys.argv[1:4]
    doc = json.load(open(catalog, encoding="utf-8"))
    steps = manifest_steps(root)
    out = {}
    for comp in doc.get("components", []):
        for step in comp.get("steps", []):
            env = step.get("environment") or {}
            url = step.get("url") or env.get("url") or ""
            if "/releases/download/%s/" % tag not in str(url):
                continue
            md5 = str(step.get("file_checksum") or env.get("file_checksum") or "").lower()
            asset = url.rsplit("/", 1)[-1]
            if len(md5) == 32 and md5 in steps and asset not in out:
                out[asset] = steps[md5][1]
    overrides = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upstream-overrides.json")
    if os.path.isfile(overrides):
        for asset, entry in json.load(open(overrides, encoding="utf-8")).items():
            if asset != "_" and isinstance(entry, dict) and entry.get("url"):
                out[asset] = entry["url"]
    json.dump(out, sys.stdout, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
