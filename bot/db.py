"""Слой доступа к PostgreSQL (asyncpg)."""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

import asyncpg

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    # ------------------------------------------------------------------ init
    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
            command_timeout=30,
            max_inactive_connection_lifetime=300,
        )
        log.info("Подключение к PostgreSQL установлено")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Пул соединений не инициализирован — вызовите connect()")
        return self._pool

    async def init_schema(self) -> None:
        """Идемпотентно накатывает schema.sql (CREATE TABLE IF NOT EXISTS)."""
        if not SCHEMA_PATH.exists():
            log.warning("schema.sql не найден по пути %s — пропускаю миграцию", SCHEMA_PATH)
            return
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        async with self.pool.acquire() as conn:
            await conn.execute(sql)
        log.info("Схема БД проверена/создана")

    # --------------------------------------------------------------- helpers
    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(sql, *args)

    # ----------------------------------------------------------------- users
    async def upsert_user(
        self,
        tg_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        *,
        mark_joined: bool = False,
    ) -> asyncpg.Record:
        """Создаёт пользователя или обновляет профиль. mark_joined — при входе в группу."""
        if mark_joined:
            sql = """
                INSERT INTO users (tg_id, username, first_name, last_name, joined_at, left_at, last_seen_at)
                VALUES ($1, $2, $3, $4, NOW(), NULL, NOW())
                ON CONFLICT (tg_id) DO UPDATE
                   SET username = EXCLUDED.username,
                       first_name = EXCLUDED.first_name,
                       last_name = EXCLUDED.last_name,
                       joined_at = NOW(),
                       left_at = NULL,
                       last_seen_at = NOW()
                RETURNING *
            """
        else:
            sql = """
                INSERT INTO users (tg_id, username, first_name, last_name, last_seen_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (tg_id) DO UPDATE
                   SET username = EXCLUDED.username,
                       first_name = EXCLUDED.first_name,
                       last_name = EXCLUDED.last_name,
                       last_seen_at = NOW()
                RETURNING *
            """
        row = await self.fetchrow(sql, tg_id, username, first_name, last_name)
        assert row is not None
        return row

    async def get_user(self, tg_id: int) -> asyncpg.Record | None:
        return await self.fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)

    async def find_user_by_username(self, username: str) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM users WHERE LOWER(username) = LOWER($1)", username.lstrip("@")
        )

    async def mark_left(self, tg_id: int) -> None:
        await self.execute("UPDATE users SET left_at = NOW() WHERE tg_id = $1", tg_id)

    async def set_trusted(self, tg_id: int, trusted: bool) -> None:
        await self.execute("UPDATE users SET is_trusted = $2 WHERE tg_id = $1", tg_id, trusted)

    async def set_admin(self, tg_id: int, is_admin: bool) -> None:
        await self.execute("UPDATE users SET is_admin = $2 WHERE tg_id = $1", tg_id, is_admin)

    async def is_exempt(self, tg_id: int) -> bool:
        """Доверенный пользователь, админ или менеджер — одним запросом (вызывается часто)."""
        return bool(
            await self.fetchval(
                """
                SELECT EXISTS (SELECT 1 FROM users    WHERE tg_id = $1 AND (is_trusted OR is_admin))
                    OR EXISTS (SELECT 1 FROM managers WHERE tg_id = $1 AND enabled)
                """,
                tg_id,
            )
        )

    async def add_warn(self, tg_id: int) -> int:
        return int(
            await self.fetchval(
                "UPDATE users SET warns = warns + 1 WHERE tg_id = $1 RETURNING warns", tg_id
            )
            or 0
        )

    async def reset_warns(self, tg_id: int) -> None:
        await self.execute("UPDATE users SET warns = 0 WHERE tg_id = $1", tg_id)

    async def add_mute(self, tg_id: int) -> int:
        return int(
            await self.fetchval(
                "UPDATE users SET mutes = mutes + 1, warns = 0 WHERE tg_id = $1 RETURNING mutes",
                tg_id,
            )
            or 0
        )

    async def set_banned(self, tg_id: int, banned: bool) -> None:
        await self.execute("UPDATE users SET banned = $2 WHERE tg_id = $1", tg_id, banned)

    # ----------------------------------------------------------------- leads
    async def create_lead(
        self,
        *,
        user_tg_id: int,
        username: str | None,
        full_name: str | None,
        chat_id: int | None,
        thread_id: int | None,
        message_id: int | None,
        source: str,
        raw_text: str,
        category: str | None,
        weight_kg: Decimal | None,
        volume_m3: Decimal | None,
    ) -> asyncpg.Record:
        row = await self.fetchrow(
            """
            INSERT INTO leads (user_tg_id, username, full_name, chat_id, thread_id, message_id,
                               source, raw_text, category, weight_kg, volume_m3)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            user_tg_id, username, full_name, chat_id, thread_id, message_id,
            source, raw_text, category, weight_kg, volume_m3,
        )
        assert row is not None
        return row

    async def mark_lead_forwarded(self, lead_id: int, manager_msg_id: int | None) -> None:
        await self.execute(
            """
            UPDATE leads
               SET status = 'forwarded', forwarded_at = NOW(), manager_msg_id = $2
             WHERE id = $1
            """,
            lead_id, manager_msg_id,
        )

    async def set_lead_status(self, lead_id: int, status: str) -> None:
        await self.execute("UPDATE leads SET status = $2 WHERE id = $1", lead_id, status)

    async def recent_lead_exists(self, user_tg_id: int, minutes: int) -> bool:
        return bool(
            await self.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM leads
                     WHERE user_tg_id = $1
                       AND created_at > NOW() - ($2 || ' minutes')::interval
                )
                """,
                user_tg_id, str(minutes),
            )
        )

    async def has_lead_since_join(self, user_tg_id: int) -> bool:
        return bool(
            await self.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM leads l
                      JOIN users u ON u.tg_id = l.user_tg_id
                     WHERE l.user_tg_id = $1 AND l.created_at >= u.joined_at
                )
                """,
                user_tg_id,
            )
        )

    async def joined_within_hours(self, user_tg_id: int, hours: int) -> bool:
        return bool(
            await self.fetchval(
                """
                SELECT joined_at > NOW() - ($2 || ' hours')::interval
                  FROM users WHERE tg_id = $1
                """,
                user_tg_id, str(hours),
            )
        )

    async def last_leads(self, limit: int = 10) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM leads ORDER BY created_at DESC LIMIT $1", limit
        )

    # -------------------------------------------------------------- warnings
    async def log_warning(
        self,
        *,
        user_tg_id: int,
        chat_id: int | None,
        reason: str,
        score: int,
        message_text: str | None,
        action: str,
        by_admin_id: int | None = None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO warnings (user_tg_id, chat_id, reason, score, message_text, action, by_admin_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            user_tg_id, chat_id, reason, score, message_text, action, by_admin_id,
        )

    # ------------------------------------------------------------ spam rules
    async def get_spam_rules(self) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT id, pattern, kind, score FROM spam_rules WHERE enabled ORDER BY id"
        )

    async def add_spam_rule(
        self, pattern: str, kind: str, score: int, created_by: int | None, note: str | None = None
    ) -> bool:
        result = await self.execute(
            """
            INSERT INTO spam_rules (pattern, kind, score, note, created_by)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (pattern) DO UPDATE
               SET enabled = TRUE, kind = EXCLUDED.kind, score = EXCLUDED.score
            """,
            pattern.lower().strip(), kind, score, note, created_by,
        )
        return result.startswith("INSERT")

    async def disable_spam_rule(self, rule_id: int) -> bool:
        result = await self.execute(
            "UPDATE spam_rules SET enabled = FALSE WHERE id = $1", rule_id
        )
        return result != "UPDATE 0"

    # -------------------------------------------------------------- managers
    async def upsert_manager(
        self, tg_id: int, username: str | None, full_name: str | None, chat_id: int
    ) -> None:
        await self.execute(
            """
            INSERT INTO managers (tg_id, username, full_name, chat_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tg_id) DO UPDATE
               SET username = EXCLUDED.username,
                   full_name = EXCLUDED.full_name,
                   chat_id = EXCLUDED.chat_id,
                   enabled = TRUE
            """,
            tg_id, username, full_name, chat_id,
        )

    async def manager_chat_ids(self) -> list[int]:
        rows = await self.fetch(
            "SELECT chat_id FROM managers WHERE enabled ORDER BY is_primary DESC, created_at"
        )
        return [r["chat_id"] for r in rows]

    # -------------------------------------------------------------- settings
    async def get_setting(self, key: str) -> str | None:
        return await self.fetchval("SELECT value FROM settings WHERE key = $1", key)

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            key, value,
        )

    # ----------------------------------------------------------------- stats
    async def stats(self) -> dict[str, int]:
        row = await self.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM users WHERE left_at IS NULL)                        AS members,
                (SELECT COUNT(*) FROM leads)                                              AS leads_total,
                (SELECT COUNT(*) FROM leads WHERE created_at > NOW() - INTERVAL '1 day')  AS leads_today,
                (SELECT COUNT(*) FROM leads WHERE created_at > NOW() - INTERVAL '7 days') AS leads_week,
                (SELECT COUNT(*) FROM warnings WHERE created_at > NOW() - INTERVAL '1 day') AS spam_today,
                (SELECT COUNT(*) FROM warnings)                                           AS spam_total
            """
        )
        return dict(row) if row else {}
