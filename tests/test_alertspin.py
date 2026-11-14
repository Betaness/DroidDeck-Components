"""The thread-id alert protocol of 0003-droiddeck-alert-spin.patch, run on the host.

The block the patch adds to dlls/ntdll/unix/sync.c (from "#ifdef USE_FUTEX" to "#endif /* USE_FUTEX */")
is cut out of the patch and built into droiddeck-esync/tests/alertspin.c, which runs it with real
futex system calls: ping-pong, several alerters on one waiter, timeouts and alerts racing timeouts,
with WINE_ALERT_SPIN_US unset, 0, 30 and above the cap. With clang the same test also runs under
ThreadSanitizer. ALERTSPIN_ROUNDS sets the number of rounds (default 20000).
"""

import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERIES = ROOT / "droiddeck-esync" / "patches" / "valve-11"
HARNESS = ROOT / "droiddeck-esync" / "tests" / "alertspin.c"
ROUNDS = int(os.environ.get("ALERTSPIN_ROUNDS", "20000"))
START = "#ifdef USE_FUTEX"
END = "#endif /* USE_FUTEX */"


def alert_patch() -> Path:
    found = [path for path in sorted(SERIES.glob("*.patch")) if "static NTSTATUS tid_alert_wait(" in path.read_text()]
    if len(found) != 1:
        raise AssertionError(f"expected one patch with tid_alert_wait in {SERIES}, found {found}")
    return found[0]


def protocol_block(patch: Path) -> str:
    """The run of added lines around tid_alert_wait, without the leading '+'."""
    lines = patch.read_text().splitlines()
    marker = next(i for i, line in enumerate(lines) if line.startswith("+static NTSTATUS tid_alert_wait("))
    first = marker
    while first > 0 and lines[first - 1].startswith("+") and not lines[first - 1].startswith("+++"):
        first -= 1
    last = marker
    while last + 1 < len(lines) and lines[last + 1].startswith("+"):
        last += 1
    block = [line[1:] for line in lines[first:last + 1]]
    while block and not block[0].strip():
        block.pop(0)
    while block and not block[-1].strip():
        block.pop()
    if not block or block[0] != START or block[-1] != END:
        raise AssertionError(f"{patch.name}: the protocol block must run from {START!r} to {END!r}")
    return "\n".join(block) + "\n"


def compiler(name: str) -> str | None:
    path = shutil.which(name)
    return path


@unittest.skipUnless(platform.system() == "Linux", "needs Linux futexes")
class AlertSpinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="alertspin-"))
        cls.patch = alert_patch()
        (cls.tmp / "block.c").write_text(protocol_block(cls.patch))
        cc = compiler("cc") or compiler("gcc") or compiler("clang")
        if not cc:
            raise unittest.SkipTest("no C compiler")
        cls.binary = cls.build(cc, "alertspin", ["-O2"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def build(cls, cc: str, name: str, flags: list[str]) -> Path:
        out = cls.tmp / name
        command = [cc, *flags, "-g", "-pthread", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Werror",
                   f"-I{cls.tmp}", '-DALERTSPIN_CODE="block.c"', str(HARNESS), "-o", str(out)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(f"{' '.join(command)} failed:\n{result.stdout}{result.stderr}")
        return out

    def run_harness(self, binary: Path, spin: str | None, rounds: int = ROUNDS, deadline: int = 300):
        env = dict(os.environ)
        env.pop("WINE_ALERT_SPIN_US", None)
        if spin is not None:
            env["WINE_ALERT_SPIN_US"] = spin
        result = subprocess.run([str(binary), str(rounds), str(deadline)], capture_output=True, text=True,
                                env=env, timeout=deadline + 60)
        print(f"\n--- {binary.name} WINE_ALERT_SPIN_US={spin}\n{result.stdout}{result.stderr}", end="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)
        return result

    def test_patch_touches_only_the_alert_code(self):
        files = [line[len("+++ b/"):] for line in self.patch.read_text().splitlines() if line.startswith("+++ ")]
        self.assertEqual(files, ["dlls/ntdll/unix/sync.c"])

    def test_without_polling(self):
        for spin in (None, "0", "-5", "x"):
            result = self.run_harness(self.binary, spin)
            self.assertNotIn("droiddeck-alertspin", result.stderr)
            self.assertIn("(0 us)", result.stdout)

    def test_polling(self):
        result = self.run_harness(self.binary, "30")
        self.assertIn("droiddeck-alertspin: 30 us.", result.stderr)

    def test_polling_is_capped(self):
        result = self.run_harness(self.binary, "5000")
        self.assertIn("droiddeck-alertspin: 1000 us.", result.stderr)

    def test_polling_saves_wakes(self):
        if (os.cpu_count() or 1) < 2:
            self.skipTest("needs two CPUs")

        def wakes(spin):
            out = self.run_harness(self.binary, spin, rounds=ROUNDS).stdout
            line = next(line for line in out.splitlines() if line.startswith("pingpong ") and "futex_wake" in line)
            return int(line.split("futex_wake")[1].split()[0])

        plain, polled = wakes("0"), wakes("1000")
        self.assertLess(polled, max(plain, 1) / 2, f"futex_wake calls: {plain} without polling, {polled} with 1000 us")

    def test_thread_sanitizer(self):
        cc = compiler("clang")
        if not cc:
            self.skipTest("ThreadSanitizer runs with clang")
        probe = self.tmp / "probe.c"
        probe.write_text("#include <pthread.h>\nstatic void *f(void *a) { return a; }\n"
                         "int main(void) { pthread_t t; pthread_create(&t, 0, f, 0); return pthread_join(t, 0); }\n")
        built = subprocess.run([cc, "-fsanitize=thread", "-pthread", str(probe), "-o", str(self.tmp / "probe")],
                               capture_output=True, text=True)
        if built.returncode != 0 or subprocess.run([str(self.tmp / "probe")], capture_output=True).returncode != 0:
            self.skipTest("ThreadSanitizer does not run here")
        binary = self.build(cc, "alertspin-tsan", ["-O1", "-fsanitize=thread"])
        for spin in ("0", "30"):
            result = self.run_harness(binary, spin, rounds=max(ROUNDS // 4, 1000), deadline=600)
            self.assertNotIn("ThreadSanitizer", result.stderr)


if __name__ == "__main__":
    unittest.main()
