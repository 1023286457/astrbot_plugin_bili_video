"""B站随机/搜索短视频客户端。

只用 B站公开网页接口（无需登录、无需 API Key）：
- 热门榜列表：GET /x/web-interface/popular?ps=50&pn={page}
- 搜索：GET /x/web-interface/search/type?search_type=video&keyword=...
  （需要 WBI 签名 w_rid/wts + buvid3 Cookie，否则返回 HTTP 412 风控）
- 视频详情：GET /x/web-interface/view?bvid=...（搜索结果不含 cid，用它补）
- 播放地址：GET /x/player/playurl?bvid=..&cid=..&platform=html5&high_quality=1

``platform=html5`` 通道会直接返回**带声音的 MP4 直链**（未登录最高 480P），
因此下载完即可发送，无需 ffmpeg 合成音视频。

Cookie/WBI 签名流程：先访问主站首页种 buvid3，再从 nav 接口取 WBI 密钥
（未登录时 code=-101 但 ``wbi_img`` 字段有效），密钥缓存 1 小时。

所有网络请求都是阻塞式的，统一由调用方放进线程执行
（``asyncio.to_thread``），本模块不依赖任何第三方库。
"""

from __future__ import annotations

import asyncio
import hashlib
import http.cookiejar
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BILI_HEADERS = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
HOMEPAGE_URL = "https://www.bilibili.com/"
POPULAR_API = "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={page}"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
DOWNLOAD_CHUNK_SIZE = 256 * 1024
# 热门榜里偶尔混入几秒的碎片视频，太短的没有"刷"的价值。
MIN_DURATION_SECONDS = 5
POOL_CACHE_SECONDS = 600.0
WBI_KEY_CACHE_SECONDS = 3600.0
SEARCH_PAGE_SIZE = 20
# 搜索关键词的最长长度，防止异常输入拼出超长 URL。
MAX_KEYWORD_LENGTH = 64

# WBI 签名的固定置换表（bilibili-API-collect 公开算法）。
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)


class BiliApiError(RuntimeError):
    """B站接口返回异常或内容不符合预期。"""


@dataclass(slots=True)
class BiliVideo:
    bvid: str
    title: str
    duration: int
    author: str
    views: int
    cid: int = 0

    @property
    def web_url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    def describe(self) -> str:
        minutes, seconds = divmod(max(self.duration, 0), 60)
        length = f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"
        views = self.views
        if views >= 100000000:
            view_text = f"{views / 100000000:.1f}亿"
        elif views >= 10000:
            view_text = f"{views / 10000:.1f}万"
        else:
            view_text = str(views)
        return f"《{self.title}》\nUP：{self.author} · {length} · {view_text}播放"


def _strip_keyword_markup(text: object) -> str:
    """去掉搜索结果标题里的 <em class="keyword"> 高亮标签。"""
    return re.sub(r"</?em[^>]*>", "", str(text or "")).strip()


def _parse_duration_text(text: object) -> int:
    """把 "0:37" / "4:49" / "1:02:33" 解析成秒；解析失败返回 0。"""
    parts = str(text or "").strip().split(":")
    if not parts or not all(part.isdigit() for part in parts) or len(parts) > 3:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _get_json(url: str, timeout: float, opener=None) -> dict:
    request = urllib.request.Request(url, headers=BILI_HEADERS)
    open_url = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise BiliApiError(
                "B站接口被限流(HTTP 412)，请过几分钟再试"
            ) from exc
        raise BiliApiError(f"B站接口请求失败：HTTP {exc.code}") from exc


def _download_to(url: str, dest: Path, timeout: float, max_bytes: int) -> int:
    request = urllib.request.Request(url, headers=BILI_HEADERS)
    written = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        with dest.open("wb") as target:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if max_bytes and written > max_bytes:
                    raise BiliApiError("视频下载超过体积上限，已中止")
                target.write(chunk)
    if written < 1024:
        raise BiliApiError("下载内容过小，疑似失败")
    return written


