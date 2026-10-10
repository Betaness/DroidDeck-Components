#!/usr/bin/env python3
"""Builds the DLL bundles DroidDeck's Windows components lay out, from the vendors' own packages.

A bundle is a small archive of DLLs in win32/ and win64/ folders that a catalog entry extracts
and copies into a prefix. The ones on system-libraries-v1 were made by hand in June 2026; this
rebuilds each from the vendor package named in tools/bundles/recipes.json (the Bottles manifest's
source, with its md5), by opening the package and picking the same files. The result must match
the hand-made bundle file for file (tools/bundles/expect.json, sha256 per file) before it is
packed; a difference is reported and the bundle refused, so a rebuilt bundle is never silently
different from what users have been installing.

    build.py --recipes recipes.json --expect expect.json --out out/ [--only name ...] [--source-dir dir]

Sources are downloaded (or taken from --source-dir, where a mirror of the package may already
sit), md5-checked, and opened once each: .exe/.cab/.dl_ with cabextract or 7-Zip, .zip with
7-Zip, VC++ installers through DroidDeck's own unpacker (droiddeck-msi-install --unpack, which
opens burn bundles and cab-SFX wrappers the way the phone does). Archives are packed
reproducibly: sorted names, zero timestamps, no owner.
"""
import argparse
import fnmatch
import glob
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_of(path):
    digest = hashlib.md5()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", **kw)


class Sources:
    """Downloads and opens each vendor package once."""

    def __init__(self, cache, source_dir, unpacker):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.source_dir = Path(source_dir) if source_dir else None
        self.unpacker = unpacker
        self.opened = {}
        self.log = []

    def fetch(self, source):
        name, url, md5 = source["name"], source["url"], source.get("md5", "").lower()
        target = self.cache / name
        if not target.is_file():
            local = self.source_dir / name if self.source_dir else None
            if local and local.is_file():
                shutil.copyfile(local, target)
                origin = "from the source mirror"
            else:
                for attempt in range(1, 5):
                    try:
                        request = urllib.request.Request(url, headers={"User-Agent": "DroidDeck-Components bundle builder"})
                        with urllib.request.urlopen(request, timeout=120) as response, open(target, "wb") as out:
                            shutil.copyfileobj(response, out, 1 << 20)
                        break
                    except Exception as error:  # noqa: BLE001
                        if attempt == 4:
                            raise RuntimeError("could not download %s: %s" % (url, error))
                        time.sleep(15 * attempt)
                origin = "fetched from %s" % url.split("/")[2]
            actual = md5_of(target)
            if md5 and actual != md5:
                target.unlink()
                raise RuntimeError("%s: md5 %s, the recipe says %s" % (name, actual, md5))
            self.log.append("%s: %s, md5 %s%s" % (name, origin, actual, "" if md5 else " (unchecked: no recipe md5)"))
        return target

    def open(self, source):
        """The package's contents on disk: a folder, opened once per package."""
        name = source["name"]
        if name in self.opened:
            return self.opened[name]
        package = self.fetch(source)
        folder = self.cache / (name + ".opened")
        if not folder.is_dir():
            folder.mkdir()
            how = source.get("open", "auto")
            if how == "installer":
                result = run(["python3", self.unpacker, "--unpack", str(package), str(folder)])
                if result.returncode != 0:
                    raise RuntimeError("%s: unpacker failed: %s" % (name, result.stderr[-400:]))
            elif how == "install":
                # DroidDeck's full layout of the installer (drive_c with real file names).
                result = run(["python3", self.unpacker, str(package), str(folder)])
                if result.returncode != 0:
                    raise RuntimeError("%s: install layout failed: %s" % (name, result.stderr[-400:]))
            elif package.suffix.lower() == ".zip" or how == "zip":
                with zipfile.ZipFile(package) as archive:
                    archive.extractall(folder)
            elif how in ("auto", "cab") and run(["cabextract", "-q", "-d", str(folder), str(package)]).returncode == 0 \
                    and any(folder.rglob("*")):
                pass
            else:
                result = run(["7z", "x", "-y", "-bso0", "-bsp0", "-o" + str(folder), str(package)])
                if result.returncode != 0 or not any(folder.rglob("*")):
                    raise RuntimeError("%s: neither cabextract nor 7-Zip opened it" % name)
            # Cabinets inside (DirectX's per-feature cabs, SP1's nested cab, .dl_ files): opened
            # in place so a recipe can name a file by its path inside.
            self.open_nested(folder, 0)
        self.opened[name] = folder
        return folder

    def open_nested(self, folder, depth):
        if depth > 2:
            return
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            if path.name.endswith(".opened") or path.suffix.lower() not in (".cab", ".exe", ".msi", ".dl_", ".ex_", ".zip", ".cpl"):
                continue
            if path.suffix.lower() in (".exe", ".cpl") and run(["cabextract", "-l", str(path)]).returncode != 0:
                continue  # a plain program, not a cabinet wrapper
            out = path.with_name(path.name + ".opened")
            if out.exists():
                continue
            out.mkdir()
            ok = run(["cabextract", "-q", "-d", str(out), str(path)]).returncode == 0 and any(out.rglob("*"))
            if not ok:
                shutil.rmtree(out, ignore_errors=True)
                out.mkdir()
                ok = run(["7z", "x", "-y", "-bso0", "-bsp0", "-o" + str(out), str(path)]).returncode == 0 and any(out.rglob("*"))
            if ok:
                self.open_nested(out, depth + 1)
            else:
                shutil.rmtree(out, ignore_errors=True)


