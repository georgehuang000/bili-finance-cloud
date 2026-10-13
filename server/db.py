# -*- coding: utf-8 -*-
"""SQLite persistence layer (SQLAlchemy).

Tables:
- uppers(uid PK, name, face, subscribed)   ← 订阅名单的唯一数据源
- videos(bvid PK, uid, title, cover, desc, pub_ts, play, url)
- dynamics(dyn_id PK, uid, type, text, pub_ts, url)
- summaries(id PK autoincrement, date, period, content, model, created_ts)

All writes for videos/dynamics use UPSERT for incremental dedup.
"""
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    create_engine,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.engine import make_url

import config

Base = declarative_base()

database_url = config.DATABASE_URL
if database_url.startswith(("postgres://", "postgresql://")):
    database_url = "postgresql+psycopg://" + database_url.split("://", 1)[1]
IS_POSTGRES = database_url.startswith("postgresql+psycopg://")
if database_url and not IS_POSTGRES:
    raise ValueError("DATABASE_URL 必须是 PostgreSQL 连接串")

_certificate_lock = threading.Lock()
_certificate_folder = None


def root_certificate_file():
    """Keep TLS verification when Windows libpq cannot open a Unicode path."""
    import certifi
    source = Path(certifi.where())
    if os.name != "nt" or str(source).isascii():
        return str(source)
    global _certificate_folder
    with _certificate_lock:
        if _certificate_folder is None:
            folder = tempfile.TemporaryDirectory(prefix="bili-cloud-ca-")
            target_root = Path(folder.name).resolve()
            if not target_root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or not str(target_root).isascii():
                raise ValueError("TLS证书临时目录必须为兼容的英文路径")
            (target_root / "cacert.pem").write_bytes(source.read_bytes())
            _certificate_folder = folder
        return str(Path(_certificate_folder.name) / "cacert.pem")


def connection_options(url):
    if not url:
        return {"check_same_thread": False}
    options = {"connect_timeout": 10}
    if make_url(url).host not in ("127.0.0.1", "localhost", "::1"):
        options.update(sslmode="verify-full", sslrootcert=root_certificate_file())
    return options


engine = create_engine(
    database_url or ("sqlite://" if config.CLOUD_MODE else f"sqlite:///{config.DB_PATH}"),
    connect_args=connection_options(database_url),
    **({"pool_size": 1, "max_overflow": 2, "pool_pre_ping": True} if IS_POSTGRES else {}),
    future=True,
)
upsert_insert = postgres_insert if IS_POSTGRES else sqlite_insert
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class Upper(Base):
    __tablename__ = "uppers"
    uid = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    face = Column(String, nullable=True)
    # 1=订阅中, 0=已取消(保留历史数据, 重新订阅即恢复); config 仅作首次引导
    subscribed = Column(Integer, nullable=False, default=1)


class Video(Base):
    __tablename__ = "videos"
    bvid = Column(String, primary_key=True)  # stores "av{aid}"
    uid = Column(String, nullable=False, index=True)
    title = Column(Text)
    cover = Column(Text)
    desc = Column(Text)
    pub_ts = Column(Integer, index=True)
    play = Column(Integer)
    url = Column(Text)
    subtitle = Column(Text)  # B站字幕全文（AI字幕/CC字幕）
    subtitle_segments = Column(Text)  # JSON句段，保留from/to用于原文定位
    summary = Column(Text)   # LLM 生成的单视频摘要
    image_desc = Column(Text)  # 封面图识别结果（vision 模型读图后的文字描述）


class Dynamic(Base):
    __tablename__ = "dynamics"
    dyn_id = Column(String, primary_key=True)
    uid = Column(String, nullable=False, index=True)
    type = Column(String)
    text = Column(Text)
    pub_ts = Column(Integer, index=True)
    url = Column(Text)
    # 动态配图 URL 列表（JSON 数组）。实测：feed 里图文动态常被剥离（充电专属）
    # 或本身是纯文字贴，所以多数时候是 NULL/空数组。
    pics = Column(Text)
    image_desc = Column(Text)  # 配图识别结果


class Summary(Base):
    __tablename__ = "summaries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String, index=True)
    period = Column(String)
    content = Column(Text)
    model = Column(String)
    created_ts = Column(Integer)


def _dump_pics(urls) -> Optional[str]:
    """配图 URL 列表 -> JSON 字符串。空列表存 NULL，"有没有图"一眼可判。"""
    if not urls:
        return None
    clean = [u for u in urls if isinstance(u, str) and u]
    return json.dumps(clean, ensure_ascii=False) if clean else None


