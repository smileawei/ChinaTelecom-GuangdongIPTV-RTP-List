# 广东电信 IPTV 播放列表

基于上游频道数据，每周自动测试可用性并生成优化后的 M3U 播放列表。

## 播放列表

| 文件 | 说明 | 订阅地址 (CF加速） |
|------|------|---------|
| `iptv.m3u` | 每频道保留最佳源（推荐） | `https://warp.rm.do/iptv/iptv.m3u` |
| `iptv-all.m3u` | 每频道保留所有源（聚合） | `https://warp.rm.do/iptv/iptv-all.m3u` |
| `iptv-epg.m3u` | 仅含有EPG的频道 | `https://warp.rm.do/iptv/iptv-epg.m3u` |
| `iptv-all-epg.m3u` | 仅含有EPG的频道（聚合） | `https://warp.rm.do/iptv/iptv-all-epg.m3u` |

## 使用方法

配合 [rtp2httpd](https://github.com/stackia/rtp2httpd) 使用，将 RTP 组播流转为 HTTP 单播流后，通过支持 M3U 的播放器订阅观看。聚合模式（iptv-all.m3u）推荐使用 APTV。

**本人环境**：广东电信 IPTV（OpenWrt + ipoe 拨号获取 IPTV 网络）→ rtp2httpd 组播转 HTTP 流 → APTV 播放（Apple TV）

## 频道概况

> 最后更新: 2026-04-10 04:36 | 测试地址: 402 | 可用: 355 | 不可用: 47

| 分类 | 频道数（源数） | 质量分布 |
|------|-------------|---------|
| CCTV | 37（90） | 4K:2, FHD:33, SD:2 |
| 各省卫视 | 37（114） | 4K:9, FHD:25, SD:3 |
| 深圳频道 | 5（7） | FHD:4, SD:1 |
| 广东省级频道 | 14（25） | 4K:1, FHD:13 |
| 省级和国家级频道 | 12（16） | FHD:7, SD:5 |
| 广东地方频道 | 33（34） | FHD:33 |
| IPTV主题频道 | 21（33） | FHD:16, SD:5 |
| 其他 | 9（12） | FHD:2, SD:7 |
| **合计** | **168（331）** | **4K:12, FHD:133, SD:23** |

---

# ChinaTelecom-GuangdongIPTV-RTP-List
广州电信广东IPTV列表（组播地址）

~~因为广东IPTV抓出来的RTSP地址全部都带用户验证消息，所以RTSP的地址就不贴出来了，只有IGMP组播地址~~。<br>
现在带IGMP组播地址和不带尾巴的RTSP单播地址。<br>
抓取时间2025-12-11，清除大量SD遗留项。<br>
附带一份拿ffmpeg扫组播地址扫出来的额外的频道表，因为是对着截图手写，可能有不准确。<br>

增加一个index.php，把txt和json放一起再随便一个php环境下打开能把完整列表通过网页显示出来，并且生成udpxy链接（自行填写host和port）。<br>
本来是想写成网页播放器的，但是目前找不到一个可以播放mpegts直播流的html5播放器（鼓捣了半天videojs播不出来，生成m3u再播放也一样，估计需要手动分析metadata再生成m3u），不过就算是半成品也可以直接把链接拖进播放器播放。

增加一个GuangdongIPTV_rtp_probe.txt，是ffprobe扫一遍两个组播IP段得出来的视频/音频流数据。<br>

增加一个epg.xml，是通过扫各个网站的epg api整理出来的xmltv。其他没有的台也欢迎留言反馈下哪里能有可用的官方api来获取epg。<br>
目前使用的api/现成xmltv：<br>
- http://epg.gdtv.cn/
- https://api.cntv.cn/
- https://api.cgtn.com/
- http://epg.51zmt.top:8000/
- ~~有线电视机顶盒~~

增加GuangdongIPTV_rtp_{sd/hd/4k}.m3u，是根据ffprobe得出来的结果生成的sd/hd/4k信号播放列表。因为ffprobe有时候会获取不到流分辨率，这种情况下它会把4K或者高清台放到标清列表里，所以列表不一定准确，按实际播放的信号为准。<br>

（最近由于墙又高了定时自动commit可能会不好使。）