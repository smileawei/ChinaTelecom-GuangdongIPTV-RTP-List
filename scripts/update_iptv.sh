#!/usr/bin/env bash
# 全量频道维护或仅更新节目单；所有路径均可迁移。
set -euo pipefail

usage() {
    cat <<'HELP'
用法: update_iptv.sh [--rescan] [频道检测参数...]
      update_iptv.sh --epg-only
      update_iptv.sh --help

默认先同步 origin/master 和 upstream/master，再检测、生成、推送播放列表并重启 rtp2httpd。
--epg-only  仅取上游 epg.xml，验证节目时效后更新、提交和推送；不检测频道、不重启服务。
--rescan    忽略探测缓存，重新检测全部频道。
其余参数传给 scripts/test_streams.py，可用该脚本的 --help 查看。
仓库及输出目录请用下方环境变量设置；本入口不接受 --repo 或 --output-dir。

环境变量:
  IPTV_REPO_DIR  数据仓库路径，默认本脚本的上级目录
  IPTV_WORK_DIR  缓存和检测结果目录，默认数据仓库的上级目录
  IPTV_ROUTER    rtp2httpd 所在 SSH 主机，默认 10.220.10.1

两种模式共用非阻塞锁；已有任务运行时返回非零。
同步、验证或检测失败都会停止本次发布。仓库必须在 master 分支且没有已跟踪文件的改动。
HELP
}

EPG_ONLY=0
PROBE_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --help|-h) usage; exit 0 ;;
        --epg-only) EPG_ONLY=1 ;;
        --repo|--repo=*|--output-dir|--output-dir=*)
            echo "错误: 请使用 IPTV_REPO_DIR / IPTV_WORK_DIR 环境变量设置目录，不能传入 $arg。" >&2
            exit 2
            ;;
        *) PROBE_ARGS+=("$arg") ;;
    esac
done
if (( EPG_ONLY && ${#PROBE_ARGS[@]} )); then
    echo "错误: --epg-only 不能与频道检测参数一起使用。" >&2
    exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${IPTV_REPO_DIR:-$(dirname -- "$SCRIPT_DIR")}"
REPO_DIR="$(cd -- "$REPO_DIR" && pwd)"
WORK_DIR="${IPTV_WORK_DIR:-$(dirname -- "$REPO_DIR")}"
UPSTREAM_URL="https://github.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List.git"
ROUTER="${IPTV_ROUTER:-10.220.10.1}"
mkdir -p -- "$WORK_DIR"
WORK_DIR="$(cd -- "$WORK_DIR" && pwd)"
cd -- "$REPO_DIR"

# 锁绑定仓库而非输出目录，避免不同环境变量导致并发修改同一仓库。
GIT_COMMON_DIR="$(git rev-parse --git-common-dir)"
exec 9>"${GIT_COMMON_DIR}/iptv-update.lock"
if ! flock -n 9; then
    echo "错误: 已有 IPTV 更新任务正在运行，本次退出。" >&2
    exit 1
fi

EPG_TEMP=""
MERGE_STARTED=0
cleanup() {
    local status=$?
    if (( MERGE_STARTED )) && git rev-parse --verify --quiet MERGE_HEAD >/dev/null; then
        git merge --abort || echo "错误: 无法自动撤销合并，请检查仓库。" >&2
    fi
    if [[ -n "$EPG_TEMP" ]]; then
        rm -f -- "$EPG_TEMP"
    fi
    exit "$status"
}
trap cleanup EXIT

if [[ "$(git symbolic-ref --quiet --short HEAD)" != master ]]; then
    echo "错误: 请在 master 分支执行更新。" >&2
    exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "错误: 仓库存在已跟踪文件的未提交或暂存改动，请先处理后再更新。" >&2
    exit 1
fi
for state in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
    if [[ -e "$(git rev-parse --git-path "$state")" ]]; then
        echo "错误: 仓库存在未完成的 Git 操作 ($state)，请先处理。" >&2
        exit 1
    fi
done

git remote get-url origin >/dev/null
if ! git remote get-url upstream >/dev/null 2>&1; then
    git remote add upstream "$UPSTREAM_URL"
fi

push_if_ahead() {
    # 即使播放列表未改变，同步上游产生的提交、上次推送失败留下的提交仍需发布。
    if ! git show-ref --verify --quiet refs/remotes/origin/master ||
        [[ "$(git rev-list --count origin/master..HEAD)" -gt 0 ]]; then
        git push origin master
        echo "  推送成功"
    else
        echo "  没有待推送的提交"
    fi
}

if (( EPG_ONLY )); then
    echo "[EPG] 获取上游节目单..."
    git fetch upstream master
    EPG_TEMP="$(mktemp "${WORK_DIR}/.epg-upstream.XXXXXX")"
    git show upstream/master:epg.xml >"$EPG_TEMP"
    python3 "${SCRIPT_DIR}/update_epg.py" "$EPG_TEMP" "${REPO_DIR}/epg.xml"
    if ! git diff --quiet -- epg.xml; then
        git add -- epg.xml
        git commit -m "更新 EPG 节目单 $(date '+%Y-%m-%d %H:%M')" -- epg.xml
    fi
    push_if_ahead
    echo "节目单更新完成。"
    exit 0
fi

echo "[1/4] 同步频道数据仓库..."
git fetch origin master
git merge --ff-only origin/master
git fetch upstream master
MERGE_STARTED=1
if ! git merge --no-edit upstream/master; then
    CONFLICT_FILES=()
    mapfile -d '' -t CONFLICT_FILES < <(git diff --name-only --diff-filter=U -z)
    if (( ${#CONFLICT_FILES[@]} == 0 )); then
        echo "错误: 上游合并失败，停止更新。" >&2
        exit 1
    fi
    # 仅这些上游数据允许冲突时采用上游版本。脚本、README 等冲突须人工处理。
    for file in "${CONFLICT_FILES[@]}"; do
        case "$file" in
            GuangdongIPTV_rtp.m3u8|GuangdongIPTV_rtp_4k.m3u|GuangdongIPTV_rtp_all.m3u|GuangdongIPTV_rtp_chid.m3u|GuangdongIPTV_rtp_ext.txt|GuangdongIPTV_rtp_hd.m3u|GuangdongIPTV_rtp_probe.txt|GuangdongIPTV_rtp_sd.m3u|IPTV.json|epg.xml) ;;
            *) echo "错误: 上游合并在非数据文件 $file 中产生冲突，撤销合并并停止。" >&2; exit 1 ;;
        esac
    done
    for file in "${CONFLICT_FILES[@]}"; do
        if git cat-file -e "upstream/master:$file" 2>/dev/null; then
            git checkout --theirs -- "$file"
            git add -- "$file"
        else
            git rm -- "$file"
        fi
    done
    git commit --no-edit -m "合并上游更新，冲突的数据文件采用上游版本"
fi
MERGE_STARTED=0

echo "[2/4] 检测频道并生成播放列表..."
# 固定目录放在最后，连 argparse 允许的参数缩写也不能覆盖编排层的目录。
python3 "${SCRIPT_DIR}/test_streams.py" "${PROBE_ARGS[@]}" --repo "$REPO_DIR" --output-dir "$WORK_DIR"

# 检测进程必须成功且全部文件存在，才开始复制发布文件。
OUTPUTS=(result.m3u result_all.m3u result_epg.m3u result_all_epg.m3u)
PLAYLISTS=(iptv.m3u iptv-all.m3u iptv-epg.m3u iptv-all-epg.m3u)
for output in "${OUTPUTS[@]}"; do
    if [[ ! -s "${WORK_DIR}/${output}" ]]; then
        echo "错误: 检测结果缺失或为空: $output，停止发布。" >&2
        exit 1
    fi
done
for i in "${!OUTPUTS[@]}"; do
    cp -- "${WORK_DIR}/${OUTPUTS[$i]}" "${REPO_DIR}/${PLAYLISTS[$i]}"
done

echo "[3/4] 提交并推送更新..."
PUBLISH_FILES=("${PLAYLISTS[@]}")
if [[ -f README.md ]]; then
    PUBLISH_FILES+=(README.md)
fi
git add -- "${PUBLISH_FILES[@]}"
if ! git diff --cached --quiet; then
    git commit -m "更新 IPTV 播放列表 $(date '+%Y-%m-%d %H:%M')" -- "${PUBLISH_FILES[@]}"
else
    echo "  播放列表无变化"
fi
push_if_ahead

echo "[4/4] 重启 rtp2httpd..."
ssh -o BatchMode=yes -o ConnectTimeout=10 "$ROUTER" /etc/init.d/rtp2httpd restart
echo "更新完成。"
