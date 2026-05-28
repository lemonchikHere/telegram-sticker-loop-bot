from __future__ import annotations

import asyncio
from collections import deque
import html
import secrets
import sqlite3
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from telegram import (
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Sticker,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


ROOT = Path(__file__).resolve().parents[1]
VAR_DIR = ROOT / "var"
RUNS_DIR = VAR_DIR / "runs"
LOG_DIR = ROOT / "logs"
ENV_PATH = ROOT / ".env"
LIMITS_STATE_PATH = VAR_DIR / "limits.json"
DB_PATH = VAR_DIR / "bot.sqlite3"
MENU_ASSETS_PATH = VAR_DIR / "menu_assets.json"

BACKGROUND_PRESETS = {
    "dark": ("Dark", "#080a0f"),
    "black": ("Black", "#000000"),
    "graphite": ("Graphite", "#111827"),
    "white": ("White", "#f8fafc"),
    "blue": ("Blue", "#0f172a"),
    "green": ("Green", "#052e2b"),
}

RESOLUTION_PRESETS = {
    "512x512": (512, 512, 30),
    "640x360": (640, 360, 30),
    "1280x720": (1280, 720, 30),
    "1920x1080": (1920, 1080, 30),
    "1920x600": (1920, 600, 30),
}



@dataclass(frozen=True)
class RenderSettings:
    background_key: str
    background_hex: str
    width: int
    height: int
    sticker_size: int
    fps: int
    static_seconds: float
    output_format: str
    item_color_hex: str | None
    notes: str
    watermark_enabled: bool
    watermark_text: str


@dataclass(frozen=True)
class SourceRef:
    file_id: str
    label: str


@dataclass(frozen=True)
class SafetyConfig:
    max_global_renders: int
    per_user_window_jobs: int
    per_user_window_seconds: int
    per_user_min_gap_seconds: int
    spam_events_before_ban: int
    spam_window_seconds: int
    ban_seconds: int
    max_source_bytes: int
    max_output_bytes: int
    render_timeout_seconds: int
    runs_retention_seconds: int


@dataclass
class UserLimitState:
    render_times: deque[float]
    violation_times: deque[float]
    banned_until: float = 0.0


@dataclass(frozen=True)
class BroadcastDraft:
    sender_id: int
    created_at: float
    target_user_ids: tuple[int, ...]
    text: str | None = None
    copy_from_chat_id: int | None = None
    copy_message_id: int | None = None


@dataclass(frozen=True)
class PendingAction:
    action: str
    chat_id: int
    message_id: int
    surface: str


class UserFacingError(Exception):
    pass


class RenderGate:
    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self.active = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self.active >= self.limit:
                return False
            self.active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self.active = max(0, self.active - 1)


USER_SETTINGS: dict[int, RenderSettings] = {}
LAST_SOURCE: dict[int, SourceRef] = {}
PENDING_ACTIONS: dict[int, PendingAction] = {}
BUSY: set[int] = set()
USER_LIMITS: dict[int, UserLimitState] = {}
GLOBAL_RENDER_GATE: RenderGate | None = None
BROADCAST_DRAFTS: dict[str, BroadcastDraft] = {}
HAS_DRAWTEXT_FILTER: bool | None = None
MENU_ASSETS: list[str] = []
MENU_SECTION_ASSETS: dict[str, list[str]] = {}

MENU_ASSET_SECTIONS = {
    "main": "главное меню",
    "palette": "палитра",
}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def parse_int_list(raw: str | None) -> set[int]:
    if not raw:
        return set()
    values = set()
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError:
            logging.warning("Ignoring invalid integer in env list: %s", item)
    return values


def log_chat_id() -> int | str | None:
    raw = os.getenv("LOG_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def safety_config() -> SafetyConfig:
    return SafetyConfig(
        max_global_renders=env_int("MAX_GLOBAL_RENDERS", 2),
        per_user_window_jobs=env_int("PER_USER_WINDOW_JOBS", 2),
        per_user_window_seconds=env_int("PER_USER_WINDOW_SECONDS", 60),
        per_user_min_gap_seconds=env_int("PER_USER_MIN_GAP_SECONDS", 5),
        spam_events_before_ban=env_int("SPAM_EVENTS_BEFORE_BAN", 6),
        spam_window_seconds=env_int("SPAM_WINDOW_SECONDS", 60),
        ban_seconds=env_int("BAN_SECONDS", 3600),
        max_source_bytes=env_int("MAX_SOURCE_BYTES", 10 * 1024 * 1024),
        max_output_bytes=env_int("MAX_OUTPUT_BYTES", 45 * 1024 * 1024),
        render_timeout_seconds=env_int("RENDER_TIMEOUT_SECONDS", 75),
        runs_retention_seconds=env_int("RUNS_RETENTION_SECONDS", 12 * 60 * 60),
    )


def default_settings() -> RenderSettings:
    key = os.getenv("DEFAULT_BACKGROUND", "dark")
    label, color = BACKGROUND_PRESETS.get(key, BACKGROUND_PRESETS["dark"])
    return RenderSettings(
        background_key=key if key in BACKGROUND_PRESETS else label.lower(),
        background_hex=color,
        width=env_int("OUTPUT_WIDTH", 640),
        height=env_int("OUTPUT_HEIGHT", 360),
        sticker_size=env_int("STICKER_SIZE", 220),
        fps=env_int("OUTPUT_FPS", 30),
        static_seconds=env_float("STATIC_SECONDS", 2.0),
        output_format=env_str("DEFAULT_OUTPUT_FORMAT", "gif").strip().lower(),
        item_color_hex=None,
        notes="",
        watermark_enabled=env_bool("WATERMARK_ENABLED", False),
        watermark_text=env_str("WATERMARK_TEXT", "StickerLoop").strip(),
    )


def settings_for(user_id: int) -> RenderSettings:
    return USER_SETTINGS.setdefault(user_id, default_settings())


def update_settings(user_id: int, **changes) -> RenderSettings:
    current = settings_for(user_id)
    updated = replace(current, **changes)
    USER_SETTINGS[user_id] = updated
    return updated


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def db_connect() -> sqlite3.Connection:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                is_bot INTEGER NOT NULL DEFAULT 0,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                render_count INTEGER NOT NULL DEFAULT 0,
                last_action TEXT,
                blocked_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_users_blocked_last_seen
            ON users(blocked_at, last_seen)
            """
        )


def upsert_user(user, action: str, *, render_started: bool = False) -> bool:
    now = int(time.time())
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user.id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO users (
                user_id, username, first_name, last_name, language_code, is_bot,
                first_seen, last_seen, message_count, render_count, last_action
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                language_code = excluded.language_code,
                is_bot = excluded.is_bot,
                last_seen = excluded.last_seen,
                message_count = users.message_count + 1,
                last_action = excluded.last_action,
                blocked_at = NULL
            """,
            (
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                user.language_code,
                int(user.is_bot),
                now,
                now,
                0,
                action,
            ),
        )
        return existing is None


def mark_user_blocked(user_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET blocked_at = ?, last_seen = ? WHERE user_id = ?",
            (int(time.time()), int(time.time()), user_id),
        )


def known_user_ids() -> list[int]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE blocked_at IS NULL ORDER BY first_seen"
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def user_stats() -> dict[str, int]:
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN blocked_at IS NULL THEN 1 ELSE 0 END) AS reachable,
                SUM(render_count) AS renders
            FROM users
            """
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "reachable": int(row["reachable"] or 0),
        "renders": int(row["renders"] or 0),
    }


def user_display(user) -> str:
    parts = [str(user.id)]
    if user.username:
        parts.append(f"@{user.username}")
    name = " ".join(part for part in [user.first_name, user.last_name] if part)
    if name:
        parts.append(name)
    return " | ".join(parts)


async def log_to_owner_chat(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    target = log_chat_id()
    if not target:
        return
    try:
        await context.bot.send_message(
            chat_id=target,
            text=text[:3900],
            disable_web_page_preview=True,
            read_timeout=20,
            connect_timeout=20,
        )
    except TelegramError:
        logging.exception("Failed to send owner log")


async def copy_render_source_to_owner_chat(context: ContextTypes.DEFAULT_TYPE, message: Message) -> None:
    target = log_chat_id()
    if not target or not env_bool("LOG_RENDER_SOURCE_PREVIEW", True):
        return
    try:
        await context.bot.copy_message(
            chat_id=target,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            read_timeout=20,
            connect_timeout=20,
        )
    except TelegramError:
        logging.exception("Failed to copy render source to owner log")


async def remember_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    *,
    render_started: bool = False,
    source_label: str | None = None,
    source_message: Message | None = None,
) -> None:
    user = update.effective_user
    if not user:
        return

    is_new = await asyncio.to_thread(upsert_user, user, action, render_started=render_started)
    if is_new and env_bool("LOG_NEW_USERS", True):
        await log_to_owner_chat(
            context,
            "New bot user\n"
            f"user: {user_display(user)}\n"
            f"language: {user.language_code or '-'}\n"
            f"action: {action}",
        )
    if render_started and env_bool("LOG_RENDER_REQUESTS", True):
        await log_to_owner_chat(
            context,
            "Render request\n"
            f"user: {user_display(user)}\n"
            f"action: {action}\n"
            f"source: {source_label or '-'}",
        )
        if source_message:
            await copy_render_source_to_owner_chat(context, source_message)


async def is_admin_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False

    configured_admins = parse_int_list(os.getenv("ADMIN_USER_IDS"))
    if user.id in configured_admins:
        return True

    target = log_chat_id()
    if not target or not env_bool("ALLOW_LOG_CHAT_ADMINS", True):
        return False

    try:
        member = await context.bot.get_chat_member(target, user.id, read_timeout=20, connect_timeout=20)
    except TelegramError:
        return False
    return member.status in {"creator", "administrator"}


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_admin_user(update, context):
        return True
    if update.message:
        await update.message.reply_text("Нет доступа к админ-командам.")
    return False


def state_for(user_id: int) -> UserLimitState:
    state = USER_LIMITS.get(user_id)
    if not state:
        state = UserLimitState(render_times=deque(), violation_times=deque())
        USER_LIMITS[user_id] = state
    return state


def load_limit_state() -> None:
    if not LIMITS_STATE_PATH.exists():
        return

    now = time.time()
    try:
        payload = json.loads(LIMITS_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.exception("Failed to read limit state")
        return

    for raw_user_id, raw_state in payload.get("users", {}).items():
        try:
            user_id = int(raw_user_id)
            banned_until = float(raw_state.get("banned_until", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        if banned_until > now:
            state_for(user_id).banned_until = banned_until


def save_limit_state() -> None:
    now = time.time()
    users = {
        str(user_id): {"banned_until": state.banned_until}
        for user_id, state in USER_LIMITS.items()
        if state.banned_until > now
    }
    payload = {"users": users}
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = LIMITS_STATE_PATH.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temp_path.replace(LIMITS_STATE_PATH)
    except OSError:
        logging.exception("Failed to save limit state")


def load_menu_assets() -> None:
    MENU_ASSETS.clear()
    MENU_SECTION_ASSETS.clear()
    if not MENU_ASSETS_PATH.exists():
        return
    try:
        payload = json.loads(MENU_ASSETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.exception("Failed to load menu assets")
        return
    assets = payload.get("animation_file_ids", [])
    if isinstance(assets, list):
        MENU_ASSETS.extend(str(item) for item in assets if item)
    sections = payload.get("sections", {})
    if isinstance(sections, dict):
        for raw_section, raw_assets in sections.items():
            if not isinstance(raw_assets, list):
                continue
            section = normalize_menu_asset_section(str(raw_section))
            MENU_SECTION_ASSETS[section] = [str(item) for item in raw_assets if item]


def save_menu_assets() -> None:
    payload = {
        "animation_file_ids": MENU_ASSETS[-50:],
        "sections": {
            section: assets[-50:]
            for section, assets in sorted(MENU_SECTION_ASSETS.items())
            if section != "main" and assets
        },
    }
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    try:
        MENU_ASSETS_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logging.exception("Failed to save menu assets")


def normalize_menu_asset_section(section: str | None) -> str:
    value = (section or "main").strip().lower()
    return value if value in MENU_ASSET_SECTIONS else "main"


def menu_asset_section_label(section: str) -> str:
    return MENU_ASSET_SECTIONS.get(section, MENU_ASSET_SECTIONS["main"])


def menu_asset_count(section: str) -> int:
    section = normalize_menu_asset_section(section)
    if section == "main":
        return len(MENU_ASSETS)
    return len(MENU_SECTION_ASSETS.get(section, []))


def menu_asset_for(section: str | None = None) -> str | None:
    section = normalize_menu_asset_section(section)
    assets = MENU_SECTION_ASSETS.get(section, []) if section != "main" else MENU_ASSETS
    if assets:
        return secrets.choice(assets)
    if MENU_ASSETS:
        return secrets.choice(MENU_ASSETS)
    return None


def add_menu_asset(file_id: str, section: str = "main") -> None:
    section = normalize_menu_asset_section(section)
    assets = MENU_ASSETS if section == "main" else MENU_SECTION_ASSETS.setdefault(section, [])
    if file_id in assets:
        assets.remove(file_id)
    assets.append(file_id)
    save_menu_assets()


def menu_asset_action(section: str = "main") -> str:
    return f"menu_asset:{normalize_menu_asset_section(section)}"


def menu_asset_section_from_action(action: str) -> str:
    if action == "menu_asset":
        return "main"
    if action.startswith("menu_asset:"):
        return normalize_menu_asset_section(action.split(":", 1)[1])
    return "main"


def prune_window(items: deque[float], now: float, window_seconds: int) -> None:
    cutoff = now - window_seconds
    while items and items[0] < cutoff:
        items.popleft()


def format_duration(seconds: float) -> str:
    seconds = max(1, int(seconds))
    if seconds >= 3600:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if seconds >= 60:
        minutes = seconds // 60
        rest = seconds % 60
        return f"{minutes} мин {rest} сек" if rest else f"{minutes} мин"
    return f"{seconds} сек"


def ban_remaining(user_id: int, now: float | None = None) -> float:
    now = now or time.time()
    state = state_for(user_id)
    remaining = state.banned_until - now
    if remaining <= 0:
        if state.banned_until:
            state.banned_until = 0
            save_limit_state()
        return 0
    return remaining


def note_violation(user_id: int, config: SafetyConfig, now: float | None = None) -> float:
    now = now or time.time()
    state = state_for(user_id)
    prune_window(state.violation_times, now, config.spam_window_seconds)
    state.violation_times.append(now)
    if len(state.violation_times) >= config.spam_events_before_ban:
        state.violation_times.clear()
        state.banned_until = now + config.ban_seconds
        save_limit_state()
        logging.warning("User %s temporarily banned for spam until %.0f", user_id, state.banned_until)
        return config.ban_seconds
    return 0


def user_rate_delay(user_id: int, config: SafetyConfig, now: float | None = None) -> float:
    now = now or time.time()
    state = state_for(user_id)
    prune_window(state.render_times, now, config.per_user_window_seconds)

    if state.render_times:
        since_last = now - state.render_times[-1]
        if since_last < config.per_user_min_gap_seconds:
            return config.per_user_min_gap_seconds - since_last

    if len(state.render_times) >= config.per_user_window_jobs:
        return config.per_user_window_seconds - (now - state.render_times[0])

    return 0


def mark_render_start(user_id: int, config: SafetyConfig, now: float | None = None) -> None:
    now = now or time.time()
    state = state_for(user_id)
    prune_window(state.render_times, now, config.per_user_window_seconds)
    state.render_times.append(now)


def mark_render_in_db(user_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET render_count = render_count + 1,
                last_seen = ?,
                last_action = 'render'
            WHERE user_id = ?
            """,
            (int(time.time()), user_id),
        )


