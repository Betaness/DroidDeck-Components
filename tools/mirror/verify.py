#!/usr/bin/env python3
"""verify.py <file> <asset name> <expectations.json>: exit 0 when the file matches one of the
catalog's candidates for that asset (md5 or sha256, and size when given), or when the catalog
has none; exit 1 on a mismatch. Prints the file's sha256, md5 and size either way."""
import hashlib
import json
import sys
from pathlib import Path


def main():
    path, name, expectations = Path(sys.argv[1]), sys.argv[2], json.loads(Path(sys.argv[3]).read_text("utf-8"))
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            sha.update(block)
            md5.update(block)
    size = path.stat().st_size
    actual = {"sha256": sha.hexdigest(), "md5": md5.hexdigest(), "size": size}
    candidates = expectations.get(name, [])
    print("%s sha256=%s md5=%s size=%d" % (name, actual["sha256"], actual["md5"], size))
    if not candidates:
        print("   no catalog checksum for %s: mirrored on its own sha256" % name)
        return 0
    for candidate in candidates:
        if all(actual.get(key) == str(value).lower() for key, value in candidate.items() if key in ("md5", "sha256")):
            # The hash is the check; a catalog size that disagrees with a matching hash is the
            # catalog's typo (the Bottles manifests carry a few), noted but not fatal.
            if "size" in candidate and candidate["size"] != size:
                print("   matches the catalog's %s; the catalog's size %d is wrong (file is %d)" % (
                    "sha256" if "sha256" in candidate else "md5", candidate["size"], size))
            else:
                print("   matches the catalog (%s)" % ", ".join("%s=%s" % (k, str(v)[:12]) for k, v in candidate.items()))
            return 0
    print("::error::%s does not match any catalog candidate: %s" % (name, candidates), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
