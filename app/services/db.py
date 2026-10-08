import aiosqlite
import datetime
import importlib
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from app.services.config import settings

DB_PATH = settings.SQLITE_DB_PATH
HISTORY_DAYS = 7
HISTORY_LIMIT = 10
FREE_DAILY_LIMIT = 1
PRO_PLAN_DAYS = 30
PRO_PLAN_REQUESTS = 30
ONE_TIME_PACKAGE_REQUESTS = 5

PLACEHOLDER_RE = re.compile(r"\?")


def _is_free_tester(platform: str, user_id: str) -> bool:
    return (
        str(platform).lower() == "telegram"
        and str(user_id) in settings.telegram_free_tester_ids
    )


def get_db_backend() -> str:
    return settings.db_backend


def get_database_url() -> str:
    return settings.DATABASE_URL.strip()


def get_sqlite_db_path() -> str:
    return settings.SQLITE_DB_PATH


def _unsupported_db_backend_error(backend: str) -> RuntimeError:
    if backend == "postgres":
        return RuntimeError(
            "A SQLite-specific query reached the PostgreSQL backend path. "
            "Use backend-aware query branches for PostgreSQL instead of PRAGMA, BEGIN IMMEDIATE, or INSERT OR IGNORE."
        )
    return RuntimeError(f"Unsupported DB_BACKEND: {backend}")


class PostgresCursorAdapter:
    def __init__(self, rows: list[Any] | None = None, lastrowid: int | None = None, rowcount: int = -1):
        self._rows = rows or []
        self._index = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    async def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self):
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows


class PostgresConnectionAdapter:
    def __init__(self, connection: Any):
        self._connection = connection
        self.row_factory = None
        self._in_transaction = False

    async def execute(self, query: str, params: tuple[Any, ...] = ()):
        upper_query = query.lstrip().upper()
        if upper_query.startswith(("SELECT", "WITH")) or " RETURNING " in upper_query:
            rows = await self._connection.fetch(query, *params)
            lastrowid = None
            if " RETURNING " in upper_query and rows:
                first_row = rows[0]
                try:
                    lastrowid = first_row[0]
                except Exception:
                    lastrowid = None
            return PostgresCursorAdapter(list(rows), lastrowid=lastrowid, rowcount=len(rows))

        status = await self._connection.execute(query, *params)
        rowcount = -1
        if isinstance(status, str):
            parts = status.strip().split()
            if parts:
                try:
                    rowcount = int(parts[-1])
                except ValueError:
                    rowcount = -1
        return PostgresCursorAdapter(rowcount=rowcount)

    async def commit(self):
        if self._in_transaction:
            await self._connection.execute("COMMIT")
            self._in_transaction = False
        return None

    async def begin(self):
        if not self._in_transaction:
            await self._connection.execute("BEGIN")
            self._in_transaction = True


async def begin_write_transaction(db):
    if get_db_backend() == "postgres":
        await db.begin()
        return
    await execute_query(db, "BEGIN IMMEDIATE")


