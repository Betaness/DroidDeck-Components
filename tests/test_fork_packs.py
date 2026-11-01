import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import test_esync_tools as base
from test_esync_tools import ESYNC, NTDLL, WINESERVER, WORKFLOWS, completed, discover, elf, index_entry, ref, run_main, sha256

BLEEDING = "experimental-bleeding-edge-11.0-462253-20261101-pca9e10-wfda79a-df86377-v829313"
BLEEDING_OLD = "experimental-bleeding-edge-11.0-461927-20261031-pca9e10-wfda79a-d21fda8-v4522cc"
IMAGE = "registry.gitlab.steamos.cloud/proton/steamrt4/sdk/arm64-llvm:4.0.20260714.251823-0"
MAKEFILE = f"ifeq ($(ARCH),aarch64)\n\tSTEAMRT_IMAGE ?= {IMAGE}\nendif\n"
FORK = {
    "format": 1, "repo": "Fork/DroidDeck-Components", "rev": 100, "flavors": ["ge", "valve"],
    "release_prefix": "droiddeck-sempool-", "state_release": "droiddeck-sempool-watch",
    "bleeding_edge": {"flavor": "valve", "prefix": "experimental-bleeding-edge-11.0-",
                      "tag": "^experimental-bleeding-edge-11\\.0-\\d+-\\d{8}-p[0-9a-f]+-w[0-9a-f]+-d[0-9a-f]+-v[0-9a-f]+$",
                      "keep": 1, "depot_app": "4427310", "depot_branch": "bleeding-edge"},
    "upstream": {"repo": "Droid-Deck/DroidDeck-Components", "ref": "main", "index_release": "droiddeck-esync-index",
                 "rev": {"ge": 1, "valve": 1}},
    "adopt": {"fork_rev": 100, "rev": 2, "patch_sha256": {"valve-11": "f" * 64}},
}


def fork_entry(flavor, ref_name, rev, ntdll, wine="d" * 40, image=IMAGE, asset=""):
    entry, _ = index_entry(flavor, f"{ref_name}-arm64" if flavor == "valve" else ref_name, 1793301631, rev, ntdll, ref=ref_name)
    entry["source"].update(wine_commit=wine, sdk_image=image, asset=asset)
    return entry