def get_render_gate() -> RenderGate:
    global GLOBAL_RENDER_GATE
    if GLOBAL_RENDER_GATE is None:
        GLOBAL_RENDER_GATE = RenderGate(safety_config().max_global_renders)
    return GLOBAL_RENDER_GATE


def cleanup_old_runs(config: SafetyConfig) -> None:
    if not RUNS_DIR.exists():
        return

    now = time.time()
    for child in RUNS_DIR.iterdir():
        try:
            if not child.is_dir():
                continue
            age = now - child.stat().st_mtime
            if age > config.runs_retention_seconds:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            logging.exception("Failed to clean old run directory %s", child)


def background_keyboard() -> InlineKeyboardMarkup:
    rows = []
    items = list(BACKGROUND_PRESETS.items())
    for index in range(0, len(items), 2):
        row = [
            InlineKeyboardButton(f"{name} {color}", callback_data=f"bg:{key}")
            for key, (name, color) in items[index:index + 2]
        ]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def output_format_label(value: str) -> str:
    return {
        "gif": "GIF",
        "video": "Видео",
        "file": "Файл",
    }.get(value, "GIF")


PREMIUM_EMOJI = {
    "settings": "5870982283724328568",
    "file": "5870528606328852614",
    "send": "5963103826075456248",
    "brush": "6050679691004612757",
    "media": "6035128606563241721",
    "resolution": "5778479949572738874",
    "text": "5771851822897566479",
    "write": "5870753782874246579",
    "eye": "6037397706505195857",
    "delete": "5870875489362513438",
    "check": "5870633910337015697",
    "info": "6028435952299413210",
    "bot": "6030400221232501136",
    "loading": "5345906554510012647",
}


