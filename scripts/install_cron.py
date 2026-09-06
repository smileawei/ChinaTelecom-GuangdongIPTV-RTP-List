#!/usr/bin/env python3
"""生成 IPTV 定时任务配置；默认只输出，由 crontab 命令显式安装。"""

import argparse
from pathlib import Path
import shlex
import subprocess
import sys

BEGIN = "# BEGIN gdiptv managed updates"
END = "# END gdiptv managed updates"


def build_crontab(current, work_dir):
    work_dir = Path(work_dir).resolve()
    entrypoint = work_dir / "update_iptv.sh"
    legacy = f"30 4 * * 5 {entrypoint} --rescan >> {work_dir}/cron.log 2>&1"
    retained = []
    in_block = False
    for line in current.splitlines():
        if line == BEGIN:
            if in_block:
                raise ValueError("重复的 IPTV 定时任务起始标记")
            in_block = True
        elif line == END:
            if not in_block:
                raise ValueError("IPTV 定时任务结束标记缺少起始标记")
            in_block = False
        elif not in_block and line not in (
            legacy, "# 每周五凌晨4:30 强制重新扫描IPTV频道并更新",
        ):
            retained.append(line)
    if in_block:
        raise ValueError("IPTV 定时任务标记未闭合，拒绝改写")

    def quote(path):
        # crontab 对 % 的处理先于 shell，即使在引号内也需要转义。
        return shlex.quote(str(path)).replace("%", "\\%")

    block = [
        BEGIN,
        "# 每天 06:00 同步上游频道与节目单，并全量检测更新（使用系统时区）。",
        f"0 6 * * * {quote(entrypoint)} --rescan >> {quote(work_dir / 'cron.log')} 2>&1",
        END,
    ]
    prefix = "\n".join(retained).rstrip()
    return (prefix + "\n\n" if prefix else "") + "\n".join(block) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    result = subprocess.run(["crontab", "-l"], text=True, capture_output=True)
    if result.returncode and "no crontab for" not in result.stderr.lower():
        parser.exit(1, "无法读取现有 crontab: " + result.stderr)
    try:
        sys.stdout.write(build_crontab(result.stdout, args.work_dir))
    except ValueError as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
