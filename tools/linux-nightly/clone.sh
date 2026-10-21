#!/usr/bin/env bash
# Usage: clone.sh <url> <dir> <commit>
# Clones <url> with full commit history (blobs on demand, so version.sh can count commits past the
# last tag) and its submodules, at exactly <commit>; fails if that commit cannot be checked out.
set -euo pipefail
url=$1 dir=$2 commit=$3
git clone -c gc.auto=0 --filter=blob:none --recurse-submodules --shallow-submodules "$url" "$dir"
if [ "$(git -C "$dir" rev-parse HEAD)" != "$commit" ]; then
  git -C "$dir" -c advice.detachedHead=false checkout "$commit"
  git -C "$dir" submodule update --init --recursive
fi
[ "$(git -C "$dir" rev-parse HEAD)" = "$commit" ] || { echo "::error::$dir is not at $commit"; exit 1; }
echo "$url at $commit: $(git -C "$dir" log -1 --format=%s)"