@unittest.skipUnless(base.HAVE_ZSTD and base.HAVE_GUEST, "needs zstd and the guest script, like DiscoverTest")
class ForkDiscoverTest(unittest.TestCase):
    FLAVORS = base.DiscoverTest.FLAVORS
    EXPERIMENTAL = base.DiscoverTest.EXPERIMENTAL
    EXPERIMENTAL_MORE = base.DiscoverTest.EXPERIMENTAL_MORE
    PROTON = base.DiscoverTest.PROTON
    RELEASES = base.DiscoverTest.RELEASES
    INDEX = base.DiscoverTest.INDEX
    REVOKED = base.DiscoverTest.REVOKED

    def setUp(self):
        base.DiscoverTest.setUp(self)
        flavors = json.loads(json.dumps(self.FLAVORS))
        for cfg in flavors["flavors"].values():
            cfg["rev"] = 2
        self.flavors_file.write_text(json.dumps(flavors))
        self.flavors = discover.load_flavors(self.flavors_file)
        self.fork_file = self.tmp / "fork.json"
        self.fork_file.write_text(json.dumps(FORK))
        self.fork = discover.load_fork(self.fork_file, self.flavors)
        self.wine = {BLEEDING: "f" * 40, BLEEDING_OLD: "f" * 40}

    def gh(self, command, **kwargs):
        path = command[-1]
        if path == "repos/ValveSoftware/Proton/git/matching-refs/tags/experimental-bleeding-edge-11.0-":
            self.calls.append(command)
            return completed(command, stdout=json.dumps([ref(BLEEDING_OLD), ref(BLEEDING), ref("experimental-bleeding-edge-11.0-1-x")]))
        if path.startswith("repos/ValveSoftware/Proton/contents/wine?ref="):
            self.calls.append(command)
            tag = path.split("=", 1)[1]
            return completed(command, stdout=json.dumps({"type": "submodule", "sha": self.wine[tag]}))
        if path.startswith("repos/ValveSoftware/Proton/contents/Makefile.in?ref="):
            self.calls.append(command)
            self.assertIn("Accept: application/vnd.github.raw", command)
            return completed(command, stdout=MAKEFILE)
        return base.DiscoverTest.gh(self, command, **kwargs)

    def run_fork(self, entries, ref_name="", flavor="all", failed=frozenset()):
        with mock.patch.object(discover.subprocess, "run", side_effect=self.gh):
            with mock.patch("sys.stderr") as stderr:
                rows = discover.fork_discover(self.flavors, self.fork, flavor, ref_name, entries,
                                              discover.read_revoked(self.revoked_file), failed)
        return rows, "".join(call.args[0] for call in stderr.write.call_args_list)

    def test_shipped_fork_file(self):
        flavors = discover.load_flavors(ESYNC / "flavors.json")
        fork = discover.load_fork(ESYNC / "fork.json", flavors)
        self.assertGreater(fork["rev"], max(cfg["rev"] for cfg in flavors["flavors"].values()))
        self.assertTrue(fork["state_release"].startswith(fork["release_prefix"]))
        self.assertRegex(BLEEDING, fork["bleeding_edge"]["tag"])
        self.assertNotRegex("experimental-11.0-20261027b", fork["bleeding_edge"]["tag"])
        self.assertEqual(fork["bleeding_edge"]["flavor"], "valve")

    def test_fork_file_is_validated(self):
        for change in ({"rev": 2}, {"flavors": ["nope"]}, {"release_prefix": "../x"}, {"repo": "x"},
                       {"bleeding_edge": dict(FORK["bleeding_edge"], flavor="ge")},
                       {"bleeding_edge": dict(FORK["bleeding_edge"], tag="(")},
                       {"bleeding_edge": dict(FORK["bleeding_edge"], depot_branch="a b")},
                       {"upstream": dict(FORK["upstream"], rev={"nope": 1})},
                       {"adopt": dict(FORK["adopt"], rev=100)}, {"adopt": dict(FORK["adopt"], patch_sha256="f" * 64)},
                       {"adopt": dict(FORK["adopt"], patch_sha256={"valve-11": "f"})},
                       {"adopt": {"fork_rev": 100, "rev": 2}}):
            self.fork_file.write_text(json.dumps(dict(FORK, **change)))
            with self.assertRaises(discover.PackError, msg=change):
                discover.load_fork(self.fork_file, self.flavors)

    def test_only_builds_without_a_fork_pack_and_the_newest_bleeding_edge_tag(self):
        entries = [fork_entry("valve", "experimental-11.0-20260917b", 2, "a"), fork_entry("valve", "proton-11.0-10", 100, "b"),
                   fork_entry("valve", "proton-11.0-2b", 1, "c"),
                   fork_entry("ge", "GE-Proton11-7", 2, "a", asset="GE-Proton11-7-aarch64.tar.xz")]
        rows, log = self.run_fork(entries)
        keys = sorted(row["key"] for row in rows)
        self.assertEqual(keys, sorted(["ge-GE-Proton11-6-aarch64", f"valve-{BLEEDING}", "valve-experimental-11.0-20260910b", "valve-proton-11.0-2b"]))
        self.assertTrue(all(row["rev"] == 100 for row in rows))
        bleeding = next(row for row in rows if row["tag"] == BLEEDING)
        self.assertEqual((bleeding["depot_app"], bleeding["depot_branch"], bleeding["token"]), ("4427310", "bleeding-edge", f"{BLEEDING}-arm64"))
        self.assertEqual({row["depot_branch"] for row in rows if row["tag"] != BLEEDING}, {""})
        self.assertIn("valve experimental-11.0-20260917b: the fork has a pack at rev 2", log)
        self.assertIn("valve proton-11.0-10: the fork has a pack at rev 100", log)
        self.assertNotIn(BLEEDING_OLD, json.dumps(rows))

    def test_only_adopted_and_current_rev_packs_count(self):
        def other_series(entry, series="valve-11"):
            entry["patch"].update(series=series, sha256="0" * 64 if series == "valve-11" else "f" * 64)
            return entry

        # an r3 experiment (another patch series under the same release prefix) is not a pack of this fork's series
        entries = [fork_entry("valve", "experimental-11.0-20260917b", 3, "a"),
                   other_series(fork_entry("valve", "proton-11.0-10", 2, "b")),
                   fork_entry("valve", "proton-11.0-2b", 2, "c"),
                   other_series(fork_entry("valve", "experimental-11.0-20260910b", 2, "d"), series="cachyos-11")]
        rows, log = self.run_fork(entries, flavor="valve")
        tags = sorted(row["tag"] for row in rows)
        self.assertEqual(tags, sorted([BLEEDING, "experimental-11.0-20260910b", "experimental-11.0-20260917b", "proton-11.0-10"]))
        self.assertIn("valve proton-11.0-2b: the fork has a pack at rev 2", log)
        # raising fork.json rev rebuilds every build, the adopted r2 and the r100 packs too
        entries.append(fork_entry("valve", "experimental-11.0-20260910b", 100, "d"))
        self.fork["rev"] = 101
        rows, _ = self.run_fork(entries, flavor="valve")
        self.assertEqual(sorted(row["tag"] for row in rows),
                         sorted([BLEEDING, "experimental-11.0-20260910b", "experimental-11.0-20260917b", "proton-11.0-10", "proton-11.0-2b"]))
        self.assertTrue(all(row["rev"] == 101 for row in rows))

    def test_a_bleeding_edge_tag_whose_wine_and_sdk_already_have_a_pack_is_skipped(self):
        entries = [fork_entry("valve", "experimental-11.0-20260917b", 2, "a", wine="f" * 40)]
        rows, log = self.run_fork(entries, flavor="valve")
        self.assertNotIn(BLEEDING, [row["tag"] for row in rows])
        self.assertIn(f"wine ffffffffffff with {IMAGE} already has", log)
        entries = [fork_entry("valve", "experimental-11.0-20260917b", 2, "a", wine="f" * 40, image=IMAGE + "1")]
        rows, _ = self.run_fork(entries, flavor="valve")
        self.assertIn(BLEEDING, [row["tag"] for row in rows])
        entries = [fork_entry("valve", "experimental-11.0-20260917b", 1, "a", wine="f" * 40)]
        rows, _ = self.run_fork(entries, flavor="valve")
        self.assertIn(BLEEDING, [row["tag"] for row in rows])

    def test_one_tag_by_name(self):
        rows, _ = self.run_fork([], ref_name=BLEEDING_OLD)
        self.assertEqual([row["tag"] for row in rows], [BLEEDING_OLD])
        rows, _ = self.run_fork([], ref_name="experimental-11.0-20260901", flavor="valve")
        self.assertEqual([(row["tag"], row["depot_branch"], row["rev"]) for row in rows], [("experimental-11.0-20260901", "", 100)])
        with self.assertRaises(discover.PackError):
            self.run_fork([], ref_name="experimental-bleeding-edge-11.0-9-nope")
        with self.assertRaises(discover.PackError):
            self.run_fork([], flavor="cachyos")

    def test_recent_failures_hold_a_build_back(self):
        rows, _ = self.run_fork([], flavor="valve")
        bleeding = next(row for row in rows if row["tag"] == BLEEDING)
        marker = discover.failure_marker(bleeding)
        self.assertIn("-r100-", marker)
        rows, log = self.run_fork([], flavor="valve", failed=frozenset({marker}))
        self.assertNotIn(BLEEDING, [row["tag"] for row in rows])
        self.assertIn("failed at rev 100", log)
        rows, _ = self.run_fork([], ref_name=BLEEDING, failed=frozenset({marker}))
        self.assertEqual([row["tag"] for row in rows], [BLEEDING])

    def test_fetch_reads_the_fork_releases(self):
        now = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
        entry = fork_entry("valve", "experimental-11.0-20261027b", 2, "a")
        releases = [
            {"tag_name": "droiddeck-sempool-valve3-r2", "assets": [
                {"id": 1, "name": f"{entry['id']}.json"}, {"id": 2, "name": f"{entry['id']}.tzst"}, {"id": 3, "name": "notes.json"}]},
            {"tag_name": "droiddeck-sempool-watch", "assets": [
                {"id": 4, "name": "a-r100-0123456789ab.failed", "updated_at": "2026-11-02T01:00:00Z"},
                {"id": 5, "name": "b-r100-0123456789ab.failed", "updated_at": "2026-10-30T01:00:00Z"}]},
            {"tag_name": "droiddeck-esync-valve", "assets": [{"id": 6, "name": f"{entry['id']}.json"}]},
            {"tag_name": "droiddeck-sempool-draft", "draft": True, "assets": [{"id": 7, "name": f"{entry['id']}.json"}]},
        ]

        def run(command, **kwargs):
            path = command[-1]
            if path == "repos/Fork/DroidDeck-Components/releases?per_page=100":
                self.assertIn("--paginate", command)
                return completed(command, stdout=json.dumps(releases))
            if path == "repos/Fork/DroidDeck-Components/releases/assets/1":
                return completed(command, stdout=json.dumps(entry))
            if path == "repos/Fork/DroidDeck-Components/releases/assets/3":
                return completed(command, stdout=json.dumps({"id": "other"}))
            self.fail(f"unexpected call {command}")

        out = self.tmp / "entries"
        with mock.patch.object(discover.subprocess, "run", side_effect=run), mock.patch("sys.stderr"):
            self.assertEqual(discover.fork_fetch("Fork/DroidDeck-Components", self.fork, out, now), (1, 1))
        entries, failed = discover.fork_entries(out)
        self.assertEqual(entries, [entry])
        self.assertEqual(failed, frozenset({"a-r100-0123456789ab.failed"}))
        self.assertEqual(discover.fork_entries(self.tmp / "missing"), ([], frozenset()))

    def test_matrix_command(self):
        out = self.tmp / "entries"
        (out / "droiddeck-sempool-valve3-r2").mkdir(parents=True)
        entry = fork_entry("valve", "experimental-11.0-20260917b", 2, "a")
        (out / "droiddeck-sempool-valve3-r2" / f"{entry['id']}.json").write_text(json.dumps(entry))
        (out / "failed.txt").write_text("")
        argv = ["--flavors", str(self.flavors_file), "--fork", str(self.fork_file), "fork-matrix", "--flavor", "valve",
                "--entries", str(out), "--revoked", str(self.revoked_file)]
        with mock.patch.object(discover.subprocess, "run", side_effect=self.gh):
            code, stdout, stderr = run_main(discover.main, argv, {"GITHUB_OUTPUT": str(self.output)})
        self.assertEqual(code, 0, stderr)
        tags = sorted(row["tag"] for row in json.loads(stdout)["include"])
        self.assertEqual(tags, sorted([BLEEDING, "experimental-11.0-20260910b", "proton-11.0-10", "proton-11.0-2b"]))
        self.assertIn("count=4", self.output.read_text())


class ForkCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.entry = fork_entry("valve", "experimental-11.0-20261101", 100, "a", wine="f" * 40)
        self.entry["source_match"] = True

    def upstream(self, ntdll="a", exports="e"):
        entry = fork_entry("valve", "experimental-11.0-20261101", 1, ntdll)
        entry["exports"] = exports * 64
        return {"packs": [entry, fork_entry("valve", "experimental-11.0-20261027b", 1, "b")]}

    def test_upstream_pack_of_the_same_build(self):
        lines, ok = discover.fork_check(self.entry, self.upstream(), [])
        self.assertTrue(ok)
        self.assertEqual(lines[0], "Stock check: checked against upstream's pack of the same build")
        lines, ok = discover.fork_check(self.entry, self.upstream(ntdll="c"), [])
        self.assertFalse(ok)
        self.assertIn("FAILED", lines[0])
        _, ok = discover.fork_check(self.entry, self.upstream(exports="9"), [])
        self.assertFalse(ok)

    def test_nothing_to_check_against(self):
        lines, ok = discover.fork_check(self.entry, {}, [])
        self.assertTrue(ok)
        self.assertIn("not checked against stock files", lines[0])

    def test_steam_depot(self):
        depot = {"line": "1793301631 experimental-11.0-20261101-arm64", "stock": dict(self.entry["stock"])}
        lines, ok = discover.fork_check(self.entry, {}, [], depot, "app 4427310, branch public")
        self.assertTrue(ok)
        self.assertIn("the Steam depot (app 4427310, branch public)", lines[0])
        other = {"line": "1793301631 experimental-11.0-20261101-arm64", "stock": {NTDLL: "1" * 64, WINESERVER: "2" * 64}}
        lines, ok = discover.fork_check(self.entry, {}, [], other, "app 4427310, branch public")
        self.assertFalse(ok)
        other["line"] = "1793000000 experimental-11.0-20261027b-arm64"
        lines, ok = discover.fork_check(self.entry, {}, [], other, "app 4427310, branch public")
        self.assertTrue(ok)
        self.assertIn("another build: not compared", lines[1])
        self.assertIn("not checked", lines[0])

    def test_depot_tree_is_hashed(self):
        tree = self.tmp / "depot"
        (tree / "files/lib/wine/aarch64-unix").mkdir(parents=True)
        (tree / "files/bin-arm64").mkdir(parents=True)
        (tree / NTDLL).write_bytes(elf(()))
        (tree / WINESERVER).write_bytes(elf((), b"server"))
        (tree / "version").write_text("1 experimental-11.0-20261101-arm64\n")
        hashed = discover.tree_hashes(tree)
        self.assertEqual(hashed, {"line": "1 experimental-11.0-20261101-arm64",
                                  "stock": {NTDLL: sha256(elf(())), WINESERVER: sha256(elf((), b"server"))}})

    def test_fork_packs_of_the_same_source_are_reported(self):
        twin = fork_entry("valve", BLEEDING, 100, "a", wine="f" * 40)
        other = fork_entry("valve", "experimental-11.0-20261027b", 2, "c", wine="f" * 40)
        lines, ok = discover.fork_check(self.entry, {}, [twin, other, self.entry])
        self.assertTrue(ok)
        self.assertIn(f"- `{twin['id']}` (same wine commit and SDK image) has the same stock files", lines)
        self.assertIn(f"- `{other['id']}` (same wine commit and SDK image) has other stock files", lines)

    def test_packs_of_other_builds_with_the_same_stock_files_are_named(self):
        upstream = {"packs": [fork_entry("valve", "experimental-11.0-20261027b", 1, "a")]}
        fork = [fork_entry("valve", "experimental-11.0-20261027b", 2, "a", wine="1" * 40),
                fork_entry("valve", "experimental-11.0-20261014", 2, "c", wine="1" * 40)]
        lines, ok = discover.fork_check(self.entry, upstream, fork)
        self.assertTrue(ok)
        self.assertIn("not checked", lines[0])
        self.assertEqual(lines[1], f"- packs of other builds with the same stock ntdll.so and wineserver: upstream's `{upstream['packs'][0]['id']}`, "
                                   f"the fork's `{fork[0]['id']}`; where the app has one of them, it already matches this build exactly")

    def test_release_asset_packs(self):
        entry = fork_entry("ge", "GE-Proton11-12", 100, "a", asset="GE-Proton11-12-aarch64.tar.gz")
        entry["source_match"] = False
        lines, ok = discover.fork_check(entry, {}, [])
        self.assertTrue(ok)
        self.assertIn("the stock files of the release asset `GE-Proton11-12-aarch64.tar.gz`", lines[0])


