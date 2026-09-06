"""离线回归：模拟 ffprobe，所有输入、缓存和产物均放入临时目录。"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "probe", Path(__file__).resolve().parents[1] / "scripts" / "test_streams.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def playable(height=1080):
    return {"quality": "FHD" if height == 1080 else "HD", "bitrate": 4000000,
            "video": {"width": 1920, "height": height, "codec": "h264"},
            "audio": {"codec": "aac", "channels": 2},
            "has_video": True, "has_audio": True}


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.repo.mkdir()
        self.work.mkdir()
        self.cache = self.work / "probe_cache.json"
        self.names = ["CCTV1高清", "CCTV2高清", "CCTV3高清", "CCTV4高清"]
        self.addrs = [f"239.1.1.{n}:5140" for n in range(1, 5)]
        self.write_sources()
        (self.repo / "README.md").write_text("# 原 README\n\n---\n上游说明\n")
        (self.repo / "epg.xml").write_text(
            '<tv><channel id="cctv1"><display-name>CCTV1</display-name></channel></tv>')

    def write_sources(self):
        lines = ["#EXTM3U"]
        for name, addr in zip(self.names, self.addrs):
            lines.extend([f'#EXTINF:-1 tvg-name="{name}",{name}', f"rtp://{addr}"])
        (self.repo / "GuangdongIPTV_rtp_all.m3u").write_text("\n".join(lines) + "\n")
        (self.repo / "GuangdongIPTV_rtp_ext.txt").write_text("")
        (self.repo / "IPTV.json").write_text('{"data": []}')

    def write_cache(self, infos=None, age_hours=1, legacy=False):
        infos = infos or {addr: playable() for addr in self.addrs}
        timestamp = time.time() - age_hours * 3600
        entries = {addr: {"checked_at": timestamp, "info": info,
                          "error": None if info else "上次超时"} for addr, info in infos.items()}
        self.cache.write_text(json.dumps(infos if legacy else {"version": 2, "entries": entries}))
        os.utime(self.cache, (timestamp, timestamp))
        return timestamp

    def run_probe(self, *flags, side_effect=None):
        def healthy(addr, proxy_url):
            return addr, playable(), None

        with mock.patch.object(probe.shutil, "which", return_value="/fake/ffprobe"), \
                mock.patch.object(probe, "ffprobe_stream", side_effect=side_effect or healthy) as ffprobe, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = probe.main(["--repo", str(self.repo), "--output-dir", str(self.work), *flags])
        return code, ffprobe, out.getvalue(), err.getvalue()

    def snapshot(self):
        paths = [self.repo / "README.md", self.cache, *self.work.glob("result*")]
        return {path: path.read_bytes() for path in paths if path.exists()}

    def test_legacy_migration_preserves_detection_time_and_removes_obsolete_address(self):
        infos = {addr: playable() for addr in self.addrs}
        infos["239.9.9.9:5140"] = playable()
        checked_at = self.write_cache(infos, legacy=True)
        code, ffprobe, out, _ = self.run_probe()
        self.assertEqual(code, 0)
        ffprobe.assert_not_called()
        cache = json.loads(self.cache.read_text())
        self.assertEqual(cache["version"], 2)
        self.assertEqual(set(cache["entries"]), set(self.addrs))
        self.assertTrue(all(e["checked_at"] == checked_at for e in cache["entries"].values()))
        readme = (self.repo / "README.md").read_text()
        self.assertIn("实际检测时间范围:", readme)
        self.assertIn("本次检测: 0", readme)
        self.assertIn("\n## 播放列表", readme)
        self.assertTrue(readme.endswith("\n---\n上游说明\n"))

    def test_retry_expired_failures_and_successes_but_reuse_fresh_entries(self):
        self.write_cache()
        data = json.loads(self.cache.read_text())
        entries = data["entries"]
        entries[self.addrs[0]]["checked_at"] -= 168 * 3600
        entries[self.addrs[1]].update(info=None, error="超时", checked_at=time.time() - 25 * 3600)
        entries[self.addrs[2]].update(info=None, error="超时")
        self.cache.write_text(json.dumps(data))
        code, ffprobe, _, _ = self.run_probe()
        self.assertEqual(code, 0)
        self.assertEqual({call.args[0] for call in ffprobe.call_args_list}, set(self.addrs[:2]))
        report = (self.work / "result.txt").read_text()
        self.assertIn(f"{self.addrs[2]}  超时", report)

    def test_source_drop_keeps_cache_readme_and_every_playlist(self):
        self.write_cache()
        self.assertEqual(self.run_probe()[0], 0)
        before = self.snapshot()

        def unhealthy(addr, proxy_url):
            return (addr, playable(), None) if addr in self.addrs[:2] else (addr, None, "连接超时")

        code, _, _, err = self.run_probe("--rescan", side_effect=unhealthy)
        self.assertEqual(code, 1)
        self.assertIn("可用源数量异常", err)
        self.assertIn(f"FAIL {self.addrs[2]}: 连接超时", err)
        self.assertEqual(self.snapshot(), before)

    def test_zero_result_blocks_first_publication(self):
        before = self.snapshot()
        code, _, _, err = self.run_probe(side_effect=lambda addr, _: (addr, None, "超时"))
        self.assertEqual(code, 1)
        self.assertIn("本次 0", err)
        self.assertEqual(self.snapshot(), before)

    def test_filtered_channel_drop_is_guarded_even_when_sources_are_healthy(self):
        self.write_cache()
        self.assertEqual(self.run_probe()[0], 0)
        before = self.snapshot()
        self.names = ["CCTV1高清", "CGTN1", "CGTN2", "CGTN3"]
        self.write_sources()
        code, _, _, err = self.run_probe()
        self.assertEqual(code, 1)
        self.assertIn("可用频道数量异常", err)
        self.assertEqual(self.snapshot(), before)

    def test_broken_epg_does_not_overwrite_even_the_report_or_cache(self):
        self.write_cache()
        self.assertEqual(self.run_probe()[0], 0)
        before = self.snapshot()
        (self.repo / "epg.xml").write_text("<tv><channel>")
        code, _, _, err = self.run_probe("--rescan")
        self.assertEqual(code, 1)
        self.assertIn("错误:", err)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.work.glob(".result*")))

    def test_missing_or_empty_epg_keeps_all_previously_published_outputs(self):
        self.write_cache()
        self.assertEqual(self.run_probe()[0], 0)
        before = self.snapshot()
        epg = self.repo / "epg.xml"
        for content in (None, "<tv/>"):
            with self.subTest(content=content):
                if content is None:
                    epg.unlink()
                else:
                    epg.write_text(content)
                code, _, _, err = self.run_probe("--rescan")
                self.assertEqual(code, 1)
                self.assertIn("EPG匹配频道数量异常", err)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(list(self.work.glob(".result*")))

    def test_first_publication_without_epg_is_allowed(self):
        self.write_cache()
        (self.repo / "epg.xml").unlink()
        self.assertEqual(self.run_probe()[0], 0)
        self.assertEqual((self.work / "result.m3u").read_text().count("#EXTINF"), 4)
        self.assertNotIn("#EXTINF", (self.work / "result_epg.m3u").read_text())

    def test_filter_count_and_quality_name_normalization_match_actual_playlist(self):
        self.names = ["CCTV1FHD", "CCTV1HD", "CGTN", "广东卫视超高清"]
        self.write_sources()
        self.write_cache()
        code, _, out, _ = self.run_probe()
        self.assertEqual(code, 0)
        playlist = (self.work / "result.m3u").read_text()
        self.assertEqual(playlist.count("#EXTINF"), 2)
        self.assertIn("2 个频道 /   3 个源", out)
        self.assertNotIn("CCTV1F", playlist)
        self.assertEqual(probe.normalize_for_match("广东卫视超高清"), "广东卫视")
        self.assertEqual(probe.normalize_for_match("广东卫视频道"), "广东卫视")
        self.assertEqual(probe.normalize_channel_name("CCTV-4K超高清"), "CCTV4K")

    def test_missing_ffprobe_fails_before_mutating_outputs(self):
        self.write_cache()
        before = self.snapshot()
        with mock.patch.object(probe.shutil, "which", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = probe.main(["--repo", str(self.repo), "--output-dir", str(self.work)])
        self.assertEqual(code, 1)
        self.assertIn("未找到 ffprobe", err.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_existing_probe_lock_prevents_concurrent_writer(self):
        with (self.work / ".iptv-probe.lock").open("a") as lock:
            probe.fcntl.flock(lock, probe.fcntl.LOCK_EX)
            code, ffprobe, _, err = self.run_probe()
        self.assertEqual(code, 1)
        ffprobe.assert_not_called()
        self.assertIn("已有频道检测正在运行", err)

    def test_atomic_outputs_preserve_existing_permissions_and_default_to_readable(self):
        self.write_cache()
        (self.repo / "README.md").chmod(0o640)
        self.cache.chmod(0o600)
        self.assertEqual(self.run_probe()[0], 0)
        self.assertEqual(stat.S_IMODE((self.repo / "README.md").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.work / "result.m3u").stat().st_mode), 0o644)

    def test_ffprobe_preserves_stderr_timeout_and_missing_audio_reason(self):
        failed = subprocess.CompletedProcess([], 1, "", "Connection refused\n")
        video_only = subprocess.CompletedProcess([], 0, json.dumps(
            {"streams": [{"codec_type": "video", "codec_name": "h264", "height": 1080}]}), "")
        cases = [(failed, "Connection refused"), (video_only, "缺少音频流")]
        for result, reason in cases:
            with self.subTest(reason=reason), mock.patch.object(probe.subprocess, "run", return_value=result):
                addr, info, error = probe.ffprobe_stream(self.addrs[0])
                self.assertIsNone(info)
                self.assertIn(reason, error)
        with mock.patch.object(probe.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffprobe", 20)):
            self.assertIn("检测超时", probe.ffprobe_stream(self.addrs[0])[2])


if __name__ == "__main__":
    unittest.main()
