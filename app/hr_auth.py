"""HR 账号数据库 + bcrypt 密码散列。

库设计：
- 独立 DB 文件 `<data_root>/hr_users.db`，遵循项目一业务一 DB 惯例
- 单表 `hr_users`（id / username / password_hash / created_at / last_login_at）
- 启动时若表为空，按 .env 里的 HR_ADMIN_USERNAME/HR_ADMIN_PASSWORD 创建默认管理员
  - 密码为空时自动生成随机密码并 WARNING 日志打印一次
"""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import bcrypt

from app.paths import data_root
from config import settings

logger = logging.getLogger(__name__)

DB_PATH = data_root() / "hr_users.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 用户名规则：3-32 字符，字母开头，可含字母数字下划线连字符
USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,31}$")
MIN_PASSWORD_LEN = 6


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """创建 hr_users 表（若已存在则跳过）。"""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hr_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """
        )


def _hash_password(plain: str) -> str:
    """bcrypt 散列密码，返回 hex 字符串。"""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def seed_default_admin() -> None:
    """启动时调用：把 hr_users 库同步到 .env 的 HR_ADMIN_USERNAME / HR_ADMIN_PASSWORD。

    规则：
    - 表为空 → 按 .env 插入（密码为空时生成随机密码并 WARNING 打印）
    - 表有数据 + .env 密码为空 → 不动
    - 表有数据 + .env 密码已设 → 同步：env 用户名已存在则改密；只有 1 个旧用户时改名+改密；多个时新建一个
    """
    init_db()
    username = (settings.HR_ADMIN_USERNAME or "admin").strip() or "admin"
    plain_password = settings.HR_ADMIN_PASSWORD

    with _connect() as conn:
        existing = conn.execute("SELECT id, username FROM hr_users").fetchall()

    # 表为空：按 .env 创建
    if not existing:
        if not plain_password:
            plain_password = secrets.token_urlsafe(16)
            logger.warning(
                "[hr_auth] HR_ADMIN_PASSWORD 未配置，已自动生成默认管理员密码（仅本次启动有效，重启后会重新生成）：username=%s password=%s",
                username,
                plain_password,
            )
        with _connect() as conn:
            conn.execute(
                "INSERT INTO hr_users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, _hash_password(plain_password), _now_iso()),
            )
        logger.info("[hr_auth] 已创建默认 HR 管理员 username=%s", username)
        return

    # 表有数据：.env 没设密码就不动
    if not plain_password:
        return

    # .env 密码已设：把指定 username 同步到 env 状态
    target = next((r for r in existing if r["username"] == username), None)
    if target:
        with _connect() as conn:
            conn.execute(
                "UPDATE hr_users SET password_hash = ? WHERE id = ?",
                (_hash_password(plain_password), target["id"]),
            )
        logger.info("[hr_auth] 已按 .env 同步管理员 %s 的密码", username)
        return

    if len(existing) == 1:
        # 唯一旧用户 → 改名 + 改密（典型迁移场景：admin → HR01）
        old_username = existing[0]["username"]
        with _connect() as conn:
            conn.execute(
                "UPDATE hr_users SET username = ?, password_hash = ? WHERE id = ?",
                (username, _hash_password(plain_password), existing[0]["id"]),
            )
        logger.info(
            "[hr_auth] 已将唯一管理员从 %s 改名为 %s 并按 .env 重置密码",
            old_username,
            username,
        )
        return

    # 多个用户且都不匹配 env 指定的管理员 → 新建一个
    with _connect() as conn:
        conn.execute(
            "INSERT INTO hr_users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, _hash_password(plain_password), _now_iso()),
        )
    logger.warning(
        "[hr_auth] 数据库已有 %d 个 HR 账号，但 .env 指定的管理员 %s 不在其中；已新建一个（不会影响现有账号）",
        len(existing),
        username,
    )


def verify_login(username: str, password: str) -> bool:
    """校验账号密码。校验成功会更新 last_login_at，返回 True。"""
    if not username or not password:
        return False
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash FROM hr_users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return False
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8"))
    except (ValueError, TypeError):
        return False
    if ok:
        with _connect() as conn:
            conn.execute(
                "UPDATE hr_users SET last_login_at = ? WHERE id = ?",
                (_now_iso(), row["id"]),
            )
    return ok


def get_user(username: str) -> Optional[Dict[str, Any]]:
    """取账号行（不返回 hash）。"""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, username, created_at, last_login_at FROM hr_users WHERE username = ?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    """列出所有 HR 账号（不返回 hash，仅诊断/管理用）。"""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at, last_login_at FROM hr_users ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def _validate_username(username: str) -> Optional[str]:
    if not username:
        return "请输入用户名"
    if not USERNAME_PATTERN.match(username):
        return "用户名需 3-32 字符，字母开头，可含字母、数字、下划线、连字符"
    return None


def _validate_password(password: str) -> Optional[str]:
    if not password:
        return "请输入密码"
    if len(password) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    return None


def create_user(username: str, password: str) -> Tuple[bool, str]:
    """自注册：创建新 HR 账号。

    返回 (success, message)。message 既用于日志，也用于回显到注册页。
    依赖 username UNIQUE 约束兜底重名（避免先 SELECT 后 INSERT 的竞态）。
    """
    err = _validate_username(username)
    if err:
        return False, err
    err = _validate_password(password)
    if err:
        return False, err

    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO hr_users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, _hash_password(password), _now_iso()),
            )
    except sqlite3.IntegrityError:
        return False, "该用户名已被注册"

    logger.info("[hr_auth] 自注册成功 username=%s", username)
    return True, "注册成功"