def tg_emoji(key: str, fallback: str) -> str:
    emoji_id = PREMIUM_EMOJI[key]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def menu_button(text: str, callback_data: str, icon: str | None = None) -> InlineKeyboardButton:
    kwargs = {"icon_custom_emoji_id": PREMIUM_EMOJI[icon]} if icon else None
    return InlineKeyboardButton(text, callback_data=callback_data, api_kwargs=kwargs)


def menu_surface(message: Message) -> str:
    return "caption" if message.caption is not None else "text"


def pending_from_message(action: str, message: Message) -> PendingAction:
    return PendingAction(
        action=action,
        chat_id=message.chat_id,
        message_id=message.message_id,
        surface=menu_surface(message),
    )


def settings_summary(settings: RenderSettings) -> str:
    item_color = html.escape(settings.item_color_hex or "без перекраски")
    notes = html.escape(settings.notes if settings.notes else "нет")
    watermark = html.escape(settings.watermark_text if settings.watermark_enabled and settings.watermark_text else "выкл")
    return (
        f"{tg_emoji('bot', '🤖')} <b>Создан для оформления ботов и сайтов</b>\n\n"
        f"{tg_emoji('send', '⬆')} <b>Отправь мне:</b>\n"
        "прем emoji, custom emoji, sticker, фото/видео или ссылку на pack\n\n"
        f"{tg_emoji('settings', '⚙')} <b>Конфигурация:</b>\n"
        f"{tg_emoji('brush', '🖌')} <b>Цвет фона:</b> {settings.background_hex}\n"
        f"{tg_emoji('resolution', '↔')} <b>Разрешение:</b> {settings.width}x{settings.height} {settings.fps} FPS\n"
        f"{tg_emoji('file', '📁')} <b>Формат:</b> {output_format_label(settings.output_format)}\n"
        f"{tg_emoji('brush', '🖌')} <b>ЦветEmoji:</b> {item_color}\n"
        f"{tg_emoji('write', '✍')} <b>Заметки:</b> {notes}\n"
        f"{tg_emoji('text', '🔡')} <b>Вотермарка:</b> {watermark}"
    )


def main_menu_keyboard(settings: RenderSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                menu_button("Цвет фона", "menu:bg", "brush"),
                menu_button("Разрешение", "menu:resolution", "resolution"),
            ],
            [
                menu_button("Формат", "menu:format", "file"),
                menu_button("Своя медиа", "menu:media", "media"),
            ],
            [
                menu_button("ЦветEmoji", "menu:item_color", "brush"),
                menu_button("Заметки", "menu:notes", "write"),
            ],
            [
                menu_button("Вотермарка", "menu:watermark", "text"),
                menu_button("Предпросмотр", "menu:preview", "eye"),
            ],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[menu_button("Назад", "menu:main")]])


def background_menu_keyboard() -> InlineKeyboardMarkup:
    rows = []
    items = list(BACKGROUND_PRESETS.items())
    for index in range(0, len(items), 2):
        rows.append(
            [
                menu_button(f"{name} {color}", f"setbg:{key}", "brush")
                for key, (name, color) in items[index:index + 2]
            ]
        )
    rows.append([menu_button("Назад", "menu:main")])
    return InlineKeyboardMarkup(rows)


def format_menu_keyboard(current: str) -> InlineKeyboardMarkup:
    def label(value: str, text: str) -> str:
        return f"✓ {text}" if current == value else text

    return InlineKeyboardMarkup(
        [
            [
                menu_button(label("gif", "GIF"), "fmt:gif", "file"),
                menu_button(label("video", "Видео"), "fmt:video", "media"),
                menu_button(label("file", "Файл"), "fmt:file", "file"),
            ],
            [menu_button("Назад", "menu:main")],
        ]
    )


def resolution_menu_keyboard(current: RenderSettings) -> InlineKeyboardMarkup:
    def label(key: str) -> str:
        w, h, fps = RESOLUTION_PRESETS[key]
        mark = "✓ " if current.width == w and current.height == h and current.fps == fps else ""
        return f"{mark}{key}"

    rows = []
    items = list(RESOLUTION_PRESETS.items())
    for index in range(0, len(items), 2):
        rows.append(
            [
                menu_button(label(key), f"setres:{key}")
                for key, _ in items[index:index + 2]
            ]
        )
    rows.append([menu_button("Свой размер…", "menu:res_custom")])
    rows.append([menu_button("Назад", "menu:main")])
    return InlineKeyboardMarkup(rows)



def item_color_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [menu_button("Убрать цвет", "itemcolor:clear", "delete")],
            [menu_button("Назад", "menu:main")],
        ]
    )


def notes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [menu_button("Убрать заметки", "notes:clear", "delete")],
            [menu_button("Назад", "menu:main")],
        ]
    )


def watermark_keyboard(settings: RenderSettings) -> InlineKeyboardMarkup:
    toggle = "✓ Включена" if settings.watermark_enabled else "Включить"
    return InlineKeyboardMarkup(
        [
            [menu_button(toggle, "wm:toggle", "check")],
            [menu_button("Текст вотермарки", "wm:text", "text")],
            [menu_button("Назад", "menu:main")],
        ]
    )


async def safe_delete_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