async def _init_postgres_db(db):
    await execute_query(db, """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        username TEXT,
        requests INTEGER NOT NULL DEFAULT 0,
        blocked INTEGER NOT NULL DEFAULT 0,
        registered_at TEXT,
        tariff TEXT NOT NULL DEFAULT 'free',
        balance_requests INTEGER NOT NULL DEFAULT 0,
        free_requests_used_today INTEGER NOT NULL DEFAULT 0,
        free_reset_date TEXT NOT NULL DEFAULT '',
        pro_expires_at TEXT,
        referral_code TEXT,
        referred_by TEXT,
        referral_reward_granted INTEGER NOT NULL DEFAULT 0,
        last_request_at TEXT
    )
    """)
    await execute_query(db, "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_platform_user_id ON users(platform, user_id)")
    await execute_query(db, """
    CREATE TABLE IF NOT EXISTS history (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        prompt TEXT,
        response TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    await execute_query(db, """
    CREATE TABLE IF NOT EXISTS processed_vk_messages (
        message_id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    await execute_query(db, "CREATE INDEX IF NOT EXISTS idx_history_user_platform_created ON history(platform, user_id, created_at)")
    await execute_query(db, "CREATE INDEX IF NOT EXISTS idx_processed_vk_messages_created ON processed_vk_messages(created_at)")
    await execute_query(db, """
    CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY,
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        payment_type TEXT NOT NULL,
        amount DOUBLE PRECISION NOT NULL,
        requests_added INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        entitlements_applied INTEGER NOT NULL DEFAULT 0,
        side_effects_status TEXT NOT NULL DEFAULT 'pending',
        side_effects_updated_at TIMESTAMP,
        side_effects_last_error TEXT,
        external_payment_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        paid_at TIMESTAMP
    )
    """)
    await execute_query(db, "ALTER TABLE payments ADD COLUMN IF NOT EXISTS entitlements_applied INTEGER NOT NULL DEFAULT 0")
    await execute_query(db, "ALTER TABLE payments ADD COLUMN IF NOT EXISTS side_effects_status TEXT NOT NULL DEFAULT 'pending'")
    await execute_query(db, "ALTER TABLE payments ADD COLUMN IF NOT EXISTS side_effects_updated_at TIMESTAMP")
    await execute_query(db, "ALTER TABLE payments ADD COLUMN IF NOT EXISTS side_effects_last_error TEXT")
    await execute_query(db, """
    DELETE FROM payments
    WHERE external_payment_id IS NOT NULL
      AND id NOT IN (
          SELECT MIN(id)
          FROM payments
          WHERE external_payment_id IS NOT NULL
          GROUP BY external_payment_id
      )
    """)
    await execute_query(db, "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_external_payment_id ON payments(external_payment_id)")
    await execute_query(db, """
    CREATE TABLE IF NOT EXISTS receipts (
        id SERIAL PRIMARY KEY,
        payment_external_id TEXT NOT NULL UNIQUE,
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        amount DOUBLE PRECISION NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'created',
        receipt_url TEXT,
        external_receipt_id TEXT,
        payload TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sent_at TIMESTAMP,
        fiscal_attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    )
    """)
    await execute_query(db, "CREATE INDEX IF NOT EXISTS idx_payments_platform_user_id ON payments(platform, user_id)")
    await execute_query(db, "CREATE INDEX IF NOT EXISTS idx_receipts_platform_user_id ON receipts(platform, user_id)")
    await execute_query(db, """
    CREATE TABLE IF NOT EXISTS price_compare_cache (
        cache_key TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP NOT NULL
    )
    """)
    await execute_query(db, "CREATE INDEX IF NOT EXISTS idx_price_compare_cache_expires ON price_compare_cache(expires_at)")
    await execute_query(db, """
    CREATE TABLE IF NOT EXISTS price_compare_history (
        id SERIAL PRIMARY KEY,
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_input TEXT,
        article TEXT,
        wb_title TEXT,
        wb_price DOUBLE PRECISION,
        search_query TEXT,
        ozon_results TEXT,
        yandex_results TEXT,
        best_marketplace TEXT,
        best_price DOUBLE PRECISION,
        status TEXT NOT NULL,
        error TEXT,
        ozon_count INTEGER NOT NULL DEFAULT 0,
        yandex_count INTEGER NOT NULL DEFAULT 0,
        elapsed_seconds DOUBLE PRECISION,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    await execute_query(db, "CREATE INDEX IF NOT EXISTS idx_price_compare_history_created ON price_compare_history(created_at)")
    await execute_query(db, "CREATE INDEX IF NOT EXISTS idx_price_compare_history_user ON price_compare_history(platform, user_id, created_at)")
    await execute_query(db, "CREATE INDEX IF NOT EXISTS idx_price_compare_history_article ON price_compare_history(article)")
    await db.commit()


@asynccontextmanager
async def connect_db():
    backend = get_db_backend()
    if backend == "sqlite":
        async with aiosqlite.connect(get_sqlite_db_path()) as db:
            yield db
        return
    if backend == "postgres":
        database_url = get_database_url()
        if not database_url:
            raise RuntimeError("DB_BACKEND=postgres requires DATABASE_URL to be set.")
        try:
            asyncpg = importlib.import_module("asyncpg")
        except ImportError as error:
            raise RuntimeError("DB_BACKEND=postgres requires the asyncpg package to be installed.") from error

        connection = await asyncpg.connect(database_url)
        try:
            yield PostgresConnectionAdapter(connection)
        finally:
            await connection.close()
        return
    raise _unsupported_db_backend_error(backend)


def _convert_query_placeholders(query: str, backend: str) -> str:
    if backend != "postgres":
        return query

    index = 0

    def replace(_: re.Match[str]) -> str:
        nonlocal index
        index += 1
        return f"${index}"

    return PLACEHOLDER_RE.sub(replace, query)


def _normalize_query(query: str) -> str:
    backend = get_db_backend()
    normalized = _convert_query_placeholders(query, backend)
    if backend == "postgres" and "INSERT OR IGNORE" in normalized:
        raise _unsupported_db_backend_error(backend)
    if backend == "postgres" and ("PRAGMA table_info" in normalized or "BEGIN IMMEDIATE" in normalized):
        raise _unsupported_db_backend_error(backend)
    return normalized


async def execute_query(db, query: str, params: tuple | list = ()):
    return await db.execute(_normalize_query(query), tuple(params))


def ensure_db_parent_dir() -> None:
    if get_db_backend() != "sqlite":
        return
    Path(get_sqlite_db_path()).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


ensure_db_parent_dir()


def utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def as_utc_naive(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def utcnow_iso() -> str:
    return utcnow().replace(microsecond=0).isoformat(sep=" ")


def db_timestamp_now() -> datetime.datetime | str:
    now = utcnow().replace(microsecond=0)
    if get_db_backend() == "postgres":
        return now
    return now.isoformat(sep=" ")


def today_iso() -> str:
    return utcnow().date().isoformat()


def normalize_access_profile_state(profile: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[Any], str]:
    normalized = dict(profile)
    updates: list[str] = []
    params: list[Any] = []
    current_tariff = normalized.get("tariff") or "free"
    today = today_iso()

    if normalized.get("free_reset_date") != today:
        updates.extend([
            "free_requests_used_today = 0",
            "free_reset_date = ?",
        ])
        params.append(today)
        normalized["free_requests_used_today"] = 0
        normalized["free_reset_date"] = today

    if current_tariff == "pro" and normalized.get("pro_expires_at"):
        try:
            expires_at = as_utc_naive(datetime.datetime.fromisoformat(str(normalized["pro_expires_at"])))
        except ValueError:
            expires_at = None
        if expires_at and expires_at < utcnow():
            updates.extend([
                "tariff = 'free'",
                "pro_expires_at = NULL",
            ])
            current_tariff = "free"
            normalized["tariff"] = "free"
            normalized["pro_expires_at"] = None

    return normalized, updates, params, current_tariff


def calculate_pro_expiration(current_expires_at_raw: Any, days: int) -> str:
    base_time = utcnow()
    if current_expires_at_raw:
        try:
            current_expires_at = as_utc_naive(datetime.datetime.fromisoformat(str(current_expires_at_raw)))
        except ValueError:
            current_expires_at = None
        if current_expires_at and current_expires_at > base_time:
            base_time = current_expires_at
    return (base_time + datetime.timedelta(days=days)).replace(microsecond=0).isoformat(sep=" ")


async def is_vk_message_processed(message_id: int | str) -> bool:
    async with connect_db() as db:
        cur = await execute_query(
            db,
            "SELECT 1 FROM processed_vk_messages WHERE message_id = ?",
            (str(message_id),),
        )
        return await cur.fetchone() is not None


async def mark_vk_message_processed(message_id: int | str):
    async with connect_db() as db:
        insert_query = "INSERT INTO processed_vk_messages (message_id) VALUES (?)"
        if get_db_backend() == "postgres":
            insert_query = insert_query.rstrip() + " ON CONFLICT (message_id) DO NOTHING"
        else:
            insert_query = insert_query.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)

        await execute_query(db, insert_query, (str(message_id),))
        await db.commit()


async def get_recent_history(platform: str, user_id: str, limit: int = HISTORY_LIMIT, days: int = HISTORY_DAYS):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    async with connect_db() as db:
        cur = await execute_query(
            db,
            """
            SELECT prompt, response FROM history
            WHERE user_id = ? AND platform = ? AND created_at >= ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, platform, cutoff, limit)
        )
        rows = await cur.fetchall()
        return list(reversed(rows))

async def delete_old_history(days: int = HISTORY_DAYS):
    cutoff_dt = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).replace(microsecond=0)
    cutoff = cutoff_dt if get_db_backend() == "postgres" else cutoff_dt.isoformat(sep=" ")
    async with connect_db() as db:
        await execute_query(
            db,
            "DELETE FROM history WHERE created_at < ?",
            (cutoff,)
        )
        await db.commit()


