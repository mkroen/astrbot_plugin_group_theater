"""SQLite-backed drafts and global rolling-window drawing reservations."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .story import TheaterError


class Store:
    def __init__(self, root: Path, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(self.root / "theater.db", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS drafts (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner TEXT NOT NULL,
          created REAL NOT NULL, expires REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL, source TEXT NOT NULL, story TEXT NOT NULL,
          image TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS attempts (
          id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS attempts_created ON attempts(created);
        """)
        # A process restart must never resubmit a possibly charged image request.
        self.db.execute(
            "UPDATE drafts SET status='unknown', error=? WHERE status='rendering'",
            ("插件在出图期间重载或退出，结果未知；不会自动补发。",),
        )

    def close(self):
        self.db.close()

    def create(self, scope: str, owner: str, source: list[dict], story: dict, ttl: int) -> dict:
        did = uuid.uuid4().hex[:12]
        now = self.clock()
        self.db.execute(
            "INSERT INTO drafts(id,scope,owner,created,expires,status,source,story) VALUES(?,?,?,?,?,?,?,?)",
            (
                did,
                scope,
                owner,
                now,
                now + ttl,
                "draft",
                json.dumps(source, ensure_ascii=False),
                json.dumps(story, ensure_ascii=False),
            ),
        )
        return self.get(did, scope, owner)

    def get(self, did: str, scope: str, owner: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM drafts WHERE id=? AND scope=? AND owner=?", (did, scope, owner)
        ).fetchone()
        if not row:
            raise TheaterError("找不到这份分镜；只能访问本群中自己创建的分镜。")
        if row["expires"] <= self.clock():
            raise TheaterError("分镜已过期，请重新取材。")
        result = dict(row)
        for k in ("source", "story"):
            result[k] = json.loads(result[k])
        return result

    def revise(self, draft: dict, story: dict) -> dict:
        cursor = self.db.execute(
            "UPDATE drafts SET story=?,revision=revision+1 WHERE id=? AND revision=? AND status='draft' AND expires>?",
            (json.dumps(story, ensure_ascii=False), draft["id"], draft["revision"], self.clock()),
        )
        if cursor.rowcount != 1:
            raise TheaterError("分镜状态已改变或过期，不能覆盖，请重新查看。")
        return self.get(draft["id"], draft["scope"], draft["owner"])

    def reserve(self, draft: dict, limit: int, window: int) -> None:
        if window < 1 or limit < 1:
            raise TheaterError("绘图额度及窗口必须为正数，未提交绘图。")
        now = self.clock()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM drafts WHERE id=?", (draft["id"],)).fetchone()
            if (
                not row
                or row["status"] != "draft"
                or row["revision"] != draft["revision"]
                or row["expires"] <= now
            ):
                raise TheaterError("分镜已修改、过期或已提交；不会重复绘图。")
            if self.db.execute("SELECT 1 FROM drafts WHERE status='rendering'").fetchone():
                raise TheaterError("已有绘图请求正在执行，请等待结束。")
            attempts = self.db.execute(
                "SELECT created FROM attempts WHERE created>? ORDER BY created", (now - window,)
            ).fetchall()
            if limit < 1 or len(attempts) >= limit:
                wait = (
                    max(1, int((attempts[0]["created"] + window - now) / 60) + 1) if attempts else 1
                )
                raise TheaterError(f"本插件绘图额度已用完，约{wait}分钟后再试；没有发送绘图请求。")
            self.db.execute(
                "INSERT INTO attempts(draft_id,created) VALUES(?,?)", (draft["id"], now)
            )
            self.db.execute("UPDATE drafts SET status='rendering' WHERE id=?", (draft["id"],))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def finish(self, did: str, status: str, image: str = "", error: str = ""):
        if status not in {"done", "failed", "unknown"}:
            raise ValueError(status)
        self.db.execute(
            "UPDATE drafts SET status=?,image=?,error=? WHERE id=? AND status='rendering'",
            (status, image, error, did),
        )

    def cancel(self, draft: dict):
        changed = self.db.execute(
            "UPDATE drafts SET status='cancelled' WHERE id=? AND status='draft'", (draft["id"],)
        ).rowcount
        if not changed:
            raise TheaterError("只有未出图的草稿可以取消；已提交的上游请求不能撤回。")

    def quota(self, window: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM attempts WHERE created>?", (self.clock() - window,)
        ).fetchone()[0]

    def cleanup(self) -> int:
        rows = self.db.execute(
            "SELECT id,image FROM drafts WHERE expires<=? AND status!='rendering'", (self.clock(),)
        ).fetchall()
        for row in rows:
            # Only remove our own managed images, never a path supplied by model output.
            for suffix in (".png", ".png.tmp"):
                path = self.root / "images" / (row["id"] + suffix)
                if path.is_file():
                    path.unlink()
            self.db.execute("DELETE FROM drafts WHERE id=?", (row["id"],))
        # Quota reservations intentionally survive draft deletion.
        return len(rows)