async def edit_menu_message(message: Message, text: str, reply_markup: InlineKeyboardMarkup) -> Message | None:
    try:
        if message.caption is not None:
            return await message.edit_caption(
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        return await message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as error:
        if "message is not modified" in str(error).lower():
            return message
        logging.warning("Failed to edit menu message: %s", error)
    except TelegramError:
        logging.exception("Failed to edit menu message")
    return None


async def edit_pending_menu(
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingAction,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    try:
        if pending.surface == "caption":
            await context.bot.edit_message_caption(
                chat_id=pending.chat_id,
                message_id=pending.message_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return
        await context.bot.edit_message_text(
            chat_id=pending.chat_id,
            message_id=pending.message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as error:
        if "message is not modified" not in str(error).lower():
            logging.warning("Failed to edit pending menu message: %s", error)
    except TelegramError:
        logging.exception("Failed to edit pending menu message")


async def send_menu_message(message: Message, settings: RenderSettings, section: str = "main") -> Message:
    asset = menu_asset_for(section)
    if asset:
        try:
            return await message.reply_animation(
                animation=asset,
                caption=settings_summary(settings),
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(settings),
                read_timeout=30,
                connect_timeout=20,
            )
        except TelegramError:
            logging.exception("Failed to send menu animation")
    return await message.reply_text(
        settings_summary(settings),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(settings),
        disable_web_page_preview=True,
    )


async def show_section_menu_message(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    section: str,
) -> Message | None:
    section = normalize_menu_asset_section(section)
    if section != "main" and not MENU_SECTION_ASSETS.get(section):
        return await edit_menu_message(message, text, reply_markup)
    asset = menu_asset_for(section)
    if not asset or section == "main":
        return await edit_menu_message(message, text, reply_markup)

    try:
        sent = await context.bot.send_animation(
            chat_id=message.chat_id,
            animation=asset,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            read_timeout=30,
            connect_timeout=20,
        )
        await safe_delete_message(message)
        return sent
    except TelegramError:
        logging.exception("Failed to send section menu animation for %s", section)
        return await edit_menu_message(message, text, reply_markup)


def source_from_sticker(sticker: Sticker) -> SourceRef:
    premium_animation = getattr(sticker, "premium_animation", None)
    if sticker.is_animated:
        return SourceRef(sticker.file_id, "animated .tgs sticker")
    if sticker.is_video:
        return SourceRef(sticker.file_id, "video .webm sticker")
    if premium_animation and getattr(premium_animation, "file_id", None):
        return SourceRef(premium_animation.file_id, "premium animation")
    return SourceRef(sticker.file_id, "static sticker")


def custom_emoji_ids(message: Message) -> list[str]:
    entities = list(message.entities or []) + list(message.caption_entities or [])
    ids = []
    for entity in entities:
        if entity.type == "custom_emoji" and entity.custom_emoji_id:
            ids.append(entity.custom_emoji_id)
    return ids


def detect_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    head = path.read_bytes()[:16]
    if suffix == ".tgs" or head.startswith(b"\x1f\x8b"):
        return "tgs"
    if suffix == ".webm" or head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if suffix in {".webp", ".png", ".jpg", ".jpeg"}:
        return "image"
    if suffix in {".mp4", ".mov", ".m4v", ".gif"}:
        return "video"
    return "unknown"


def ffprobe_duration(path: Path) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15)
        duration = float(result.stdout.strip())
        if duration > 0:
            return min(duration, 6.0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return None
    return None


def run_command(cmd: list[str], cwd: Path | None = None) -> None:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=safety_config().render_timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Render command timed out after {error.timeout:.0f}s") from error

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(message[-4000:])


def ffmpeg_common_output(output: Path) -> list[str]:
    return [
        "-map",
        "[v]",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]


def drawtext_escape(value: str) -> str:
    return (
        value
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def has_ffmpeg_drawtext() -> bool:
    global HAS_DRAWTEXT_FILTER
    if HAS_DRAWTEXT_FILTER is not None:
        return HAS_DRAWTEXT_FILTER

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        HAS_DRAWTEXT_FILTER = " drawtext " in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        HAS_DRAWTEXT_FILTER = False

    if not HAS_DRAWTEXT_FILTER:
        logging.warning("ffmpeg drawtext filter is unavailable; watermark disabled")
    return HAS_DRAWTEXT_FILTER


def watermark_drawtext_filter(settings: RenderSettings) -> str:
    if not settings.watermark_enabled:
        return ""
    if not has_ffmpeg_drawtext():
        return ""

    text = settings.watermark_text.strip()
    if not text:
        return ""

    opacity = clamp(env_float("WATERMARK_OPACITY", 0.16), 0.0, 1.0)
    shadow_opacity = clamp(env_float("WATERMARK_SHADOW_OPACITY", 0.12), 0.0, 1.0)
    font_size = max(8, env_int("WATERMARK_FONT_SIZE", 13))
    margin = max(0, env_int("WATERMARK_MARGIN", 12))
    font_color = env_str("WATERMARK_COLOR", "white")
    shadow_color = env_str("WATERMARK_SHADOW_COLOR", "black")
    position = env_str("WATERMARK_POSITION", "bottom_left").strip().lower()
    if position == "bottom_left":
        x_expr = str(margin)
        y_expr = f"h-th-{margin}"
    elif position == "top_left":
        x_expr = str(margin)
        y_expr = str(margin)
    elif position == "top_right":
        x_expr = f"w-tw-{margin}"
        y_expr = str(margin)
    else:
        x_expr = f"w-tw-{margin}"
        y_expr = f"h-th-{margin}"
    escaped_text = drawtext_escape(text)

    return (
        f",drawtext=text='{escaped_text}':"
        f"fontcolor={font_color}@{opacity:.3f}:"
        f"fontsize={font_size}:"
        f"x={x_expr}:"
        f"y={y_expr}:"
        f"shadowcolor={shadow_color}@{shadow_opacity:.3f}:"
        "shadowx=1:shadowy=1"
    )


def sticker_filter(settings: RenderSettings) -> str:
    base = (
        f"fps={settings.fps},"
        f"scale={settings.sticker_size}:{settings.sticker_size}:"
        "force_original_aspect_ratio=decrease:flags=lanczos,"
        "format=rgba"
    )
    if settings.item_color_hex:
        red = int(settings.item_color_hex[1:3], 16)
        green = int(settings.item_color_hex[3:5], 16)
        blue = int(settings.item_color_hex[5:7], 16)
        base += f",geq=r={red}:g={green}:b={blue}:a=alpha(X\\,Y)"
    return base


def compose_filter(settings: RenderSettings) -> str:
    watermark = watermark_drawtext_filter(settings)
    return (
        f"[0:v]{sticker_filter(settings)}[st];"
        f"[1:v][st]overlay=(W-w)/2:(H-h)/2:shortest=1:format=auto,"
        f"format=yuv420p{watermark}[v]"
    )


def render_tgs(source: Path, output: Path, job_dir: Path, settings: RenderSettings) -> None:
    frames_dir = job_dir / "frames"
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required to render .tgs stickers")

    render_cmd = [
        node,
        str(ROOT / "src" / "render_lottie.mjs"),
        "--input",
        str(source),
        "--out-dir",
        str(frames_dir),
        "--width",
        "512",
        "--height",
        "512",
        "--fps",
        str(settings.fps),
        "--max-seconds",
        "6",
    ]
    run_command(render_cmd, cwd=ROOT)

    manifest = json.loads((frames_dir / "manifest.json").read_text(encoding="utf-8"))
    duration = max(0.2, min(float(manifest["duration"]), 6.0))
    frame_pattern = frames_dir / "frame_%05d.png"
    color = f"color=c={settings.background_hex}:s={settings.width}x{settings.height}:r={settings.fps}:d={duration}"
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(settings.fps),
        "-i",
        str(frame_pattern),
        "-f",
        "lavfi",
        "-i",
        color,
        "-filter_complex",
        compose_filter(settings),
        "-t",
        f"{duration:.3f}",
        *ffmpeg_common_output(output),
    ]
    run_command(cmd)


def render_webm(source: Path, output: Path, settings: RenderSettings) -> None:
    duration = ffprobe_duration(source) or 3.0
    color = f"color=c={settings.background_hex}:s={settings.width}x{settings.height}:r={settings.fps}:d={duration}"
    cmd = [
        "ffmpeg",
        "-y",
        "-stream_loop",
        "-1",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        "-f",
        "lavfi",
        "-i",
        color,
        "-filter_complex",
        compose_filter(settings),
        "-t",
        f"{duration:.3f}",
        *ffmpeg_common_output(output),
    ]
    run_command(cmd)


def render_video(source: Path, output: Path, settings: RenderSettings) -> None:
    duration = ffprobe_duration(source) or 3.0
    color = f"color=c={settings.background_hex}:s={settings.width}x{settings.height}:r={settings.fps}:d={duration}"
    cmd = [
        "ffmpeg",
        "-y",
        "-stream_loop",
        "-1",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        "-f",
        "lavfi",
        "-i",
        color,
        "-filter_complex",
        compose_filter(settings),
        "-t",
        f"{duration:.3f}",
        *ffmpeg_common_output(output),
    ]
    run_command(cmd)


def render_image(source: Path, output: Path, settings: RenderSettings) -> None:
    duration = max(0.5, min(settings.static_seconds, 6.0))
    color = f"color=c={settings.background_hex}:s={settings.width}x{settings.height}:r={settings.fps}:d={duration}"
    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        "-f",
        "lavfi",
        "-i",
        color,
        "-filter_complex",
        compose_filter(settings),
        "-t",
        f"{duration:.3f}",
        *ffmpeg_common_output(output),
    ]
    run_command(cmd)


def render_source(source: Path, job_dir: Path, settings: RenderSettings) -> Path:
    kind = detect_kind(source)
    output = job_dir / "loop.mp4"
    if kind == "tgs":
        render_tgs(source, output, job_dir, settings)
    elif kind == "webm":
        render_webm(source, output, settings)
    elif kind == "image":
        render_image(source, output, settings)
    elif kind == "video":
        render_video(source, output, settings)
    else:
        raise RuntimeError("Unsupported sticker file format from Telegram")

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("Renderer produced an empty output file")
    if output.stat().st_size > safety_config().max_output_bytes:
        raise UserFacingError("Результат получился слишком большим для отправки. Попробуй другой стикер.")
    return output


async def download_source(context: ContextTypes.DEFAULT_TYPE, source: SourceRef, job_dir: Path) -> Path:
    tg_file = await context.bot.get_file(source.file_id, read_timeout=30, connect_timeout=30)
    file_size = getattr(tg_file, "file_size", None)
    if file_size and file_size > safety_config().max_source_bytes:
        raise UserFacingError("Файл стикера слишком большой для безопасной обработки.")

    suffix = Path(tg_file.file_path or "").suffix or ".bin"
    local_path = job_dir / f"source{suffix}"
    await tg_file.download_to_drive(custom_path=local_path, read_timeout=60, write_timeout=60)
    if local_path.stat().st_size > safety_config().max_source_bytes:
        raise UserFacingError("Файл стикера слишком большой для безопасной обработки.")
    return local_path


async def reply_ban_or_warning(
    message: Message,
    user_id: int,
    config: SafetyConfig,
    warning: str,
) -> None:
    ban_seconds = note_violation(user_id, config)
    if ban_seconds:
        await message.reply_text(
            f"Слишком много попыток подряд. Ставлю паузу на {format_duration(ban_seconds)}."
        )
        return
    await message.reply_text(warning)


async def process_source(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    source: SourceRef,
    settings: RenderSettings,
    actor_user_id: int | None = None,
    charge_rate: bool = True,
) -> Message | None:
    config = safety_config()
    user_id = actor_user_id or (message.from_user.id if message.from_user else message.chat_id)
    remaining = ban_remaining(user_id)
    if remaining:
        await message.reply_text(f"Пауза после спама еще {format_duration(remaining)}.")
        return

    if user_id in BUSY:
        await reply_ban_or_warning(
            message,
            user_id,
            config,
            "Уже собираю предыдущую анимацию. Дождись результата; повторные стикеры подряд считаются спамом.",
        )
        return

    delay = user_rate_delay(user_id, config) if charge_rate else 0
    if delay > 0:
        await reply_ban_or_warning(
            message,
            user_id,
            config,
            (
                f"Лимит: не больше {config.per_user_window_jobs} рендеров "
                f"за {format_duration(config.per_user_window_seconds)}. "
                f"Попробуй через {format_duration(delay)}."
            ),
        )
        return

    gate = get_render_gate()
    if not await gate.try_acquire():
        await reply_ban_or_warning(
            message,
            user_id,
            config,
            f"Сейчас заняты все {config.max_global_renders} слота рендера. Попробуй чуть позже.",
        )
        return

    BUSY.add(user_id)
    if charge_rate:
        mark_render_start(user_id, config)
    await asyncio.to_thread(mark_render_in_db, user_id)
    started = time.time()
    job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=RUNS_DIR))
    sent_message: Message | None = None
    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)
        source_path = await download_source(context, source, job_dir)
        output = await asyncio.to_thread(render_source, source_path, job_dir, settings)
        elapsed = time.time() - started
        caption = (
            f"Готово: {settings.width}x{settings.height}, "
            f"{settings.background_hex}, {output_format_label(settings.output_format)}, {elapsed:.1f}s"
        )
        if settings.notes:
            caption = f"{caption}\n{settings.notes[:800]}"
        with output.open("rb") as file_obj:
            if settings.output_format == "video":
                sent_message = await message.reply_video(
                    video=file_obj,
                    caption=caption,
                    reply_markup=main_menu_keyboard(settings),
                    read_timeout=60,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=60,
                )
            elif settings.output_format == "file":
                sent_message = await message.reply_document(
                    document=file_obj,
                    filename="sticker-loop.mp4",
                    caption=caption,
                    reply_markup=main_menu_keyboard(settings),
                    read_timeout=60,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=60,
                )
            else:
                sent_message = await message.reply_animation(
                    animation=file_obj,
                    caption=caption,
                    reply_markup=main_menu_keyboard(settings),
                    read_timeout=60,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=60,
                )
    except UserFacingError as error:
        await message.reply_text(str(error))
    except Exception as error:  # noqa: BLE001 - bot replies need a compact user-facing error.
        logging.exception("Failed to process %s", source.label)
        await message.reply_text(
            "Не смог собрать анимацию. Пришли другой стикер или попробуй фон попроще.\n"
            f"Технически: {str(error)[-900:]}"
        )
    finally:
        BUSY.discard(user_id)
        await gate.release()
        shutil.rmtree(job_dir, ignore_errors=True)
    return sent_message


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await remember_user(update, context, "start")
    current = settings_for(update.effective_user.id)
    await send_menu_message(update.message, current)


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await remember_user(update, context, "settings")
    current = settings_for(update.effective_user.id)
    await send_menu_message(update.message, current)


