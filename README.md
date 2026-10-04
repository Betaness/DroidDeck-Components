# DroidDeck Components

Prebuilt pieces that [DroidDeck](https://github.com/Droid-Deck/DroidDeck) downloads, and the
pipelines that build and publish them. Keeping them here keeps the app repository's releases to
the app itself.

## droiddeck-esync packs

Patched `ntdll.so` and `wineserver` pairs that add eventfd sync (droiddeck-esync) to stock Proton
11 builds, one pack per Proton release.

| Release | Contents |
| --- | --- |
| `droiddeck-esync-valve` | packs for Valve's Proton and Proton Experimental (ARM64) |
| `droiddeck-esync-ge` | packs for GE-Proton (aarch64) |
| `droiddeck-esync-cachyos` | packs for proton-cachyos (arm64) |
| `droiddeck-esync-index` | `index.json` and `index.json.sig` (signed), the apk bundles, `watch-state.json` |

The app reads the signed index and fetches only the pack built for the Proton release a user has
installed. A pack carries the sha256 of the stock binaries it replaces, so it is never applied
to another release. The apk ships the bundle that `tools/droiddeck-esync/release.env` in the app
repository pins by name and sha256.

- `droiddeck-esync/`: patch series, pack and index tools, flavor list, revocations, signing
  public key. [PATCHES.md](droiddeck-esync/PATCHES.md) explains the backends, matching, the build
  and publish steps, signing setup and revocation.
- `.github/workflows/watch-proton-releases.yml`: checks every 20 minutes for new Proton builds and
  starts the pack build when one appears.
- `.github/workflows/build-droiddeck-esync-packs.yml`: builds, smoke-tests and publishes packs, then
  signs a new index (environment `droiddeck-esync-signing`, `main` only).
- `tests/`: unit tests for the tools and workflows.

## License

GPL-3.0, the same as DroidDeck. See [LICENSE](LICENSE). The patch series modifies Wine, which is
LGPL-2.1-or-later.
