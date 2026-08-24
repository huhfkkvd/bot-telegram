import aiosqlite
from datetime import datetime
from config import DB_PATH


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                plan_id INTEGER,
                plan_name TEXT,
                price INTEGER,
                receipt_file_id TEXT,
                status TEXT DEFAULT 'pending',
                panel_info TEXT,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                referred_username TEXT,
                converted INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reward_claims (
                referrer_id INTEGER PRIMARY KEY,
                claimed_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS gaming_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_gb INTEGER NOT NULL,
                price INTEGER NOT NULL,
                active INTEGER DEFAULT 1
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS multi_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                price INTEGER NOT NULL,
                active INTEGER DEFAULT 1
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await db.commit()

        # اولین اجرا: اگه جدول‌های تعرفه خالی بودن، از مقادیر پیش‌فرض config.py پر می‌شن
        import config

        cursor = await db.execute("SELECT COUNT(*) FROM gaming_plans")
        row = await cursor.fetchone()
        if row[0] == 0:
            for volume, price in config.DEFAULT_GAMING_PLANS:
                await db.execute(
                    "INSERT INTO gaming_plans (volume_gb, price, active) VALUES (?, ?, 1)", (volume, price)
                )
            await db.commit()

        cursor = await db.execute("SELECT COUNT(*) FROM multi_plans")
        row = await cursor.fetchone()
        if row[0] == 0:
            for label, price in config.DEFAULT_MULTI_PLANS:
                await db.execute(
                    "INSERT INTO multi_plans (label, price, active) VALUES (?, ?, 1)", (label, price)
                )
            await db.commit()


async def create_order(user_id, username, full_name, plan_id, plan_name, price):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO orders (user_id, username, full_name, plan_id, plan_name, price, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'awaiting_receipt', ?)""",
            (user_id, username, full_name, plan_id, plan_name, price, datetime.now().isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def attach_receipt(order_id, file_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET receipt_file_id = ?, status = 'pending' WHERE id = ?",
            (file_id, order_id),
        )
        await db.commit()


async def get_order(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        return await cursor.fetchone()


async def set_order_status(order_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        await db.commit()


async def deliver_order(order_id, panel_info):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET status = 'delivered', panel_info = ? WHERE id = ?",
            (panel_info, order_id),
        )
        await db.commit()


async def get_user_orders(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (user_id,)
        )
        return await cursor.fetchall()


async def get_last_pending_order_without_receipt(user_id, plan_id):
    """آخرین سفارش کاربر برای یک پلن خاص که هنوز رسیدش ثبت نشده"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM orders WHERE user_id = ? AND plan_id = ? AND status = 'awaiting_receipt'
               ORDER BY id DESC LIMIT 1""",
            (user_id, plan_id),
        )
        return await cursor.fetchone()


# ---------- Referral system ----------
async def add_referral(referrer_id: int, referred_id: int, referred_username: str) -> bool:
    """ثبت یک رفرال جدید. اگر کاربر قبلاً رفرال شده باشه، False برمی‌گردونه."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM referrals WHERE referred_id = ?", (referred_id,))
        if await cursor.fetchone():
            return False
        await db.execute(
            """INSERT INTO referrals (referrer_id, referred_id, referred_username, converted, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (referrer_id, referred_id, referred_username, datetime.now().isoformat()),
        )
        await db.commit()
        return True


async def get_referral_by_referred(referred_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM referrals WHERE referred_id = ?", (referred_id,))
        return await cursor.fetchone()


async def mark_referral_converted(referred_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE referrals SET converted = 1 WHERE referred_id = ? AND converted = 0", (referred_id,)
        )
        await db.commit()


async def count_referrals(referrer_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0


async def count_converted_referrals(referrer_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND converted = 1", (referrer_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def has_claimed_reward(referrer_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM reward_claims WHERE referrer_id = ?", (referrer_id,))
        return await cursor.fetchone() is not None


async def set_reward_claimed(referrer_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO reward_claims (referrer_id, claimed_at) VALUES (?, ?)",
            (referrer_id, datetime.now().isoformat()),
        )
        await db.commit()


# ---------- Gaming plans (تعرفه سرویس گیمینگ) ----------
async def get_gaming_plans(active_only: bool = True):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM gaming_plans"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY volume_gb ASC"
        cursor = await db.execute(query)
        return await cursor.fetchall()


async def get_gaming_plan(plan_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM gaming_plans WHERE id = ?", (plan_id,))
        return await cursor.fetchone()


async def get_gaming_plan_by_volume(volume_gb: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM gaming_plans WHERE volume_gb = ? ORDER BY id LIMIT 1", (volume_gb,)
        )
        return await cursor.fetchone()


async def update_gaming_price(plan_id: int, new_price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE gaming_plans SET price = ? WHERE id = ?", (new_price, plan_id))
        await db.commit()


async def toggle_gaming_active(plan_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE gaming_plans SET active = 1 - active WHERE id = ?", (plan_id,))
        await db.commit()


async def add_gaming_plan(volume_gb: int, price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO gaming_plans (volume_gb, price, active) VALUES (?, ?, 1)", (volume_gb, price)
        )
        await db.commit()


# ---------- Multi-location plans (تعرفه سرویس مولتی لوکیشن) ----------
async def get_multi_plans(active_only: bool = True):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM multi_plans"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY id ASC"
        cursor = await db.execute(query)
        return await cursor.fetchall()


async def get_multi_plan(plan_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM multi_plans WHERE id = ?", (plan_id,))
        return await cursor.fetchone()


async def update_multi_price(plan_id: int, new_price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE multi_plans SET price = ? WHERE id = ?", (new_price, plan_id))
        await db.commit()


async def toggle_multi_active(plan_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE multi_plans SET active = 1 - active WHERE id = ?", (plan_id,))
        await db.commit()


async def add_multi_plan(label: str, price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO multi_plans (label, price, active) VALUES (?, ?, 1)", (label, price))
        await db.commit()


# ---------- Settings (تنظیمات پویا - قابل تغییر توسط ادمین از داخل ربات) ----------
async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(value)),
        )
        await db.commit()


async def get_welcome_message():
    """پیام خوش‌آمدگویی سفارشی؛ اگه ادمین تنظیم نکرده باشه None برمی‌گردونه (یعنی از متن پیش‌فرض استفاده بشه)."""
    return await get_setting("welcome_message")


async def set_welcome_message(text: str) -> None:
    await set_setting("welcome_message", text)


async def get_referral_required_count() -> int:
    import config
    val = await get_setting("referral_required_count")
    return int(val) if val is not None else config.REFERRAL_REQUIRED_COUNT


async def get_referral_reward_volume() -> int:
    import config
    val = await get_setting("referral_reward_volume")
    return int(val) if val is not None else config.REFERRAL_REWARD_VOLUME