async def limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "limits")
    config = safety_config()
    await update.message.reply_text(
        "Лимиты:\n"
        f"- одновременно рендерится максимум {config.max_global_renders} "
        f"(сейчас активно {get_render_gate().active})\n"
        "- у одного пользователя максимум 1 активная задача\n"
        f"- не больше {config.per_user_window_jobs} рендеров за "
        f"{format_duration(config.per_user_window_seconds)}\n"
        f"- спам-пауза: {format_duration(config.ban_seconds)}\n"
        f"- таймаут рендера: {format_duration(config.render_timeout_seconds)}"
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "admin:users")
    if not await require_admin(update, context):
        return
    stats = await asyncio.to_thread(user_stats)
    await update.message.reply_text(
        "Пользователи:\n"
        f"всего: {stats['total']}\n"
        f"доступны для рассылки: {stats['reachable']}\n"
        f"рендеров: {stats['renders']}"
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await remember_user(update, context, "whoami")
    await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


async def start_menu_asset_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, section: str) -> None:
    if not update.message or not update.effective_user:
        return
    section = normalize_menu_asset_section(section)
    await remember_user(update, context, f"admin:menu_assets:{section}")
    if not await require_admin(update, context):
        return
    label = menu_asset_section_label(section)
    reply = await update.message.reply_text(
        f"{tg_emoji('media', '🖼')} <b>Режим добавления GIF: {html.escape(label)}.</b>\n"
        "Кидай sticker, premium/custom emoji, фото, видео или GIF.\n"
        "Бот отрендерит и сохранит как верхнюю карточку нужного раздела.\n\n"
        f"Сейчас в разделе: {menu_asset_count(section)}\n"
        "- чтобы закончить.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )
    PENDING_ACTIONS[update.effective_user.id] = pending_from_message(menu_asset_action(section), reply)


async def menu_assets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw_section = context.args[0] if context.args else "main"
    await start_menu_asset_mode(update, context, raw_section)


async def menu_asset_palette_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_menu_asset_mode(update, context, "palette")


def build_broadcast_draft(update: Update, target_user_ids: Sequence[int]) -> BroadcastDraft | None:
    if not update.message or not update.effective_user:
        return None

    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        return BroadcastDraft(
            sender_id=update.effective_user.id,
            created_at=time.time(),
            target_user_ids=tuple(target_user_ids),
            copy_from_chat_id=replied.chat_id,
            copy_message_id=replied.message_id,
        )

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None
    return BroadcastDraft(
        sender_id=update.effective_user.id,
        created_at=time.time(),
        target_user_ids=tuple(target_user_ids),
        text=parts[1].strip(),
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "admin:broadcast")
    if not await require_admin(update, context):
        return

    target_user_ids = await asyncio.to_thread(known_user_ids)
    if not target_user_ids:
        await update.message.reply_text("Пока нет пользователей для рассылки.")
        return

    draft = build_broadcast_draft(update, target_user_ids)
    if not draft:
        await update.message.reply_text(
            "Использование:\n"
            "/broadcast текст сообщения\n"
            "или ответь /broadcast на сообщение, которое нужно скопировать всем."
        )
        return

    draft_id = secrets.token_urlsafe(4)
    BROADCAST_DRAFTS[draft_id] = draft
    kind = "копия сообщения" if draft.copy_message_id else "текст"
    preview = draft.text[:500] if draft.text else kind
    await update.message.reply_text(
        "Черновик рассылки создан.\n"
        f"id: {draft_id}\n"
        f"тип: {kind}\n"
        f"получателей: {len(target_user_ids)}\n"
        f"превью: {preview}\n\n"
        f"Отправить: /broadcast_send {draft_id}\n"
        f"Отменить: /broadcast_cancel {draft_id}"
    )


async def send_broadcast_message(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    draft: BroadcastDraft,
) -> bool:
    try:
        if draft.copy_message_id and draft.copy_from_chat_id:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=draft.copy_from_chat_id,
                message_id=draft.copy_message_id,
                read_timeout=20,
                connect_timeout=20,
            )
        elif draft.text:
            await context.bot.send_message(
                chat_id=user_id,
                text=draft.text,
                disable_web_page_preview=False,
                read_timeout=20,
                connect_timeout=20,
            )
        return True
    except Forbidden:
        await asyncio.to_thread(mark_user_blocked, user_id)
    except BadRequest as error:
        if "chat not found" in str(error).lower() or "bot was blocked" in str(error).lower():
            await asyncio.to_thread(mark_user_blocked, user_id)
        else:
            logging.warning("Broadcast bad request for %s: %s", user_id, error)
    except TelegramError:
        logging.exception("Broadcast failed for %s", user_id)
    return False


