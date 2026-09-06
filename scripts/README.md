# 自动更新与维护

主程序在本目录中，随频道仓库一起版本管理。`/data/code/gdiptv/test_streams.py` 和
`/data/code/gdiptv/update_iptv.sh` 是兼容原命令、原 crontab 的转发入口。

依赖：Python 3（仅标准库）、FFmpeg 的 `ffprobe`、Bash、Git、OpenSSH、`flock`、cron。
PHP 页面不参与自动更新流程。

## 常用命令

```sh
# 读取上游并更新列表；有效缓存复用，过期或失败的地址按期限重新检测
/data/code/gdiptv/update_iptv.sh

# 读取上游并全量重新检测
/data/code/gdiptv/update_iptv.sh --rescan

# 只更新 EPG：不扫描频道，不重启 rtp2httpd
/data/code/gdiptv/update_iptv.sh --epg-only

# 只运行本地检测与生成，不同步、提交、推送或重启服务
python3 /data/code/gdiptv/test_streams.py

# 查看参数；不会执行更新
/data/code/gdiptv/update_iptv.sh --help
python3 /data/code/gdiptv/test_streams.py --help
```

完整更新会同步 `origin/master` 和 `upstream/master`，检测成功后更新四份列表和 README，
提交并推送 GitHub，最后通过 SSH 重启路由器上的 `rtp2httpd`。运行前应先提交或收起已跟踪文件的改动；
已有暂存内容、未完成的合并或同步失败都会停止更新。上游合并冲突只对明确列出的上游数据文件采用上游版本，
README、脚本等其他冲突会中止并撤销本次合并，留待人工处理。

## 缓存与异常保护

- 成功探测默认缓存 168 小时，失败默认在 24 小时后重试；`--rescan` 强制重测全部地址。
- 兼容原 `probe_cache.json`，旧记录以原文件修改时间作为检测时间，成功生成后升级格式。
- 当前上游已不存在的地址不会继续出现在输出中。
- 没有可用源，或可用源相比上次成功结果保留不足 70%，会非零退出，保留原缓存和播放列表。
- 最终频道数量也进行骤降检查；已有 EPG 列表的匹配频道归零或骤降同样停止发布。
  每个探测失败的地址与原因立即写入日志，保护触发后也能排查。
- 新结果先在临时目录生成，全部生成和检查完成后替换正式文件。
  每个文件采用原子替换，但多文件替换不构成文件系统事务。
- 缓存、报告中的检测时间与本次生成时间分开记录。

确认上游确实撤销了大量频道时，可显式调整保护比例，例如
`--min-retained-ratio 0.5`。先排查 IPTV 网络和 `ffprobe`，不要仅为了让定时任务通过而降低比例。
`--cache-ttl-hours`、`--retry-hours` 可调整缓存期限；所有参数以 `--help` 为准。

Python 支持 `--repo`、`--output-dir`、`--proxy-url`；默认探测代理为
`http://10.220.10.1:5140/rtp`。目录也可由环境变量 `IPTV_REPO_DIR`、`IPTV_WORK_DIR` 配置。
输出列表仍使用 RTP 地址，播放依赖本地 IPTV 网络和组播转 HTTP 服务。

## EPG 单独更新

`--epg-only` 从 `upstream/master` 提取 `epg.xml`，验证 XML、频道与近期有效节目后原子替换。
无效或过期节目单不会覆盖现有文件，也不会推送。内容相同时不重复提交。
上游偶有零时长等异常节目，时效校验容忍不超过 10% 的无效条目并记录数量；超过比例则拒绝更新。
节目单内容保持上游原样，不重写或删改其中的节目。
此模式只提交 EPG 文件，但 `git push` 会一并推送分支上已有的未推送提交；请勿将不准备发布的提交留在更新分支。

节目单与频道扫描使用同一更新锁，重叠触发时后一个任务跳过。Python 探测还使用独立锁，避免多个检测进程同时写产物。

## 定时任务

保留每周五 04:30 全量扫描，增加每天 06:15 节目单更新。时间使用系统时区，当前为 Asia/Shanghai。
节目单安排在上游通常的凌晨更新之后。

以下命令生成候选配置，保留其他定时任务，并替换旧的 IPTV 扫描配置；重复执行不会重复添加任务：

```sh
python3 /data/code/gdiptv/ChinaTelecom-GuangdongIPTV-RTP-List/scripts/install_cron.py > /tmp/gdiptv.crontab
cat /tmp/gdiptv.crontab
crontab /tmp/gdiptv.crontab
```

完整更新日志：`/data/code/gdiptv/cron.log`；节目单更新日志：`/data/code/gdiptv/epg.log`。
维护脚本自身不修改 crontab。

## 离线回归验证

```sh
cd /data/code/gdiptv/ChinaTelecom-GuangdongIPTV-RTP-List
python3 -B -m unittest discover -s tests -v
bash -n scripts/update_iptv.sh
```

测试使用临时目录、模拟探测和本地 Git 仓库，不访问真实 IPTV 网关、不发布生产列表、不重启路由器。
