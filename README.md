<div align="center">

> **AstrBot· 可视化视频面板**

# 🎬 B站刷视频

**B站刷视频插件：发 `/刷视频` 从 B站热门榜随机发一个短视频，或用 `/搜视频 关键词` 搜索后按序号发送到当前会话。**

[![版本 1.0.2](https://img.shields.io/badge/版本-1.0.2-89b4fa.svg)](CHANGELOG.md)
[![License: CC BY-NC-ND 4.0](https://img.shields.io/badge/License-CC%20BY--NC--ND%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc-nd/4.0/)
[![作者 简单么么熊](https://img.shields.io/badge/作者-简单么么熊-f5b7c7.svg)](https://github.com/1023286457)
[![QQ群 1081896895](https://img.shields.io/badge/QQ群-1081896895-12B7F5.svg)](https://qm.qq.com/q/LWmwpA9kuk)

<img src="https://count.getloli.com/@1023286457_bili_video?name=1023286457_bili_video&amp;theme=green&amp;padding=7&amp;offset=0&amp;align=top&amp;scale=1&amp;pixelated=1&amp;darkmode=auto" alt="访问计数" />

[功能总览](#overview) · [安装](#install) · [指令](#commands) · [配置项](#config) · [支持平台](#platforms) · [工作原理](#how) · [已知限制](#limits) · [更新日志](CHANGELOG.md)

</div>

---

<a id="overview"></a>

## 🧭 功能总览

发送 `/刷视频`，机器人会从 **B站热门榜**随机挑一个短视频（默认不超过 3 分钟）下载后发到当前会话；也可以用 `/搜视频 关键词` 按名字搜索，例如 `/搜视频 三角洲行动`。

- 无需 B站账号 / API Key / Cookie
- 无需 yt-dlp、无需 ffmpeg（QQ 官方路径）
- 无第三方 Python 依赖
- 支持 QQ 官方、aiocqhttp / OneBot、钉钉

---

<a id="install"></a>

## 📦 安装

把插件目录放到 AstrBot 的：

```text
data/plugins/astrbot_plugin_bili_video
```

重载插件即可。

---

<a id="commands"></a>

## ⌨️ 指令

| 指令 | 说明 |
| --- | --- |
| `刷视频`（别名 `随机视频`） | 从热门榜随机挑一个短视频：先回复视频信息（标题/UP主/时长/播放量/链接），再发视频文件 |
| `搜视频 关键词` | 在B站搜索关键词（支持含空格），回复编号列表（前 10 条：标题 - UP主（时长）），**回复序号**即下载发送对应视频；回复 `取消` 可放弃。与点歌插件同款交互 |
| `切换发送模式`（别名 `切换模式`） | 仅钉钉可用：在视频发送模式和文件发送模式之间切换，自动写回配置，并回复 `已切换到文件发送模式` / `已切换到视频发送模式` |

> 搜视频等待期间，该会话的新消息会被本插件接管（用于接收序号），超时后自动恢复；等待时长可在配置里调。

---

<a id="config"></a>

## ⚙️ 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `max_duration_seconds` | 180 | 刷视频的时长上限（秒）；搜视频由用户自选，不受此限制 |
| `wait_seconds` | 60 | 搜视频列表等待序号的时长 |
| `popular_pages` | 4 | 热门榜抓取页数（每页 50 条，作为随机池） |
| `max_file_mb` | 90 | 单视频体积上限；视频消息模式下，超限的选中视频会提示并放弃 |
| `max_file_send_mb` | 90 | 单文件视频体积上限；钉钉文件发送模式下独立使用，和视频消息上限分开 |
| `dingtalk_send_mode` | `video` | 钉钉发送模式：`video` 原生视频 / `file` mp4 文件卡片；可用 `/切换发送模式` 切换 |
| `max_retries` | 3 | 刷视频解析/下载失败时自动换片重试次数 |
| `download_timeout_seconds` | 120 | 下载超时 |
| `cooldown_seconds` | 20 | 同会话冷却（刷视频/搜视频共用），防止刷屏触发 QQ 频控（仅成功后计时） |

---

<a id="platforms"></a>

## 🖥️ 支持平台

| 平台 | 说明 |
| --- | --- |
| QQ 官方（含 webhook） | 走官方媒体上传接口发视频，无需任何额外组件 |
| aiocqhttp / OneBot | 原生视频消息 |
| 钉钉 | 默认发送原生视频消息，需要适配器用 ffmpeg 抽封面：**插件会自动准备 ffmpeg**（pip 依赖 `imageio-ffmpeg` 自带各平台静态 ffmpeg，装插件时自动安装，无需系统安装、无需管理员权限）；自动准备不可用时自动降级为文件消息（mp4 文件卡片，钉钉内可直接预览播放）。也可以发送 `/切换发送模式` 固定切换为文件模式 |

其他平台回复「此平台暂不支持刷视频」。

---

<a id="how"></a>

## 🔧 工作原理

1. 拉取 B站热门榜（默认 4 页 × 50 条，结果缓存 10 分钟），筛出时长达标的候选
2. 随机挑一条，通过 `playurl?platform=html5` 通道获取**带声音的 MP4 直链**（未登录最高 480P）
3. 流式下载到临时目录（带体积上限保护），用 AstrBot 的 `Video` 组件发送
4. 发送完成后延迟清理临时文件

`搜视频` 的额外流程：先访问主站首页种 buvid3 Cookie，再从 nav 接口取 WBI 签名密钥（缓存 1 小时），对搜索接口做 `w_rid/wts` 签名（未签名的搜索会被 HTTP 412 风控拦截）；搜索结果不含视频 cid，选片后通过详情接口补全并校验真实时长，再走同样的 MP4 直链下载。

---

<a id="limits"></a>

## ⚠️ 已知限制

- B站网页接口为非官方接口，目前免登录可用；若 B站调整风控或接口，需要跟随更新
- 搜索接口需要 WBI 签名 + Cookie，触发风控（HTTP 412）时插件会提示“B站搜索被限流”，一般几分钟后自动恢复
- 随机内容不可控：热门榜内容一般很正经，但偶尔可能刷到你不想要的类型
- QQ 官方平台对视频消息有官方限制（体积/频控），本插件默认参数已留足余量；如遇官方风控提示请调大 `cooldown_seconds`
- 钉钉：原生视频消息必须附带封面图（钉钉接口硬性要求），适配器用 ffmpeg 抽帧。插件通过 pip 依赖 `imageio-ffmpeg` 自动提供静态 ffmpeg（装插件时自动安装）；若 pip 环境异常导致自动供给失败，会自动降级为 mp4 文件消息（钉钉内可直接预览播放）。手动在 AstrBot 主机安装系统 ffmpeg 可获得最快、最稳的体验

---

## 📄 许可证

本项目采用 **CC BY-NC-ND 4.0** 许可：

- 允许非商业分享未修改版本，并注明作者
- 不允许商业使用、售卖或收费分发
- 不允许分发修改版或编译衍生版
- 私有修改可以保留，但不得分发

完整条款见 [LICENSE](LICENSE) 或 [Creative Commons 官方说明](https://creativecommons.org/licenses/by-nc-nd/4.0/)。