async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "admin:broadcast_send")
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text("Укажи id: /broadcast_send <id>")
        return

    draft_id = context.args[0]
    draft = BROADCAST_DRAFTS.get(draft_id)
    if not draft:
        await update.message.reply_text("Черновик не найден или уже отправлен.")
        return

    if time.time() - draft.created_at > env_int("BROADCAST_DRAFT_TTL_SECONDS", 1800):
        BROADCAST_DRAFTS.pop(draft_id, None)
        await update.message.reply_text("Черновик устарел. Создай новый /broadcast.")
        return

    await update.message.reply_text(f"Начинаю рассылку на {len(draft.target_user_ids)} пользователей.")
    sent = 0
    failed = 0
    delay = env_float("BROADCAST_DELAY_SECONDS", 0.05)
    for user_id in draft.target_user_ids:
        ok = await send_broadcast_message(context, user_id, draft)
        if ok:
            sent += 1
        else:
            failed += 1
        if delay > 0:
            await asyncio.sleep(delay)

    BROADCAST_DRAFTS.pop(draft_id, None)
    await update.message.reply_text(f"Рассылка завершена. Отправлено: {sent}, ошибок: {failed}.")
    await log_to_owner_chat(
        context,
        "Broadcast sent\n"
        f"admin: {user_display(update.effective_user)}\n"
        f"sent: {sent}\n"
        f"failed: {failed}",
    )


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "admin:broadcast_cancel")
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text("Укажи id: /broadcast_cancel <id>")
        return
    draft_id = context.args[0]
    if BROADCAST_DRAFTS.pop(draft_id, None):
        await update.message.reply_text("Черновик рассылки отменен.")
    else:
        await update.message.reply_text("Черновик не найден.")


HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
RESOLUTION_RE = re.compile(r"^(\d{2,4})\s*[xх×]\s*(\d{2,4})(?:\s+(\d{1,3})\s*fps)?$", re.IGNORECASE)
RATIO_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$")


def normalize_hex(value: str) -> str:
    color = value.strip()
    if not HEX_RE.match(color):
        raise ValueError("Use #RGB or #RRGGBB")
    if len(color) == 4:
        color = "#" + "".join(ch * 2 for ch in color[1:])
    return color.lower()


def parse_resolution(value: str, current: RenderSettings) -> tuple[int, int, int]:
    text = value.strip().lower().replace(",", ".")
    fps = current.fps
    match = RESOLUTION_RE.match(text)
    if match:
        width = int(match.group(1))
        height = int(match.group(2))
        if match.group(3):
            fps = int(match.group(3))
    else:
        ratio = RATIO_RE.match(text)
        if not ratio:
            raise ValueError("bad resolution")
        left = float(ratio.group(1))
        right = float(ratio.group(2))
        if left <= 0 or right <= 0:
            raise ValueError("bad ratio")
        width = current.width
        height = round(width * right / left)

    max_width = env_int("MAX_OUTPUT_WIDTH", 1920)
    max_height = env_int("MAX_OUTPUT_HEIGHT", 1080)
    max_pixels = env_int("MAX_OUTPUT_PIXELS", 1920 * 1080)
    max_fps = env_int("MAX_OUTPUT_FPS", 60)
    if width < 64 or height < 64 or width > max_width or height > max_height:
        raise ValueError("resolution out of range")
    if width * height > max_pixels:
        raise ValueError("too many pixels")
    if fps < 12 or fps > max_fps:
        raise ValueError("fps out of range")
    return width, height, fps


def pack_name_from_link(text: str) -> str | None:
    match = re.search(r"(?:t\.me|telegram\.me)/(?:addstickers|addemoji)/([A-Za-z0-9_]+)", text)
    if match:
        return match.group(1)
    return None


async def bg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await remember_user(update, context, "bg")

    if context.args:
        try:
            color = normalize_hex(context.args[0])
        except ValueError:
            await update.message.reply_text("Цвет нужен в формате /bg #101820")
            return
        update_settings(
            update.effective_user.id,
            background_key="custom",
            background_hex=color,
        )
        await update.message.reply_text(
            f"Поставил фон {color}. Кидай стикер.",
            reply_markup=main_menu_keyboard(settings_for(update.effective_user.id)),
        )
        return

    await update.message.reply_text("Выбери фон или отправь /bg #101820", reply_markup=background_menu_keyboard())