def _load_pics(raw: Optional[str]) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [u for u in data if isinstance(u, str) and u]


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db():
    with engine.begin() as connection:
        if IS_POSTGRES:
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(82001431)")
        Base.metadata.create_all(connection)
        _migrate_columns(connection)
    if not config.CLOUD_MODE:
        seed_uppers()


def _migrate_columns(connection=None):
    """Lightweight migration: ADD COLUMN for tables created before a column
    existed (create_all won't alter existing tables)."""
    wanted = {
        "videos": [
            ("subtitle", "TEXT"),
            ("subtitle_segments", "TEXT"),
            ("summary", "TEXT"),
            ("image_desc", "TEXT"),
        ],
        "uppers": [("subscribed", "INTEGER NOT NULL DEFAULT 1")],
        "dynamics": [("pics", "TEXT"), ("image_desc", "TEXT")],
    }
    with (nullcontext(connection) if connection is not None else engine.begin()) as conn:
        for table, cols in wanted.items():
            existing = {col["name"] for col in inspect(conn).get_columns(table)}
            for name, ddl in cols:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def seed_uppers():
    """Bootstrap the configured UP masters on first run.

    uppers 表本身就是订阅名单：.env 的 UP_UIDS 仅在对应行不存在时引导插入，
    不会重新激活用户已取消订阅（subscribed=0）的 UP 主。
    """
    with session_scope() as s:
        for uid in config.UP_UIDS:
            existing = s.get(Upper, str(uid))
            if existing is None:
                s.add(Upper(uid=str(uid), name=config.up_name(uid), face=None,
                            subscribed=1))
            elif existing.subscribed and not config.CLOUD_MODE:
                existing.name = config.up_name(uid)


def subscribed_uids() -> list:
    """当前订阅中的 UID 列表（爬取/总结以此为准）。

    DB 尚未初始化（如表还不存在）时回退到 config.UP_UIDS，保证模块导入期
    的调用（如 crawler_runner._load_state）不炸。
    """
    try:
        with session_scope() as s:
            rows = s.execute(
                select(Upper.uid).where(Upper.subscribed == 1)
            ).scalars().all()
            return [str(u) for u in rows]
    except Exception:
        return [str(u) for u in config.UP_UIDS]


def subscribed_uppers() -> list:
    """当前订阅中的 (uid, name) 列表，供状态展示用。"""
    try:
        with session_scope() as s:
            rows = s.execute(
                select(Upper.uid, Upper.name).where(Upper.subscribed == 1)
            ).all()
            return [{"uid": str(uid), "name": name} for uid, name in rows]
    except Exception:
        return [{"uid": str(u), "name": config.up_name(u)} for u in config.UP_UIDS]


def add_upper(uid: str, name: str, face=None) -> bool:
    """加入订阅。返回 True=新增/恢复, False=原本就在订阅中。"""
    uid = str(uid).strip()
    with session_scope() as s:
        existing = s.get(Upper, uid)
        if existing is None:
            s.add(Upper(uid=uid, name=name, face=face, subscribed=1))
            return True
        if existing.subscribed:
            return False
        existing.subscribed = 1
        if name:
            existing.name = name
        if face and not existing.face:
            existing.face = face
        return True


def remove_upper(uid: str) -> bool:
    """取消订阅（保留其视频/动态历史数据）。返回 True=确实取消了订阅。"""
    uid = str(uid).strip()
    with session_scope() as s:
        existing = s.get(Upper, uid)
        if existing is None or not existing.subscribed:
            return False
        existing.subscribed = 0
        return True


# ---------------------------------------------------------------------------
# UPSERT helpers
# ---------------------------------------------------------------------------
def upsert_videos(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    n = 0
    with session_scope() as s:
        for r in rows:
            stmt = upsert_insert(Video).values(**r)
            stmt = stmt.on_conflict_do_update(
                index_elements=[Video.bvid],
                set_={
                    "title": stmt.excluded.title,
                    "cover": stmt.excluded.cover,
                    "desc": stmt.excluded.desc,
                    "pub_ts": stmt.excluded.pub_ts,
                    "play": stmt.excluded.play,
                    "url": stmt.excluded.url,
                },
            )
            s.execute(stmt)
            n += 1
    return n


def upsert_dynamics(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    n = 0
    # 配图在调用方是 list，落库前转成 JSON 字符串（空列表存 NULL）
    rows = [
        {**r, "pics": _dump_pics(r.get("pics")) if isinstance(r.get("pics"), (list, tuple)) else r.get("pics")}
        for r in rows
    ]
    with session_scope() as s:
        for r in rows:
            stmt = upsert_insert(Dynamic).values(**r)
            stmt = stmt.on_conflict_do_update(
                index_elements=[Dynamic.dyn_id],
                set_={
                    "type": stmt.excluded.type,
                    "text": stmt.excluded.text,
                    "pub_ts": stmt.excluded.pub_ts,
                    "url": stmt.excluded.url,
                    # pics 会随动态更新；image_desc 故意不在这里：
                    # 重新爬取不能把已经识别好的结果覆盖成 NULL
                    "pics": stmt.excluded.pics,
                },
            )
            s.execute(stmt)
            n += 1
    return n


def update_upper_face(uid: str, face: Optional[str]):
    if not face:
        return
    with session_scope() as s:
        u = s.get(Upper, str(uid))
        if u is not None and not u.face:
            u.face = face


def update_video_subtitle(bvid: str, text: str, desc: str = "",
                          play: Optional[int] = None, pubdate: Optional[int] = None,
                          segments=None):
    """写字幕，并可选回填 view 接口的元数据（feed 拿不到的字段）。"""
    with session_scope() as s:
        v = s.get(Video, bvid)
        if v is None:
            return
        if text:
            v.subtitle = text
            v.subtitle_segments = json.dumps(segments, ensure_ascii=False) if segments else None
        if desc:
            v.desc = desc
        if play:
            v.play = int(play)
        if pubdate:
            v.pub_ts = int(pubdate)


def update_video_summary(bvid: str, summary: str):
    with session_scope() as s:
        v = s.get(Video, bvid)
        if v is not None:
            v.summary = summary


# 图片识别失败时写进 image_desc 的前缀。带上它就不会被当成"还没识别过"而每轮
# 重试（省钱），展示层/日报用这个前缀把它过滤掉。
IMAGE_FAIL_PREFIX = "[识别失败]"


def set_video_image_desc(bvid: str, text: str):
    with session_scope() as s:
        v = s.get(Video, bvid)
        if v is not None:
            v.image_desc = text


def set_dynamic_image_desc(dyn_id: str, text: str):
    with session_scope() as s:
        d = s.get(Dynamic, dyn_id)
        if d is not None:
            d.image_desc = text


def videos_missing_image_desc(since_ts: int, limit: int = 20) -> list:
    """近 since_ts 内、有封面但还没做过图片识别的视频（按发布时间倒序，跨所有 UP）。

    只挑 image_desc 为 NULL 的：失败时我们会写入带前缀的占位文本，
    所以识别失败的行不会被反复重试，也就不会把配额耗在同一个坏图上。
    """
    with session_scope() as s:
        rows = (
            s.execute(
                select(Video)
                .where(
                    Video.pub_ts >= since_ts,
                    Video.cover.isnot(None),
                    Video.cover != "",
                    Video.image_desc.is_(None),
                )
                .order_by(Video.pub_ts.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [
            {"bvid": r.bvid, "uid": r.uid, "title": r.title or "",
             "cover": r.cover, "pub_ts": r.pub_ts}
            for r in rows
        ]


def dynamics_missing_image_desc(since_ts: int, limit: int = 20) -> list:
    """近 since_ts 内、确实带配图且还没识别过的动态。

    pics 在 SQL 里就过滤掉 NULL/空数组，避免全表扫完再在 Python 里筛。
    """
    with session_scope() as s:
        rows = (
            s.execute(
                select(Dynamic)
                .where(
                    Dynamic.pub_ts >= since_ts,
                    Dynamic.image_desc.is_(None),
                    Dynamic.pics.isnot(None),
                    Dynamic.pics != "",
                    Dynamic.pics != "[]",
                )
                .order_by(Dynamic.pub_ts.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [
            {"dyn_id": r.dyn_id, "uid": r.uid, "text": r.text or "",
             "pics": _load_pics(r.pics), "pub_ts": r.pub_ts}
            for r in rows
        ]


def list_videos_missing_summary_since(since_ts: int) -> list:
    """近 since_ts 内、还没有 AI 摘要的视频（批量回填用，跨所有 UP）。"""
    with session_scope() as s:
        rows = (
            s.execute(
                select(Video)
                .where(
                    Video.pub_ts >= since_ts,
                    (Video.summary.is_(None)) | (Video.summary == ""),
                )
                .order_by(Video.pub_ts.desc())
            )
            .scalars()
            .all()
        )
        return [
            {"bvid": r.bvid, "uid": r.uid, "title": r.title or "",
             "desc": r.desc or "", "pub_ts": r.pub_ts, "url": r.url or ""}
            for r in rows
        ]


def recent_videos_unsummarized(uid: str, since_ts: int, limit: int = 5) -> list:
    """近期新发布且还没有 AI 摘要的视频（字幕/摘要补充流程的工作队列）。"""
    with session_scope() as s:
        rows = (
            s.execute(
                select(Video)
                .where(
                    Video.uid == str(uid),
                    Video.pub_ts >= since_ts,
                    (Video.summary.is_(None)) | (Video.summary == ""),
                )
                .order_by(Video.pub_ts.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [
            {"bvid": r.bvid, "title": r.title, "desc": r.desc,
             "pub_ts": r.pub_ts, "url": r.url}
            for r in rows
        ]


def insert_summary(date: str, period: str, content: str, model: str) -> int:
    with session_scope() as s:
        row = Summary(
            date=date,
            period=period,
            content=content,
            model=model,
            created_ts=int(time.time()),
        )
        s.add(row)
        s.flush()
        return row.id


# ---------------------------------------------------------------------------
# query helpers (return plain dicts matching API contract)
# ---------------------------------------------------------------------------
def list_uppers() -> list:
    out = []
    with session_scope() as s:
        uppers = (
            s.execute(select(Upper).where(Upper.subscribed == 1))
            .scalars()
            .all()
        )
        for u in uppers:
            vc = s.execute(
                select(func.count()).select_from(Video).where(Video.uid == u.uid)
            ).scalar_one()
            dc = s.execute(
                select(func.count()).select_from(Dynamic).where(Dynamic.uid == u.uid)
            ).scalar_one()
            out.append(
                {
                    "uid": u.uid,
                    "name": u.name,
                    "face": u.face,
                    "video_count": vc,
                    "dynamic_count": dc,
                    "last_crawl_ts": None,  # filled by caller from state
                }
            )
    return out


def list_videos(uid: str, limit: int = 30, offset: int = 0) -> list:
    with session_scope() as s:
        rows = (
            s.execute(
                select(Video)
                .where(Video.uid == str(uid))
                .order_by(Video.pub_ts.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return [
            {
                "bvid": r.bvid,
                "uid": r.uid,
                "title": r.title,
                "cover": r.cover,
                "pub_ts": r.pub_ts,
                "play": int(r.play) if r.play is not None else 0,
                "url": r.url,
                "desc": r.desc,
                "summary": r.summary,
                "image_desc": r.image_desc,
            }
            for r in rows
        ]


def list_dynamics(uid: str, limit: int = 30, offset: int = 0) -> list:
    with session_scope() as s:
        rows = (
            s.execute(
                select(Dynamic)
                .where(Dynamic.uid == str(uid))
                .order_by(Dynamic.pub_ts.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return [
            {
                "dyn_id": r.dyn_id,
                "uid": r.uid,
                "type": r.type,
                "text": r.text,
                "pub_ts": r.pub_ts,
                "url": r.url,
                "pics": _load_pics(r.pics),
                "image_desc": r.image_desc,
            }
            for r in rows
        ]


def list_summaries(date: Optional[str] = None) -> list:
    with session_scope() as s:
        q = select(Summary)
        if date:
            q = q.where(Summary.date == date).order_by(Summary.created_ts.desc())
        else:
            q = q.order_by(Summary.created_ts.desc()).limit(10)
        rows = s.execute(q).scalars().all()
        return [
            {
                "id": r.id,
                "date": r.date,
                "period": r.period,
                "content": r.content,
                "model": r.model,
                "created_ts": r.created_ts,
            }
            for r in rows
        ]


def recent_videos_since(uid: str, since_ts: int) -> list:
    with session_scope() as s:
        rows = (
            s.execute(
                select(Video)
                .where(Video.uid == str(uid), Video.pub_ts >= since_ts)
                .order_by(Video.pub_ts.desc())
            )
            .scalars()
            .all()
        )
        return [
            {"bvid": r.bvid, "title": r.title, "desc": r.desc,
             "summary": r.summary, "pub_ts": r.pub_ts, "url": r.url,
             "subtitle": r.subtitle, "image_desc": r.image_desc}
            for r in rows
        ]


def recent_dynamics_since(uid: str, since_ts: int) -> list:
    with session_scope() as s:
        rows = (
            s.execute(
                select(Dynamic)
                .where(Dynamic.uid == str(uid), Dynamic.pub_ts >= since_ts)
                .order_by(Dynamic.pub_ts.desc())
            )
            .scalars()
            .all()
        )
        return [
            {"dyn_id": r.dyn_id, "type": r.type, "text": r.text,
             "pub_ts": r.pub_ts, "url": r.url,
             "pics": _load_pics(r.pics), "image_desc": r.image_desc}
            for r in rows
        ]
