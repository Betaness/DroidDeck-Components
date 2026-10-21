import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "linux-nightly"

spec = importlib.util.spec_from_file_location("publish", TOOLS / "publish.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)

DXVK_OLD = "a" * 40
DXVK_NEW = "b" * 40
FEX = "c" * 40
NOW = "2026-10-21T20:00:00Z"
RUN = "https://github.com/Droid-Deck/DroidDeck-Components/actions/runs/1"


def built(pid, comp, file, version, commit, distance=21):
    return {"id": pid, "comp": comp, "status": "built", "file": file, "version": version, "commit": commit,
            "tag": "v3.1.1", "distance": distance, "subject": "a | b", "built_at": NOW, "sha256": "0" * 64}


class MergeTest(unittest.TestCase):
    def test_built_package_replaces_entry_and_component_commit_moves(self):
        state = {"components": {"dxvk": {"commit": DXVK_OLD}},
                 "packages": {"dxvk": built("dxvk", "dxvk", "dxvk-3.1.1+20-linux.wcp", "3.1.1+20", DXVK_OLD, 20)}}
        metas = {"dxvk": built("dxvk", "dxvk", "dxvk-3.1.1+21-linux.wcp", "3.1.1+21", DXVK_NEW)}
        ran = publish.merge(state, {"dxvk": DXVK_NEW}, metas, NOW, RUN)
        self.assertEqual(ran, [("dxvk", DXVK_OLD, DXVK_NEW)])
        self.assertEqual(state["packages"]["dxvk"]["file"], "dxvk-3.1.1+21-linux.wcp")
        self.assertEqual(state["components"]["dxvk"]["commit"], DXVK_NEW)

    def test_gplasync_kept_keeps_previous_file_with_note(self):
        prev = built("dxvk-gplasync", "dxvk", "dxvk-gplasync-3.1.1+20-linux.wcp", "gplasync-3.1.1+20", DXVK_OLD, 20)
        state = {"packages": {"dxvk-gplasync": prev}}
        metas = {"dxvk-gplasync": {"id": "dxvk-gplasync", "comp": "dxvk", "status": "kept",
                                   "reason": "the gplasync patch does not apply to this DXVK commit"}}
        publish.merge(state, {"dxvk": DXVK_NEW}, metas, NOW, RUN)
        p = state["packages"]["dxvk-gplasync"]
        self.assertEqual(p["file"], prev["file"])
        self.assertIn("does not apply", p["note"])
        self.assertIn("previous build", p["note"])
        # The component still counts as checked at the new commit, so the watcher does not retry it hourly.
        self.assertEqual(state["components"]["dxvk"]["commit"], DXVK_NEW)

    def test_failed_job_without_previous_build_leaves_only_a_note(self):
        state = {}
        publish.merge(state, {"fex": FEX}, {}, NOW, RUN)
        self.assertNotIn("file", state["packages"]["fex"])
        self.assertIn("the build failed", state["packages"]["fex"]["note"])

    def test_untouched_components_are_left_alone(self):
        vk = built("vkd3d-proton", "vkd3d", "vkd3d-proton-3.0.1+5-linux.wcp", "3.0.1+5", "d" * 40, 5)
        state = {"components": {"vkd3d": {"commit": "d" * 40}}, "packages": {"vkd3d-proton": vk}}
        publish.merge(state, {"fex": FEX}, {"fex": built("fex", "fex", "FEXCore-2610+54-linux.wcp", "2610+54", FEX)}, NOW, RUN)
        self.assertEqual(state["packages"]["vkd3d-proton"], vk)
        self.assertEqual(state["components"]["vkd3d"]["commit"], "d" * 40)


class NotesTest(unittest.TestCase):
    def test_notes_list_packages_with_commit_links_and_escape_pipes(self):
        state = {}
        metas = {"dxvk": built("dxvk", "dxvk", "dxvk-3.1.1+21-linux.wcp", "3.1.1+21", DXVK_NEW),
                 "dxvk-arm64ec": built("dxvk-arm64ec", "dxvk", "dxvk-arm64ec-3.1.1-linux.wcp", "arm64ec-3.1.1", DXVK_NEW, 0)}
        ran = publish.merge(state, {"dxvk": DXVK_NEW}, metas, NOW, RUN)
        text = publish.notes(state, ran)
        self.assertIn(f"https://github.com/doitsujin/dxvk/commit/{DXVK_NEW}", text)
        self.assertIn("`dxvk-3.1.1+21-linux.wcp`", text)
        self.assertIn("21 commits past v3.1.1", text)
        self.assertIn("a \\| b", text)
        self.assertIn("**DXVK gplasync:** Not updated", text)


class VersionTest(unittest.TestCase):
    def git(self, d, *args):
        subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    def test_on_tag_is_plain_and_past_tag_counts_commits(self):
        with tempfile.TemporaryDirectory() as d:
            self.git(d, "init", "-q")
            self.git(d, "commit", "-q", "--allow-empty", "-m", "one")
            self.git(d, "tag", "-a", "v3.1.1", "-m", "v3.1.1")
            out = subprocess.run(["bash", str(TOOLS / "version.sh"), d, "v[0-9]*", "v"],
                                 check=True, capture_output=True, text=True).stdout.split()
            self.assertEqual(out, ["v3.1.1", "0", "3.1.1"])
            for n in range(3):
                self.git(d, "commit", "-q", "--allow-empty", "-m", f"c{n}")
            out = subprocess.run(["bash", str(TOOLS / "version.sh"), d, "v[0-9]*", "v"],
                                 check=True, capture_output=True, text=True).stdout.split()
            self.assertEqual(out, ["v3.1.1", "3", "3.1.1+3"])


class WorkflowTest(unittest.TestCase):
    def test_only_main_publishes_and_watcher_avoids_the_top_of_the_hour(self):
        build = yaml.safe_load((ROOT / ".github/workflows/linux-nightly.yml").read_text())
        step = build["jobs"]["publish"]["steps"][-1]
        self.assertIn("github.ref == 'refs/heads/main'", step["env"]["PUBLISH"])
        watch = yaml.safe_load((ROOT / ".github/workflows/watch-linux-nightly.yml").read_text())
        cron = watch[True]["schedule"][0]["cron"]
        self.assertNotEqual(cron.split()[0], "0")


if __name__ == "__main__":
    unittest.main()
