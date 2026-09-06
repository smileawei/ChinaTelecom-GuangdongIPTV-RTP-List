import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "install_cron", Path(__file__).resolve().parents[1] / "scripts/install_cron.py",
)
cron = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cron)


class CronTests(unittest.TestCase):
    def test_migration_preserves_unrelated_jobs_and_is_idempotent(self):
        existing = (
            "MAILTO=admin\n0 1 * * * /usr/local/bin/backup\n"
            "# 每周五凌晨4:30 强制重新扫描IPTV频道并更新\n"
            "30 4 * * 5 /data/code/gdiptv/update_iptv.sh --rescan >> /data/code/gdiptv/cron.log 2>&1\n"
        )
        updated = cron.build_crontab(existing, "/data/code/gdiptv")
        self.assertIn("MAILTO=admin\n0 1 * * * /usr/local/bin/backup", updated)
        self.assertEqual(updated.count("--rescan"), 1)
        self.assertNotIn("--epg-only", updated)
        self.assertIn("0 6 * * * /data/code/gdiptv/update_iptv.sh --rescan", updated)
        self.assertEqual(cron.build_crontab(updated, "/data/code/gdiptv"), updated)

        # 替换上一版已安装的托管区块，移除两个旧时间，避免留下重复任务。
        old_managed = (
            "MAILTO=admin\n0 1 * * * /usr/local/bin/backup\n\n"
            + cron.BEGIN + "\n"
            "30 4 * * 5 /data/code/gdiptv/update_iptv.sh --rescan >> /data/code/gdiptv/cron.log 2>&1\n"
            "15 6 * * * /data/code/gdiptv/update_iptv.sh --epg-only >> /data/code/gdiptv/epg.log 2>&1\n"
            + cron.END + "\n"
        )
        self.assertEqual(cron.build_crontab(old_managed, "/data/code/gdiptv"), updated)

    def test_incomplete_managed_block_is_rejected(self):
        with self.assertRaises(ValueError):
            cron.build_crontab(cron.BEGIN + "\n0 1 * * * /backup\n", "/tmp/iptv")


if __name__ == "__main__":
    unittest.main()
