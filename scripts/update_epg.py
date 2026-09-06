#!/usr/bin/env python3
"""验证上游 XMLTV 节目单的结构和时效，成功后原子替换本地节目单。"""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET


def parse_xmltv_time(value):
    """XMLTV 无时区时按 UTC 解释，只接受精确到秒的节目时间。"""
    match = re.fullmatch(r"(\d{14})(?:\s*([+-]\d{4}|Z|UTC|GMT))?", value or "")
    if not match:
        raise ValueError(f"无效的 XMLTV 时间: {value!r}")
    zone = match.group(2)
    if zone in (None, "Z", "UTC", "GMT"):
        zone = "+0000"
    return datetime.strptime(f"{match.group(1)} {zone}", "%Y%m%d%H%M%S %z")


def validate_epg(path, now=None):
    """要求至少一个已声明频道在当前或未来 24 小时内有有效节目。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("校验时间必须包含时区")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"节目单 XML 无法解析: {exc}") from exc
    if root.tag != "tv":
        raise ValueError("节目单根元素必须是 tv")
    channels = {channel.get("id") for channel in root.findall("channel") if channel.get("id")}
    if not channels:
        raise ValueError("节目单没有有效频道")
    programmes = root.findall("programme")
    if not programmes:
        raise ValueError("节目单没有节目")
    recent_count = 0
    invalid_count = 0
    latest_stop = None
    for programme in programmes:
        try:
            if programme.get("channel") not in channels:
                raise ValueError("节目引用了未声明的频道")
            start = parse_xmltv_time(programme.get("start"))
            stop = parse_xmltv_time(programme.get("stop"))
            if stop <= start:
                raise ValueError("节目结束时间必须晚于开始时间")
        except ValueError:
            # 上游偶有零时长节目等脏数据，不让少量坏条目阻断整份节目单。
            invalid_count += 1
            continue
        latest_stop = max(latest_stop, stop) if latest_stop else stop
        if stop > now and now - timedelta(days=1) <= start <= now + timedelta(days=1):
            recent_count += 1
    if invalid_count / len(programmes) > 0.1:
        raise ValueError(f"节目单无效条目超过 10% ({invalid_count}/{len(programmes)})")
    if not recent_count:
        raise ValueError("节目单已过期或只有遥远未来的节目，未来 24 小时没有有效节目")
    return {"channels": len(channels), "programmes": len(programmes), "recent": recent_count,
            "invalid": invalid_count, "latest_stop": latest_stop.isoformat()}


def update_epg(source, destination, now=None):
    """无效输入保持旧文件不变；有效且不同的输入通过同目录 rename 替换。"""
    source, destination = Path(source), Path(destination)
    summary = validate_epg(source, now=now)
    if destination.exists() and source.read_bytes() == destination.read_bytes():
        return False, summary
    mode = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else 0o644
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".epg-", delete=False) as output:
            temporary = Path(output.name)
            with source.open("rb") as content:
                shutil.copyfileobj(content, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="待验证的上游 epg.xml")
    parser.add_argument("destination", type=Path, help="本地 epg.xml 路径")
    args = parser.parse_args()
    try:
        changed, summary = update_epg(args.source, args.destination)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"EPG 更新失败: {exc}\n")
    print(f"EPG {'已更新' if changed else '未变化'}: {summary['channels']} 个频道，"
          f"{summary['programmes']} 个节目，忽略 {summary['invalid']} 个无效条目，"
          f"最后结束于 {summary['latest_stop']}")


if __name__ == "__main__":
    main()