async def on_background_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    await remember_user(update, context, "bg_callback")

    await query.answer()
    key = (query.data or "").removeprefix("bg:")
    if key not in BACKGROUND_PRESETS:
        return

    name, color = BACKGROUND_PRESETS[key]
    update_settings(
        query.from_user.id,
        background_key=key,
        background_hex=color,
    )

    await query.message.reply_text(f"Фон: {name} {color}")
    source = LAST_SOURCE.get(query.from_user.id)
    if source and query.message:
        await query.message.reply_text("Пересобираю последний стикер с новым фоном.")
        await process_source(
            query.message,
            context,
            source,
            settings_for(query.from_user.id),
            actor_user_id=query.from_user.id,
        )


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    await remember_user(update, context, "menu_callback")
    await query.answer()

    user_id = query.from_user.id
    data = query.data or ""
    current = settings_for(user_id)

    if data == "menu:main":
        PENDING_ACTIONS.pop(user_id, None)
        await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:bg":
        shown = await show_section_menu_message(
            context,
            query.message,
            f"{tg_emoji('brush', '🖌')} <b>Введи новый HEX-цвет для фона:</b>\n"
            "FFFFFF - белый\n000000 - черный",
            background_menu_keyboard(),
            "palette",
        )
        if shown:
            PENDING_ACTIONS[user_id] = pending_from_message("bg", shown)
        return
    if data.startswith("setbg:"):
        key = data.removeprefix("setbg:")
        if key in BACKGROUND_PRESETS:
            name, color = BACKGROUND_PRESETS[key]
            current = update_settings(user_id, background_key=key, background_hex=color)
            await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:resolution":
        await edit_menu_message(
            query.message,
            f"{tg_emoji('resolution', '↔')} <b>Выбери разрешение:</b>\n"
            f"Сейчас: {current.width}x{current.height} {current.fps} FPS",
            resolution_menu_keyboard(current),
        )
        return
    if data == "menu:res_custom":
        PENDING_ACTIONS[user_id] = pending_from_message("resolution", query.message)
        await edit_menu_message(
            query.message,
            f"{tg_emoji('resolution', '↔')} <b>Введи своё разрешение:</b>\n"
            "1920x600 или 1920x530 60fps\n2.35:1 или 16:9 или 1:1\n\n"
            f"<b>Сейчас:</b> {current.width}x{current.height} {current.fps} FPS",
            back_keyboard(),
        )
        return
    if data.startswith("setres:"):
        key = data.removeprefix("setres:")
        if key in RESOLUTION_PRESETS:
            w, h, fps = RESOLUTION_PRESETS[key]
            current = update_settings(user_id, width=w, height=h, fps=fps)
            await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:format":
        await edit_menu_message(
            query.message,
            f"{tg_emoji('file', '📁')} <b>Выбери формат вывода:</b>",
            format_menu_keyboard(current.output_format),
        )
        return
    if data.startswith("fmt:"):
        value = data.removeprefix("fmt:")
        if value in {"gif", "video", "file"}:
            current = update_settings(user_id, output_format=value)
            await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:media":
        PENDING_ACTIONS[user_id] = pending_from_message("media", query.message)
        await edit_menu_message(
            query.message,
            f"{tg_emoji('media', '🖼')} <b>Загрузи фото, видео, GIF, файл или стикер.</b>\n"
            "Отрендерю как свою медиа.",
            back_keyboard(),
        )
        return
    if data == "menu:item_color":
        shown = await show_section_menu_message(
            context,
            query.message,
            f"{tg_emoji('brush', '🖌')} <b>Введи HEX-цвет, чтобы перекрасить emoji/sticker при рендере:</b>\n"
            "FFFFFF - белый\nF2E9E4 - серый",
            item_color_keyboard(),
            "palette",
        )
        if shown:
            PENDING_ACTIONS[user_id] = pending_from_message("item_color", shown)
        return
    if data == "itemcolor:clear":
        current = update_settings(user_id, item_color_hex=None)
        await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:notes":
        PENDING_ACTIONS[user_id] = pending_from_message("notes", query.message)
        await edit_menu_message(
            query.message,
            f"{tg_emoji('write', '✍')} <b>Введи заметку для подписи результата.</b>\n- чтобы очистить.",
            notes_keyboard(),
        )
        return
    if data == "notes:clear":
        current = update_settings(user_id, notes="")
        await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:watermark":
        await edit_menu_message(
            query.message,
            f"{tg_emoji('text', '🔡')} <b>Вотермарка для угла результата:</b>",
            watermark_keyboard(current),
        )
        return
    if data == "wm:toggle":
        current = update_settings(user_id, watermark_enabled=not current.watermark_enabled)
        await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "wm:text":
        PENDING_ACTIONS[user_id] = pending_from_message("watermark_text", query.message)
        await edit_menu_message(
            query.message,
            f"{tg_emoji('text', '🔡')} <b>Введи текст вотермарки.</b>\n- чтобы выключить.",
            back_keyboard(),
        )
        return
    if data == "menu:preview":
        source = LAST_SOURCE.get(user_id)
        if not source:
            await edit_menu_message(
                query.message,
                f"{tg_emoji('eye', '👁')} <b>Пока нет исходника для предпросмотра.</b>\n"
                "Пришли sticker/emoji/медиа.",
                main_menu_keyboard(current),
            )
            return
        await edit_menu_message(
            query.message,
            f"{tg_emoji('loading', '🔄')} <b>Собираю предпросмотр...</b>",
            main_menu_keyboard(current),
        )
        await process_source(query.message, context, source, current, actor_user_id=user_id)


async def handle_pending_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.effective_user or not update.message.text:
        return False
    user_id = update.effective_user.id
    pending = PENDING_ACTIONS.get(user_id)
    if not pending:
        return False

    action = pending.action
    text = update.message.text.strip()
    current = settings_for(user_id)
    try:
        if action == "bg":
            color = normalize_hex(text)
            current = update_settings(user_id, background_key="custom", background_hex=color)
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action == "resolution":
            width, height, fps = parse_resolution(text, current)
            current = update_settings(user_id, width=width, height=height, fps=fps)
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action == "item_color":
            if text in {"-", "0", "off", "нет"}:
                current = update_settings(user_id, item_color_hex=None)
            else:
                color = normalize_hex(text)
                current = update_settings(user_id, item_color_hex=color)
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action == "notes":
            notes = "" if text == "-" else text[:800]
            current = update_settings(user_id, notes=notes)
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action == "watermark_text":
            if text == "-":
                current = update_settings(user_id, watermark_enabled=False, watermark_text="")
            else:
                current = update_settings(user_id, watermark_enabled=True, watermark_text=text[:48])
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action == "media":
            await safe_delete_message(update.message)
            await edit_pending_menu(
                context,
                pending,
                f"{tg_emoji('media', '🖼')} <b>Жду именно фото, видео, GIF, файл или стикер.</b>",
                back_keyboard(),
            )
            return True
        elif action.startswith("menu_asset"):
            section = menu_asset_section_from_action(action)
            if text == "-":
                PENDING_ACTIONS.pop(user_id, None)
                await safe_delete_message(update.message)
                await edit_pending_menu(
                    context,
                    pending,
                    f"{tg_emoji('check', '✅')} <b>Режим добавления GIF в меню выключен.</b>\n"
                    f"Раздел: {html.escape(menu_asset_section_label(section))}\n"
                    f"Сейчас в разделе: {menu_asset_count(section)}",
                    main_menu_keyboard(current),
                )
            else:
                await safe_delete_message(update.message)
                await edit_pending_menu(
                    context,
                    pending,
                    f"{tg_emoji('media', '🖼')} <b>Кидай sticker/emoji/media для раздела "
                    f"{html.escape(menu_asset_section_label(section))}.</b>\n- чтобы закончить.",
                    back_keyboard(),
                )
            return True
    except ValueError:
        await safe_delete_message(update.message)
        await edit_pending_menu(
            context,
            pending,
            f"{tg_emoji('info', 'ℹ')} <b>Не понял формат.</b>\nПопробуй еще раз или нажми Назад.",
            back_keyboard(),
        )
        return True

    PENDING_ACTIONS.pop(user_id, None)
    return True


def media_source_from_message(message: Message) -> SourceRef | None:
    if message.photo:
        return SourceRef(message.photo[-1].file_id, "custom photo")
    if message.video:
        return SourceRef(message.video.file_id, "custom video")
    if message.animation:
        return SourceRef(message.animation.file_id, "custom animation")
    if message.document:
        return SourceRef(message.document.file_id, "custom document")
    return None


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    source = media_source_from_message(update.message)
    if not source:
        return
    pending = PENDING_ACTIONS.get(update.effective_user.id)
    if pending and pending.action.startswith("menu_asset") and await is_admin_user(update, context):
        await process_menu_asset(
            update.message,
            context,
            source,
            update.effective_user.id,
            menu_asset_section_from_action(pending.action),
        )
        return
    PENDING_ACTIONS.pop(update.effective_user.id, None)
    await remember_user(
        update,
        context,
        "custom_media",
        render_started=True,
        source_label=source.label,
        source_message=update.message,
    )
    LAST_SOURCE[update.effective_user.id] = source
    await process_source(update.message, context, source, settings_for(update.effective_user.id))