def find(folder, patterns, expected=None, log=None):
    """The file under folder matching one of the globs (tried in order; relative, / separated,
    case-insensitive; nested '.opened' folders count as path parts). When a package holds several
    copies (Windows 7 SP1 carries more than one gdiplus.dll), the one with the expected sha256 is
    taken and its path logged, so the recipe can be pinned to it."""
    if isinstance(patterns, str):
        patterns = [patterns]
    for pattern in patterns:
        pattern = pattern.lower()
        hits = []
        for path in folder.rglob("*"):
            if path.is_file():
                rel = path.relative_to(folder).as_posix().lower()
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, "*/" + pattern):
                    hits.append(path)
        if not hits:
            continue
        hits.sort(key=lambda p: (len(p.relative_to(folder).parts), str(p)))
        if len(hits) > 1 and expected:
            for hit in hits:
                if sha256_of(hit) == expected:
                    if log is not None:
                        log.append("%s: %d copies match, took %s (the expected one)" % (pattern, len(hits), hit.relative_to(folder).as_posix()))
                    return hit
        return hits[0]
    raise RuntimeError("no file matches %s" % " | ".join(patterns))


def pack_tar(tree, out):
    with tarfile.open(out, "w:xz", preset=9) as archive:
        for path in sorted(p for p in Path(tree).rglob("*") if p.is_file()):
            info = archive.gettarinfo(str(path), path.relative_to(tree).as_posix())
            info.mtime, info.uid, info.gid, info.uname, info.gname, info.mode = 0, 0, 0, "", "", 0o644
            with open(path, "rb") as source:
                archive.addfile(info, source)


def pack_zip(tree, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in Path(tree).rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(tree).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def build_one(name, recipe, expect, sources, out_dir):
    if recipe.get("skip"):
        return None, recipe["skip"]
    tree = Path(tempfile.mkdtemp(prefix="bundle-"))
    try:
        for item in recipe["files"]:
            patterns = item["from"] if isinstance(item["from"], list) else [item["from"]]
            source = next(s for s in recipe["sources"] if s["name"] == patterns[0].split(":", 1)[0])
            folder = sources.open(source)
            src = find(folder, [p.split(":", 1)[1] for p in patterns], expect.get(name, {}).get(item["to"]), sources.log)
            target = tree / item["to"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        built = {p.relative_to(tree).as_posix(): sha256_of(p) for p in tree.rglob("*") if p.is_file()}
        wanted = expect.get(name, {})
        accepted = ""
        if built != wanted:
            missing = sorted(set(wanted) - set(built))
            extra = sorted(set(built) - set(wanted))
            differ = sorted(k for k in set(wanted) & set(built) if wanted[k] != built[k])
            if recipe.get("accept_different") and not missing and not extra:
                accepted = "; differs from the hand-made bundle in %d file(s), accepted: %s" % (len(differ), recipe["accept_different"])
            else:
                return False, "does not match the hand-made bundle: missing %s, extra %s, different %s" % (missing, extra, differ)
        out = Path(out_dir) / name
        if name.endswith(".tar.xz"):
            pack_tar(tree, out)
        elif name.endswith(".zip"):
            pack_zip(tree, out)
        else:
            shutil.copyfile(tree / recipe["files"][0]["to"], out)
        return True, "%d files, %d bytes, %s%s" % (len(built), out.stat().st_size, sha256_of(out), accepted)
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipes", required=True)
    parser.add_argument("--expect", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cache", default=None)
    parser.add_argument("--source-dir", default=None)
    parser.add_argument("--unpacker", default="droiddeck-msi-install")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    recipes = json.loads(Path(args.recipes).read_text("utf-8"))
    expect = json.loads(Path(args.expect).read_text("utf-8"))
    Path(args.out).mkdir(parents=True, exist_ok=True)
    sources = Sources(args.cache or tempfile.mkdtemp(prefix="sources-"), args.source_dir, args.unpacker)
    names = [n for n in recipes if n != "_" and (not args.only or n in args.only)]
    results = {}
    for name in names:
        print("== %s" % name, flush=True)
        try:
            ok, note = build_one(name, recipes[name], expect, sources, args.out)
        except Exception as error:  # noqa: BLE001
            ok, note = False, "failed: %s" % error
        results[name] = (ok, note)
        print("   %s %s" % ("built" if ok else ("skipped" if ok is None else "REFUSED"), note), flush=True)
    print("\n== sources")
    for line in sources.log:
        print("   " + line)
    Path(args.out, "BUILT.md").write_text(
        "# DLL bundles built from the vendors' packages\n\n| bundle | result |\n|---|---|\n" +
        "".join("| %s | %s %s |\n" % (n, "built:" if ok else ("skipped:" if ok is None else "REFUSED:"), note) for n, (ok, note) in results.items()) +
        "\n## Sources\n\n" + "".join("- %s\n" % line for line in sources.log), "utf-8")
    bad = [n for n, (ok, _) in results.items() if ok is False]
    skipped = [n for n, (ok, _) in results.items() if ok is None]
    print("\n%d built, %d skipped, %d refused%s" % (len(results) - len(bad) - len(skipped), len(skipped), len(bad), ": " + " ".join(bad) if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