async def delete_old_processed_vk_messages(days: int = HISTORY_DAYS):
    cutoff_dt = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).replace(microsecond=0)
    cutoff = cutoff_dt if get_db_backend() == "postgres" else cutoff_dt.isoformat(sep=" ")
    async with connect_db() as db:
        await execute_query(
            db,
            "DELETE FROM processed_vk_messages WHERE created_at < ?",
            (cutoff,)
        )
        await db.commit()


async def delete_user_history(platform: str, user_id: str):
    async with connect_db() as db:
        await execute_query(
            db,
            "DELETE FROM history WHERE user_id = ? AND platform = ?",
            (user_id, platform)
        )
        await db.commit()

async def init_db():
    async with connect_db() as db:
        if get_db_backend() == "postgres":
            await _init_postgres_db(db)
            return
        await execute_query(db, "PRAGMA journal_mode=WAL")
        await execute_query(db, "PRAGMA busy_timeout=5000")
        await execute_query(db, """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            username TEXT,
            requests INTEGER DEFAULT 0
        )
        """)
        await execute_query(
            db,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_platform_user_id ON users(platform, user_id)"
        )
        cur = await execute_query(db, "PRAGMA table_info(users)")
        columns = [row[1] for row in await cur.fetchall()]
        if "blocked" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0")
        if "registered_at" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN registered_at TEXT")
            await execute_query(db, "UPDATE users SET registered_at = ? WHERE registered_at IS NULL OR registered_at = ''", (utcnow_iso(),))
        if "tariff" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN tariff TEXT DEFAULT 'free'")
        if "balance_requests" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN balance_requests INTEGER DEFAULT 0")
        if "free_requests_used_today" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN free_requests_used_today INTEGER DEFAULT 0")
        if "free_reset_date" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN free_reset_date TEXT DEFAULT ''")
        if "pro_expires_at" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN pro_expires_at TEXT")
        if "referral_code" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN referral_code TEXT")
        if "referred_by" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN referred_by TEXT")
        if "referral_reward_granted" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN referral_reward_granted INTEGER DEFAULT 0")
        if "last_request_at" not in columns:
            await execute_query(db, "ALTER TABLE users ADD COLUMN last_request_at TEXT")

        await execute_query(db, """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            prompt TEXT,
            response TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        await execute_query(db, """
        CREATE TABLE IF NOT EXISTS processed_vk_messages (
            message_id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        await execute_query(
            db,
            "CREATE INDEX IF NOT EXISTS idx_history_user_platform_created ON history(platform, user_id, created_at)"
        )
        await execute_query(
            db,
            "CREATE INDEX IF NOT EXISTS idx_processed_vk_messages_created ON processed_vk_messages(created_at)"
        )
        await execute_query(db, """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            payment_type TEXT NOT NULL,
            amount REAL NOT NULL,
            requests_added INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            entitlements_applied INTEGER NOT NULL DEFAULT 0,
            side_effects_status TEXT NOT NULL DEFAULT 'pending',
            side_effects_updated_at TIMESTAMP,
            side_effects_last_error TEXT,
            external_payment_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP
        )
        """)
        cur = await execute_query(db, "PRAGMA table_info(payments)")
        payment_columns = [row[1] for row in await cur.fetchall()]
        if "entitlements_applied" not in payment_columns:
            await execute_query(db, "ALTER TABLE payments ADD COLUMN entitlements_applied INTEGER NOT NULL DEFAULT 0")
        if "side_effects_status" not in payment_columns:
            await execute_query(db, "ALTER TABLE payments ADD COLUMN side_effects_status TEXT NOT NULL DEFAULT 'pending'")
        if "side_effects_updated_at" not in payment_columns:
            await execute_query(db, "ALTER TABLE payments ADD COLUMN side_effects_updated_at TIMESTAMP")
        if "side_effects_last_error" not in payment_columns:
            await execute_query(db, "ALTER TABLE payments ADD COLUMN side_effects_last_error TEXT")
        await execute_query(db, """
        DELETE FROM payments
        WHERE external_payment_id IS NOT NULL
          AND id NOT IN (
              SELECT MIN(id)
              FROM payments
              WHERE external_payment_id IS NOT NULL
              GROUP BY external_payment_id
          )
        """)
        await execute_query(
            db,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_external_payment_id ON payments(external_payment_id)"
        )
        await execute_query(db, """
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_external_id TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            amount REAL NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            receipt_url TEXT,
            external_receipt_id TEXT,
            payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP
        )
        """)
        cur = await execute_query(db, "PRAGMA table_info(receipts)")
        receipt_columns = [row[1] for row in await cur.fetchall()]
        if "fiscal_attempts" not in receipt_columns:
            await execute_query(db, "ALTER TABLE receipts ADD COLUMN fiscal_attempts INTEGER DEFAULT 0")
        if "last_error" not in receipt_columns:
            await execute_query(db, "ALTER TABLE receipts ADD COLUMN last_error TEXT")
        await execute_query(
            db,
            "CREATE INDEX IF NOT EXISTS idx_payments_platform_user_id ON payments(platform, user_id)"
        )
        await execute_query(
            db,
            "CREATE INDEX IF NOT EXISTS idx_receipts_platform_user_id ON receipts(platform, user_id)"
        )
        await execute_query(db, """
        CREATE TABLE IF NOT EXISTS price_compare_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
        """)
        await execute_query(
            db,
            "CREATE INDEX IF NOT EXISTS idx_price_compare_cache_expires ON price_compare_cache(expires_at)"
        )
        await execute_query(db, """
        CREATE TABLE IF NOT EXISTS price_compare_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_input TEXT,
            article TEXT,
            wb_title TEXT,
            wb_price REAL,
            search_query TEXT,
            ozon_results TEXT,
            yandex_results TEXT,
            best_marketplace TEXT,
            best_price REAL,
            status TEXT NOT NULL,
            error TEXT,
            ozon_count INTEGER NOT NULL DEFAULT 0,
            yandex_count INTEGER NOT NULL DEFAULT 0,
            elapsed_seconds REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        await execute_query(
            db,
            "CREATE INDEX IF NOT EXISTS idx_price_compare_history_created ON price_compare_history(created_at)"
        )
        await execute_query(
            db,
            "CREATE INDEX IF NOT EXISTS idx_price_compare_history_user ON price_compare_history(platform, user_id, created_at)"
        )
        await execute_query(
            db,
            "CREATE INDEX IF NOT EXISTS idx_price_compare_history_article ON price_compare_history(article)"
        )
        await db.commit()


async def ensure_user_state(platform: str, user_id: str):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await begin_write_transaction(db)
        select_user_state_query = """
            SELECT tariff, free_requests_used_today, free_reset_date, pro_expires_at
            FROM users
            WHERE platform = ? AND user_id = ?
        """
        if get_db_backend() == "postgres":
            select_user_state_query = select_user_state_query.rstrip() + " FOR UPDATE"

        cur = await execute_query(db, select_user_state_query, (platform, user_id))
        row = await cur.fetchone()
        if row is None:
            await db.commit()
            return

        updates = []
        params = []
        today = today_iso()
        current_tariff = row["tariff"] or "free"
        if row["free_reset_date"] != today:
            updates.extend([
                "free_requests_used_today = 0",
                "free_reset_date = ?"
            ])
            params.append(today)
        if current_tariff == "pro" and row["pro_expires_at"]:
            try:
                expires_at = as_utc_naive(datetime.datetime.fromisoformat(str(row["pro_expires_at"])))
            except ValueError:
                expires_at = None
            if expires_at and expires_at < utcnow():
                updates.append("tariff = 'free'")
                updates.append("pro_expires_at = NULL")
        if updates:
            params.extend([platform, user_id])
            await execute_query(
                db,
                f"UPDATE users SET {', '.join(updates)} WHERE platform = ? AND user_id = ?",
                tuple(params)
            )
        await db.commit()

async def add_user(platform: str, user_id: str, username: Optional[str] = None):
    if platform == "telegram":
        from app.platforms.telegram.aiogram_bot import bot
        bot_id = str((await bot.get_me()).id)
        if str(user_id) == bot_id:
            return
    async with connect_db() as db:
        if get_db_backend() == "postgres":
            await execute_query(
                db,
                """
                INSERT INTO users (
                    platform, user_id, username, registered_at, free_reset_date, referral_code
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (platform, user_id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, users.username)
                """,
                (platform, user_id, username, utcnow_iso(), today_iso(), f"{platform}_{user_id}")
            )
        else:
            await execute_query(
                db,
                """
                INSERT OR IGNORE INTO users (
                    platform, user_id, username, registered_at, free_reset_date, referral_code
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (platform, user_id, username, utcnow_iso(), today_iso(), f"{platform}_{user_id}")
            )
        if username and get_db_backend() != "postgres":
            await execute_query(
                db,
                "UPDATE users SET username = COALESCE(?, username) WHERE platform = ? AND user_id = ?",
                (username, platform, user_id)
            )
        await db.commit()
    await ensure_user_state(platform, user_id)

async def increment_requests(platform: str, user_id: str):
    async with connect_db() as db:
        await execute_query(
            db,
            "UPDATE users SET requests = requests + 1, last_request_at = ? WHERE platform = ? AND user_id = ?",
            (utcnow_iso(), platform, user_id)
        )
        await db.commit()

async def add_history(platform: str, user_id: str, prompt: str, response: str):
    async with connect_db() as db:
        await execute_query(
            db,
            "INSERT INTO history (user_id, platform, prompt, response) VALUES (?, ?, ?, ?)",
            (user_id, platform, prompt, response)
        )
        await db.commit()

async def get_stats():
    from app.platforms.telegram.aiogram_bot import bot
    bot_id = str((await bot.get_me()).id)
    async with connect_db() as db:
        cur = await execute_query(
            db,
            "SELECT platform, COUNT(*), SUM(requests) FROM users WHERE blocked=0 AND user_id != ? GROUP BY platform",
            (bot_id,)
        )
        rows = await cur.fetchall()
        return rows


def _parse_db_datetime(value: Any) -> datetime.datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


async def get_price_compare_cache(cache_key: str) -> dict | None:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            "SELECT payload, expires_at FROM price_compare_cache WHERE cache_key = ?",
            (cache_key,),
        )
        row = await cur.fetchone()
        if not row:
            return None

        expires_at = _parse_db_datetime(row["expires_at"])
        if expires_at and expires_at <= utcnow():
            await execute_query(db, "DELETE FROM price_compare_cache WHERE cache_key = ?", (cache_key,))
            await db.commit()
            return None

        try:
            return json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            await execute_query(db, "DELETE FROM price_compare_cache WHERE cache_key = ?", (cache_key,))
            await db.commit()
            return None


async def set_price_compare_cache(cache_key: str, payload: dict, ttl_seconds: int):
    ttl = max(1, int(ttl_seconds or 1))
    expires_at_dt = utcnow() + datetime.timedelta(seconds=ttl)
    expires_at = expires_at_dt if get_db_backend() == "postgres" else expires_at_dt.replace(microsecond=0).isoformat(sep=" ")
    payload_text = json.dumps(payload, ensure_ascii=False)
    async with connect_db() as db:
        await execute_query(
            db,
            """
            INSERT INTO price_compare_cache (cache_key, payload, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT (cache_key) DO UPDATE SET
                payload = excluded.payload,
                created_at = CURRENT_TIMESTAMP,
                expires_at = excluded.expires_at
            """,
            (cache_key, payload_text, expires_at),
        )
        await db.commit()


async def record_price_compare_history(
    platform: str,
    user_id: str,
    user_input: str,
    article: str,
    wb_title: str,
    wb_price: float | None,
    search_query: str,
    ozon_results: list[dict],
    yandex_results: list[dict],
    best_marketplace: str,
    best_price: float | None,
    status: str,
    error: str = "",
    elapsed_seconds: float | None = None,
):
    async with connect_db() as db:
        await execute_query(
            db,
            """
            INSERT INTO price_compare_history (
                platform, user_id, user_input, article, wb_title, wb_price, search_query,
                ozon_results, yandex_results, best_marketplace, best_price, status, error,
                ozon_count, yandex_count, elapsed_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                user_id,
                user_input,
                article,
                wb_title,
                wb_price,
                search_query,
                json.dumps(ozon_results, ensure_ascii=False),
                json.dumps(yandex_results, ensure_ascii=False),
                best_marketplace,
                best_price,
                status,
                error,
                len(ozon_results),
                len(yandex_results),
                elapsed_seconds,
            ),
        )
        await db.commit()


async def get_price_compare_stats(limit: int = 10) -> dict:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END) AS partial,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                SUM(CASE WHEN ozon_count > 0 THEN 1 ELSE 0 END) AS ozon_success,
                SUM(CASE WHEN yandex_count > 0 THEN 1 ELSE 0 END) AS yandex_success,
                AVG(elapsed_seconds) AS avg_elapsed
            FROM price_compare_history
            """,
        )
        totals_row = await cur.fetchone()

        cur = await execute_query(
            db,
            """
            SELECT platform, user_id, COUNT(*) AS requests
            FROM price_compare_history
            GROUP BY platform, user_id
            ORDER BY requests DESC
            LIMIT ?
            """,
            (limit,),
        )
        per_user_rows = await cur.fetchall()

        cur = await execute_query(
            db,
            """
            SELECT article, wb_title, COUNT(*) AS requests
            FROM price_compare_history
            WHERE article IS NOT NULL AND article != ''
            GROUP BY article, wb_title
            ORDER BY requests DESC
            LIMIT ?
            """,
            (limit,),
        )
        top_product_rows = await cur.fetchall()

    totals = dict(totals_row) if totals_row else {}
    total = int(totals.get("total") or 0)
    ozon_success = int(totals.get("ozon_success") or 0)
    yandex_success = int(totals.get("yandex_success") or 0)
    return {
        "total": total,
        "success": int(totals.get("success") or 0),
        "partial": int(totals.get("partial") or 0),
        "errors": int(totals.get("errors") or 0),
        "avg_elapsed": round(float(totals.get("avg_elapsed") or 0), 2),
        "ozon_success_rate": round((ozon_success / total * 100), 1) if total else 0,
        "yandex_success_rate": round((yandex_success / total * 100), 1) if total else 0,
        "top_users": [dict(row) for row in per_user_rows],
        "top_products": [dict(row) for row in top_product_rows],
    }


async def get_user_profile(platform: str, user_id: str):
    await ensure_user_state(platform, user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await begin_write_transaction(db)

        select_profile_query = "SELECT * FROM users WHERE platform = ? AND user_id = ?"
        if get_db_backend() == "postgres":
            select_profile_query = select_profile_query.rstrip() + " FOR UPDATE"

        cur = await execute_query(
            db,
            select_profile_query,
            (platform, user_id)
        )
        row = await cur.fetchone()
        if row is None:
            await db.commit()
            return None

        profile, updates, params, _ = normalize_access_profile_state(dict(row))
        if updates:
            params.extend([platform, user_id])
            await execute_query(
                db,
                f"UPDATE users SET {', '.join(updates)} WHERE platform = ? AND user_id = ?",
                tuple(params)
            )

        await db.commit()
        profile["free_tester"] = _is_free_tester(platform, user_id)
        return profile


async def has_active_pro_access(platform: str, user_id: str) -> bool:
    """Return feature entitlement without consuming a product-check request."""
    profile = await get_user_profile(platform, user_id)
    if not profile or profile.get("blocked"):
        return False
    return bool(profile.get("free_tester") or (profile.get("tariff") or "").lower() == "pro")


async def get_request_access(platform: str, user_id: str):
    await ensure_user_state(platform, user_id)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await begin_write_transaction(db)

        select_profile_query = """
            SELECT blocked, tariff, balance_requests, free_requests_used_today,
                   free_reset_date, pro_expires_at, referral_code, registered_at
            FROM users
            WHERE platform = ? AND user_id = ?
        """
        if get_db_backend() == "postgres":
            select_profile_query = select_profile_query.rstrip() + " FOR UPDATE"

        cur = await execute_query(db, select_profile_query, (platform, user_id))
        row = await cur.fetchone()
        if row is None:
            await db.commit()
            return False, "Профиль пользователя не найден.", None

        profile, updates, params, current_tariff = normalize_access_profile_state(dict(row))
        if updates:
            params.extend([platform, user_id])
            await execute_query(
                db,
                f"UPDATE users SET {', '.join(updates)} WHERE platform = ? AND user_id = ?",
                tuple(params)
            )

        if profile.get("blocked"):
            await db.commit()
            return False, "Доступ ограничен администратором.", profile
        if _is_free_tester(platform, user_id):
            profile["free_tester"] = True
            await db.commit()
            return True, "free_tester", profile
        if (profile.get("balance_requests") or 0) > 0:
            await db.commit()
            return True, "paid_balance", profile
        if current_tariff == "pro":
            await db.commit()
            return False, "Лимит проверок по подписке PRO исчерпан. Продлите подписку или купите пакет проверок.", profile
        if (profile.get("free_requests_used_today") or 0) < FREE_DAILY_LIMIT:
            await db.commit()
            return True, "free_daily", profile

        await db.commit()
        return False, "Лимит бесплатных проверок на сегодня исчерпан. Купите пакет или подписку.", profile


async def consume_request_limit(platform: str, user_id: str, _retry_depth: int = 0):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await begin_write_transaction(db)

        select_profile_query = """
            SELECT blocked, tariff, balance_requests, free_requests_used_today,
                   free_reset_date, pro_expires_at
            FROM users
            WHERE platform = ? AND user_id = ?
        """
        if get_db_backend() == "postgres":
            select_profile_query = select_profile_query.rstrip() + " FOR UPDATE"

        cur = await execute_query(db, select_profile_query, (platform, user_id))
        row = await cur.fetchone()
        if row is None:
            await db.commit()
            return False, "Профиль пользователя не найден."

        profile, updates, params, current_tariff = normalize_access_profile_state(dict(row))

        if updates:
            params.extend([platform, user_id])
            await execute_query(
                db,
                f"UPDATE users SET {', '.join(updates)} WHERE platform = ? AND user_id = ?",
                tuple(params)
            )

        if profile.get("blocked"):
            await db.commit()
            return False, "Доступ ограничен администратором."

        if _is_free_tester(platform, user_id):
            await db.commit()
            return True, "free_tester"

        if (profile.get("balance_requests") or 0) > 0:
            balance_cur = await execute_query(
                db,
                """
                UPDATE users
                SET balance_requests = balance_requests - 1
                WHERE platform = ? AND user_id = ? AND balance_requests > 0
                """,
                (platform, user_id)
            )
            if balance_cur.rowcount == 0:
                await db.commit()
                if _retry_depth >= 1:
                    return False, "Лимит бесплатных проверок на сегодня исчерпан. Купите пакет или подписку."
                return await consume_request_limit(platform, user_id, _retry_depth + 1)
            await db.commit()
            return True, "paid_balance"

        if current_tariff == "pro":
            await db.commit()
            return False, "Лимит проверок по подписке PRO исчерпан. Продлите подписку или купите пакет проверок."

        if (profile.get("free_requests_used_today") or 0) < FREE_DAILY_LIMIT:
            free_cur = await execute_query(
                db,
                """
                UPDATE users
                SET free_requests_used_today = free_requests_used_today + 1
                WHERE platform = ? AND user_id = ? AND free_requests_used_today < ?
                """,
                (platform, user_id, FREE_DAILY_LIMIT)
            )
            if free_cur.rowcount == 0:
                await db.commit()
                if _retry_depth >= 1:
                    return False, "Лимит бесплатных проверок на сегодня исчерпан. Купите пакет или подписку."
                return await consume_request_limit(platform, user_id, _retry_depth + 1)
            await db.commit()
            return True, "free_daily"

        await db.commit()
        return False, "Лимит бесплатных проверок на сегодня исчерпан. Купите пакет или подписку."


async def add_request_balance(platform: str, user_id: str, amount: int):
    async with connect_db() as db:
        await execute_query(
            db,
            "UPDATE users SET balance_requests = balance_requests + ? WHERE platform = ? AND user_id = ?",
            (amount, platform, user_id)
        )
        await db.commit()


async def activate_pro_subscription(platform: str, user_id: str, days: int = PRO_PLAN_DAYS, requests: int = PRO_PLAN_REQUESTS):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await begin_write_transaction(db)

        select_user_query = "SELECT pro_expires_at FROM users WHERE platform = ? AND user_id = ?"
        if get_db_backend() == "postgres":
            select_user_query = select_user_query.rstrip() + " FOR UPDATE"

        user_cur = await execute_query(db, select_user_query, (platform, user_id))
        user_row = await user_cur.fetchone()
        expires_at = calculate_pro_expiration(user_row["pro_expires_at"] if user_row else None, days)

        await execute_query(
            db,
            """
            UPDATE users
            SET tariff = 'pro',
                pro_expires_at = ?,
                balance_requests = balance_requests + ?
            WHERE platform = ? AND user_id = ?
            """,
            (expires_at, requests, platform, user_id)
        )
        await db.commit()


async def record_payment(
    platform: str,
    user_id: str,
    provider: str,
    payment_type: str,
    amount: float,
    requests_added: int = 0,
    status: str = "pending",
    external_payment_id: Optional[str] = None,
):
    insert_payment_query = """
        INSERT INTO payments (
            platform, user_id, provider, payment_type, amount,
            requests_added, status, external_payment_id, paid_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    if get_db_backend() == "postgres":
        insert_payment_query = insert_payment_query.rstrip() + " RETURNING id"

    async with connect_db() as db:
        cur = await execute_query(
            db,
            insert_payment_query,
            (
                platform,
                user_id,
                provider,
                payment_type,
                amount,
                requests_added,
                status,
                external_payment_id,
                db_timestamp_now() if status == "paid" else None,
            )
        )
        await db.commit()
        return cur.lastrowid


async def insert_pending_payment_if_missing(
    platform: str,
    user_id: str,
    provider: str,
    payment_type: str,
    amount: float,
    requests_added: int,
    external_payment_id: str,
) -> bool:
    async with connect_db() as db:
        cur = await execute_query(
            db,
            """
            INSERT INTO payments (
                platform, user_id, provider, payment_type, amount,
                requests_added, status, external_payment_id, paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
            ON CONFLICT (external_payment_id) DO NOTHING
            """,
            (
                platform,
                user_id,
                provider,
                payment_type,
                amount,
                requests_added,
                external_payment_id,
            )
        )
        await db.commit()
        return cur.rowcount > 0


async def get_recent_payments(platform: str, user_id: str, limit: int = 10):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            """
            SELECT provider, payment_type, amount, requests_added, status, created_at, paid_at
            FROM payments
            WHERE platform = ? AND user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (platform, user_id, limit)
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def list_users(limit: int = 100):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            """
            SELECT platform, user_id, username, tariff, balance_requests,
                   free_requests_used_today, pro_expires_at, requests,
                   blocked, registered_at, last_request_at
            FROM users
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def list_platform_user_ids(platform: str) -> list[str]:
    async with connect_db() as db:
        cur = await execute_query(
            db,
            "SELECT user_id FROM users WHERE platform = ? AND COALESCE(blocked, 0) = 0 ORDER BY id DESC",
            (platform,),
        )
        rows = await cur.fetchall()
        return [row[0] for row in rows]


async def list_recent_payments(limit: int = 100):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            """
            SELECT platform, user_id, provider, payment_type, amount,
                   requests_added, status, external_payment_id, created_at, paid_at
            FROM payments
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def get_payment_by_external_id(external_payment_id: str):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            "SELECT * FROM payments WHERE external_payment_id = ? ORDER BY id DESC LIMIT 1",
            (external_payment_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_payment_status_by_external_id(external_payment_id: str, status: str):
    async with connect_db() as db:
        paid_at = db_timestamp_now() if status == "paid" else None
        await execute_query(
            db,
            """
            UPDATE payments
            SET status = ?, paid_at = ?
            WHERE external_payment_id = ?
            """,
            (status, paid_at, external_payment_id)
        )
        await db.commit()


async def mark_payment_paid_if_unpaid(external_payment_id: str) -> bool:
    async with connect_db() as db:
        cur = await execute_query(
            db,
            """
            UPDATE payments
            SET status = 'paid', paid_at = ?
            WHERE external_payment_id = ? AND COALESCE(status, '') != 'paid'
            """,
            (db_timestamp_now(), external_payment_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def apply_payment_entitlements_if_needed(
    external_payment_id: str,
    pro_duration_days: int = PRO_PLAN_DAYS,
    referral_bonus_requests: int = 0,
) -> str:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await begin_write_transaction(db)

        select_payment_query = """
            SELECT platform, user_id, payment_type, requests_added, status, entitlements_applied
            FROM payments
            WHERE external_payment_id = ?
        """
        if get_db_backend() == "postgres":
            select_payment_query = select_payment_query.rstrip() + " FOR UPDATE"

        payment_cur = await execute_query(db, select_payment_query, (external_payment_id,))
        payment_row = await payment_cur.fetchone()
        if not payment_row:
            await db.commit()
            return "missing"

        payment = dict(payment_row)
        if payment.get("entitlements_applied"):
            await db.commit()
            return "already_applied"

        platform = payment["platform"]
        user_id = payment["user_id"]
        requests_added = int(payment.get("requests_added") or 0)
        payment_type = payment.get("payment_type") or "one_time"

        if payment_type == "pro":
            select_user_query = "SELECT pro_expires_at FROM users WHERE platform = ? AND user_id = ?"
            if get_db_backend() == "postgres":
                select_user_query = select_user_query.rstrip() + " FOR UPDATE"

            user_cur = await execute_query(db, select_user_query, (platform, user_id))
            user_row = await user_cur.fetchone()
            expires_at = calculate_pro_expiration(user_row["pro_expires_at"] if user_row else None, pro_duration_days)
            user_update_cur = await execute_query(
                db,
                """
                UPDATE users
                SET tariff = 'pro',
                    pro_expires_at = ?,
                    balance_requests = balance_requests + ?
                WHERE platform = ? AND user_id = ?
                """,
                (expires_at, requests_added, platform, user_id)
            )
        else:
            user_update_cur = await execute_query(
                db,
                "UPDATE users SET balance_requests = balance_requests + ? WHERE platform = ? AND user_id = ?",
                (requests_added, platform, user_id)
            )

        if user_update_cur.rowcount == 0:
            await db.commit()
            raise RuntimeError(f"Payment target user not found for entitlements: {external_payment_id}")

        if referral_bonus_requests > 0:
            referral_cur = await execute_query(
                db,
                "SELECT referred_by, referral_reward_granted FROM users WHERE platform = ? AND user_id = ?",
                (platform, user_id)
            )
            user_row = await referral_cur.fetchone()
            if user_row and user_row["referred_by"] and not user_row["referral_reward_granted"]:
                ref_owner_cur = await execute_query(
                    db,
                    "SELECT user_id FROM users WHERE platform = ? AND referral_code = ?",
                    (platform, user_row["referred_by"])
                )
                ref_owner = await ref_owner_cur.fetchone()
                if ref_owner:
                    reward_cur = await execute_query(
                        db,
                        """
                        UPDATE users
                        SET referral_reward_granted = 1
                        WHERE platform = ? AND user_id = ? AND referred_by = ? AND referral_reward_granted = 0
                        """,
                        (platform, user_id, user_row["referred_by"])
                    )
                    if reward_cur.rowcount > 0:
                        await execute_query(
                            db,
                            "UPDATE users SET balance_requests = balance_requests + ? WHERE platform = ? AND user_id = ?",
                            (referral_bonus_requests, platform, ref_owner["user_id"])
                        )

        await execute_query(
            db,
            """
            UPDATE payments
            SET status = 'paid',
                paid_at = COALESCE(paid_at, ?),
                entitlements_applied = 1
            WHERE external_payment_id = ?
            """,
            (db_timestamp_now(), external_payment_id)
        )
        await db.commit()
        return "applied"


async def claim_payment_side_effects(
    external_payment_id: str,
    stale_after_seconds: int = 300,
) -> tuple[str, dict | None]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await begin_write_transaction(db)

        select_payment_query = """
            SELECT platform, user_id, provider, payment_type, amount, requests_added,
                   status, entitlements_applied, side_effects_status, side_effects_updated_at
            FROM payments
            WHERE external_payment_id = ?
        """
        if get_db_backend() == "postgres":
            select_payment_query = select_payment_query.rstrip() + " FOR UPDATE"

        payment_cur = await execute_query(db, select_payment_query, (external_payment_id,))
        payment_row = await payment_cur.fetchone()
        if not payment_row:
            await db.commit()
            return "missing", None

        payment = dict(payment_row)
        if payment.get("status") != "paid" or not payment.get("entitlements_applied"):
            await db.commit()
            return "not_ready", payment

        side_effects_status = payment.get("side_effects_status") or "pending"
        if side_effects_status == "applied":
            await db.commit()
            return "already_applied", payment

        if side_effects_status == "processing":
            updated_at_raw = payment.get("side_effects_updated_at")
            updated_at = None
            if updated_at_raw:
                try:
                    updated_at = datetime.datetime.fromisoformat(str(updated_at_raw))
                except ValueError:
                    updated_at = None
            if updated_at and (utcnow() - updated_at).total_seconds() < stale_after_seconds:
                await db.commit()
                return "in_progress", payment

        await execute_query(
            db,
            """
            UPDATE payments
            SET side_effects_status = 'processing',
                side_effects_updated_at = ?,
                side_effects_last_error = NULL
            WHERE external_payment_id = ?
            """,
            (db_timestamp_now(), external_payment_id)
        )
        await db.commit()
        payment["side_effects_status"] = "processing"
        return "claimed", payment


async def complete_payment_side_effects(
    external_payment_id: str,
    success: bool,
    error_text: str | None = None,
):
    async with connect_db() as db:
        await execute_query(
            db,
            """
            UPDATE payments
            SET side_effects_status = ?,
                side_effects_updated_at = ?,
                side_effects_last_error = ?
            WHERE external_payment_id = ?
            """,
            (
                "applied" if success else "failed",
                db_timestamp_now(),
                None if success else (error_text or "Unknown side effect error"),
                external_payment_id,
            )
        )
        await db.commit()


async def list_payments_for_side_effect_retry(limit: int = 50):
        async with connect_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await execute_query(
                        db,
                        """
                        SELECT platform, user_id, provider, payment_type, external_payment_id,
                                     side_effects_status, side_effects_updated_at, side_effects_last_error
                        FROM payments
                        WHERE status = 'paid'
                            AND entitlements_applied = 1
                            AND side_effects_status IN ('pending', 'failed')
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (limit,)
                )
                rows = await cur.fetchall()
                return [dict(row) for row in rows]


async def upsert_receipt(
    payment_external_id: str,
    platform: str,
    user_id: str,
    provider: str,
    amount: float,
    title: str,
    status: str,
    receipt_url: str | None = None,
    external_receipt_id: str | None = None,
    payload: str | None = None,
    sent: bool = False,
    fiscal_attempts: int | None = None,
    last_error: str | None = None,
):
    async with connect_db() as db:
        sent_at = db_timestamp_now() if sent else None
        await execute_query(
            db,
            """
            INSERT INTO receipts (
                payment_external_id, platform, user_id, provider, amount, title,
                status, receipt_url, external_receipt_id, payload, sent_at, fiscal_attempts, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (payment_external_id) DO UPDATE SET
                platform = excluded.platform,
                user_id = excluded.user_id,
                provider = excluded.provider,
                amount = excluded.amount,
                title = excluded.title,
                status = excluded.status,
                receipt_url = excluded.receipt_url,
                external_receipt_id = excluded.external_receipt_id,
                payload = excluded.payload,
                sent_at = COALESCE(excluded.sent_at, receipts.sent_at),
                fiscal_attempts = COALESCE(excluded.fiscal_attempts, receipts.fiscal_attempts),
                last_error = excluded.last_error
            """,
            (
                payment_external_id,
                platform,
                user_id,
                provider,
                amount,
                title,
                status,
                receipt_url,
                external_receipt_id,
                payload,
                sent_at,
                fiscal_attempts,
                last_error,
            )
        )
        await db.commit()


async def get_receipt_by_payment_id(payment_external_id: str):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            "SELECT * FROM receipts WHERE payment_external_id = ? LIMIT 1",
            (payment_external_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_receipts_by_payment_ids(payment_external_ids: list[str]):
    ids = [payment_id for payment_id in payment_external_ids if payment_id]
    if not ids:
        return {}

    placeholders = ", ".join("?" for _ in ids)
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            f"SELECT * FROM receipts WHERE payment_external_id IN ({placeholders})",
            tuple(ids)
        )
        rows = await cur.fetchall()
        return {row["payment_external_id"]: dict(row) for row in rows}


async def list_receipts_for_retry(limit: int = 50, max_attempts: int = 5):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            """
            SELECT *
            FROM receipts
            WHERE status = 'error' AND COALESCE(fiscal_attempts, 0) < ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (max_attempts, limit)
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def set_user_blocked(platform: str, user_id: str, blocked: bool):
    async with connect_db() as db:
        await execute_query(
            db,
            "UPDATE users SET blocked = ? WHERE platform = ? AND user_id = ?",
            (1 if blocked else 0, platform, user_id)
        )
        await db.commit()


async def grant_user_requests(platform: str, user_id: str, amount: int):
    async with connect_db() as db:
        await execute_query(
            db,
            "UPDATE users SET balance_requests = balance_requests + ? WHERE platform = ? AND user_id = ?",
            (amount, platform, user_id)
        )
        await db.commit()


async def get_user(platform: str, user_id: str):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            "SELECT * FROM users WHERE platform = ? AND user_id = ? LIMIT 1",
            (platform, user_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_referred_by(platform: str, user_id: str, referral_code: str) -> bool:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await begin_write_transaction(db)
        cur = await execute_query(
            db,
            "SELECT referral_code, referred_by FROM users WHERE platform = ? AND user_id = ?",
            (platform, user_id)
        )
        current_user = await cur.fetchone()
        if not current_user:
            await db.commit()
            return False
        if current_user["referred_by"]:
            await db.commit()
            return False
        if current_user["referral_code"] == referral_code:
            await db.commit()
            return False

        ref_cur = await execute_query(
            db,
            "SELECT referral_code FROM users WHERE platform = ? AND referral_code = ?",
            (platform, referral_code)
        )
        ref_owner = await ref_cur.fetchone()
        if not ref_owner:
            await db.commit()
            return False

        update_cur = await execute_query(
            db,
            """
            UPDATE users
            SET referred_by = ?
            WHERE platform = ? AND user_id = ? AND referred_by IS NULL
            """,
            (referral_code, platform, user_id)
        )
        if update_cur.rowcount == 0:
            await db.commit()
            return False

        await db.commit()
        return True


async def get_referral_stats(platform: str, user_id: str):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            "SELECT referral_code FROM users WHERE platform = ? AND user_id = ?",
            (platform, user_id)
        )
        row = await cur.fetchone()
        if not row:
            return {"referral_code": None, "invited_total": 0, "rewarded_total": 0}

        referral_code = row["referral_code"]
        invited_cur = await execute_query(
            db,
            "SELECT COUNT(*) FROM users WHERE platform = ? AND referred_by = ?",
            (platform, referral_code)
        )
        rewarded_cur = await execute_query(
            db,
            "SELECT COUNT(*) FROM users WHERE platform = ? AND referred_by = ? AND referral_reward_granted = 1",
            (platform, referral_code)
        )
        invited_total = (await invited_cur.fetchone())[0]
        rewarded_total = (await rewarded_cur.fetchone())[0]
        return {
            "referral_code": referral_code,
            "invited_total": invited_total,
            "rewarded_total": rewarded_total,
        }


async def list_referred_users(platform: str, referral_code: str, limit: int = 50):
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await execute_query(
            db,
            """
            SELECT platform, user_id, username, tariff, balance_requests,
                   referral_reward_granted, registered_at, last_request_at
            FROM users
            WHERE platform = ? AND referred_by = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (platform, referral_code, limit)
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def apply_referral_bonus_if_eligible(platform: str, user_id: str, bonus_requests: int) -> bool:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        await begin_write_transaction(db)
        cur = await execute_query(
            db,
            "SELECT referred_by, referral_reward_granted FROM users WHERE platform = ? AND user_id = ?",
            (platform, user_id)
        )
        user_row = await cur.fetchone()
        if not user_row:
            await db.commit()
            return False
        if not user_row["referred_by"] or user_row["referral_reward_granted"]:
            await db.commit()
            return False

        ref_cur = await execute_query(
            db,
            "SELECT user_id FROM users WHERE platform = ? AND referral_code = ?",
            (platform, user_row["referred_by"])
        )
        ref_owner = await ref_cur.fetchone()
        if not ref_owner:
            await db.commit()
            return False

        reward_cur = await execute_query(
            db,
            """
            UPDATE users
            SET referral_reward_granted = 1
            WHERE platform = ? AND user_id = ? AND referred_by = ? AND referral_reward_granted = 0
            """,
            (platform, user_id, user_row["referred_by"])
        )
        if reward_cur.rowcount == 0:
            await db.commit()
            return False

        await execute_query(
            db,
            "UPDATE users SET balance_requests = balance_requests + ? WHERE platform = ? AND user_id = ?",
            (bonus_requests, platform, ref_owner["user_id"])
        )
        await db.commit()
        return True