def _resolve_play_url(bvid: str, cid: int, timeout: float) -> tuple[str, int]:
    """返回 (MP4 直链, 文件字节数)。"""
    data = _get_json(
        f"{PLAYURL_API}?bvid={bvid}&cid={cid}&qn=64&platform=html5&high_quality=1",
        timeout,
    )
    if data.get("code") != 0:
        message = str(data.get("message") or "").strip()
        raise BiliApiError(f"播放地址接口返回 code={data.get('code')} {message}".strip())
    durl = (data.get("data") or {}).get("durl") or []
    if len(durl) != 1:
        raise BiliApiError("该视频没有单一 MP4 直链（分多段或受限制），已跳过")
    link = str(durl[0].get("url") or "")
    if not link:
        raise BiliApiError("播放地址为空")
    if link.startswith("http://"):
        link = "https://" + link[len("http://") :]
    try:
        size = int(durl[0].get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return link, size


class BiliVideoClient:
    """带热门榜缓存与 WBI 签名的随机/搜索视频挑选器。

    阻塞实现 + ``asyncio.to_thread`` 门面；Cookie 持久保存在实例的
    CookieJar 里（buvid3 种一次一直有效）。
    """

    def __init__(self) -> None:
        self.pages = 4
        self.max_duration = 180
        self.api_timeout = 15.0
        self._pool: list[BiliVideo] = []
        self._pool_monotonic = 0.0
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._cookies_warmed = False
        self._mixin_key = ""
        self._mixin_monotonic = 0.0

    def configure(
        self, *, pages: int, max_duration: int, cache_seconds: float = POOL_CACHE_SECONDS
    ) -> None:
        self.pages = max(1, min(int(pages), 10))
        self.max_duration = max(30, int(max_duration))
        self._cache_seconds = max(60.0, float(cache_seconds))

    # ------------------------------------------------------------------
    # Cookie 与 WBI 签名
    # ------------------------------------------------------------------
    def _warm_cookies(self) -> None:
        """访问主站首页种 buvid3 Cookie，显著降低搜索接口被风控的概率。"""
        if self._cookies_warmed:
            return
        request = urllib.request.Request(HOMEPAGE_URL, headers=BILI_HEADERS)
        try:
            with self._opener.open(request, timeout=self.api_timeout) as response:
                response.read(4096)
        except Exception as exc:
            raise BiliApiError(f"B站首页访问失败：{exc}") from exc
        self._cookies_warmed = True

    def _wbi_mixin_key(self) -> str:
        now = time.monotonic()
        if self._mixin_key and now - self._mixin_monotonic < WBI_KEY_CACHE_SECONDS:
            return self._mixin_key
        data = _get_json(NAV_API, self.api_timeout, self._opener)
        # 未登录时 nav 返回 code=-101，但 wbi_img 字段仍然有效。
        wbi = (data.get("data") or {}).get("wbi_img") or {}
        img_key = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if len(img_key) < 16 or len(sub_key) < 16:
            raise BiliApiError("获取 WBI 签名密钥失败")
        combined = img_key + sub_key
        self._mixin_key = "".join(combined[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        self._mixin_monotonic = now
        return self._mixin_key

    def _sign_params(self, params: dict) -> dict:
        cleaned = {
            key: "".join(ch for ch in str(value) if ch not in "!'()*")
            for key, value in params.items()
        }
        cleaned["wts"] = str(int(time.time()))
        query = urllib.parse.urlencode(sorted(cleaned.items()))
        cleaned["w_rid"] = hashlib.md5((query + self._wbi_mixin_key()).encode()).hexdigest()
        return cleaned

    # ------------------------------------------------------------------
    # 热门榜随机
    # ------------------------------------------------------------------
    def _fetch_popular_page(self, page: int) -> list[BiliVideo]:
        data = _get_json(POPULAR_API.format(page=page), self.api_timeout)
        if data.get("code") != 0:
            message = str(data.get("message") or "").strip()
            raise BiliApiError(
                f"热门榜接口返回 code={data.get('code')} {message}".strip()
            )
        videos: list[BiliVideo] = []
        for item in (data.get("data") or {}).get("list") or []:
            try:
                videos.append(
                    BiliVideo(
                        bvid=str(item["bvid"]),
                        cid=int(item["cid"]),
                        title=str(item.get("title") or "").strip(),
                        duration=int(item.get("duration") or 0),
                        author=str((item.get("owner") or {}).get("name") or "").strip(),
                        views=int((item.get("stat") or {}).get("view") or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return videos

    def _refresh_pool(self) -> None:
        now = time.monotonic()
        if self._pool and now - self._pool_monotonic < self._cache_seconds:
            return
        collected: list[BiliVideo] = []
        first_error: Exception | None = None
        for page in range(1, self.pages + 1):
            try:
                collected.extend(self._fetch_popular_page(page))
            except Exception as exc:  # 一页失败就停，避免空转
                if first_error is None:
                    first_error = exc
                break
        if collected:
            self._pool = collected
            self._pool_monotonic = now
        elif first_error is not None:
            raise first_error
        else:
            raise BiliApiError("热门榜返回为空")

    @staticmethod
    def _choose(candidates: list[BiliVideo], exclude_bvid: str | None) -> BiliVideo:
        if exclude_bvid and len(candidates) > 1:
            narrowed = [v for v in candidates if v.bvid != exclude_bvid]
            if narrowed:
                candidates = narrowed
        return random.choice(candidates)

    def _pick_popular_sync(self, tried: set[str], exclude_bvid: str | None) -> BiliVideo:
        self._refresh_pool()

        def usable(pool: list[BiliVideo]) -> list[BiliVideo]:
            return [
                video
                for video in pool
                if MIN_DURATION_SECONDS <= video.duration <= self.max_duration
                and video.bvid not in tried
            ]

        candidates = usable(self._pool)
        if not candidates:
            # 池子筛完了：强制翻新缓存再筛一次。
            self._pool_monotonic = 0.0
            self._refresh_pool()
            candidates = usable(self._pool)
        if not candidates:
            raise BiliApiError(
                f"热门榜里没有时长 ≤{self.max_duration} 秒且未试过的视频"
            )
        return self._choose(candidates, exclude_bvid)

    # ------------------------------------------------------------------
    # 关键词搜索
    # ------------------------------------------------------------------
    def _fetch_search_page(self, keyword: str, page: int) -> list[BiliVideo]:
        self._warm_cookies()
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "page_size": SEARCH_PAGE_SIZE,
        }
        signed = self._sign_params(params)
        url = SEARCH_API + "?" + urllib.parse.urlencode(sorted(signed.items()))
        data = _get_json(url, self.api_timeout, self._opener)
        if data.get("code") != 0:
            message = str(data.get("message") or "").strip()
            raise BiliApiError(
                f"搜索接口返回 code={data.get('code')} {message}".strip()
            )
        videos: list[BiliVideo] = []
        for item in (data.get("data") or {}).get("result") or []:
            bvid = str(item.get("bvid") or "")
            if not bvid:
                continue
            try:
                views = int(item.get("play") or 0)
            except (TypeError, ValueError):
                views = 0
            videos.append(
                BiliVideo(
                    bvid=bvid,
                    title=_strip_keyword_markup(item.get("title")),
                    duration=_parse_duration_text(item.get("duration")),
                    author=str(item.get("author") or "").strip(),
                    views=views,
                )
            )
        return videos

    def _search_sync(self, keyword: str, pages: int) -> list[BiliVideo]:
        keyword = str(keyword or "").strip()[:MAX_KEYWORD_LENGTH]
        if not keyword:
            raise BiliApiError("搜索关键词为空")
        pool: list[BiliVideo] = []
        for page in range(1, max(1, pages) + 1):
            try:
                pool.extend(self._fetch_search_page(keyword, page))
            except Exception:
                if page == 1:
                    raise  # 第一页就失败说明接口不可用，直接把错误抛出去
                break  # 后续页失败不致命，用已有结果
        return pool

    def _fetch_view_sync(self, bvid: str) -> BiliVideo:
        data = _get_json(VIEW_API.format(bvid=bvid), self.api_timeout)
        if data.get("code") != 0:
            message = str(data.get("message") or "").strip()
            raise BiliApiError(
                f"视频详情接口返回 code={data.get('code')} {message}".strip()
            )
        detail = data.get("data") or {}
        return BiliVideo(
            bvid=str(detail.get("bvid") or bvid),
            cid=int(detail.get("cid") or 0),
            title=str(detail.get("title") or "").strip(),
            duration=int(detail.get("duration") or 0),
            author=str((detail.get("owner") or {}).get("name") or "").strip(),
            views=int((detail.get("stat") or {}).get("view") or 0),
        )

    # ------------------------------------------------------------------
    # 播放地址与下载
    # ------------------------------------------------------------------
    def _resolve_sync(self, video: BiliVideo) -> tuple[str, int]:
        if not video.cid:
            raise BiliApiError("视频缺少 cid，无法获取播放地址")
        return _resolve_play_url(video.bvid, video.cid, self.api_timeout)

    def _download_sync(self, url: str, dest: Path, max_bytes: int, timeout: float) -> int:
        # B站 CDN 偶发 502/503/504，自动重试几次；其他错误直接抛出。
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return _download_to(url, dest, timeout, max_bytes)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 502, 503, 504) and attempt < 2:
                    last_error = exc
                    time.sleep(1.5)
                    continue
                raise BiliApiError(f"视频下载失败：HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                if attempt < 2:
                    last_error = exc
                    time.sleep(1.5)
                    continue
                raise BiliApiError(f"视频下载失败：{exc.reason}") from exc
        raise BiliApiError(f"视频下载失败：{last_error}")

    # ------------------------------------------------------------------
    # async 门面
    # ------------------------------------------------------------------
    async def pick(
        self, tried: set[str] | None = None, exclude_bvid: str | None = None
    ) -> BiliVideo:
        """从热门榜随机挑一个（刷视频用）。"""
        return await asyncio.to_thread(self._pick_popular_sync, set(tried or ()), exclude_bvid)

    async def search(
        self, keyword: str, *, pages: int = 1
    ) -> list[BiliVideo]:
        """按关键词搜索（搜视频用），按相关性顺序返回候选，不做时长过滤。"""
        return await asyncio.to_thread(self._search_sync, keyword, pages)

    async def pick_from(
        self,
        pool: list[BiliVideo],
        tried: set[str] | None = None,
        exclude_bvid: str | None = None,
    ) -> BiliVideo:
        """从给定候选池里随机挑一个。"""
        return await asyncio.to_thread(
            self._choose, [v for v in pool if v.bvid not in set(tried or ())], exclude_bvid
        )

    async def fetch_details(self, video: BiliVideo) -> BiliVideo:
        """补全 cid 并用真实数据覆盖；搜索结果必须先调用它。"""
        detail = await asyncio.to_thread(self._fetch_view_sync, video.bvid)
        if not detail.cid:
            raise BiliApiError("视频详情里没有 cid，无法下载")
        return BiliVideo(
            bvid=video.bvid,
            cid=detail.cid,
            title=detail.title or video.title,
            duration=detail.duration or video.duration,
            author=detail.author or video.author,
            views=detail.views or video.views,
        )

    async def resolve(self, video: BiliVideo) -> tuple[str, int]:
        return await asyncio.to_thread(self._resolve_sync, video)

    async def download(
        self, url: str, dest: Path, *, max_bytes: int, timeout: float = 120.0
    ) -> int:
        return await asyncio.to_thread(
            self._download_sync, url, dest, max_bytes, max(30.0, float(timeout))
        )
