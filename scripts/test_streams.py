#!/usr/bin/env python3
"""测试广东电信IPTV组播频道可用性，并按分类和清晰度生成播放列表。"""

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

UDPXY_BASE = "http://10.220.10.1:5140/rtp"
REPO = Path(__file__).resolve().parent.parent
TIMEOUT = 15  # ffprobe timeout in seconds
MAX_WORKERS = 5


class PublishGuardError(RuntimeError):
    """检测结果异常，保留上一版缓存与播放列表。"""


def positive_hours(value):
    value = float(value)
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("小时数必须是有限正数")
    return value


def retained_ratio(value):
    value = float(value)
    if not 0 < value <= 1:
        raise argparse.ArgumentTypeError("保留比例必须满足 0 < 比例 <= 1")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO, help="频道数据仓库路径")
    parser.add_argument("--output-dir", type=Path,
                        default=os.environ.get("IPTV_WORK_DIR"), help="缓存和产物目录，默认仓库的父目录")
    parser.add_argument("--proxy-url", default=os.environ.get("IPTV_PROXY_URL", UDPXY_BASE),
                        help="rtp2httpd/udpxy 的 HTTP RTP 路径")
    parser.add_argument("--rescan", action="store_true", help="忽略缓存有效期并全量检测")
    parser.add_argument("--cache-ttl-hours", type=positive_hours, default=168,
                        help="成功检测结果的缓存有效期（小时，默认 168）")
    parser.add_argument("--retry-hours", type=positive_hours, default=24,
                        help="失败地址的重试间隔（小时，默认 24）")
    parser.add_argument("--min-retained-ratio", type=retained_ratio, default=0.7,
                        help="允许发布所需的源数/频道数保留比例（默认 0.7）")
    args = parser.parse_args(argv)
    args.repo = args.repo.resolve()
    args.output_dir = (args.output_dir or args.repo.parent).resolve()
    args.proxy_url = args.proxy_url.rstrip("/")
    return args


