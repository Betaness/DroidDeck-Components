#!/usr/bin/env python3
"""Publish linux-nightly builds to the single linux-nightly release.

Each package built in this run replaces the previous file of the same package, so the release
always holds one file per package. state.json on the release records what every file was built
from and which upstream commit each component last ran at (the watcher compares against it).
The release notes are rebuilt from state.json every time.

Without --publish nothing on GitHub is changed: the notes are only written out (branch runs).
"""
import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PACKAGES = [
    # id, component, label
    ("dxvk", "dxvk", "DXVK"),
    ("dxvk-arm64ec", "dxvk", "DXVK ARM64EC"),
    ("dxvk-gplasync", "dxvk", "DXVK gplasync"),
    ("vkd3d-proton", "vkd3d", "VKD3D-Proton"),
    ("vkd3d-proton-arm64ec", "vkd3d", "VKD3D-Proton ARM64EC"),
    ("fex", "fex", "FEXCore"),
]
COMPONENTS = {
    "dxvk": ("DXVK", "doitsujin/dxvk"),
    "vkd3d": ("VKD3D-Proton", "HansKristian-Work/vkd3d-proton"),
    "fex": ("FEX", "FEX-Emu/FEX"),
}


def commit_link(comp, sha):
    return f"[`{sha[:8]}`](https://github.com/{COMPONENTS[comp][1]}/commit/{sha})"


def cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ")


def merge(state, plan, metas, now, run_url):
    """Fold this run's results into state. Returns the list of (comp, old, new) that ran."""
    state.setdefault("components", {})
    state.setdefault("packages", {})
    ran = []
    for comp, commit in plan.items():
        old = state["components"].get(comp, {}).get("commit")
        ran.append((comp, old, commit))
        state["components"][comp] = {"commit": commit, "checked_at": now, "run": run_url}
        for pid, pcomp, _ in PACKAGES:
            if pcomp != comp:
                continue
            meta = metas.get(pid)
            prev = state["packages"].get(pid)
            if meta and meta.get("status") == "built":
                entry = {k: meta[k] for k in ("file", "version", "commit", "tag", "distance", "subject", "built_at", "sha256")}
                entry["run"] = run_url
                state["packages"][pid] = entry
            else:
                reason = (meta or {}).get("reason") or "the build failed"
                note = f"Not updated at {commit[:8]} ({now[:10]}): {reason}."
                if prev and prev.get("file"):
                    prev = dict(prev)
                    prev["note"] = note + " This is the previous build."
                    state["packages"][pid] = prev
                else:
                    state["packages"][pid] = {"note": note}
    state["updated_at"] = now
    state["run"] = run_url
    return ran