async def process_menu_asset(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    source: SourceRef,
    user_id: int,
    section: str = "main",
) -> None:
    section = normalize_menu_asset_section(section)
    settings = replace(
        settings_for(user_id),
        output_format="gif",
        notes="",
        watermark_enabled=False,
        width=env_int("MENU_ASSET_WIDTH", 640),
        height=env_int("MENU_ASSET_HEIGHT", 360),
    )
    sent = await process_source(message, context, source, settings, actor_user_id=user_id, charge_rate=False)
    if sent and sent.animation:
        await asyncio.to_thread(add_menu_asset, sent.animation.file_id, section)
        await message.reply_text(
            f"Добавил GIF в раздел: {menu_asset_section_label(section)}. Всего: {menu_asset_count(section)}\n"
            "Кидай следующий или отправь '-' чтобы закончить.",
        )


async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.sticker or not update.effective_user:
        return
    source = source_from_sticker(update.message.sticker)
    pending = PENDING_ACTIONS.get(update.effective_user.id)
    if pending and pending.action.startswith("menu_asset") and await is_admin_user(update, context):
        await process_menu_asset(
            update.message,
            context,
            source,
            update.effective_user.id,
            menu_asset_section_from_action(pending.action),
        )
        return
    await remember_user(
        update,
        context,
        "sticker",
        render_started=True,
        source_label=source.label,
        source_message=update.message,
    )
    LAST_SOURCE[update.effective_user.id] = source
    await process_source(update.message, context, source, settings_for(update.effective_user.id))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    ids = custom_emoji_ids(update.message)
    pending = PENDING_ACTIONS.get(update.effective_user.id)
    if pending and pending.action.startswith("menu_asset") and ids and await is_admin_user(update, context):
        section = menu_asset_section_from_action(pending.action)
        limit = max(1, min(env_int("MAX_CUSTOM_EMOJI_RENDER_ITEMS", 5), 10))
        stickers: Iterable[Sticker] = await context.bot.get_custom_emoji_stickers(ids[:limit], read_timeout=30)
        for sticker in stickers:
            await process_menu_asset(
                update.message,
                context,
                source_from_sticker(sticker),
                update.effective_user.id,
                section,
            )
        return

    if await handle_pending_text(update, context):
        return

    pack_name = pack_name_from_link(update.message.text or "")

    if not ids:
        if pack_name:
            await remember_user(update, context, "pack_link", render_started=True)
            try:
                sticker_set = await context.bot.get_sticker_set(pack_name, read_timeout=30)
            except TelegramError:
                logging.exception("Failed to load sticker set %s", pack_name)
                await update.message.reply_text("Не смог открыть этот pack. Проверь ссылку.")
                return
            limit = max(1, min(env_int("MAX_PACK_RENDER_ITEMS", 3), 10))
            stickers = list(sticker_set.stickers[:limit])
            if not stickers:
                await update.message.reply_text("В этом pack не нашел стикеров.")
                return
            await update.message.reply_text(f"Нашел pack, рендерю первые {len(stickers)} шт.")
            for index, sticker in enumerate(stickers):
                source = source_from_sticker(sticker)
                LAST_SOURCE[update.effective_user.id] = source
                await process_source(
                    update.message,
                    context,
                    source,
                    settings_for(update.effective_user.id),
                    charge_rate=index == 0,
                )
            return

        await remember_user(update, context, "text")
        current = settings_for(update.effective_user.id)
        await update.message.reply_text(
            "Пришли animated sticker, video sticker, premium/custom emoji, фото/видео или ссылку на pack.",
            reply_markup=main_menu_keyboard(current),
        )
        return

    limit = max(1, min(env_int("MAX_CUSTOM_EMOJI_RENDER_ITEMS", 5), 10))
    stickers: Iterable[Sticker] = await context.bot.get_custom_emoji_stickers(ids[:limit], read_timeout=30)
    sticker_list = list(stickers)
    if not sticker_list:
        await update.message.reply_text("Не смог получить файл этого custom emoji.")
        return

    source = source_from_sticker(sticker_list[0])
    await remember_user(
        update,
        context,
        "custom_emoji",
        render_started=True,
        source_label=source.label,
        source_message=update.message,
    )
    for index, sticker in enumerate(sticker_list):
        source = source_from_sticker(sticker)
        LAST_SOURCE[update.effective_user.id] = source
        await process_source(
            update.message,
            context,
            source,
            settings_for(update.effective_user.id),
            charge_rate=index == 0,
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logging.critical(
            "Polling conflict detected. Another getUpdates request used this bot token; exiting for systemd restart."
        )
        os._exit(75)
    logging.exception("Unhandled error for update %s", update, exc_info=context.error)


async def safe_startup_api_call(label: str, awaitable) -> None:
    try:
        await awaitable
    except RetryAfter as error:
        logging.warning(
            "Skipping startup Telegram API call %s after flood control: retry_after=%s",
            label,
            getattr(error, "retry_after", "unknown"),
        )
    except TelegramError:
        logging.exception("Startup Telegram API call failed: %s", label)


async def post_init(app: Application) -> None:
    public_commands = [
        ("start", "как сделать GIF из стикера"),
        ("menu", "меню рендера"),
        ("bg", "выбрать или задать фон"),
        ("settings", "текущие настройки"),
        ("limits", "лимиты и очередь"),
        ("whoami", "показать мой Telegram ID"),
        ("help", "помощь"),
    ]
    admin_commands = [
        *public_commands,
        ("users", "админ: статистика пользователей"),
        ("menu_assets", "админ: GIF для меню"),
        ("menu_asset_palette", "админ: GIF для палитры"),
        ("broadcast", "админ: черновик рассылки"),
        ("broadcast_send", "админ: отправить рассылку"),
        ("broadcast_cancel", "админ: отменить рассылку"),
    ]

    if env_bool("SYNC_BOT_PROFILE_ON_STARTUP", False):
        await safe_startup_api_call("set_my_name", app.bot.set_my_name("Sticker Loop GIF"))
        await safe_startup_api_call(
            "set_my_short_description",
            app.bot.set_my_short_description(
                "Делаю GIF/MP4 из Telegram стикеров, premium emoji и custom emoji. Фон на выбор."
            ),
        )
        await safe_startup_api_call(
            "set_my_description",
            app.bot.set_my_description(
                "Отправь animated sticker, video sticker, premium emoji или custom emoji. "
                "Бот соберет зацикленную MP4-анимацию как GIF: темный фон по умолчанию, "
                "цвета через /bg #101820. Поддержка .tgs и .webm стикеров, фоновые пресеты "
                "и аккуратные лимиты очереди."
            ),
        )

    await safe_startup_api_call(
        "set_my_commands:default",
        app.bot.set_my_commands(public_commands, scope=BotCommandScopeDefault()),
    )

    target = log_chat_id()
    if target:
        await safe_startup_api_call(
            "set_my_commands:log_chat_admins",
            app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChatAdministrators(chat_id=target)),
        )

    for admin_id in parse_int_list(os.getenv("ADMIN_USER_IDS")):
        await safe_startup_api_call(
            f"set_my_commands:admin:{admin_id}",
            app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id)),
        )


def build_app(token: str) -> Application:
    return (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )


def main() -> None:
    global GLOBAL_RENDER_GATE

    load_dotenv(ENV_PATH)
    setup_logging()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    config = safety_config()
    GLOBAL_RENDER_GATE = RenderGate(config.max_global_renders)
    load_limit_state()
    load_menu_assets()
    cleanup_old_runs(config)

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is missing. Put it in .env or export it.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg and ffprobe are required.")

    app = build_app(token)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler(["settings", "menu"], settings))
    app.add_handler(CommandHandler("limits", limits))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("menu_assets", menu_assets_command))
    app.add_handler(CommandHandler("menu_asset_palette", menu_asset_palette_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("broadcast_send", broadcast_send))
    app.add_handler(CommandHandler("broadcast_cancel", broadcast_cancel))
    app.add_handler(CommandHandler("bg", bg))
    app.add_handler(
        CallbackQueryHandler(on_menu_callback, pattern=r"^(menu:|fmt:|setbg:|itemcolor:|notes:|wm:)")
    )
    app.add_handler(CallbackQueryHandler(on_background_callback, pattern=r"^bg:"))
    app.add_handler(MessageHandler(filters.Sticker.ALL, on_sticker))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    logging.info("Sticker loop bot is running with polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
