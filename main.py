"""AstrBot 插件：发送 /刷视频，从B站热门榜随机挑一个短视频发到当前会话。"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain, Video
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .bili import BiliApiError, BiliVideo, BiliVideoClient

PLUGIN_NAME = "astrbot_plugin_bili_video"
PLUGIN_VERSION = "1.0.1"
SUPPORTED_PLATFORMS = {"qq_official", "qq_official_webhook", "dingtalk", "aiocqhttp"}
DINGTALK_PLATFORM = "dingtalk"
SEARCH_LIST_SIZE = 10
# 发送完成后延迟删除临时视频，给平台适配器留足上传时间。
TEMP_DELETE_DELAY_SECONDS = 180.0
# 启动/每次指令时清理超过 1 小时的遗留临时文件。
TEMP_KEEP_SECONDS = 3600.0


def _format_length(seconds: int) -> str:
    """把秒数格式化成 "3分12秒" / "1小时02分33秒"。"""
    minutes, sec = divmod(max(int(seconds or 0), 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分{sec:02d}秒"
    if minutes:
        return f"{minutes}分{sec:02d}秒"
    return f"{sec}秒"


def _safe_file_name(title: str, bvid: str) -> str:
    """把视频标题清理成合法文件名（钉钉文件消息展示用）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(title or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:40].strip()
    return f"{cleaned or bvid}.mp4"


@register(
    PLUGIN_NAME,
    "简单么么熊",
    "发送 /刷视频 从B站热门榜随机挑一个短视频发到当前会话",
    PLUGIN_VERSION,
)
class BiliVideoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.client = BiliVideoClient()
        self._cooldowns: dict[str, float] = {}
        self._last_bvid: dict[str, str] = {}
        self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self._bin_dir = self._data_dir / "bin"
        self._temp_dir = self._data_dir / "temp"
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._prime_ffmpeg_path()
        self._cleanup_temp()

    def terminate(self) -> None:
        self._cleanup_temp(force=True)

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _int_cfg(self, key: str, default: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _configure_client(self) -> tuple[int, int, int, int]:
        """按当前配置刷新客户端参数，返回常用配置的元组。"""
        max_retries = self._int_cfg("max_retries", 3)
        max_file_mb = self._int_cfg("max_file_mb", 90)
        cooldown = self._int_cfg("cooldown_seconds", 20)
        download_timeout = self._int_cfg("download_timeout_seconds", 120)
        self.client.configure(
            pages=self._int_cfg("popular_pages", 4),
            max_duration=self._int_cfg("max_duration_seconds", 180),
        )
        return max_retries, max_file_mb, cooldown, download_timeout

    # ------------------------------------------------------------------
    # ffmpeg 自动供给（钉钉原生视频消息需要适配器调用 ffmpeg 抽封面）
    # ------------------------------------------------------------------
    def _prime_ffmpeg_path(self) -> None:
        """插件加载时，若此前已准备过 pip 版 ffmpeg，直接挂回 PATH。"""
        exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        if not (self._bin_dir / exe).exists():
            return
        bin_str = str(self._bin_dir)
        if bin_str not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = bin_str + os.pathsep + os.environ.get("PATH", "")

    def _pip_ffmpeg_sync(self) -> bool:
        """用 pip 依赖 imageio-ffmpeg 自带的静态 ffmpeg 建立可用的 ffmpeg 命令。

        imageio-ffmpeg 由 requirements.txt 声明，AstrBot 安装/更新插件时自动
        pip 安装；其 wheel 内含各平台静态 ffmpeg 二进制，无需系统安装、无需
        管理员权限。复制为标准名字并挂到当前进程 PATH 头部后，适配器的
        ``create_subprocess_exec("ffmpeg", ...)`` 即可找到。
        """
        exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        target = self._bin_dir / exe
        if not target.exists():
            try:
                import imageio_ffmpeg

                source = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception as exc:
                logger.warning(
                    "[%s] 自动获取 ffmpeg 失败（可手动在 AstrBot 主机安装 ffmpeg）："
                    "%s",
                    PLUGIN_NAME,
                    exc,
                )
                return False
            if not source or not Path(source).exists():
                return False
            try:
                self._bin_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            except Exception as exc:
                logger.warning("[%s] 复制 ffmpeg 失败：%s", PLUGIN_NAME, exc)
                return False
        bin_str = str(self._bin_dir)
        if bin_str not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = bin_str + os.pathsep + os.environ.get("PATH", "")
        return True

    async def _ensure_ffmpeg(self) -> bool:
        """确保 ffmpeg 可用：优先系统安装，其次 pip 静态版（限时 90 秒）。"""
        if shutil.which("ffmpeg"):
            return True
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._pip_ffmpeg_sync), timeout=90.0
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    async def _download_video(
        self,
        video: BiliVideo,
        dest: Path,
        *,
        max_file_mb: int,
        download_timeout: int,
    ) -> None:
        """解析 MP4 直链并下载到 dest；体积超限抛 BiliApiError。"""
        url, size = await self.client.resolve(video)
        max_bytes = max_file_mb * 1048576
        if size and size > max_bytes:
            raise BiliApiError(
                f"视频体积约 {size / 1048576:.0f}MB，超过上限 {max_file_mb}MB"
            )
        await self.client.download(
            url, dest, max_bytes=max_bytes, timeout=download_timeout
        )

    async def _video_chain(
        self, event: AstrMessageEvent, video: BiliVideo, dest: Path
    ):
        """构造视频消息链。

        钉钉的原生视频消息必须附带封面图（适配器用 ffmpeg 从视频抽帧）。
        主机没装 ffmpeg 时会先尝试 pip 静态 ffmpeg（自动供给）；仍不可用才
        降级为文件消息（mp4 文件卡片，钉钉内可直接预览播放）。
        """
        if (
            event.get_platform_name() == DINGTALK_PLATFORM
            and shutil.which("ffmpeg") is None
        ):
            if await self._ensure_ffmpeg():
                logger.info(
                    "[%s] 已启用 pip 静态 ffmpeg，钉钉将发送原生视频消息", PLUGIN_NAME
                )
            else:
                logger.info(
                    "[%s] ffmpeg 不可用，钉钉降级为文件消息", PLUGIN_NAME
                )
                return event.chain_result(
                    [File(name=_safe_file_name(video.title, video.bvid), file=str(dest))]
                )
        return event.chain_result([Video.fromFileSystem(str(dest))])

    # ------------------------------------------------------------------
    # 消息构造
    # ------------------------------------------------------------------
    @staticmethod
    def _text_result(event: AstrMessageEvent, text: str):
        """发送纯文本并强制关闭文本转图片。

        AstrBot 全局开启「文本转图片」(t2i) 时，超过字数阈值的纯文本会被
        渲染成图片发送；本插件的列表/提示始终以文字呈现，故逐条显式关闭。
        """
        result = event.plain_result(text)
        result.use_t2i_ = False
        return result

    # ------------------------------------------------------------------
    # 临时文件
    # ------------------------------------------------------------------
    def _cleanup_temp(self, force: bool = False) -> None:
        now = time.time()
        try:
            for path in self._temp_dir.glob("*.mp4"):
                try:
                    if force or now - path.stat().st_mtime > TEMP_KEEP_SECONDS:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            logger.debug("[%s] 清理临时目录失败", PLUGIN_NAME)

    async def _delayed_unlink(self, path: Path, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            path.unlink(missing_ok=True)
        except Exception:
            logger.debug("[%s] 延迟清理临时视频失败：%s", PLUGIN_NAME, path)

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("刷视频", alias={"随机视频"})
    async def random_video_command(self, event: AstrMessageEvent) -> None:
        platform = event.get_platform_name()
        if platform not in SUPPORTED_PLATFORMS:
            yield self._text_result(event, "此平台暂不支持刷视频。")
            event.stop_event()
            return

        umo = event.unified_msg_origin
        max_retries, max_file_mb, cooldown, download_timeout = self._configure_client()
        self._cleanup_temp()

        remaining = int(cooldown - (time.monotonic() - self._cooldowns.get(umo, 0.0)))
        if remaining > 0:
            yield self._text_result(event, f"刷视频冷却中，{remaining} 秒后再来~")
            event.stop_event()
            return

        tried: set[str] = set()
        last_error = ""
        for _attempt in range(max_retries):
            try:
                video = await self.client.pick(
                    tried=tried, exclude_bvid=self._last_bvid.get(umo)
                )
            except Exception as exc:
                last_error = f"获取B站热门榜失败：{exc}"
                logger.warning("[%s] %s", PLUGIN_NAME, last_error)
                break
            tried.add(video.bvid)
            dest = self._temp_dir / f"bili_{uuid.uuid4().hex[:12]}_{video.bvid}.mp4"
            try:
                await self._download_video(
                    video,
                    dest,
                    max_file_mb=max_file_mb,
                    download_timeout=download_timeout,
                )
            except Exception as exc:
                dest.unlink(missing_ok=True)
                last_error = str(exc)
                logger.warning(
                    "[%s] 候选视频 %s 失败：%s", PLUGIN_NAME, video.bvid, exc
                )
                continue

            # 成功才记录冷却与去重，失败不占用冷却次数。
            self._last_bvid[umo] = video.bvid
            self._cooldowns[umo] = time.monotonic()
            try:
                yield self._text_result(event, f"从B站给你刷到一个：{video.describe()}")
                yield await self._video_chain(event, video, dest)
            finally:
                asyncio.create_task(self._delayed_unlink(dest, TEMP_DELETE_DELAY_SECONDS))
            event.stop_event()
            return

        yield self._text_result(event, 
            f"刷视频失败了：{last_error or '未知原因'}，稍后再试吧。"
        )
        event.stop_event()

    @filter.command("搜视频")
    async def search_video_command(self, event: AstrMessageEvent) -> None:
        """按关键词搜索B站视频，回复序号下载：搜视频 三角洲行动。"""
        platform = event.get_platform_name()
        if platform not in SUPPORTED_PLATFORMS:
            yield self._text_result(event, "此平台暂不支持搜视频。")
            event.stop_event()
            return

        text = str(event.get_message_str() or "").strip()
        keyword = text[len("搜视频") :].strip() if text.startswith("搜视频") else ""
        if not keyword:
            yield self._text_result(event, "用法：搜视频 <关键词>，例如：搜视频 三角洲行动")
            event.stop_event()
            return

        umo = event.unified_msg_origin
        self._configure_client()
        max_file_mb = self._int_cfg("max_file_mb", 90)
        download_timeout = self._int_cfg("download_timeout_seconds", 120)
        wait_seconds = self._int_cfg("wait_seconds", 60)
        cooldown = self._int_cfg("cooldown_seconds", 20)
        self._cleanup_temp()

        remaining = int(cooldown - (time.monotonic() - self._cooldowns.get(umo, 0.0)))
        if remaining > 0:
            yield self._text_result(event, f"搜视频冷却中，{remaining} 秒后再来~")
            event.stop_event()
            return

        yield self._text_result(event, f"正在B站搜索《{keyword}》…")
        try:
            pool = await self.client.search(keyword)
        except Exception as exc:
            logger.warning("[%s] 搜索《%s》失败：%s", PLUGIN_NAME, keyword, exc)
            yield self._text_result(event, f"B站搜索失败了：{exc}")
            event.stop_event()
            return
        if not pool:
            yield self._text_result(event, f"没搜到《{keyword}》的相关视频，换个关键词试试？")
            event.stop_event()
            return

        listing = pool[:SEARCH_LIST_SIZE]
        lines = [
            f"{index}. {video.title} - {video.author}（{_format_length(video.duration)}）"
            for index, video in enumerate(listing, 1)
        ]
        yield self._text_result(event, 
            "【B站视频】\n"
            + "\n".join(lines)
            + f"\n\n请回复序号选择 1~{len(listing)}，{wait_seconds} 秒内有效；"
            "回复 取消 结束。"
        )

        choice: int | None = None
        cancelled = False

        @session_waiter(timeout=wait_seconds, record_history_chains=False)
        async def waiter(controller: SessionController, ev: AstrMessageEvent) -> None:
            nonlocal choice, cancelled
            reply = str(ev.get_message_str() or "").strip()
            if reply in {"取消", "退出", "取消搜索"}:
                cancelled = True
                ev.stop_event()
                controller.stop()
                return
            if not reply.isdigit():
                # 普通消息不响应，等待期间由管线拦截。
                return
            number = int(reply)
            if not 1 <= number <= len(listing):
                try:
                    await ev.send(
                        self._text_result(
                            ev, f"序号超出范围，请输入 1~{len(listing)}"
                        )
                    )
                except Exception:
                    pass
                return
            choice = number
            ev.stop_event()
            controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            yield self._text_result(event, "输入超时，本次搜索已取消。")
            event.stop_event()
            return
        if cancelled:
            yield self._text_result(event, "已取消。")
            event.stop_event()
            return
        if choice is None:
            yield self._text_result(event, "没有收到有效选择，已结束。")
            event.stop_event()
            return

        video = listing[choice - 1]
        yield self._text_result(event, f"好的，正在下载《{video.title}》…")
        dest = self._temp_dir / f"bili_{uuid.uuid4().hex[:12]}_{video.bvid}.mp4"
        try:
            # 搜索结果没有 cid，先用详情接口补全（顺带拿到真实时长）。
            video = await self.client.fetch_details(video)
            await self._download_video(
                video,
                dest,
                max_file_mb=max_file_mb,
                download_timeout=download_timeout,
            )
        except Exception as exc:
            dest.unlink(missing_ok=True)
            logger.warning(
                "[%s] 下载选中视频 %s 失败：%s", PLUGIN_NAME, video.bvid, exc
            )
            yield self._text_result(event, f"下载《{video.title}》失败了：{exc}")
            event.stop_event()
            return

        # 成功才记录冷却，避免失败占用冷却次数。
        self._last_bvid[umo] = video.bvid
        self._cooldowns[umo] = time.monotonic()
        try:
            yield self._text_result(event, f"从B站给你搜到一个：{video.describe()}")
            yield await self._video_chain(event, video, dest)
        finally:
            asyncio.create_task(self._delayed_unlink(dest, TEMP_DELETE_DELAY_SECONDS))
        event.stop_event()