def load_cache(path):
    """升级旧版 addr -> info/null 缓存；旧记录的时间取原文件 mtime。"""
    path = Path(path)
    if not path.exists():
        return {}, {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("缓存格式错误，应为 JSON 对象")
    if "version" in data:
        if data["version"] != 2 or not isinstance(data.get("entries"), dict):
            raise ValueError("不支持的缓存版本或 entries 格式")
        entries = data["entries"]
        for addr, entry in entries.items():
            if (not isinstance(entry, dict) or "info" not in entry
                    or not isinstance(entry.get("checked_at"), (int, float))
                    or not 0 < entry["checked_at"] < float("inf")
                    or (entry["info"] is not None and not isinstance(entry["info"], dict))):
                raise ValueError(f"缓存记录格式错误: {addr}")
        return entries, data.get("last_success", {})
    checked_at = path.stat().st_mtime
    entries = {}
    for addr, info in data.items():
        if info is not None and not isinstance(info, dict):
            raise ValueError(f"旧缓存记录格式错误: {addr}")
        entries[addr] = {"checked_at": checked_at, "info": info,
                         "error": None if info is not None else "旧缓存未记录失败原因"}
    return entries, {}


def needs_probe(entry, now, success_hours, retry_hours):
    if entry is None:
        return True
    ttl = success_hours if entry["info"] is not None else retry_hours
    age = now - entry["checked_at"]
    return age < 0 or age >= ttl * 3600


def check_retained(current, previous, ratio, label):
    if current == 0 or (previous and current < previous * ratio):
        raise PublishGuardError(
            f"{label}异常: 本次 {current}，上次 {previous}，最低保留比例 {ratio:.0%}；"
            "已停止发布，保留原缓存、播放列表和 README。请检查网关及数据源后重试。")


class AtomicOutputs:
    """先在目标文件同一磁盘暂存全部产物，生成成功后逐个原子替换。"""

    def __init__(self):
        self.staged = []

    def __enter__(self):
        return self

    @contextmanager
    def open(self, path, mode="w", encoding="utf-8"):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        self.staged.append((Path(temporary), destination))
        with os.fdopen(fd, mode, encoding=encoding) as f:
            permission = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else 0o644
            os.fchmod(f.fileno(), permission)
            yield f
            f.flush()
            os.fsync(f.fileno())

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                for temporary, destination in self.staged:
                    os.replace(temporary, destination)
        finally:
            for temporary, _ in self.staged:
                temporary.unlink(missing_ok=True)


def normalize_channel_name(name):
    if re.match(r"(?i)^CCTV-?4K", name):
        return "CCTV4K"
    norm = re.sub(r"4K超高清|超高清|高清|标清|超清|4K超|FHD|HD|SD|4K|25P",
                  "", name, flags=re.IGNORECASE)
    for suffix in ["超���", "时移专用", "-测��", "-1M开机标清", "-精选"]:
        norm = norm.replace(suffix, "")
    norm = re.sub(r"[（(][^)）]*[)）]", "", norm)
    norm = re.sub(r"(CCTV|CETV|HZTV|PPTV)-(\d)", r"\1\2", norm, flags=re.IGNORECASE)
    return norm.strip() or name


def normalize_for_match(name):
    norm = normalize_channel_name(name).upper().replace("-", "").replace(" ", "")
    return norm.replace("频道", "").strip()

# 卫视热门程度排序（越靠前越热门）
SATELLITE_TV_POPULARITY = [
    "湖南卫视", "浙江卫视", "江苏卫视", "东方卫视", "北京卫视",
    "广东卫视", "深圳卫视", "山东卫视", "四川卫视", "湖北卫视",
    "安徽卫视", "天津卫视", "辽宁卫视", "重庆卫视", "江西卫视",
    "黑龙江卫视", "河南卫视", "河北卫视", "内蒙古卫视", "陕西卫视",
    "广西卫视", "东南卫视", "云南卫视", "贵州卫视", "甘肃卫视",
    "山西卫视", "吉林卫视", "海南卫视", "新疆卫视", "西藏卫视",
    "青海卫视", "宁夏卫视", "兵团卫视", "大湾区卫视", "延边卫视",
    "三沙卫视", "厦门卫视",
]


def parse_m3u(path):
    """解析 m3u 文件，返回 [{name, group, addr, tvg_name}]"""
    channels = []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            # parse extinf
            tvg_name = ""
            group = ""
            m = re.search(r'tvg-name="([^"]*)"', line)
            if m:
                tvg_name = m.group(1)
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                group = m.group(1)
            # channel name is after the last comma
            name = line.rsplit(",", 1)[-1].strip()
            # next non-empty line is the address
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines):
                addr_line = lines[i].strip()
                addr = addr_line.replace("rtp://", "")
                channels.append({
                    "name": name,
                    "group": group,
                    "addr": addr,
                    "tvg_name": tvg_name,
                })
        i += 1
    return channels


def parse_ext_txt(path):
    """解析 ext.txt，返回 [{name, addr}]"""
    channels = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                channels.append({"addr": parts[0], "name": parts[1]})
    return channels


