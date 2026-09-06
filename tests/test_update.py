"""EPG validation and update orchestration, using only temporary local Git remotes."""

from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("update_epg", SCRIPT_DIR / "update_epg.py")
epg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(epg)


def xmltv(start, stop, title="节目", channel="1"):
    return (f'<tv><channel id="1"><display-name>频道</display-name></channel>'
            f'<programme channel="{channel}" start="{start:%Y%m%d%H%M%S %z}" '
            f'stop="{stop:%Y%m%d%H%M%S %z}"><title>{title}</title></programme></tv>')


class EpgValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "upstream.xml"
        self.destination = Path(self.tmp.name) / "epg.xml"
        self.destination.write_text("previous programme", encoding="utf-8")
        self.now = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)

    def test_expired_or_distant_future_does_not_replace_previous_file(self):
        for offset in (-2, 3):
            with self.subTest(offset=offset):
                start = self.now + timedelta(days=offset)
                self.source.write_text(xmltv(start, start + timedelta(hours=1)), encoding="utf-8")
                with self.assertRaises(ValueError):
                    epg.update_epg(self.source, self.destination, now=self.now)
                self.assertEqual(self.destination.read_text(), "previous programme")

    def test_missing_channels_bad_timestamps_and_unknown_channels_are_rejected(self):
        cases = ["<tv/>", '<tv><channel id="1"/></tv>', '<invalid/>', '<tv>',
                 '<tv><channel id="1"/><programme channel="1" start="oops" stop="oops"/></tv>',
                 xmltv(self.now, self.now + timedelta(hours=1), channel="2"),
                 xmltv(self.now, self.now - timedelta(hours=1))]
        for content in cases:
            with self.subTest(content=content):
                self.source.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    epg.update_epg(self.source, self.destination, now=self.now)
                self.assertEqual(self.destination.read_text(), "previous programme")

    def test_valid_update_preserves_permissions_and_unchanged_file(self):
        start = self.now.astimezone(timezone(timedelta(hours=8))) - timedelta(minutes=30)
        self.source.write_text(xmltv(start, start + timedelta(hours=1)), encoding="utf-8")
        self.destination.chmod(0o644)
        changed, summary = epg.update_epg(self.source, self.destination, now=self.now)
        self.assertTrue(changed)
        self.assertEqual(summary["recent"], 1)
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o644)
        original_stat = self.destination.stat()
        changed, _ = epg.update_epg(self.source, self.destination, now=self.now)
        self.assertFalse(changed)
        self.assertEqual(self.destination.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(self.destination.stat().st_ino, original_stat.st_ino)

    def test_a_few_invalid_programmes_are_ignored_but_extensive_damage_is_rejected(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xmltv(self.now, self.now + timedelta(hours=1)))
        valid = ET.tostring(root.find("programme"), encoding="unicode")
        broken = '<programme channel="1" start="20260906120000 +0000" stop="20260906120000 +0000"/>'
        header = '<tv><channel id="1"/>'
        self.source.write_text(header + valid * 10 + broken + '</tv>', encoding="utf-8")
        summary = epg.validate_epg(self.source, now=self.now)
        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["recent"], 10)
        self.source.write_text(header + valid * 2 + broken + '</tv>', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "10%"):
            epg.update_epg(self.source, self.destination, now=self.now)
        self.assertEqual(self.destination.read_text(), "previous programme")


class UpdateScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "upstream"
        self.origin = self.root / "origin.git"
        self.repo = self.root / "fork"
        self.work = self.root / "results"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.log"
        self.real_git = shutil.which("git")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="IPTV Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="IPTV Test", GIT_COMMITTER_EMAIL="test@example.invalid",
                        IPTV_REPO_DIR=str(self.repo), IPTV_WORK_DIR=str(self.work),
                        COMMAND_LOG=str(self.log))
        self.git(self.root, "init", "-b", "master", str(self.base))
        self.old_epg = xmltv(datetime.now(timezone.utc) - timedelta(days=2),
                             datetime.now(timezone.utc) - timedelta(days=1))
        (self.base / "epg.xml").write_text(self.old_epg, encoding="utf-8")
        (self.base / "GuangdongIPTV_rtp.m3u8").write_text("upstream data v1\n")
        (self.base / "README.md").write_text("README v1\n")
        for name in ("iptv.m3u", "iptv-all.m3u", "iptv-epg.m3u", "iptv-all-epg.m3u"):
            (self.base / name).write_text("#EXTM3U\n#EXTINF:-1,Fixture\nrtp://239.1.1.1:1234\n")
        self.git(self.base, "add", ".")
        self.git(self.base, "commit", "-m", "initial")
        self.git(self.root, "clone", "--bare", str(self.base), str(self.origin))
        self.git(self.root, "clone", str(self.origin), str(self.repo))
        self.git(self.repo, "remote", "add", "upstream", str(self.base))
        (self.repo / "scripts").mkdir()
        for name in ("update_iptv.sh", "update_epg.py"):
            shutil.copy2(SCRIPT_DIR / name, self.repo / "scripts" / name)
        # Stub detector deliberately has no ffprobe/network capability.
        (self.repo / "scripts" / "test_streams.py").write_text('''
import argparse, os, pathlib, sys
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write("probe\\n")
if os.environ.get("FAIL_PROBE"):
    sys.exit(17)
parser = argparse.ArgumentParser()
parser.add_argument("--repo", required=True)
parser.add_argument("--output-dir", required=True)
args, rest = parser.parse_known_args()
for name in ("result.m3u", "result_all.m3u", "result_epg.m3u", "result_all_epg.m3u"):
    (pathlib.Path(args.output_dir) / name).write_text("#EXTM3U\\n#EXTINF:-1,Fixture\\nrtp://239.1.1.1:1234\\n")
''', encoding="utf-8")
        (self.bin / "git").write_text(f'''#!{sys.executable}
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write("git " + " ".join(args) + "\\n")
if args[:2] == ["fetch", "upstream"] and os.environ.get("FAIL_FETCH"):
    sys.exit(21)
if args[:1] == ["push"] and os.environ.get("FAIL_PUSH"):
    sys.exit(22)
sys.exit(subprocess.call([{self.real_git!r}] + args))
''', encoding="utf-8")
        (self.bin / "ssh").write_text(f'''#!{sys.executable}
import os
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write("ssh\\n")
''', encoding="utf-8")
        (self.bin / "git").chmod(0o755)
        (self.bin / "ssh").chmod(0o755)
        self.run_env = dict(self.env, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        self.initial_head = self.git(self.repo, "rev-parse", "HEAD")

    def git(self, cwd, *args):
        return subprocess.check_output([self.real_git, "-C", str(cwd), *args],
                                       env=self.env, stderr=subprocess.DEVNULL, text=True).strip()

    def update_upstream(self, filename="epg.xml", content=None):
        if content is None:
            now = datetime.now(timezone.utc)
            content = xmltv(now - timedelta(minutes=5), now + timedelta(hours=2))
        (self.base / filename).write_text(content, encoding="utf-8")
        self.git(self.base, "add", filename)
        self.git(self.base, "commit", "-m", "upstream update")
        return content

    def run_update(self, *args, **env):
        result = subprocess.run(["bash", str(self.repo / "scripts" / "update_iptv.sh"), *args],
                                env=dict(self.run_env, **env), capture_output=True, text=True,
                                timeout=20)
        self.output = result.stdout + result.stderr
        return result

    def commands(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_not_published(self):
        self.assertFalse(any(line.startswith("git push ") for line in self.commands()), self.output)
        self.assertNotIn("ssh", self.commands(), self.output)
        self.assertEqual(self.git(self.origin, "rev-parse", "master"), self.initial_head)

    def test_epg_only_updates_only_epg_without_probe_restart_or_other_upstream_files(self):
        content = self.update_upstream()
        self.update_upstream("README.md", "new upstream README\n")
        result = self.run_update("--epg-only")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual((self.repo / "epg.xml").read_text(), content)
        self.assertEqual((self.repo / "README.md").read_text(), "README v1\n")
        self.assertNotIn("probe", self.commands())
        self.assertNotIn("ssh", self.commands())
        self.assertNotIn("git fetch origin master", self.commands())
        self.assertEqual(self.git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "epg.xml")
        self.assertEqual(self.git(self.origin, "rev-parse", "master"), self.git(self.repo, "rev-parse", "HEAD"))
        first_head = self.git(self.repo, "rev-parse", "HEAD")
        self.log.write_text("")
        self.assertEqual(self.run_update("--epg-only").returncode, 0, self.output)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), first_head)
        self.assertFalse(any(line.startswith("git push ") for line in self.commands()))

    def test_expired_epg_preserves_previous_programme_and_does_not_publish(self):
        result = self.run_update("--epg-only")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual((self.repo / "epg.xml").read_text(), self.old_epg)
        self.assertNotIn("probe", self.commands())
        self.assert_not_published()

    def test_fetch_failure_stops_both_modes_before_probe_or_push(self):
        for args in ((), ("--epg-only",)):
            with self.subTest(args=args):
                self.log.write_text("")
                self.assertNotEqual(self.run_update(*args, FAIL_FETCH="1").returncode, 0, self.output)
                self.assertNotIn("probe", self.commands())
                self.assert_not_published()

    def test_probe_failure_does_not_publish_or_restart(self):
        self.update_upstream("GuangdongIPTV_rtp.m3u8", "new data\n")
        self.assertNotEqual(self.run_update(FAIL_PROBE="1").returncode, 0, self.output)
        self.assertIn("probe", self.commands())
        self.assert_not_published()

    def test_upstream_only_changes_still_push_when_playlists_unchanged(self):
        self.update_upstream("GuangdongIPTV_rtp.m3u8", "new data\n")
        self.assertEqual(self.run_update().returncode, 0, self.output)
        self.assertEqual(self.git(self.origin, "rev-parse", "master"), self.git(self.base, "rev-parse", "HEAD"))
        self.assertIn("ssh", self.commands())
        self.assertFalse(any(line.startswith("git commit ") for line in self.commands()))

    def test_epg_day_updates_can_be_followed_by_full_upstream_merge(self):
        self.update_upstream()
        self.assertEqual(self.run_update("--epg-only").returncode, 0, self.output)
        self.update_upstream("epg.xml", xmltv(datetime.now(timezone.utc),
                             datetime.now(timezone.utc) + timedelta(hours=3), title="下期节目"))
        self.assertEqual(self.run_update().returncode, 0, self.output)
        self.assertEqual((self.repo / "epg.xml").read_bytes(), (self.base / "epg.xml").read_bytes())
        self.assertEqual(self.git(self.origin, "rev-parse", "master"), self.git(self.repo, "rev-parse", "HEAD"))

    def test_non_data_conflict_aborts_merge_without_publishing(self):
        (self.repo / "README.md").write_text("local README\n")
        self.git(self.repo, "add", "README.md")
        self.git(self.repo, "commit", "-m", "local changes")
        before = self.git(self.repo, "rev-parse", "HEAD")
        self.update_upstream("README.md", "upstream README\n")
        self.assertNotEqual(self.run_update().returncode, 0, self.output)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / "README.md").read_text(), "local README\n")
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())
        self.assertNotIn("probe", self.commands())
        self.assert_not_published()

    def test_dirty_or_staged_worktree_stops_before_fetch(self):
        (self.repo / "README.md").write_text("unfinished edit\n")
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    self.git(self.repo, "add", "README.md")
                self.log.write_text("")
                self.assertNotEqual(self.run_update("--epg-only").returncode, 0, self.output)
                self.assertFalse(any(line.startswith("git fetch ") for line in self.commands()))
                self.assert_not_published()

    def test_failed_epg_push_is_retried_without_a_duplicate_commit(self):
        self.update_upstream()
        self.assertNotEqual(self.run_update("--epg-only", FAIL_PUSH="1").returncode, 0, self.output)
        head = self.git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.origin, "rev-parse", "master"), self.initial_head)
        self.assertEqual(self.run_update("--epg-only").returncode, 0, self.output)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.origin, "rev-parse", "master"), head)

    def test_shared_lock_prevents_both_modes_from_starting(self):
        import fcntl
        with (self.repo / ".git" / "iptv-update.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for args in ((), ("--epg-only",)):
                with self.subTest(args=args):
                    self.log.write_text("")
                    self.assertNotEqual(self.run_update(*args).returncode, 0, self.output)
                    self.assertFalse(any(line.startswith("git fetch ") for line in self.commands()))
                    self.assert_not_published()

    def test_help_runs_before_repository_access(self):
        self.assertEqual(self.run_update("--help", IPTV_REPO_DIR="/missing/iptv-repo").returncode, 0, self.output)
        self.assertEqual(self.commands(), [])

    def test_directory_options_are_rejected_before_git_or_probe(self):
        for args in (("--repo", "/tmp/other"), ("--repo=/tmp/other",),
                     ("--output-dir", "/tmp/other"), ("--output-dir=/tmp/other",)):
            with self.subTest(args=args):
                self.assertEqual(self.run_update(*args).returncode, 2, self.output)
                self.assertIn("IPTV_WORK_DIR", self.output)
                self.assertEqual(self.commands(), [])

    def test_abbreviated_directory_options_cannot_redirect_detector_outputs(self):
        other = self.root / "other-results"
        other.mkdir()
        self.assertEqual(self.run_update("--output", str(other)).returncode, 0, self.output)
        self.assertEqual(list(other.iterdir()), [])
        self.assertTrue((self.work / "result.m3u").exists())


if __name__ == "__main__":
    unittest.main()