class ForkNotesTest(unittest.TestCase):
    def test_notes_and_extra_list_lines(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        entry, payload = index_entry("valve", f"{BLEEDING}-arm64", 1793301631, 100, "a", ref=BLEEDING)
        (tmp / f"{entry['id']}.json").write_text(json.dumps(entry))
        (tmp / f"{entry['id']}.tzst").write_bytes(payload)
        text, lines = discover.fork_notes(tmp / f"{entry['id']}.json", tmp / f"{entry['id']}.tzst", "Fork/DroidDeck-Components",
                                          "droiddeck-sempool-x-r100", ["Stock check: none at hand", "- detail"], "run 7",
                                          "semaphore-pool-flavors", True)
        self.assertEqual(lines, [
            f"Fork/DroidDeck-Components\tdroiddeck-sempool-x-r100\t{entry['id']}.tzst\t{sha256(payload)}",
            f"Fork/DroidDeck-Components\tdroiddeck-sempool-x-r100\t{entry['id']}.json\t{sha256((tmp / (entry['id'] + '.json')).read_bytes())}"])
        self.assertIn("`0001-droiddeck-esync.patch`, `0002-droiddeck-semaphore-pool.patch`", text)
        self.assertIn("- Stock check: none at hand\n  - detail\n", text)
        self.assertIn("bleeding-edge tag before Steam ships it", text)
        self.assertIn("run 7", text)
        self.assertIn(lines[0], text)


class ForkUpstreamTest(unittest.TestCase):
    def test_drift_is_reported(self):
        flavors = discover.load_flavors(ESYNC / "flavors.json")
        fork = discover.load_fork(ESYNC / "fork.json", flavors)
        theirs = {"flavors": {"valve": {"rev": 1}, "ge": {"rev": 1}, "cachyos": {"rev": 1}}}
        ours = (ESYNC / "patches/valve-11/0001-droiddeck-esync.patch").read_text()

        def run(command, changed=False):
            path = command[-1]
            if path.startswith("repos/Droid-Deck/DroidDeck-Components/contents/droiddeck-esync/flavors.json"):
                return completed(command, stdout=json.dumps(theirs))
            if "/0001-droiddeck-esync.patch" in path:
                return completed(command, stdout=ours + ("x" if changed and "valve-11" in path else ""))
            return completed(command, 1, stderr="gh: Not Found (HTTP 404)")

        with mock.patch.object(discover.subprocess, "run", side_effect=lambda command, **kwargs: run(command)):
            self.assertEqual(discover.fork_upstream(flavors, fork), [])
        theirs["flavors"]["valve"]["rev"] = 2
        with mock.patch.object(discover.subprocess, "run", side_effect=lambda command, **kwargs: run(command, True)):
            notes = discover.fork_upstream(flavors, fork)
        self.assertEqual(len(notes), 2)
        self.assertIn("upstream valve packs are at rev 2, not 1", notes[0])
        self.assertIn("upstream changed patches/valve-11/0001-droiddeck-esync.patch", notes[1])


class ForkWorkflowTest(unittest.TestCase):
    SHARED = ("Check the build script and the patch series", "Install the host tools", "Fetch the stock binaries",
              "Prepare the wine source", "Build the control and the patched binaries", "Make the pack",
              "Smoke-test the pack on the release it was made for", "Fetch the guest script the packs are measured with")

    def setUp(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        self.packs = yaml.safe_load((WORKFLOWS / "build-droiddeck-esync-packs.yml").read_text())
        self.fork = yaml.safe_load((WORKFLOWS / "fork-sempool-packs.yml").read_text())

    def test_build_steps_follow_the_upstream_workflow(self):
        theirs = {step.get("name"): step for step in self.packs["jobs"]["build"]["steps"]}
        ours = {step.get("name"): step for step in self.fork["jobs"]["build"]["steps"]}
        for name in self.SHARED:
            self.assertEqual(ours[name], theirs[name], name)
        for key in ("runs-on", "timeout-minutes", "strategy", "permissions"):
            self.assertEqual(self.fork["jobs"]["build"][key], self.packs["jobs"]["build"][key], key)
        names = [step.get("name") for step in self.fork["jobs"]["build"]["steps"]]
        self.assertLess(names.index("Make the pack"), names.index("Check the control build against the stock files at hand"))
        self.assertLess(names.index("Check the control build against the stock files at hand"),
                        names.index("Smoke-test the pack on the release it was made for"))

    def test_fork_workflow_stays_on_the_fork(self):
        triggers = self.fork.get("on", self.fork.get(True))
        self.assertEqual(set(triggers["workflow_dispatch"]["inputs"]), {"flavor", "ref", "publish"})
        self.assertEqual(set(triggers["workflow_call"]["inputs"]), {"code_ref", "flavor", "ref", "publish"})
        jobs = self.fork["jobs"]
        self.assertIn("github.repository != 'Droid-Deck/DroidDeck-Components'", jobs["discover"]["if"])
        self.assertIn("github.repository != 'Droid-Deck/DroidDeck-Components'", jobs["publish"]["if"])
        self.assertIn("inputs.publish", jobs["publish"]["if"])
        text = json.dumps(self.fork)
        self.assertNotIn("SYNC_INDEX_KEY", text)
        self.assertNotIn("-R Droid-Deck", text)
        upload = next(step for step in jobs["publish"]["steps"] if step.get("name") == "Upload each pack to its own prerelease")
        self.assertIn("--prerelease --latest=false", upload["run"])
        self.assertIn('-R "$GITHUB_REPOSITORY"', upload["run"])
        for name in ("discover", "build", "publish"):
            checkout = next(step for step in jobs[name]["steps"] if step.get("uses") == "actions/checkout@v4")
            self.assertEqual(checkout["with"]["ref"], "${{ inputs.code_ref || github.sha }}")
            self.assertIs(checkout["with"]["persist-credentials"], False)
        for name in ("discover", "build"):
            self.assertEqual(jobs[name]["permissions"], {"contents": "read"})
        depot = next(step for step in jobs["build"]["steps"] if step.get("name") == "Fetch the stock depot (Valve only)")
        self.assertIs(depot["continue-on-error"], True)
        self.assertIn("sha256sum -c -", depot["run"])
        self.assertNotIn("${login[*]", depot["run"])
        self.assertEqual(set(triggers["workflow_call"]["secrets"]), {"STEAM_DEPOT_USER", "STEAM_DEPOT_TOKEN"})
        # DepotDownloader names the account it logs in with; its log stays out of the public artifacts
        logs = [step for step in jobs["build"]["steps"] if step.get("with", {}).get("name", "").startswith("droiddeck-esync-logs-")]
        self.assertEqual(len(logs), 1)
        self.assertNotIn("depot", logs[0]["with"]["path"])
        # a pack already in its release is never replaced, and does not come back every hour either
        self.assertIn('rm -rf "$dir"', upload["run"])
        self.assertIn('"$dir/$id.json" --clobber', upload["run"])
        self.assertNotIn('"$dir/$id.tzst" --clobber', upload["run"])


if __name__ == "__main__":
    unittest.main()