def parse_iptv_json(path):
    """解析 IPTV.json，返回 {addr: {name, channel_id}}"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    result = {}
    for ch in data.get("data", []):
        addr = ch.get("MultiCastURI", "")
        result[addr] = {
            "name": ch.get("ChannelName", ""),
            "channel_id": ch.get("UserChannelID", ""),
        }
    return result


def parse_probe_txt(path):
    """解析 probe.txt，返回 {addr: quality_str}"""
    result = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                result[parts[0]] = parts[1]
    return result


def ffprobe_stream(addr, proxy_url=UDPXY_BASE):
    """返回 (addr, info_dict/None, error/None)，保留可诊断的失败原因。"""
    url = f"{proxy_url}/{addr}"
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-print_format", "json",
                "-show_format", "-show_streams",
                "-timeout", str(TIMEOUT * 1000000),
                url,
            ],
            capture_output=True, text=True,
            timeout=TIMEOUT + 5,
        )
        if result.returncode != 0:
            detail = " ".join(result.stderr.split())[:500]
            return addr, None, f"ffprobe 退出码 {result.returncode}: {detail or '未返回错误详情'}"
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return addr, None, "未检测到任何媒体流"

        has_video = False
        has_audio = False
        video_info = {}
        audio_info = {}

        for s in streams:
            if s.get("codec_type") == "video" and not has_video:
                has_video = True
                video_info = {
                    "codec": s.get("codec_name", "unknown"),
                    "width": s.get("width", 0),
                    "height": s.get("height", 0),
                    "profile": s.get("profile", ""),
                    "bit_rate": int(s.get("bit_rate", 0) or 0),
                }
            elif s.get("codec_type") == "audio" and not has_audio:
                has_audio = True
                audio_info = {
                    "codec": s.get("codec_name", "unknown"),
                    "channels": s.get("channels", 0),
                    "sample_rate": s.get("sample_rate", ""),
                    "bit_rate": int(s.get("bit_rate", 0) or 0),
                }

        # 尝试从 format 获取总码率作为备用
        fmt = data.get("format", {})
        total_bitrate = int(fmt.get("bit_rate", 0) or 0)

        if not has_video or not has_audio:
            missing = "视频" if not has_video else "音频"
            return addr, None, f"缺少{missing}流"

        # determine quality tier
        h = video_info.get("height", 0)
        if h >= 2160:
            quality = "4K"
        elif h >= 1080:
            quality = "FHD"
        elif h >= 720:
            quality = "HD"
        else:
            quality = "SD"

        # 排除 AVS/AVS2/CAVS 编码（播放器不支持）
        vcodec = video_info.get("codec", "").lower()
        if vcodec in ("cavs", "avs2", "avs", "avs3"):
            return addr, None, f"播放器不支持的视频编码: {vcodec}"

        # 综合码率：优先用 format 总码率，否则用视频+音频码率之和
        bitrate = total_bitrate or (video_info.get("bit_rate", 0) + audio_info.get("bit_rate", 0))

        return addr, {
            "video": video_info,
            "audio": audio_info,
            "quality": quality,
            "bitrate": bitrate,
            "has_video": has_video,
            "has_audio": has_audio,
        }, None
    except subprocess.TimeoutExpired:
        return addr, None, f"检测超时（{TIMEOUT + 5} 秒）"
    except (OSError, ValueError, TypeError) as exc:
        return addr, None, f"{type(exc).__name__}: {exc}"


def classify_channel(name, group):
    """根据频道名和分组分类"""
    name_upper = name.upper()

    # 过滤 CGTN
    if "CGTN" in name_upper:
        return None

    # 过滤低价值频道
    filter_keywords = ["IPTV广告", "收视指南", "购物", "广东移动", "南国都市"]
    if any(k in name for k in filter_keywords):
        return None
    if name_upper == "EBU" or name == "茶" or name.startswith("Unknown@"):
        return None
    # 过滤 CCTV 海外版
    if "中文国际欧洲" in name or "中文国际美洲" in name:
        return None

    # CCTV 主频道
    if "CCTV" in name_upper:
        return "CCTV"

    # CCTV 旗下付费频道
    cctv_sub_keywords = [
        "央视", "中国天气", "中国气象",
        "风云足球", "风云剧场", "风云音乐",
        "第一剧场", "怀旧剧场",
        "世界地理", "兵器科技", "女性时尚", "电视指南",
        "高尔夫网球", "卫生健康", "早期教育",
        "发现之旅", "老故事",
    ]
    if any(k in name for k in cctv_sub_keywords):
        return "CCTV"

    # 卫视（在广东/深圳之前判断，广东卫视、深圳卫视归入卫视）
    if "卫视" in name:
        return "各省卫视"

    # 深圳频道（非卫视）
    if "深圳" in name:
        return "深圳频道"

    # 广东省级频道（非卫视）
    gd_keywords = ["广东", "珠江", "南方", "经济科教", "嘉佳卡通", "广州",
                    "岭南", "现代教育", "睛彩"]
    if any(k in name for k in gd_keywords) or "广东电视台" in group or "广州" in group:
        return "广东省级频道"

    # 广东地方频道
    local_cities = [
        "佛山", "中山", "珠海", "肇庆", "江门", "惠州", "东莞", "汕头",
        "揭阳", "潮州", "梅州", "韶关", "清远", "湛江", "茂名", "阳江",
        "云浮", "河源", "汕尾", "潮阳", "顺德", "南海", "番禺", "花都",
        "增城", "从化", "台山", "开平", "鹤山", "新会", "恩平",
        "南沙", "英德", "连州", "乐昌", "客家",
    ]
    if any(c in name for c in local_cities):
        return "广东地方频道"
    if "IPTV-市" in group or "地方" in group:
        return "广东地方频道"

    # 省级和国家级频道（非卫视、非CCTV的其他省台/国家级频道）
    national_provincial_keywords = [
        "CETV", "金鹰", "优漫卡通", "卡酷动画", "快乐垂钓", "求索纪录",
        "天元围棋", "书画", "山东教育",
    ]
    if any(k in name or k in name_upper for k in national_provincial_keywords):
        return "省级和国家级频道"

    # IPTV 主题频道（电信自制轮播）
    iptv_keywords = ["IPTV", "爱体育", "爱大剧", "爱电影", "爱综艺",
                      "热播剧场", "经典电影", "少儿动画", "魅力时尚", "百视通"]
    if any(k in name for k in iptv_keywords):
        return "IPTV主题频道"

    return "其他"


def quality_sort_key(quality):
    """质量排序 key，值越大越好"""
    return {"4K": 4, "FHD": 3, "HD": 2, "SD": 1}.get(quality, 0)


def run(args):
    repo = args.repo
    output_dir = args.output_dir
    cache_file = output_dir / "probe_cache.json"
    generated_at = time.time()
    if shutil.which("ffprobe") is None:
        raise RuntimeError("未找到 ffprobe，请先安装 ffmpeg；原产物保持不变")
    print("=" * 60)
    print("广东电信 IPTV 频道测试工具")
    print("=" * 60)

    # 1. Parse all data sources
    print("\n[1/4] 解析数据源...")
    m3u_channels = parse_m3u(f"{repo}/GuangdongIPTV_rtp_all.m3u")
    ext_channels = parse_ext_txt(f"{repo}/GuangdongIPTV_rtp_ext.txt")
    json_channels = parse_iptv_json(f"{repo}/IPTV.json")

    # Build unified channel map: addr -> {name, group, tvg_name, ...}
    # m3u is the most complete source
    addr_map = {}
    for ch in m3u_channels:
        addr_map[ch["addr"]] = ch

    # supplement from ext.txt (addr not in m3u)
    for ch in ext_channels:
        if ch["addr"] not in addr_map:
            addr_map[ch["addr"]] = {
                "name": ch["name"],
                "group": "",
                "addr": ch["addr"],
                "tvg_name": "",
            }

    # supplement from json
    for addr, info in json_channels.items():
        if addr not in addr_map:
            addr_map[addr] = {
                "name": info["name"],
                "group": "",
                "addr": addr,
                "tvg_name": "",
            }

    all_addrs = list(addr_map.keys())
    print(f"  共 {len(all_addrs)} 个唯一组播地址待测试")

    # 2. 成功缓存默认 7 天，失败默认 24 小时；仅保留当前输入的地址。
    previous_entries, previous_success = load_cache(cache_file)
    previous_ok = sum(entry["info"] is not None for entry in previous_entries.values())
    cache_entries = {addr: previous_entries[addr] for addr in all_addrs if addr in previous_entries}
    pending = [addr for addr in all_addrs if args.rescan or needs_probe(
        cache_entries.get(addr), generated_at, args.cache_ttl_hours, args.retry_hours)]
    total = len(all_addrs)
    print(f"\n[2/4] 复用 {total - len(pending)} 个有效缓存，检测 {len(pending)} 个地址...")
    if pending:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(ffprobe_stream, addr, args.proxy_url): addr for addr in pending}
            for done, future in enumerate(as_completed(futures), 1):
                addr, info, error = future.result()
                cache_entries[addr] = {"checked_at": time.time(), "info": info, "error": error}
                if info is None:
                    print(f"  FAIL {addr}: {(error or '未知原因')[:500]}", file=sys.stderr, flush=True)
                if done % 20 == 0 or done == len(pending):
                    print(f"  进度: {done}/{len(pending)} ...", flush=True)
    results = {addr: cache_entries[addr]["info"] for addr in all_addrs}
    ok_count = sum(info is not None for info in results.values())
    fail_count = total - ok_count
    print(f"  测试完成: {ok_count} 可用, {fail_count} 不可用")
    check_retained(ok_count, previous_ok, args.min_retained_ratio, "可用源数量")
    timestamps = [entry["checked_at"] for entry in cache_entries.values()]
    checked_first = datetime.fromtimestamp(min(timestamps)).strftime("%Y-%m-%d %H:%M")
    checked_last = datetime.fromtimestamp(max(timestamps)).strftime("%Y-%m-%d %H:%M")
    generated_time = datetime.fromtimestamp(generated_at).strftime("%Y-%m-%d %H:%M")

    # 3. Deduplicate: for channels with same tvg_name, keep highest quality
    print("\n[3/4] 去重与分类...")
    # Group by normalized channel name
    name_groups = defaultdict(list)
    for addr, info in results.items():
        if info is None:
            continue
        ch = addr_map.get(addr, {})
        # use tvg_name for grouping if available, else channel name
        key = ch.get("tvg_name") or ch.get("name", addr)
        norm_key = normalize_channel_name(key)
        name_groups[norm_key].append({
            "addr": addr,
            "name": ch.get("name", ""),
            "group": ch.get("group", ""),
            "tvg_name": ch.get("tvg_name", ""),
            "quality": info["quality"],
            "bitrate": info["bitrate"],
            "video": info["video"],
            "audio": info["audio"],
        })

    # For each group, sort all entries by quality desc then bitrate desc
    # Keep best for result.m3u, keep all for aggregated version
    best_channels = []
    all_channels_grouped = {}  # norm_name -> sorted entries list
    for norm_name, entries in name_groups.items():
        entries.sort(key=lambda x: (quality_sort_key(x["quality"]), x["bitrate"]), reverse=True)
        for e in entries:
            e["norm_name"] = norm_name
        best = entries[0].copy()
        best["alt_count"] = len(entries) - 1
        best_channels.append(best)
        all_channels_grouped[norm_name] = entries

    # 4. Classify and output
    categories = {
        "CCTV": [],
        "各省卫视": [],
        "深圳频道": [],
        "广东省级频道": [],
        "省级和国家级频道": [],
        "广东地方频道": [],
        "IPTV主题频道": [],
        "其他": [],
    }
    for ch in best_channels:
        cat = classify_channel(ch["name"], ch["group"])
        if cat is None:
            continue  # 过滤掉的频道（如 CGTN）
        categories[cat].append(ch)

    def cctv_sort_key(ch):
        """CCTV 按频道数字排序，付费频道排后面"""
        name = ch["norm_name"].upper()
        m = re.search(r'CCTV[- ]*(\d+)', name)
        if m:
            return (0, int(m.group(1)), name)  # CCTV-1, CCTV-2 ... 按数字
        return (1, 0, name)  # 付费频道排后面

    def satellite_sort_key(ch):
        """卫视按清晰度+热门程度排序"""
        norm = ch["norm_name"]
        pop_idx = len(SATELLITE_TV_POPULARITY)
        for i, sat_name in enumerate(SATELLITE_TV_POPULARITY):
            if sat_name in norm or norm in sat_name:
                pop_idx = i
                break
        return (-quality_sort_key(ch["quality"]), pop_idx, norm)

    # Sort within each category
    for cat in categories:
        if cat == "CCTV":
            categories[cat].sort(key=cctv_sort_key)
        elif cat == "各省卫视":
            categories[cat].sort(key=satellite_sort_key)
        else:
            categories[cat].sort(
                key=lambda x: (-quality_sort_key(x["quality"]), x["norm_name"])
            )

    total_ch = sum(len(chs) for chs in categories.values())
    previous_ch = previous_success.get("channel_count", 0)
    previous_playlist = output_dir / "result.m3u"
    if not previous_ch and previous_playlist.exists():
        previous_ch = len(parse_m3u(previous_playlist))
    check_retained(total_ch, previous_ch, args.min_retained_ratio, "可用频道数量")

    # Output report
    print("\n[4/4] 生成输出文件...")

    cat_order = ["CCTV", "各省卫视", "深圳频道", "广东省级频道", "省级和国家级频道",
                 "广东地方频道", "IPTV主题频道", "其他"]

    with AtomicOutputs() as outputs:
        # result.txt
        with outputs.open(f"{output_dir}/result.txt", "w", encoding="utf-8") as f:
            f.write("广东电信 IPTV 可用频道测试报告\n")
            f.write(f"测试地址: {args.proxy_url}\n")
            f.write(f"生成时间: {generated_time}\n")
            f.write(f"实际检测时间范围: {checked_first} 至 {checked_last}\n")
            f.write(f"本次检测: {len(pending)} | 复用缓存: {total - len(pending)}\n")
            f.write(f"总计测试: {total} 个地址\n")
            f.write(f"可用: {ok_count} | 不可用: {fail_count}\n")
            total_ch = sum(len(categories[c]) for c in cat_order)
            f.write(f"去重后可用频道: {total_ch} 个\n")
            f.write("=" * 70 + "\n\n")

            total_best = 0
            for cat in cat_order:
                chs = categories[cat]
                total_best += len(chs)
                f.write(f"\n{'='*60}\n")
                f.write(f"  {cat} ({len(chs)} 个频道)\n")
                f.write(f"{'='*60}\n")
                for ch in chs:
                    v = ch["video"]
                    a = ch["audio"]
                    quality_tag = f"[{ch['quality']}]"
                    res_str = f"{v['width']}x{v['height']}" if v['width'] else "N/A"
                    br = ch.get("bitrate", 0)
                    br_str = f"{br // 1000}kbps" if br else "N/A"
                    f.write(
                        f"  {quality_tag:6s} {ch['norm_name']}\n"
                        f"         地址: {ch['addr']}\n"
                        f"         视频: {v['codec']} {res_str} | 音频: {a['codec']} {a['channels']}ch | 码率: {br_str}\n"
                    )
                    if ch["alt_count"] > 0:
                        f.write(f"         (还有 {ch['alt_count']} 个备用地址)\n")
                    f.write("\n")

            # Print failed addresses
            f.write(f"\n{'='*60}\n")
            f.write(f"  不可用的地址 ({fail_count} 个)\n")
            f.write(f"{'='*60}\n")
            for addr, info in results.items():
                if info is None:
                    ch = addr_map.get(addr, {})
                    f.write(f"  FAIL  {ch.get('name', 'unknown'):30s}  {addr}  {cache_entries[addr].get('error') or '未知原因'}\n")

        # 构建 EPG 匹配表：norm_name -> epg channel id
        epg_file = f"{repo}/epg.xml"
        epg_map = {}  # norm_name -> (epg_id, epg_display_name)
        if os.path.exists(epg_file):
            epg_tree = ET.parse(epg_file)
            epg_channels = {}  # display_name -> id
            for epg_ch in epg_tree.getroot().findall("channel"):
                dn = epg_ch.find("display-name")
                if dn is not None and dn.text:
                    epg_channels[dn.text] = epg_ch.get("id")

            epg_norm = {}  # normalized -> (id, original_name)
            for dn, eid in epg_channels.items():
                epg_norm[normalize_for_match(dn)] = (eid, dn)

            # 为每个频道匹配 EPG
            all_norm_names = set()
            for cat in cat_order:
                for ch in categories[cat]:
                    all_norm_names.add(ch["norm_name"])

            for norm_name in all_norm_names:
                # 精确匹配
                if norm_name in epg_channels:
                    epg_map[norm_name] = (epg_channels[norm_name], norm_name)
                    continue
                # 归一化匹配
                nn = normalize_for_match(norm_name)
                if nn in epg_norm:
                    epg_map[norm_name] = epg_norm[nn]
                    continue

            matched = len(epg_map)
            print(f"  EPG 匹配: {matched}/{len(all_norm_names)} 个频道")

        previous_epg = output_dir / "result_epg.m3u"
        previous_epg_count = len(parse_m3u(previous_epg)) if previous_epg.exists() else 0
        if previous_epg_count:
            check_retained(len(epg_map), previous_epg_count, args.min_retained_ratio,
                           "EPG匹配频道数量")

        epg_url = "https://warp.rm.do/iptv/epg.xml"

        def get_tvg_name(norm_name):
            """返回匹配 EPG 的 tvg-name"""
            if norm_name in epg_map:
                return epg_map[norm_name][1]
            return norm_name

        # result.m3u
        with outputs.open(f"{output_dir}/result.m3u", "w", encoding="utf-8") as f:
            f.write(f'#EXTM3U x-tvg-url="{epg_url}" name="广东电信IPTV可用频道"\n')
            for cat in cat_order:
                for ch in categories[cat]:
                    tvg = get_tvg_name(ch["norm_name"])
                    f.write(
                        f'#EXTINF:-1 tvg-name="{tvg}" '
                        f'group-title="{cat}",'
                        f'{ch["norm_name"]}\n'
                    )
                    f.write(f'rtp://{ch["addr"]}\n')

        # result_all.m3u — 聚合版，同一频道所有可用源都列出，按清晰度和码率排序
        with outputs.open(f"{output_dir}/result_all.m3u", "w", encoding="utf-8") as f:
            f.write(f'#EXTM3U x-tvg-url="{epg_url}" name="广东电信IPTV可用频道(聚合)"\n')
            for cat in cat_order:
                for ch in categories[cat]:
                    norm_name = ch["norm_name"]
                    tvg = get_tvg_name(norm_name)
                    entries = all_channels_grouped.get(norm_name, [])
                    for entry in entries:
                        f.write(
                            f'#EXTINF:-1 tvg-name="{tvg}" '
                            f'group-title="{cat}",'
                            f'{norm_name}\n'
                        )
                        f.write(f'rtp://{entry["addr"]}\n')

        # result_epg.m3u — 仅包含能匹配 EPG 的频道（最佳源）
        with outputs.open(f"{output_dir}/result_epg.m3u", "w", encoding="utf-8") as f:
            f.write(f'#EXTM3U x-tvg-url="{epg_url}" name="广东电信IPTV可用频道(EPG)"\n')
            epg_ch_count = 0
            for cat in cat_order:
                for ch in categories[cat]:
                    if ch["norm_name"] not in epg_map:
                        continue
                    tvg = get_tvg_name(ch["norm_name"])
                    f.write(
                        f'#EXTINF:-1 tvg-name="{tvg}" '
                        f'group-title="{cat}",'
                        f'{ch["norm_name"]}\n'
                    )
                    f.write(f'rtp://{ch["addr"]}\n')
                    epg_ch_count += 1

        # result_all_epg.m3u — 仅包含能匹配 EPG 的频道（聚合所有源）
        with outputs.open(f"{output_dir}/result_all_epg.m3u", "w", encoding="utf-8") as f:
            f.write(f'#EXTM3U x-tvg-url="{epg_url}" name="广东电信IPTV可用频道(EPG聚合)"\n')
            epg_src_count = 0
            for cat in cat_order:
                for ch in categories[cat]:
                    if ch["norm_name"] not in epg_map:
                        continue
                    norm_name = ch["norm_name"]
                    tvg = get_tvg_name(norm_name)
                    entries = all_channels_grouped.get(norm_name, [])
                    for entry in entries:
                        f.write(
                            f'#EXTINF:-1 tvg-name="{tvg}" '
                            f'group-title="{cat}",'
                            f'{norm_name}\n'
                        )
                        f.write(f'rtp://{entry["addr"]}\n')
                        epg_src_count += 1

        print(f"  EPG 版本: {epg_ch_count} 个频道 / {epg_src_count} 个源")

        # Print summary to console
        print("\n" + "=" * 60)
        print("测试结果摘要")
        print("=" * 60)
        total_sources = 0
        for cat in cat_order:
            chs = categories[cat]
            src_count = sum(len(all_channels_grouped.get(ch["norm_name"], [])) for ch in chs)
            total_sources += src_count
            q_counts = defaultdict(int)
            for ch in chs:
                q_counts[ch["quality"]] += 1
            q_str = ", ".join(f"{q}:{c}" for q, c in sorted(q_counts.items(), key=lambda x: -quality_sort_key(x[0])))
            print(f"  {cat:10s}: {len(chs):3d} 个频道 / {src_count:3d} 个源  ({q_str})")
        print(f"  {'合计':10s}: {total_ch:3d} 个频道 / {total_sources:3d} 个源")
        print(f"\n输出文件:")
        print(f"  {output_dir}/result.m3u      (M3U 播放列表 - 每频道最佳源)")
        print(f"  {output_dir}/result_all.m3u  (M3U 播放列表 - 聚合多源)")
        print(f"  {output_dir}/result.txt      (详细测试报告)")

        # 生成 README.md
        readme_path = f"{repo}/README.md"
        if os.path.exists(readme_path):
            with open(readme_path, encoding="utf-8") as f:
                old_content = f.read()
            # 找到分割线，保留分割线及之后的上游内容
            sep = "\n---\n"
            if sep in old_content:
                upstream_part = old_content[old_content.index(sep):]
            else:
                upstream_part = sep + "\n" + old_content


            # 构建频道统计表
            summary_rows = []
            total_ch = 0
            for cat in cat_order:
                chs = categories[cat]
                src_count = sum(len(all_channels_grouped.get(ch["norm_name"], [])) for ch in chs)
                q_counts = defaultdict(int)
                for ch in chs:
                    q_counts[ch["quality"]] += 1
                q_parts = []
                for q in ["4K", "FHD", "HD", "SD"]:
                    if q_counts[q] > 0:
                        q_parts.append(f"{q}:{q_counts[q]}")
                summary_rows.append(f"| {cat} | {len(chs)}（{src_count}） | {', '.join(q_parts)} |")
                total_ch += len(chs)
            # 按清晰度汇总
            total_q = defaultdict(int)
            for cat in cat_order:
                for ch in categories[cat]:
                    total_q[ch["quality"]] += 1
            total_q_parts = []
            for q in ["4K", "FHD", "HD", "SD"]:
                if total_q[q] > 0:
                    total_q_parts.append(f"{q}:{total_q[q]}")
            summary_rows.append(f"| **合计** | **{total_ch}（{total_sources}）** | **{', '.join(total_q_parts)}** |")

            readme_top = f"""# 广东电信 IPTV 播放列表

