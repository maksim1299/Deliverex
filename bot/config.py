"""Конфигурация бота: читается из переменных окружения (.env локально, Variables на Railway)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # на проде dotenv не обязателен
    pass


def _str(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _int(key: str, default: int) -> int:
    raw = _str(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _opt_int(key: str) -> int | None:
    raw = _str(key)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _bool(key: str, default: bool = False) -> bool:
    raw = _str(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "да"}


def _int_set(key: str) -> set[int]:
    out: set[int] = set()
    for chunk in _str(key).replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


def _normalize_dsn(dsn: str) -> str:
    """asyncpg не понимает схему postgresql+asyncpg:// из SQLAlchemy."""
    if dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + dsn.split("://", 1)[1]
    if dsn.startswith("postgres+asyncpg://"):
        return "postgres://" + dsn.split("://", 1)[1]
    return dsn


@dataclass(frozen=True)
class Config:
    # --- обязательное ---
    bot_token: str
    database_url: str

    # --- маршрутизация заявок ---
    group_chat_id: int | None          # ID рабочей группы (-100...)
    manager_username: str              # @kurotoplol — для отображения
    manager_chat_id: int | None        # куда слать заявки (если не задан — берётся из БД)
    admin_ids: set[int] = field(default_factory=set)

    # --- приветствие ---
    welcome_delete_after: int = 0      # сек; 0 = не удалять приветствие в группе
    welcome_dm: bool = True            # дублировать приветствие в личку новичку
    welcome_mention: bool = True       # добавлять обращение по имени перед текстом
    welcome_topic_id: int | None = None  # ID темы для приветствий (пусто = General)
    lead_window_hours: int = 48        # сколько часов после входа ждём заявку
    lead_cooldown_min: int = 10        # антидубль заявок от одного клиента

    # --- антиспам ---
    antispam_enabled: bool = True
    spam_score_threshold: int = 3      # с какого балла считаем спамом
    warn_limit: int = 3                # предупреждений до мута
    mute_minutes: int = 60             # длительность мута
    ban_after_mutes: int = 3           # 0 = никогда не банить
    newbie_hours: int = 24             # первые N часов участник «новичок» (строже фильтр)
    flood_messages: int = 5            # N сообщений
    flood_seconds: int = 10            # за M секунд = флуд
    delete_service_messages: bool = True  # чистить «X вошёл в чат»

    # --- запуск ---
    webhook_url: str = ""              # пусто = long polling
    webhook_path: str = "/webhook"
    webhook_secret: str = ""
    port: int = 8080
    log_level: str = "INFO"

    @property
    def use_webhook(self) -> bool:
        return bool(self.webhook_url)


def load_config() -> Config:
    token = _str("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Укажите его в .env или в Variables на Railway.")

    dsn = _str("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL не задан. На Railway используйте ${{Postgres.DATABASE_URL}}.")

    manager = _str("MANAGER_USERNAME", "kurotoplol").lstrip("@")

    return Config(
        bot_token=token,
        database_url=_normalize_dsn(dsn),
        group_chat_id=_opt_int("GROUP_CHAT_ID"),
        manager_username=manager,
        manager_chat_id=_opt_int("MANAGER_CHAT_ID"),
        admin_ids=_int_set("ADMIN_IDS"),
        welcome_delete_after=_int("WELCOME_DELETE_AFTER", 0),
        welcome_dm=_bool("WELCOME_DM", True),
        welcome_mention=_bool("WELCOME_MENTION", True),
        welcome_topic_id=_opt_int("WELCOME_TOPIC_ID"),
        lead_window_hours=_int("LEAD_WINDOW_HOURS", 48),
        lead_cooldown_min=_int("LEAD_COOLDOWN_MIN", 10),
        antispam_enabled=_bool("ANTISPAM_ENABLED", True),
        spam_score_threshold=_int("SPAM_SCORE_THRESHOLD", 3),
        warn_limit=_int("WARN_LIMIT", 3),
        mute_minutes=_int("MUTE_MINUTES", 60),
        ban_after_mutes=_int("BAN_AFTER_MUTES", 3),
        newbie_hours=_int("NEWBIE_HOURS", 24),
        flood_messages=_int("FLOOD_MESSAGES", 5),
        flood_seconds=_int("FLOOD_SECONDS", 10),
        delete_service_messages=_bool("DELETE_SERVICE_MESSAGES", True),
        webhook_url=_str("WEBHOOK_URL"),
        webhook_path=_str("WEBHOOK_PATH", "/webhook"),
        webhook_secret=_str("WEBHOOK_SECRET"),
        port=_int("PORT", 8080),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )
