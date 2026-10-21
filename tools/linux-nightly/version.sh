#!/usr/bin/env bash
# Usage: version.sh <checkout> <tag glob> <tag prefix to strip>
# Prints "<tag> <distance> <version>" for HEAD of <checkout>: the newest tag matching the glob
# that HEAD descends from, the commits past it, and the version used in file and profile names:
# "3.1.1" exactly on the tag, "3.1.1+21" when HEAD is 21 commits past it.
set -euo pipefail
dir=$1 glob=$2 prefix=$3
tag=$(git -C "$dir" describe --tags --abbrev=0 --match "$glob" HEAD)
distance=$(git -C "$dir" rev-list --count "$tag..HEAD")
version=${tag#"$prefix"}
[ "$distance" -eq 0 ] || version="$version+$distance"
echo "$tag $distance $version"