def notes(state, ran):
    out = [
        "Nightly builds of **DXVK**, **VKD3D-Proton** and **FEX** for DroidDeck's Linux Steam client, "
        "whose games run on glibc Protons (Valve's Proton ARM64, GE-Proton aarch64, proton-cachyos arm64).",
        "",
        "Built from the latest upstream commit. A watcher checks upstream every hour and rebuilds only the "
        "components that changed; each rebuilt file replaces the previous one, so this release always holds "
        "the newest build of every package.",
        "",
        "> Nightly builds of upstream master: not always stable.",
        "",
        f"**Updated:** {state['updated_at'][:16].replace('T', ' ')} UTC · [build run]({state['run']})",
        "",
    ]
    if ran:
        out += ["### This update", "", "| Component | Previous | Now |", "| :--- | :--- | :--- |"]
        for comp, old, new in ran:
            prev = commit_link(comp, old) if old else "—"
            now = commit_link(comp, new) + (" (unchanged)" if old == new else "")
            out.append(f"| **{COMPONENTS[comp][0]}** | {prev} | {now} |")
        out.append("")
    out += ["### Packages", "", "| Package | Version | Built from | Built |", "| :--- | :--- | :--- | :--- |"]
    footnotes = []
    for pid, comp, label in PACKAGES:
        p = state["packages"].get(pid)
        if not p:
            continue
        if p.get("note"):
            footnotes.append(f"- **{label}:** {p['note']}")
        if not p.get("file"):
            continue
        since = f"{p['tag']}" if p["distance"] == 0 else f"{p['distance']} commits past {p['tag']}"
        out.append(
            f"| **{label}**<br>`{p['file']}` | `{p['version']}`<br>{since} | "
            f"{commit_link(comp, p['commit'])} {cell(p['subject'])} | {p['built_at'][:10]} |"
        )
    out.append("")
    if footnotes:
        out += footnotes + [""]
    out += [
        "### Installing",
        "Each `.wcp` is tar + zstd with a `profile.json` (type `DXVK-Linux`, `VKD3D-Linux` or `FEXCore-Linux`), "
        "laid out as a Proton tree under `files/lib/wine/`. Not for Android containers.",
        "Back up the Proton's own files, then copy the package's `files/` over the Proton's `files/`. "
        "Use GE-Proton or proton-cachyos: Steam restores Valve's own Proton on its next update.",
        "",
        "### Sources",
        "- DXVK: [doitsujin/dxvk](https://github.com/doitsujin/dxvk) master (Philip Rebohle). gplasync: Ph42oN's patch, "
        "the copy kept in [The412Banner/Nightlies](https://github.com/The412Banner/Nightlies/blob/main/patches/dxvk-gplasync-master.patch).",
        "- VKD3D-Proton: [HansKristian-Work/vkd3d-proton](https://github.com/HansKristian-Work/vkd3d-proton) master.",
        "- FEX: [FEX-Emu/FEX](https://github.com/FEX-Emu/FEX) main. DLLs built with llvm-mingw, unixlibs against glibc.",
        "",
        "Build steps follow the all-in-one nightly of [The412Banner/Nightlies](https://github.com/The412Banner/Nightlies).",
    ]
    return "\n".join(out) + "\n"


def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", default="linux-nightly")
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--plan", required=True, help='JSON {"dxvk": "<sha>", ...} of the components this run built')
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--target", required=True, help="full commit sha the release is created at")
    ap.add_argument("--notes-out", required=True)
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args()

    plan = json.loads(a.plan)
    art = Path(a.artifacts)
    metas = {}
    for f in art.glob("*.json"):
        m = json.loads(f.read_text())
        metas[m["id"]] = m
        if m.get("status") == "built" and not (art / m["file"]).is_file():
            sys.exit(f"{m['file']} is missing from the artifacts")

    exists = gh("release", "view", a.tag, "-R", a.repo, check=False).returncode == 0
    state = {}
    if exists:
        with tempfile.TemporaryDirectory() as d:
            if gh("release", "download", a.tag, "-R", a.repo, "-p", "state.json", "-D", d, check=False).returncode == 0:
                state = json.loads((Path(d) / "state.json").read_text())
    before = {pid: p.get("file") for pid, p in state.get("packages", {}).items()}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ran = merge(state, plan, metas, now, a.run_url)
    body = notes(state, ran)
    Path(a.notes_out).write_text(body)
    state_file = art / "state.json"
    state_file.write_text(json.dumps(state, indent=2) + "\n")

    if not a.publish:
        print("dry run: release not changed")
        return

    if not exists:
        gh("release", "create", a.tag, "-R", a.repo, "--target", a.target, "--prerelease", "--latest=false",
           "--title", "Linux components nightly (DXVK, VKD3D-Proton, FEX)", "--notes", "Being built.")
    built = [m for m in metas.values() if m.get("status") == "built"]
    if built:
        gh("release", "upload", a.tag, "-R", a.repo, "--clobber", *[str(art / m["file"]) for m in built])
    listing = json.loads(gh("release", "view", a.tag, "-R", a.repo, "--json", "assets").stdout)
    assets = {x["name"] for x in listing["assets"]}
    for m in built:
        old = before.get(m["id"])
        if old and old != m["file"] and old in assets:
            gh("release", "delete-asset", a.tag, old, "-R", a.repo, "-y")
            print(f"replaced {old} -> {m['file']}")
    gh("release", "upload", a.tag, "-R", a.repo, "--clobber", str(state_file))
    gh("release", "edit", a.tag, "-R", a.repo, "--notes-file", a.notes_out, "--prerelease", "--latest=false")
    print(f"published {len(built)} package(s) to {a.tag}")


if __name__ == "__main__":
    main()
