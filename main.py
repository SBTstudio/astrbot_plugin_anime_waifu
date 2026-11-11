# -*- coding: utf-8 -*-
"""二次元老婆抽卡插件（AstrBot）

从本地角色库中随机抽取动漫 / 游戏角色作为"今日老婆"。
支持在抽卡时附带角色立绘（双图源 + 名字校验 + 本地缓存）。

指令：
    抽二次元老婆  抽纸片人  anw
    换二次元老婆  换纸片人  huan
    我的二次元老婆
    二次元老婆排行
    查二次元老婆 <名字>
    二次元老婆帮助
"""

import asyncio
import html
import json
import os
import random
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 图鉴文本超过这个字数就打包成 md 文件（QQ 被动回复过长会被截）
_TXT_THRESHOLD = 400

PLUGIN_NAME = "astrbot_plugin_anime_waifu"
# 插件自身所在目录。角色库里的本地立绘可以写相对路径（如 data/images/xx.png），
# 按这个目录解析，换机器、换用户名、换安装路径都不必改数据。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# SR 角色的专属介绍（结构与 _SSR_TEXT 相同）




# 抽到其中一个时，会提到另一个

# 神椿 V.W.P：抽到时介绍她们在《神椿市建设中。》里的同位体

# 粉毛三人组：抽到任意一个，提到另外两个

# 米哈游跨作品同位体：抽到任意一个，提到同组另外几个



























CN_TZ = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_IMG_BAD_WORDS = (
    "logo", "icon", "nav", "banner", "title", "placeholder", "edit",
    "question", "star", "commons/", "watermark", "disambig", "empty",
)

_ANILIST_QUERY = """
query ($s: String) {
  Page(perPage: 5) {
    characters(search: $s) {
      name { full native alternative }
      image { large }
    }
  }
}
"""


# ---------------- 文案：从 data/texts.json 读取 ----------------
# 文案放在 JSON 里，改了保存即可生效——会自动重载，不必重启插件。
_TEXT_NAMES = ("_SSR_TEXT", "_SR_TEXT", "_MHY_TWIN", "_PINK_TRIO",
               "_TWIN_MENTION", "_PAIR_MENTION", "_TOUHOU_TIPS", "_SNOWMAN_HINT")
_TEXTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "texts.json")
_SSR_TEXT, _SR_TEXT = {}, {}
_MHY_TWIN, _PINK_TRIO, _TWIN_MENTION, _PAIR_MENTION = {}, {}, {}, {}
_TOUHOU_TIPS, _SNOWMAN_HINT = "", ""


def load_texts() -> bool:
    """把 data/texts.json 读进来，覆盖上面这些全局名。

    读失败时保持现状（不抛错），免得因为一个坏文件把插件带崩。
    """
    try:
        with open(_TEXTS_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return False
    g = globals()
    for k in _TEXT_NAMES:
        if k in d:
            g[k] = d[k]
    return True


load_texts()


def _today(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), CN_TZ).strftime("%Y-%m-%d")


def _lcs_len(a: str, b: str) -> int:
    """最长公共连续子串长度（用于判断两个作品名是不是同一个）。"""
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def _series_matches(series: str, tag: str) -> bool:
    """条目括号里的作品标签，与角色所属作品是否一致。

    「远坂凛(星穹铁道)」这种联动/异闻录条目必须挡掉——那不是本人。
    """
    if not series or not tag:
        return False
    if tag in series or series in tag:
        return True
    return _lcs_len(series, tag) >= 3


def pick_moegirl_image(html: str, name: str) -> str | None:
    """从萌娘百科页面 HTML 中挑出角色立绘（纯函数，便于测试）。"""
    if not html:
        return None

    info_idx = -1
    for anchor in ("infobox-image-container", "infobox-image", "moe-infobox"):
        idx = html.find(anchor)
        if idx != -1:
            info_idx = idx if info_idx == -1 else min(info_idx, idx)
            break

    scored: list[tuple[int, int, str]] = []
    for m in re.finditer(r"<img\b[^>]*>", html):
        tag = m.group(0)
        src = re.search(r'(?:data-src|src)="(https?://[^"]+?)"', tag)
        if not src:
            continue
        url = src.group(1).split("!")[0]
        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1]).lower()

        width = 0
        wm = re.search(r'width="(\d+)"', tag)
        if wm:
            width = int(wm.group(1))

        score = 0
        if info_idx != -1 and 0 <= m.start() - info_idx < 6000:
            score += 3
        if name.lower() in filename:
            score += 3 if len(name) >= 3 else 1
        if width >= 200:
            score += 1
        if any(bad in filename for bad in _IMG_BAD_WORDS) or url.lower().endswith(".svg"):
            score -= 5
        scored.append((score, m.start(), url))

    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = scored[0]
    return best[2] if best[0] >= 3 else None


class AnimeWaifuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        here = os.path.dirname(os.path.abspath(__file__))
        self._data_path = os.path.join(here, "data", "characters.json")
        with open(self._data_path, encoding="utf-8") as f:
            self.characters: list[dict] = json.load(f)
        self.by_name = {c["name"]: c for c in self.characters}
        # 记下时间戳与大小，用于自动检测角色库更新
        try:
            st = os.stat(self._data_path)
            self._data_stamp = (st.st_mtime, st.st_size)
        except OSError:
            self._data_stamp = None
        try:
            st = os.stat(_TEXTS_PATH)
            self._texts_stamp = (st.st_mtime, st.st_size)
        except OSError:
            self._texts_stamp = None

        data_dir = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "waifu.db")
        self.image_cache_path = os.path.join(data_dir, "images.json")
        # 查询指令的独立冷却记录（不占用抽卡冷却）
        self._search_ts: dict[str, int] = {}
        self.image_cache: dict[str, str | None] = {}
        if os.path.exists(self.image_cache_path):
            try:
                with open(self.image_cache_path, encoding="utf-8") as f:
                    self.image_cache = json.load(f)
            except Exception:
                self.image_cache = {}
        self._init_db()
        logger.info(
            f"[anime_waifu] 已加载 {len(self.characters)} 个角色，"
            f"图片缓存 {len(self.image_cache)} 条，数据库：{self.db_path}"
        )

    # ---------------- 基础 ----------------

    def cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return default if value is None else value

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS draws (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    umo       TEXT NOT NULL,
                    user_id   TEXT NOT NULL,
                    user_name TEXT,
                    char_id   TEXT NOT NULL,
                    char_name TEXT NOT NULL,
                    series    TEXT NOT NULL,
                    day       TEXT NOT NULL,
                    ts        INTEGER NOT NULL
                )
                """
            )
            cols = {row[1] for row in conn.execute("PRAGMA table_info(draws)")}
            if "pool" not in cols:
                conn.execute("ALTER TABLE draws ADD COLUMN pool TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_draws_umo_day ON draws(umo, day)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_draws_user ON draws(umo, user_id, ts)"
            )
            conn.commit()
        self._backfill_pool()

    def _backfill_pool(self):
        """给历史记录补上卡池字段（按其角色当前所属池）。"""
        pool_of = {c["id"]: self.char_pool(c) for c in self.characters}
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, char_id FROM draws WHERE pool IS NULL OR pool = ''"
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE draws SET pool=? WHERE id=?",
                    (pool_of.get(r["char_id"], "anime"), r["id"]),
                )
            if rows:
                conn.commit()
                logger.info(f"[anime_waifu] 已为 {len(rows)} 条历史记录补全卡池")


    def group_allowed(self, group_id) -> bool:
        gid = str(group_id or "")
        blacklist = [str(x) for x in (self.cfg("blacklist_groups", []) or [])]
        whitelist = [str(x) for x in (self.cfg("whitelist_groups", []) or [])]
        if gid and gid in blacklist:
            return False
        if whitelist:
            return gid in whitelist
        return True

    # ---------------- 图片 ----------------

    def _save_image_cache(self):
        """写入缓存时与磁盘上的内容合并，避免覆盖外部批量预抓取的结果。"""
        try:
            merged: dict[str, str | None] = {}
            if os.path.exists(self.image_cache_path):
                try:
                    with open(self.image_cache_path, encoding="utf-8") as f:
                        merged = json.load(f) or {}
                except Exception:
                    merged = {}
            merged.update(self.image_cache)  # 内存中的优先
            with open(self.image_cache_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False)
            self.image_cache = merged
        except Exception as exc:
            logger.warning(f"[anime_waifu] 图片缓存写入失败：{exc}")

    @staticmethod
    def _validate_anilist(name: str, item: dict) -> bool:
        names = item.get("name") or {}
        pool = [names.get("full"), names.get("native")]
        pool.extend(names.get("alternative") or [])
        pool = [re.sub(r"[(（].*?[)）]", "", p or "").strip() for p in pool]
        if len(name) >= 3:
            return any(name and name in p for p in pool)
        # 短名字用子串匹配必然误中：搜「年」会命中别人别名里的某个「年」，
        # 于是把「Cid Kagenou」的图当成「年」的立绘。
        # 1 个字必须完全相等；2 个字必须作为独立片段出现（如「初音ミク」里的「初音」不算）。
        for p in pool:
            if not p:
                continue
            if name == p:
                return True
            if len(name) >= 2 and re.search(
                r"(?:^|[・·/、\s(（])" + re.escape(name) + r"(?:$|[・·/、\s)）])", p
            ):
                return True
        return False

    async def _fetch_moegirl(self, session, title: str, match_name: str | None = None) -> str | None:
        url = "https://zh.moegirl.org.cn/" + urllib.parse.quote(title)
        async with session.get(url, timeout=20) as resp:
            if resp.status != 200:
                return None
            page = await resp.text(errors="ignore")
        return pick_moegirl_image(page, match_name or title)

    @staticmethod
    def _title_acceptable(name: str, series: str, title: str) -> bool:
        """只接受「角色名」或「角色名(作品)」这类明确指向本人的条目名。

        这条过滤是必须的：搜索兜底会把「雷霆」匹配到游戏封面页、
        把「萨莎」匹配到「萨莎的父亲」，那种图不如不给。
        只认「名字(作品)」这一种写法，**并且括号里的作品必须和角色对得上**——
        「远坂凛(星穹铁道)」「御坂美琴(蔚蓝档案)」这类联动条目不是本人，必须挡掉。
        「名字·姓」那种（如「尼禄·卡奥斯」）风险太大，交给 moegirl 字段手工指定。
        """
        if title == name:
            return True
        m = re.match(r"^" + re.escape(name) + r"[(（](.+?)[)）]$", title)
        if not m:
            return False
        return _series_matches(series, m.group(1))

    async def _search_moegirl_titles(self, session, query: str, limit: int = 5) -> list[str]:
        url = (
            "https://zh.moegirl.org.cn/index.php?title=Special:%E6%90%9C%E7%B4%A2&search="
            + urllib.parse.quote(query)
        )
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                return []
            page = await resp.text(errors="ignore")
        found: list[str] = []
        for m in re.finditer(r'<a[^>]+href="/([^"#?]+)"[^>]*title="([^"]+)"', page):
            title = html.unescape(m.group(2))
            if ":" in title or title in found:
                continue
            found.append(title)
            if len(found) >= limit:
                break
        return found

    async def _fetch_anilist(self, session, name: str) -> str | None:
        payload = {"query": _ANILIST_QUERY, "variables": {"s": name}}
        async with session.post(
            "https://graphql.anilist.co", json=payload, timeout=20
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        items = ((data.get("data") or {}).get("Page") or {}).get("characters") or []
        for item in items:
            if self._validate_anilist(name, item):
                img = (item.get("image") or {}).get("large")
                if img:
                    return img
        return None

    @staticmethod
    def _local_image_path(direct: str) -> str | None:
        """解析本地立绘路径。

        绝对路径直接看文件在不在；相对路径按插件根目录解析，
        这样整份角色库可以跟着插件搬家，不必逐条改路径。
        路径失效时返回 None，交给后面在线取图兜底。
        """
        if not direct:
            return None
        path = direct if os.path.isabs(direct) else os.path.join(_PLUGIN_DIR, direct)
        return path if os.path.isfile(path) else None

    async def resolve_image(self, char: dict) -> str | None:
        """按需解析角色立绘，结果写入本地缓存。

        顺序：指定条目名 / 角色名 → 严格搜索兜底 → AniList（带名字校验）。
        不确定的一律不采纳——宁可没图，也不给错图。
        角色数据里可用 "moegirl" 字段手工指定正确条目名。
        """
        if not bool(self.cfg("enable_image", True)):
            return None
        # 资料里标了 no_image 的角色，一律不给图。
        # 用于名字与知名条目撞车的角色（如「千钧」会撞到《侠岚》的同名角色），
        # 宁可没图，也不给错图。
        if char.get("no_image"):
            return None
        cid = char["id"]

        # 顺序很重要：数据永远优先于缓存。
        # 否则改了 image 字段、或给角色打了 no_image，旧缓存还会一直吐错图。
        name = char["name"]
        direct = str(char.get("image") or "").strip()
        if direct.startswith("http"):
            self.image_cache[cid] = direct
            self._save_image_cache()
            return direct
        local = self._local_image_path(direct)
        if local:
            self.image_cache[cid] = local
            self._save_image_cache()
            return local
        if cid in self.image_cache:
            return self.image_cache[cid]

        explicit = str(char.get("moegirl") or "").strip()

        url = None
        hard_fail = False
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=int(self.cfg("image_timeout", 20) or 20))
            async with aiohttp.ClientSession(
                timeout=timeout, headers={"User-Agent": UA}
            ) as session:
                title = explicit or name
                try:
                    url = await self._fetch_moegirl(session, title, title)
                except Exception as exc:
                    hard_fail = True
                    logger.debug(f"[anime_waifu] 萌百取图失败 {name}: {exc}")

                if not url:
                    series_key = str(char.get("series") or "").split("/")[0][:6]
                    try:
                        titles = await self._search_moegirl_titles(
                            session, f"{name} {series_key}"
                        )
                        for title in titles:
                            if title == (explicit or name):
                                continue
                            if not self._title_acceptable(
                                name, str(char.get("series") or ""), title
                            ):
                                continue
                            url = await self._fetch_moegirl(session, title, name)
                            if url:
                                logger.info(
                                    f"[anime_waifu] 搜索兜底命中 {name} → {title}"
                                )
                                break
                    except Exception as exc:
                        hard_fail = True
                        logger.debug(f"[anime_waifu] 萌百搜索兜底失败 {name}: {exc}")

                if not url:
                    try:
                        url = await self._fetch_anilist(session, name)
                    except Exception as exc:
                        hard_fail = True
                        logger.debug(f"[anime_waifu] AniList 取图失败 {name}: {exc}")
        except Exception as exc:
            hard_fail = True
            logger.warning(f"[anime_waifu] 图片解析异常 {name}: {exc}")

        if url is None and hard_fail:
            # 网络异常/超时不算「没有图」，不写负缓存，下次继续尝试
            logger.info(f"[anime_waifu] {name} 取图遇到网络问题，本次不记入缓存")
            return None

        self.image_cache[cid] = url
        self._save_image_cache()
        return url

    # ---------------- 数据查询 ----------------

    @staticmethod
    def char_pool(char: dict) -> str:
        return str(char.get("pool") or "anime")

    def limits(self) -> dict[str, int]:
        return {
            "game": int(self.cfg("daily_limit_game", 6) or 0),
            "anime": int(self.cfg("daily_limit_anime", 4) or 0),
        }

    def today_count(self, umo: str, user_id: str, pool: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM draws WHERE umo=? AND user_id=? AND day=?"
        args: list = [umo, user_id, _today()]
        if pool:
            sql += " AND pool=?"
            args.append(pool)
        with self._conn() as conn:
            row = conn.execute(sql, args).fetchone()
        return int(row[0]) if row else 0

    def used_map(self, umo: str, user_id: str) -> dict[str, int]:
        return {p: self.today_count(umo, user_id, p) for p in ("game", "anime")}

    def quota_text(self, umo: str, user_id: str) -> str:
        used, limits = self.used_map(umo, user_id), self.limits()
        parts = []
        for p, label in (("game", "二游"), ("anime", "动漫")):
            lim = limits[p]
            parts.append(f"{label} {used[p]}/{lim}" if lim > 0 else f"{label} {used[p]}/不限")
        return " · ".join(parts)

    def choose_pool(self, umo: str, user_id: str) -> str | None:
        """按比例随机选池；某池满额则只出另一池；两池都满返回 None。"""
        used, limits = self.used_map(umo, user_id), self.limits()
        avail = [p for p in ("game", "anime") if limits[p] <= 0 or used[p] < limits[p]]
        if not avail:
            return None
        if len(avail) == 1:
            return avail[0]
        ratio = int(self.cfg("game_pool_ratio", 60))   # 注意：不能用 `or 60`，否则 0% 会被当成假值
        ratio = max(0, min(100, ratio))
        return "game" if random.randint(1, 100) <= ratio else "anime"

    def last_pool(self, umo: str, user_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT pool FROM draws WHERE umo=? AND user_id=? ORDER BY id DESC LIMIT 1",
                (umo, user_id),
            ).fetchone()
        return str(row["pool"]) if row and row["pool"] else None

    def owned_ids(self, umo: str, user_id: str) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT char_id FROM draws WHERE umo=? AND user_id=?",
                (umo, user_id),
            ).fetchall()
        return {r["char_id"] for r in rows}

    def last_ts(self, umo: str, user_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(ts) FROM draws WHERE umo=? AND user_id=?", (umo, user_id)
            ).fetchone()
        return int(row[0]) if row and row[0] else 0

    def pick(self, owned: set[str], pool: str) -> dict:
        candidates = [c for c in self.characters if self.char_pool(c) == pool]
        if not candidates:
            candidates = self.characters
        if bool(self.cfg("allow_repeat", False)):
            return random.choice(candidates)
        fresh = [c for c in candidates if c["id"] not in owned]
        return random.choice(fresh or candidates)

    def record(self, umo, user_id, user_name, char, replace_last: bool = False):
        now = int(time.time())
        pool = self.char_pool(char)
        with self._conn() as conn:
            if replace_last:
                row = conn.execute(
                    "SELECT id FROM draws WHERE umo=? AND user_id=? ORDER BY id DESC LIMIT 1",
                    (umo, user_id),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE draws SET char_id=?, char_name=?, series=?, pool=?, day=?, ts=?"
                        " WHERE id=?",
                        (char["id"], char["name"], char["series"], pool, _today(now), now, row["id"]),
                    )
                    conn.commit()
                    return
            conn.execute(
                "INSERT INTO draws(umo, user_id, user_name, char_id, char_name, series, pool, day, ts)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    umo, user_id, user_name or "", char["id"], char["name"],
                    char["series"], pool, _today(now), now,
                ),
            )
            conn.commit()

    def today_list(self, umo: str, user_id: str) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT char_id, char_name, series, pool, ts FROM draws"
                " WHERE umo=? AND user_id=? AND day=? ORDER BY id ASC",
                (umo, user_id, _today()),
            ).fetchall()

    def rank_tag(self, char_id: str) -> str:
        """SSR / SR / 空串。图鉴与今日记录显示用。"""
        for c in self.characters:
            if c["id"] == char_id:
                if c.get("ssr"):
                    return "SSR"
                if c.get("sr"):
                    return "SR"
                return ""
        return ""

    def rank(self, umo: str, limit: int) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT user_name, user_id, COUNT(DISTINCT char_id) AS cnt"
                " FROM draws WHERE umo=? AND user_name!=''"
                " GROUP BY user_id ORDER BY cnt DESC LIMIT ?",
                (umo, limit),
            ).fetchall()

    def total_owned(self, umo: str, user_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT char_id) FROM draws WHERE umo=? AND user_id=?",
                (umo, user_id),
            ).fetchone()
        return int(row[0]) if row else 0

    def collect_level(self, owned: int) -> tuple[int, int]:
        """图鉴等级：每 level_step 位升一级。返回（等级, 距下一级还差几位）。"""
        step = max(1, int(self.cfg("level_step", 25) or 25))
        owned = max(0, int(owned))
        return 1 + owned // step, step - owned % step

    def level_text(self, owned: int) -> str:
        """一行式等级条：等级 · 已收录 N/全池（占比）· 距下一级。"""
        full = len(self.characters)
        lv, need = self.collect_level(owned)
        pct = (owned / full * 100) if full else 0.0
        return (
            f"Lv.{lv} · 已收录 {owned}/{full}（{pct:.0f}%）"
            f"　再收 {need} 位升 Lv.{lv + 1}"
        )


    def _maybe_reload(self) -> bool:
        """角色库（characters.json）被改动时自动重读，免去手动重载插件。

        只在文件的时间戳或大小变化时才真读盘，平时零开销。
        读失败时保留旧数据，绝不让插件因为一次坏写入而崩掉。
        """
        path = getattr(self, "_data_path", None)
        if not path:
            return False
        try:
            st = os.stat(path)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            return False
        if stamp == getattr(self, "_data_stamp", None):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                chars = json.load(f)
        except Exception as exc:
            logger.warning(f"[anime_waifu] 角色库自动重载失败，继续用旧数据：{exc}")
            return False
        self.characters = chars
        self.by_name = {c["name"]: c for c in chars}
        self._data_stamp = stamp
        logger.info(f"[anime_waifu] 检测到角色库更新，已自动重载 {len(chars)} 个角色")
        self._maybe_reload_texts()
        return True

    def _maybe_reload_texts(self) -> bool:
        """文案库（data/texts.json）被改动时自动重读。"""
        try:
            st = os.stat(_TEXTS_PATH)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            return False
        if stamp == getattr(self, "_texts_stamp", None):
            return False
        if load_texts():
            self._texts_stamp = stamp
            logger.info(
                f"[anime_waifu] 检测到文案库更新，已自动重载"
                f"（SSR {len(_SSR_TEXT)} / SR {len(_SR_TEXT)}）"
            )
            return True
        return False

    def _guard(self, event: AstrMessageEvent):
        """只做群权限与冷却检查；每日次数由卡池逻辑负责。"""
        self._maybe_reload()
        self._maybe_reload_texts()
        if not self.group_allowed(event.get_group_id()):
            return "这个群还没有开放抽卡哦……"
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        cooldown = int(self.cfg("draw_cooldown_seconds", 30) or 0)
        if cooldown > 0:
            passed = int(time.time()) - self.last_ts(umo, uid)
            if passed < cooldown:
                return f"沃还没缓过来……再过 {cooldown - passed} 秒再抽吧。"
        return None


    def ssr_text(self, char: dict) -> str:
        """抽卡附带的角色简介。

        SSR：详细版（含 B 站彩蛋、雪人歌）。
        其它角色：一句百科摘要，约 40~90 字，与奥黛塔那段体量相当。
        """
        if not char.get("ssr"):
            if char.get("sr"):
                # 同名不同作品时（如三条「露西」），用「名字@作品」单独指定；
                # 没写就退回按名字取 —— 老写法照旧可用 ✓
                body = _SR_TEXT.get("%s@%s" % (char["name"], char.get("series")), "") or _SR_TEXT.get(char["name"], "")
                intro = str(char.get("intro") or "").strip()
                out = "\n————— SR —————"
                if body:
                    out += "\n" + body
                if intro:
                    out += "\n" + intro
            else:
                intro = str(char.get("intro") or "").strip()
                out = f"\n{intro}" if intro else ""
        else:
            name = char["name"]
            # 同名不同作品时（如两条「御坂美琴」），用「名字@作品」单独指定；
            # 没写就退回按名字取 —— 老写法照旧可用 ✓
            body = _SSR_TEXT.get("%s@%s" % (name, char.get("series")), "") or _SSR_TEXT.get(name, "")
            if not body:
                # 兜底：SSR 文案表里还没写这一位时，退回角色库自带的简介，
                # 免得整段介绍变成一片空白。
                body = str(char.get("intro") or "").strip()
                if body:
                    logger.warning(f"[anime_waifu] SSR 文案缺失，已回落到 intro：{name}")
            parts = ["————— SSR —————"]
            if body:
                parts.append(body)
            if name in ("沃雅妮莎", "奥黛塔·苏佩茜娃"):
                parts.append(self._snowman_lyrics())
            parts.append("「这是哪位的推呢」")
            out = "\n" + "\n".join(parts)
        tip = str(char.get("tip") or "").strip()
        if tip:
            out += "\n" + tip
        elif char.get("series") == "东方Project":
            out += "\n" + _TOUHOU_TIPS
        pair = _PAIR_MENTION.get(char["name"])
        if pair:
            out += "\n" + pair
        twin = _TWIN_MENTION.get(char["name"])
        if twin:
            out += "\n" + twin
        others = _PINK_TRIO.get(char["name"])
        if others:
            out += "\n※ 粉毛三人组之一，另外两个是" + others + "。这两个粉毛又是谁。"
        mhy = _MHY_TWIN.get(char["name"])
        if mhy:
            out += "\n" + mhy
        return out

    def _snowman_lyrics(self) -> str:
        """雪人歌：只显示一句。

        若插件 data/ode_snowman.txt 里有内容（你自己贴的歌词），则优先显示它。
        """
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "ode_snowman.txt")
        try:
            if os.path.exists(path):
                text = open(path, encoding="utf-8").read().strip()
                if text:
                    return text
        except Exception:
            pass
        return "「奥黛塔，快陪我去堆个雪人吧……」"


    def _result(self, event: AstrMessageEvent, text: str, image: str | None):
        """有图则图 + 文，无图则纯文本。"""
        if image:
            try:
                comp = (
                    Comp.Image.fromURL(image)
                    if str(image).startswith("http")
                    else Comp.Image.fromFileSystem(image)
                )
                return event.chain_result([comp, Comp.Plain(text)])
            except Exception as exc:
                logger.warning(f"[anime_waifu] 图片发送失败，退回纯文本：{exc}")
        return event.plain_result(text)

    @filter.command("抽二次元老婆", alias={"抽纸片人", "anw", "wmc", "抽acg"})
    async def cmd_draw(self, event: AstrMessageEvent):
        """抽取一位今日老婆（二游 / 动漫 双卡池）"""
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return

        umo, uid = event.unified_msg_origin, str(event.get_sender_id())
        pool = self.choose_pool(umo, uid)
        if pool is None:
            yield event.plain_result(
                f"今天的次数都用完啦（{self.quota_text(umo, uid)}），明天再来吧……"
            )
            return

        owned = self.owned_ids(umo, uid)
        char = self.pick(owned, pool)
        is_new = char["id"] not in owned
        self.record(umo, uid, event.get_sender_name(), char)

        pool_label = "二游" if pool == "game" else "动漫"
        text = (
            f"你抽到了（{pool_label}池）：《{char['series']}》{char['name']}"
            f"（No.{char.get('no', '?')} · {'新收集' if is_new else '重复'}）"
            f"\n{self.quota_text(umo, uid)}　已收集 {self.total_owned(umo, uid)}/{len(self.characters)}"
        )
        text += self.ssr_text(char)
        image = await self.resolve_image(char)
        yield self._result(event, text, image)


    @filter.command("换二次元老婆", alias={"换纸片人", "huan", "换一个"})
    async def cmd_reroll(self, event: AstrMessageEvent):
        """重抽一次，替换今天最后抽到的角色（沿用同一卡池）"""
        if not self.group_allowed(event.get_group_id()):
            yield event.plain_result("这个群还没有开放抽卡哦……")
            return

        umo, uid = event.unified_msg_origin, str(event.get_sender_id())
        if not self.today_list(umo, uid):
            yield event.plain_result("你今天还没抽过，先抽一次再换吧……")
            return

        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return

        pool = self.last_pool(umo, uid) or self.choose_pool(umo, uid) or "anime"
        char = self.pick(self.owned_ids(umo, uid), pool)
        self.record(umo, uid, event.get_sender_name(), char, replace_last=True)
        pool_label = "二游" if pool == "game" else "动漫"
        text = (
            f"换好了（{pool_label}池）：《{char['series']}》{char['name']}（No.{char.get('no', '?')}）"
            f"\n{self.quota_text(umo, uid)}　已收集 {self.total_owned(umo, uid)}/{len(self.characters)}"
        )
        text += self.ssr_text(char)
        image = await self.resolve_image(char)
        yield self._result(event, text, image)


    @filter.command("我的二次元老婆", alias={"我的纸片人", "myanw"})
    async def cmd_mine(self, event: AstrMessageEvent):
        """查看今天抽到的角色、卡池额度与收集进度"""
        self._maybe_reload()
        umo, uid = event.unified_msg_origin, str(event.get_sender_id())
        rows = self.today_list(umo, uid)
        total = self.total_owned(umo, uid)
        if not rows:
            yield event.plain_result(
                f"你今天还没抽过哦。\n{self.level_text(total)}"
                f"\n{self.quota_text(umo, uid)}"
            )
            return
        names = "、".join(
            ("【二游】" if (r["pool"] or "anime") == "game" else "【动漫】")
            + r["char_name"]
            + ((tag := self.rank_tag(r["char_id"])) and f"[{tag}]" or "")
            for r in rows
        )
        if len(names) > 130:
            names = names[:130] + "…"
        yield event.plain_result(
            f"你今天抽到了 {len(rows)} 位：\n{names}\n"
            f"{self.level_text(total)}\n{self.quota_text(umo, uid)}"
        )


    @filter.command("二次元老婆排行", alias={"纸片人排行", "anwph"})
    async def cmd_rank(self, event: AstrMessageEvent):
        """查看本群收集排行"""
        umo = event.unified_msg_origin
        rows = self.rank(umo, int(self.cfg("rank_size", 10) or 10))
        if not rows:
            yield event.plain_result("这个群还没有人抽过呢……")
            return
        lines = [
            f"{i}. {r['user_name'] or r['user_id'][:6]} —— {r['cnt']} 位"
            for i, r in enumerate(rows, 1)
        ]
        yield event.plain_result("本群收集排行\n" + "\n".join(lines))

    @filter.command("查二次元老婆", alias={"查纸片人", "chaanw"})
    async def cmd_search(self, event: AstrMessageEvent):
        """按名字/编号查询角色（有独立冷却）"""
        self._maybe_reload()
        # —— 查询冷却：默认 2 分钟一次，与抽卡冷却分开算 ——
        cd = int(self.cfg("search_cooldown_seconds", 120) or 0)
        if cd > 0:
            key = f"{event.unified_msg_origin}:{event.get_sender_id()}"
            passed = int(time.time()) - int(self._search_ts.get(key, 0))
            if passed < cd:
                yield event.plain_result(f"沃不行了……再过 {cd - passed} 秒吧。")
                return
            self._search_ts[key] = int(time.time())

        keyword = event.message_str.strip()
        for prefix in ("查二次元老婆", "查纸片人", "chaanw"):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix):].strip()
                break
        if not keyword:
            yield event.plain_result("要查谁呀？用法：查二次元老婆 角色名")
            return
        kw = keyword.strip()
        low = kw.lower()
        # 编号写法：42 / c42 / No.42 / no42 / #42 / 编号42 / 42号
        # —— 若查的是作品名、且该作品角色数 > 8 → 打包成 md（含全部角色）——
        series_hits = {}
        if kw:
            for c in self.characters:
                if kw in c["series"]:
                    series_hits.setdefault(c["series"], []).append(c)
        if series_hits and sum(len(v) for v in series_hits.values()) > 8:
            n_all = sum(len(v) for v in series_hits.values())
            md = [f"# 《{kw}》角色表", "", f"共 **{n_all}** 位",
                  f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M')}"]
            for s, cs in sorted(series_hits.items(), key=lambda x: -len(x[1])):
                md += ["", f"## {s}", "", f"共 {len(cs)} 位", "",
                       "| 编号 | 名字 | 星级 |", "| ---: | --- | --- |"]
                for c in sorted(cs, key=lambda x: x.get("no", 0)):
                    rar = "SSR" if c.get("ssr") else ("SR" if c.get("sr") else "—")
                    md.append(f"| {c.get('no', '?')} | {c['name']} | {rar} |")
            md_text = "\n".join(md)
            safe = re.sub(r'[\\/:*?"<>|]', "_", kw)[:24] or "作品"
            fname = f"{safe}角色表.md"
            fpath = os.path.join(os.path.dirname(self.db_path), fname)
            try:
                with open(fpath, "w", encoding="utf-8") as fh:
                    fh.write(md_text)
            except Exception as exc:
                yield event.plain_result(f"生成角色表失败：{exc}")
                return
            yield event.chain_result([
                Comp.File(name=fname, file=fpath),
                Comp.Plain(f"《{kw}》共有 {sum(len(v) for v in series_hits.values())} 位角色（超过 8 位），沃打包成文件了。"),
            ])
            return

        probe = low.replace("编号", "").replace("号", "").strip()   # 兼容「编号42」「42号」
        m_no = re.fullmatch(r"(?:no\.?|#)?\s*(\d{1,5})", probe)
        m_cid = re.fullmatch(r"c(\d{1,5})", low)
        if m_cid:
            num = int(m_cid.group(1))
            hits = [c for c in self.characters
                    if c.get("no") == num or str(c["id"]) == "c%04d" % num]
        elif m_no:   # 纯编号，别把"42号"当名字
            num = int(m_no.group(1))
            hits = [c for c in self.characters if c.get("no") == num]
        else:
            hits = [
                c for c in self.characters if kw in c["name"] or kw in c["series"]
            ][:8]
        if not hits:
            yield event.plain_result(f"没有找到「{keyword}」，换个名字试试？")
            return
        if len(hits) == 1:
            # 只命中一个 → 给出完整介绍（含 SSR/SR 标记、入坑建议、同位体互提等）
            c = hits[0]
            rarity = "SSR" if c.get("ssr") else ("SR" if c.get("sr") else "")
            head = f"《{c['series']}》{c['name']}（No.{c.get('no', '?')}）"
            if rarity:
                head += f"（{rarity}）"
            text = head + self.ssr_text(c)
            image = await self.resolve_image(c)
            yield self._result(event, text, image)
            return
        yield event.plain_result(
            "找到这些：\n" + "\n".join(f"No.{c.get('no', '?')}《{c['series']}》{c['name']}" for c in hits)
        )

    @filter.command("谁有二次元老婆", alias={"谁有纸片人", "查持有", "持有查询", "shuiyou"})
    async def cmd_owners(self, event: AstrMessageEvent, arg: str = ""):
        """查某个角色本群（本会话）里都有谁拥有。支持名字或编号。"""
        self._maybe_reload()
        key = (arg or "").strip()
        if not key:
            yield event.plain_result(
                "用法：谁有二次元老婆 名字（或编号）\n"
                "例：谁有二次元老婆 初音未来／谁有二次元老婆 39"
            )
            return

        # 找这个角色：先按编号，再按名字（与「查二次元老婆」同一套认法）
        char = None
        if key.isdigit():
            char = next((c for c in self.characters if str(c.get("no")) == key), None)
        if char is None:
            exact = [c for c in self.characters if c["name"] == key]
            fuzzy = [c for c in self.characters if key in c["name"]]
            char = (exact or fuzzy or [None])[0]
        if char is None:
            yield event.plain_result(f"没找着「{key}」这位 ✗ —— 名字或编号再确认下？")
            return

        umo = event.unified_msg_origin
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_name, user_id, MIN(ts) AS t, COUNT(*) AS n"
                " FROM draws WHERE umo=? AND char_id=?"
                " GROUP BY user_id ORDER BY t ASC",
                (umo, char["id"]),
            ).fetchall()

        title = f"「{char['name']}」（{char['series']}·No.{char.get('no', '?')}）"
        if not rows:
            yield event.plain_result(
                f"{title}\n\n本群还没人抽到过这一位 ✗ —— 谁先抽到，谁就在这儿留名。"
            )
            return

        def _d(ts):
            try:
                return time.strftime("%m-%d", time.localtime(int(ts)))
            except Exception:  # noqa: BLE001
                return "?"

        lines = [f"—— {title} 的持有者 ——", f"本群共 {len(rows)} 人拥有："]
        for i, r in enumerate(rows[:30], 1):
            nick = (r["user_name"] or "某位").strip()[:14] or "某位"
            extra = "" if int(r["n"] or 1) <= 1 else f"（抽到 {int(r['n'])} 次）"
            lines.append(f"{i:2d}. {nick}｜{_d(r['t'])} 首次{extra}")
        if len(rows) > 30:
            lines.append(f"…另有 {len(rows) - 30} 人，就不一一列了。")
        lines.append("— — —")
        lines.append("只统计本群（本会话）的记录。想查自己有哪些，发「我的图鉴」。")
        yield event.plain_result("\n".join(lines))

    @filter.command("SSR排行", alias={"ssr排行", "ssrph", "SSR排行榜", "二次元老婆SSR排行"})
    async def cmd_ssr_rank(self, event: AstrMessageEvent):
        """本群（本会话）的 SSR 排行榜：SSR 张数、拥有总数、SSR 占比。太长就打包成 md。"""
        self._maybe_reload()
        umo = event.unified_msg_origin
        ssr = {c["id"]: c for c in self.characters if c.get("ssr")}
        if not ssr:
            yield event.plain_result("库里现在还没有 SSR 角色呢……")
            return
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_name, user_id, char_id, char_name, series, MIN(ts) AS t"
                " FROM draws WHERE umo=? GROUP BY user_id, char_id ORDER BY t ASC",
                (umo,),
            ).fetchall()
            own_rows = conn.execute(
                "SELECT user_id, COUNT(DISTINCT char_id) AS cnt"
                " FROM draws WHERE umo=? GROUP BY user_id",
                (umo,),
            ).fetchall()
        own_total = {r["user_id"]: int(r["cnt"] or 0) for r in own_rows}
        participants = len(own_total)
        # 按 user_id 聚合（昵称可能重名），顺手把总收藏数带进来
        per: dict[str, dict] = {}
        got_ssr: set[str] = set()
        for r in rows:
            uid = r["user_id"]
            slot = per.setdefault(uid, {"nick": "某位", "chars": []})
            nick = (r["user_name"] or "").strip()
            if nick:
                slot["nick"] = nick
            if r["char_id"] in ssr:
                slot["chars"].append((r["char_name"], r["series"]))
                got_ssr.add(r["char_id"])
        per = {k: v for k, v in per.items() if v["chars"]}
        if not per:
            yield event.plain_result("本群还没人抽到过 SSR ✗ —— 谁先抽到，谁就上榜首。")
            return

        total = sum(len(v["chars"]) for v in per.values())
        rank = sorted(per.items(), key=lambda kv: (-len(kv[1]["chars"]), kv[1]["nick"]))
        people_pct = len(per) / participants * 100 if participants else 0.0
        pool_pct = len(got_ssr) / len(ssr) * 100 if ssr else 0.0
        lines = [
            "SSR 排行榜",
            "本群参与 %d 人 · 抽到过 SSR 的 %d 人（%.0f%%）"
            % (participants, len(per), people_pct),
            "SSR 全池 %d 位 · 本群已收录 %d 位（%.0f%%）"
            % (len(ssr), len(got_ssr), pool_pct),
            "— — —",
        ]
        for i, (uid, v) in enumerate(rank, 1):
            n_ssr = len(v["chars"])
            n_all = own_total.get(uid, n_ssr)
            ratio = n_ssr / n_all * 100 if n_all else 0.0
            names = "、".join("%s（%s）" % (n, se) for n, se in v["chars"])
            lines.append(
                "%2d. %s　SSR %d 张 / 共 %d 位（%.2f%%）"
                % (i, v["nick"][:14], n_ssr, n_all, ratio)
            )
            lines.append("　　" + names)
        lines.append("— — —")
        lines.append("口径：本会话抽卡记录中属于 SSR 的条目，同一张重复抽到只计一次；"
                     "占比 = SSR 张数 / 该玩家已收集总数。")
        text = "\n".join(lines)

        # Markdown 版：长回复打包成 .md 文件，避免被平台截断
        md = [
            "# SSR 排行榜",
            "",
            "- 本群参与 **%d** 人，抽到过 SSR 的 **%d** 人（%.0f%%）"
            % (participants, len(per), people_pct),
            "- SSR 全池 **%d** 位，本群已收录 **%d** 位（%.0f%%）"
            % (len(ssr), len(got_ssr), pool_pct),
            "- 口径：本会话抽卡记录中属于 SSR 的条目，同一张重复抽到只计一次；"
            "占比 = SSR 张数 / 该玩家已收集总数",
            "",
            "## 总览",
            "",
            "| 排名 | 玩家 | SSR 张数 | 已收集 | SSR 占比 |",
            "| ---: | --- | ---: | ---: | ---: |",
        ]
        for i, (uid, v) in enumerate(rank, 1):
            n_ssr = len(v["chars"])
            n_all = own_total.get(uid, n_ssr)
            ratio = (n_ssr / n_all * 100) if n_all else 0.0
            md.append("| %d | %s | %d | %d | %.2f%% |"
                      % (i, v["nick"], n_ssr, n_all, ratio))
        md += ["", "## 明细", ""]
        for i, (uid, v) in enumerate(rank, 1):
            n_ssr = len(v["chars"])
            n_all = own_total.get(uid, n_ssr)
            pct = (n_ssr / n_all * 100) if n_all else 0.0
            md.append("### %d. %s · %d 张 / 共 %d 位（%.2f%%）"
                      % (i, v["nick"], n_ssr, n_all, pct))
            md.append("")
            md += ["- %s（%s）" % (nm, se) for nm, se in v["chars"]]
            md.append("")
        md_text = "\n".join(md)

        # 短就直接发；长了打包成文件（QQ 长回复会被截）
        if len(text) <= _TXT_THRESHOLD:
            yield event.plain_result(text)
            return
        fname = "SSR排行榜.md"
        fpath = os.path.join(os.path.dirname(self.db_path), fname)
        try:
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(md_text)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"生成排行榜文件失败：{exc}")
            return
        yield event.chain_result([Comp.File(name=fname, file=fpath)])

    @filter.command("我的二次元老婆图鉴", alias={"我的图鉴", "我的收集", "wdtj"})
    async def cmd_collection(self, event: AstrMessageEvent):
        """查看自己抽到过的全部角色。太长就打包成 md 发文件。"""
        self._maybe_reload()
        umo, uid = event.unified_msg_origin, str(event.get_sender_id())
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT char_name, series, char_id, MAX(ts) AS t FROM draws"
                " WHERE umo=? AND user_id=? GROUP BY char_name, series ORDER BY t DESC",
                (umo, uid),
            ).fetchall()
        if not rows:
            yield event.plain_result("你还没抽过呢——先发「抽二次元老婆」吧……")
            return

        nick = (event.get_sender_name() or "你").strip() or "你"
        total = len(rows)
        by_series: dict[str, list[str]] = {}
        no_of = {c["id"]: c.get("no", "?") for c in self.characters}
        for r in rows:
            no = no_of.get(r["char_id"], "?")
            tag = self.rank_tag(r["char_id"])
            tail = f"{no}·{tag}" if tag else f"{no}"
            by_series.setdefault(r["series"], []).append(f"{r['char_name']}({tail})")
        ordered = sorted(by_series.items(), key=lambda kv: (-len(kv[1]), kv[0]))

        lines = [f"{nick}老婆图鉴",
                 self.level_text(total),
                 "— — —"]
        for series, names in ordered:
            lines.append(f"《{series}》（{len(names)}）")
            lines.append("　" + "、".join(names))
        lines += ["— — —", "想看谁的详细介绍，发「查二次元老婆 名字」或「查二次元老婆 编号」都成。"]
        text = "\n".join(lines)

        # 短就直接发；长了打包成文件（QQ 长回复会被截）
        if len(text) <= _TXT_THRESHOLD:
            yield event.plain_result(text)
            return

        md = [f"# {nick} 老婆图鉴", "",
              f"- {self.level_text(total)}",
              f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M')}", "",
              "## 按作品统计", "", "| 作品 | 数量 |", "| --- | ---: |"]
        md += [f"| {s} | {len(ns)} |" for s, ns in ordered]
        md += ["", "## 明细", ""]
        for series, names in ordered:
            md += [f"### {series}（{len(names)}）", ""]
            md += [f"- {x}" for x in names]
            md.append("")
        md_text = "\n".join(md)
        safe = re.sub(r'[\\/:*?"<>|\r\n]', "_", nick)[:20] or "你"
        fname = f"{safe}老婆图鉴.md"
        fpath = os.path.join(os.path.dirname(self.db_path), fname)
        try:
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(md_text)
        except Exception as e:
            yield event.plain_result(f"生成图鉴文件失败：{e}")
            return
        yield event.chain_result([
            Comp.File(name=fname, file=fpath),
            Comp.Plain(
                f"{nick}老婆图鉴太长（{len(text)} 字），沃打包成文件了——共 {total} 位。\n"
                "想看谁的详细介绍，发「查二次元老婆 名字」或「编号」都成。"
            ),
        ])

    @filter.command("二次元老婆帮助", alias={"anwhelp", "纸片人帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助"""
        limits = self.limits()
        yield event.plain_result(
            "二次元老婆抽卡（双卡池）\n"
            "抽二次元老婆 —— 抽一位（别名 anw / wmc）\n"
            "换二次元老婆 —— 换掉今天最后一位\n"
            "我的二次元老婆 —— 今日记录、图鉴等级与额度\n"
            "二次元老婆排行 —— 本群收集排行\n"
            "查二次元老婆 <名字/编号> —— 查询角色\n"
            "谁有二次元老婆 <名字/编号> —— 看本群谁有这张\n"
            "我的二次元老婆图鉴 —— 你收集过的全部角色（带等级）\n"
            "SSR排行 —— 本群 SSR 榜（张数 / 拥有数 / 占比）\n"
            "二次元老婆文档 —— 群内速查表\n"
            f"角色池共 {len(self.characters)} 位（二游 {limits['game']} 次/天、"
            f"动漫 {limits['anime']} 次/天）。"
        )


    def build_quickdoc(self) -> str:
        """生成适合在群里直接看的精简速查（纯函数，便于测试）。"""
        limits = self.limits()
        cooldown = int(self.cfg("draw_cooldown_seconds", 30) or 0)
        cooldown_text = f"间隔 {cooldown} 秒" if cooldown > 0 else "无冷却"
        ratio = int(self.cfg("game_pool_ratio", 60) or 60)
        image_text = (
            "带立绘时会发两条消息（图+文）"
            if bool(self.cfg("enable_image", True))
            else "已关闭立绘，只发文字"
        )
        return (
            "二次元老婆抽卡 · 速查\n"
            "抽二次元老婆 / anw / wmc —— 抽一位\n"
            "换二次元老婆 —— 换掉今天最后一位\n"
            "我的二次元老婆 —— 今日记录、图鉴等级与额度\n"
            "二次元老婆排行 —— 本群收集排行\n"
            "查二次元老婆 <名字/编号> —— 查询角色\n"
            "谁有二次元老婆 <名字> —— 看本群谁有这张\n"
            "我的二次元老婆图鉴 —— 你收集过的全部角色（带等级）\n"
            "SSR排行 —— 本群 SSR 榜（张数 / 拥有数 / 占比）\n"
            "二次元老婆帮助 —— 完整帮助\n"
            "— — —\n"
            f"角色池 {len(self.characters)} 位 / "
            f"{len({c['series'] for c in self.characters})} 部作品\n"
            f"二游 {limits['game']} 次 · 动漫 {limits['anime']} 次（{ratio}% / {100-ratio}%）\n"
            f"{cooldown_text}\n"
            f"{image_text}"
        )


    @filter.command("二次元老婆文档", alias={"anwdoc", "抽卡文档"})
    async def cmd_doc(self, event: AstrMessageEvent):
        """查看精简版使用文档"""
        yield event.plain_result(self.build_quickdoc())

    @filter.command("重置二次元老婆记录", alias={"czanw"})
    async def cmd_reset(self, event: AstrMessageEvent):
        """管理员：清空本会话的抽卡记录"""
        if not event.is_admin():
            yield event.plain_result("这条只有管理员能用哦。")
            return
        with self._conn() as conn:
            conn.execute("DELETE FROM draws WHERE umo=?", (event.unified_msg_origin,))
            conn.commit()
        yield event.plain_result("本会话的抽卡记录已清空。")