每天 06:00（Asia/Shanghai）同步上游频道与 EPG，全量测试频道可用性并生成优化后的 M3U 播放列表。\n\n[自动更新与维护](scripts/README.md)

## 播放列表

| 文件 | 说明 | 订阅地址（CF加速） |
|------|------|---------|
| `iptv.m3u` | 每频道保留最佳源（推荐） | `https://warp.rm.do/iptv/iptv.m3u` |
| `iptv-all.m3u` | 每频道保留所有源（聚合） | `https://warp.rm.do/iptv/iptv-all.m3u` |
| `iptv-epg.m3u` | 仅含有EPG的频道 | `https://warp.rm.do/iptv/iptv-epg.m3u` |
| `iptv-all-epg.m3u` | 仅含有EPG的频道（聚合） | `https://warp.rm.do/iptv/iptv-all-epg.m3u` |

## 使用方法

配合 [rtp2httpd](https://github.com/stackia/rtp2httpd) 使用，将 RTP 组播流转为 HTTP 单播流后，通过支持 M3U 的播放器订阅观看。聚合模式（iptv-all.m3u）推荐使用 APTV。

**本人环境**：广东电信 IPTV（OpenWrt + ipoe 拨号获取 IPTV 网络）→ rtp2httpd 组播转 HTTP 流 → APTV 播放（Apple TV）

## 频道概况

> 列表生成时间: {generated_time} | 地址: {total} | 可用: {ok_count} | 不可用: {fail_count}\n> 实际检测时间范围: {checked_first} 至 {checked_last} | 本次检测: {len(pending)} | 复用缓存: {total - len(pending)}

| 分类 | 频道数（源数） | 质量分布 |
|------|-------------|---------|
""" + "\n".join(summary_rows) + "\n"

            with outputs.open(readme_path, "w", encoding="utf-8") as f:
                f.write(readme_top + upstream_part)
            print(f"  {readme_path}  (README.md)")

        with outputs.open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "entries": cache_entries,
                       "last_success": {"source_count": ok_count, "channel_count": total_ch,
                                        "generated_at": generated_at}},
                      f, ensure_ascii=False, indent=2)

    print("\n全部产物已生成并保存。")
    return 0


def main(argv=None):
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (args.output_dir / ".iptv-probe.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("已有频道检测正在运行，请稍后重试") from None
            return run(args)
    except (OSError, ValueError, RuntimeError, ET.ParseError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
