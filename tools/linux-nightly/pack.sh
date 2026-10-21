#!/usr/bin/env bash
# Usage: pack.sh <package id> <stage dir> <out dir> <file> <versionName> <description> <commit> <tag> <distance> <subject>
#
# Lays the staged build out as a Proton tree and packs it as a .wcp (tar + zstd) with a
# profile.json DroidDeck's components manager reads, plus <package id>.json describing the build
# for the publish step.
#   dxvk*, vkd3d-proton*: <stage>/system32 -> files/lib/wine/<dxvk|vkd3d-proton>/aarch64-windows
#                         <stage>/syswow64 -> files/lib/wine/<dxvk|vkd3d-proton>/i386-windows
#   fex:                  <stage>/aarch64-windows, <stage>/aarch64-unix -> files/lib/wine/<same>
set -euo pipefail
id=$1 stage=$2 out=$3 file=$4 version=$5 description=$6 commit=$7 tag=$8 distance=$9 subject=${10}

case "$id" in
  dxvk*)         type=DXVK-Linux;    comp=dxvk ;;
  vkd3d-proton*) type=VKD3D-Linux;   comp=vkd3d ;;
  fex)           type=FEXCore-Linux; comp=fex ;;
  *) echo "unknown package id $id" >&2; exit 1 ;;
esac

pkg=$(mktemp -d)
wine="$pkg/files/lib/wine"
if [ "$comp" = fex ]; then
  for d in aarch64-windows aarch64-unix; do
    [ -d "$stage/$d" ] || { echo "$stage/$d missing" >&2; exit 1; }
    mkdir -p "$wine/$d" && cp "$stage/$d"/* "$wine/$d/"
  done
  chmod 0755 "$wine"/aarch64-unix/*.so
else
  sub=dxvk; [ "$comp" = vkd3d ] && sub=vkd3d-proton
  for pair in system32:aarch64-windows syswow64:i386-windows; do
    from=${pair%%:*} to=${pair#*:}
    [ -d "$stage/$from" ] || { echo "$stage/$from missing" >&2; exit 1; }
    mkdir -p "$wine/$sub/$to"
    for dll in "$stage/$from"/*.dll; do
      file -b "$dll" | grep -q '^PE32' || { echo "$dll is not a PE file" >&2; exit 1; }
      cp "$dll" "$wine/$sub/$to/"
    done
  done
fi

files_json=$( cd "$pkg" && find files -type f | sort | jq -R '{source: ., target: ("${proton}/" + .)}' | jq -cs . )
jq -n --arg type "$type" --arg v "$version" --arg d "$description" --argjson files "$files_json" \
  '{type: $type, versionName: $v, versionCode: 0, description: $d, files: $files}' > "$pkg/profile.json"
( cd "$pkg" && find files -type f | sort | xargs sha256sum ) > "$pkg/SHA256SUMS.txt"

mkdir -p "$out"
out=$(cd "$out" && pwd)
( cd "$pkg" && tar -cf - . | zstd -19 -q -o "$out/$file" )
rm -rf "$pkg"

jq -n --arg id "$id" --arg comp "$comp" --arg file "$file" --arg version "$version" --arg commit "$commit" \
  --arg tag "$tag" --argjson distance "$distance" --arg subject "$subject" \
  --arg built_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg sha256 "$(sha256sum "$out/$file" | cut -d' ' -f1)" \
  '{id: $id, comp: $comp, status: "built", file: $file, version: $version, commit: $commit, tag: $tag,
    distance: $distance, subject: $subject, built_at: $built_at, sha256: $sha256}' > "$out/$id.json"
echo "packed $file ($version)"
zstd -dc "$out/$file" | tar -t
