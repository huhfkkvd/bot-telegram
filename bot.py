import asyncio
import html
import logging
import os
from time import monotonic

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
    ErrorEvent,
)

import config
import database as db

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

BOT_USERNAME = ""  # در main() پر میشه


# ---------- Rate limit / آنتی‌اسپم ----------
class ThrottlingMiddleware(BaseMiddleware):
    """جلوگیری از اسپم کردن دکمه‌ها یا ثبت پشت‌سرهم سفارش توسط یه کاربر."""

    def __init__(self, rate_limit: float = 0.6):
        self.rate_limit = rate_limit
        self.last_call: dict[int, float] = {}

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None:
            now = monotonic()
            last = self.last_call.get(user.id)
            if last is not None and (now - last) < self.rate_limit:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ لطفاً کمی آروم‌تر بزنید!", show_alert=False)
                return  # این درخواست به‌خاطر اسپم بودن نادیده گرفته میشه
            self.last_call[user.id] = now
            try:
                await db.touch_user(user.id, user.username or "", user.full_name or "")
            except Exception:
                pass  # ثبت کاربر نباید جلوی پردازش اصلی رو بگیره
        return await handler(event, data)


dp.message.outer_middleware(ThrottlingMiddleware(rate_limit=0.7))
dp.callback_query.outer_middleware(ThrottlingMiddleware(rate_limit=0.4))


# ---------- عضویت اجباری در کانال ----------
_membership_cache: dict[int, tuple[bool, float]] = {}
MEMBERSHIP_CACHE_TTL = 60  # ثانیه - برای جلوگیری از فراخوانی زیاد Telegram API روی هر پیام/دکمه


def _force_join_channel_username() -> str:
    """یوزرنیم کانال اجباری رو نرمالایز می‌کنه (با @ در ابتدا)، یا رشته خالی اگه قابلیت غیرفعال باشه."""
    ch = (config.FORCE_JOIN_CHANNEL or "").strip()
    if not ch:
        return ""
    return ch if ch.startswith("@") else f"@{ch}"


async def is_channel_member(user_id: int) -> bool:
    """چک می‌کنه کاربر عضو کانال اجباریه یا نه.
    اگه قابلیت غیرفعال باشه یا خطایی پیش بیاد (مثلاً ربات ادمین کانال نیست)، True برمی‌گردونه
    تا در صورت تنظیم نادرست، کل ربات برای همه کاربرها قفل نشه."""
    channel = _force_join_channel_username()
    if not channel:
        return True

    cached = _membership_cache.get(user_id)
    if cached and (monotonic() - cached[1]) < MEMBERSHIP_CACHE_TTL:
        return cached[0]

    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        is_member = member.status in ("member", "administrator", "creator")
    except Exception as e:
        logging.warning(
            f"Could not check channel membership for {user_id}: {e} "
            f"(مطمئن شوید ربات به عنوان ادمین توی کانال {channel} اضافه شده)"
        )
        is_member = True
    _membership_cache[user_id] = (is_member, monotonic())
    return is_member


def join_channel_kb() -> InlineKeyboardMarkup:
    channel = _force_join_channel_username().lstrip("@")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 عضویت در کانال", url=f"https://t.me/{channel}", style="primary")],
            [InlineKeyboardButton(text="✅ بررسی عضویت", callback_data="checkjoin", style="success")],
        ]
    )


JOIN_REQUIRED_TEXT = (
    "🚫 <b>برای استفاده از ربات، اول باید عضو کانال ما بشید.</b>\n\n"
    "بعد از عضویت روی دکمه «✅ بررسی عضویت» بزنید."
)


class ForceJoinMiddleware(BaseMiddleware):
    """قبل از پردازش هر پیام/دکمه، عضویت کاربر توی کانال اجباری رو چک می‌کنه.
    دستور /start (که خودش این چک رو انجام میده) و دکمه «بررسی عضویت» از این میان‌افزار مستثنی هستن."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or not _force_join_channel_username():
            return await handler(event, data)

        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "checkjoin":
            return await handler(event, data)

        if not await is_channel_member(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("⚠️ ابتدا باید عضو کانال بشید!", show_alert=True)
                try:
                    await event.message.edit_text(
                        JOIN_REQUIRED_TEXT, parse_mode="HTML", reply_markup=join_channel_kb()
                    )
                except Exception:
                    pass
            elif isinstance(event, Message):
                await event.answer(JOIN_REQUIRED_TEXT, parse_mode="HTML", reply_markup=join_channel_kb())
            return  # درخواست همینجا متوقف میشه و به هندلر اصلی نمی‌رسه
        return await handler(event, data)


dp.message.outer_middleware(ForceJoinMiddleware())
dp.callback_query.outer_middleware(ForceJoinMiddleware())


# ---------- States ----------
class BuyStates(StatesGroup):
    waiting_for_receipt = State()
    entering_coupon_code = State()


class WalletStates(StatesGroup):
    entering_topup_amount = State()
    waiting_for_topup_receipt = State()


class AdminStates(StatesGroup):
    waiting_for_panel_info = State()
    waiting_for_reject_reason = State()
    editing_gaming_price = State()
    editing_multi_price = State()
    editing_gaming_volume = State()
    editing_multi_label = State()
    adding_gaming_volume = State()
    adding_gaming_price = State()
    adding_multi_label = State()
    adding_multi_price = State()
    adding_panel_category_name = State()
    editing_panel_category_name = State()
    adding_panel_item_title = State()
    adding_panel_item_price = State()
    editing_panel_item_title = State()
    editing_panel_item_price = State()
    editing_welcome_message = State()
    editing_referral_percent = State()
    editing_rules_text = State()
    adding_coupon_code = State()
    adding_coupon_percent = State()
    adding_coupon_maxuses = State()
    editing_wallet_bonus_threshold = State()
    editing_wallet_bonus_percent = State()
    searching_users = State()
    editing_wallet_balance = State()
    broadcasting_message = State()
    adding_admin_id = State()


# ---------- سطوح دسترسی ادمین‌ها ----------
# owner   = مالک ربات (از ENV، config.ADMIN_IDS) - دسترسی کامل به همه‌چیز
# manager = ادمین سطح ۲ - دسترسی به کاربران + کدهای تخفیف
# support = ادمین سطح ۱ - فقط دسترسی به بخش کاربران
ADMIN_ROLE_LABELS = {
    "owner": "👑 مالک ربات",
    "manager": "🥈 ادمین سطح ۲ (کاربران + کدهای تخفیف)",
    "support": "🥉 ادمین سطح ۱ (فقط کاربران)",
}


async def get_admin_role(user_id: int) -> str | None:
    """نقش ادمین رو برمی‌گردونه: 'owner' / 'manager' / 'support' یا None اگه ادمین نباشه."""
    if user_id in config.ADMIN_IDS:
        return "owner"
    return await db.get_admin_role(user_id)


async def can_manage_coupons(role: str | None) -> bool:
    return role in ("owner", "manager")


async def get_all_admin_ids() -> list[int]:
    """آیدی همه‌ی ادمین‌ها (مالک + ادمین‌های سطح ۱ و ۲) برای اطلاع‌رسانی سفارش‌ها و واریزی‌های جدید."""
    managed = await db.list_admins()
    ids = set(config.ADMIN_IDS)
    ids.update(a["user_id"] for a in managed)
    return list(ids)


# ---------- Keyboards ----------
# رنگ دکمه‌های منوی اصلی (نیازمند Bot API 9.4+ / نسخه به‌روز تلگرام - در کلاینت‌های قدیمی‌تر رنگ پیش‌فرض نمایش داده میشه)
async def main_menu_kb(user_id: int | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text="🛍 خرید سرویس", style="danger"),
            KeyboardButton(text="🖥 سرویس‌های من", style="success"),
        ],
        [
            KeyboardButton(text="💰 کیف پول", style="success"),
            KeyboardButton(text="💬 پشتیبانی", style="primary"),
        ],
        [
            KeyboardButton(text="🤝 دعوت دوستان", style="danger"),
            KeyboardButton(text="📜 قوانین", style="primary"),
        ],
    ]
    if user_id is not None and await get_admin_role(user_id) is not None:
        keyboard.append([KeyboardButton(text="🛠 مدیریت ربات", style="primary")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")]]
    )


def services_kb() -> InlineKeyboardMarkup:
    """منوی اصلیِ «خرید سرویس»: انتخاب بین کانفیگ یا پنل نمایندگی."""
    rows = [
        [InlineKeyboardButton(text="🧩 کانفیگ", callback_data="svc:config", style="primary")],
        [InlineKeyboardButton(text="🖥 خرید پنل نمایندگی", callback_data="svc:panel", style="danger")],
        [InlineKeyboardButton(text="🎁 دریافت اکانت تست رایگان", callback_data="svc:trial", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def config_kb() -> InlineKeyboardMarkup:
    """زیرمنوی «کانفیگ»: گیم / وبگردی."""
    rows = [
        [InlineKeyboardButton(text="🎮 کانفیگ گیم", callback_data="svc:gaming", style="primary")],
        [InlineKeyboardButton(text="🌐 کانفیگ وبگردی (مولتی لوکیشن)", callback_data="svc:multi", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="back:services")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def panel_categories_kb(categories) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=c["name"], callback_data=f"panelcat:{c['id']}", style="danger")]
        for c in categories
    ]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="back:services")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


PANEL_VOLUME_TIERS_GB = [250, 500, 750, 1000, 2000, 3000]


def panel_items_kb(items) -> InlineKeyboardMarkup:
    """لیست گزینه‌های هر پنل - قیمت نمایش داده‌شده نرخ هر گیگه؛ حجم نهایی توی مرحله بعد انتخاب میشه."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{it['title']} - {it['price']:,} تومان/گیگ", callback_data=f"panelitem:{it['id']}", style="danger"
            )
        ]
        for it in items
    ]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="svc:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def panel_volume_kb(item_id: int, price_per_gb: int, category_id: int) -> InlineKeyboardMarkup:
    """لیست حجم‌های ثابت (۲۵۰ تا ۳۰۰۰ گیگ) برای گزینه انتخاب‌شده - قیمت هرکدوم = حجم × نرخ هر گیگ."""
    rows = []
    row = []
    for vol in PANEL_VOLUME_TIERS_GB:
        price = vol * price_per_gb
        row.append(
            InlineKeyboardButton(
                text=f"{vol:,} گیگ - {price:,} تومان", callback_data=f"panelvol:{item_id}:{vol}", style="danger"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"panelcat:{category_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trial_categories_kb(categories) -> InlineKeyboardMarkup:
    """کیبورد انتخاب پنل برای درخواست تست - از همون پنل‌های نمایندگی فعلی."""
    rows = [
        [InlineKeyboardButton(text=c["name"], callback_data=f"trialcat:{c['id']}", style="primary")]
        for c in categories
    ]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="back:services")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trial_items_kb(items) -> InlineKeyboardMarkup:
    """کیبورد انتخاب کانفیگ/گزینه مورد نظر برای تست (بدون نمایش قیمت، چون رایگانه)."""
    rows = [
        [InlineKeyboardButton(text=it["title"], callback_data=f"trialitem:{it['id']}", style="primary")]
        for it in items
    ]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="svc:trial")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gaming_plans_kb(plans) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for p in plans:
        row.append(
            InlineKeyboardButton(
                text=f"{p['volume_gb']} گیگ - {p['price']:,} تومان", callback_data=f"gplan:{p['id']}", style="success"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="back:config")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def multi_plans_kb(plans) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p['label']} - {p['price']:,} تومان", callback_data=f"mplan:{p['id']}", style="success"
            )
        ]
        for p in plans
    ]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="back:config")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_order_summary_kb(order) -> InlineKeyboardMarkup:
    """کیبورد صفحه خلاصه سفارش: ارسال رسید، کد تخفیف، پرداخت با کیف پول (در صورت کافی بودن موجودی) یا بازگشت."""
    plan_name = str(order["plan_name"])
    if plan_name.startswith("🎮"):
        kind = "gaming"
    elif plan_name.startswith("🖥"):
        item = await db.get_panel_item(order["plan_id"])
        kind = f"panelvol-{order['plan_id']}" if item else "panelroot"
    else:
        kind = "multi"
    rows = [[InlineKeyboardButton(text="📤 ارسال رسید", callback_data=f"reqreceipt:{order['id']}", style="success")]]

    if order["coupon_code"]:
        rows.append(
            [
                InlineKeyboardButton(text="🔄 تغییر کد تخفیف", callback_data=f"applycoupon:{order['id']}", style="primary"),
                InlineKeyboardButton(text="🗑 حذف تخفیف", callback_data=f"removecoupon:{order['id']}", style="danger"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="🎟 اعمال کد تخفیف", callback_data=f"applycoupon:{order['id']}", style="primary")])

    if order["price"] and order["price"] > 0:
        balance = await db.get_wallet_balance(order["user_id"])
        if balance >= order["price"]:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"💰 پرداخت با کیف پول ({balance:,} تومان)",
                        callback_data=f"walletpay:{order['id']}",
                        style="success",
                    )
                ]
            )

    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"cancelorder:{order['id']}:{kind}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def waiting_receipt_kb(order_id: int) -> InlineKeyboardMarkup:
    """کیبورد صفحه‌ی در انتظار دریافت رسید: فقط بازگشت به خلاصه سفارش."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"backsummary:{order_id}")]]
    )


def order_summary_text(order) -> str:
    price_block = f"💰 قیمت: {order['price']:,} تومان"
    if order["coupon_code"]:
        price_block = (
            f"💵 قیمت اصلی: {order['original_price']:,} تومان\n"
            f"🎟 کد تخفیف: {order['coupon_code']}\n"
            f"💰 قیمت نهایی: {order['price']:,} تومان"
        )
    return (
        f"🧾 <b>خلاصه سفارش شما</b>\n"
        f"—————————————\n"
        f"📦 {order['plan_name']}\n"
        f"{price_block}\n"
        f"—————————————\n\n"
        f"💳 شماره کارت: <code>{config.CARD_NUMBER}</code>\n"
        f"👤 به نام: {config.CARD_HOLDER}\n\n"
        f"ℹ️ پس از واریز وجه، روی دکمه «📤 ارسال رسید» بزنید و سپس عکس یا فایل رسید رو ارسال کنید."
    )


def admin_decision_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأیید", callback_data=f"approve:{order_id}", style="success"),
                InlineKeyboardButton(text="❌ رد", callback_data=f"reject:{order_id}", style="danger"),
            ]
        ]
    )


# ---------- User handlers ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()

    # ثبت/بروزرسانی کاربر در جدول کاربران (برای بخش «کاربران» در پنل مدیریت)
    await db.touch_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )

    # پردازش لینک دعوت (رفرال) در صورت وجود
    args = command.args
    if args and args.startswith("ref_"):
        try:
            referrer_id = int(args[4:])
        except ValueError:
            referrer_id = None

        if referrer_id and referrer_id != message.from_user.id:
            existing = await db.get_referral_by_referred(message.from_user.id)
            if not existing:
                added = await db.add_referral(referrer_id, message.from_user.id, message.from_user.username or "")
                if added:
                    try:
                        await bot.send_message(referrer_id, "🎉 یک نفر با لینک دعوت شما وارد ربات شد!")
                    except Exception as e:
                        logging.warning(f"Could not notify referrer {referrer_id}: {e}")

    # عضویت اجباری در کانال - قبل از نمایش منوی اصلی چک میشه (لینک رفرال بالا قبلاً پردازش شد)
    if not await is_channel_member(message.from_user.id):
        await message.answer(JOIN_REQUIRED_TEXT, parse_mode="HTML", reply_markup=join_channel_kb())
        return

    custom_welcome = await db.get_welcome_message()
    if custom_welcome:
        text = custom_welcome
    else:
        text = (
            f"✨ <b>{config.BRAND_NAME}</b> ✨\n\n"
            f"👋 به پلتفرم فروش سرویس {config.BRAND_NAME} خوش اومدید\n\n"
            f"🎁 <b>چی دریافت می‌کنید؟</b>\n"
            f"🎮 سرویس گیمینگ با حجم دلخواه\n"
            f"🌍 سرویس مولتی لوکیشن (وبگردی) با پلن نامحدود\n\n"
            f"🟢 سرویس فعال دارید؟ از دکمه «🖥 سرویس‌های من» وارد شوید"
        )
    await message.answer(text, parse_mode="HTML", reply_markup=await main_menu_kb(message.from_user.id))


@dp.callback_query(F.data == "checkjoin")
async def check_join_callback(callback: CallbackQuery, state: FSMContext):
    """کاربر روی «✅ بررسی عضویت» زده - دوباره چک میشه و در صورت عضویت، منوی اصلی نشون داده میشه."""
    _membership_cache.pop(callback.from_user.id, None)  # کش رو پاک می‌کنیم تا وضعیت تازه چک بشه
    if not await is_channel_member(callback.from_user.id):
        await callback.answer("❌ هنوز عضو کانال نشدید! لطفاً اول عضو بشید.", show_alert=True)
        return

    await callback.answer("✅ عضویت شما تأیید شد!", show_alert=True)
    await db.touch_user(callback.from_user.id, callback.from_user.username or "", callback.from_user.full_name or "")

    custom_welcome = await db.get_welcome_message()
    if custom_welcome:
        text = custom_welcome
    else:
        text = (
            f"✨ <b>{config.BRAND_NAME}</b> ✨\n\n"
            f"👋 به پلتفرم فروش سرویس {config.BRAND_NAME} خوش اومدید\n\n"
            f"🎁 <b>چی دریافت می‌کنید؟</b>\n"
            f"🎮 سرویس گیمینگ با حجم دلخواه\n"
            f"🌍 سرویس مولتی لوکیشن (وبگردی) با پلن نامحدود\n\n"
            f"🟢 سرویس فعال دارید؟ از دکمه «🖥 سرویس‌های من» وارد شوید"
        )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(text, parse_mode="HTML", reply_markup=await main_menu_kb(callback.from_user.id))


@dp.message(F.text == "🛍 خرید سرویس")
async def show_services(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🛍 لطفاً نوع سرویس مورد نظر رو انتخاب کنید:\n\n۱- کانفیگ\n۲- خرید پنل نمایندگی",
        reply_markup=services_kb(),
    )


@dp.callback_query(F.data == "back:menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "↩️ به منوی اصلی برگشتید.\nبرای شروع دوباره از دکمه «🛍 خرید سرویس» در پایین صفحه استفاده کنید."
    )
    await callback.answer()


@dp.callback_query(F.data == "back:services")
async def back_to_services(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🛍 لطفاً نوع سرویس مورد نظر رو انتخاب کنید:\n\n۱- کانفیگ\n۲- خرید پنل نمایندگی",
        reply_markup=services_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "svc:config")
async def choose_config_service(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🧩 <b>کانفیگ</b>\nنوع کانفیگ مورد نظر رو انتخاب کنید:", parse_mode="HTML", reply_markup=config_kb())
    await callback.answer()


@dp.callback_query(F.data == "back:config")
async def back_to_config(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🧩 <b>کانفیگ</b>\nنوع کانفیگ مورد نظر رو انتخاب کنید:", parse_mode="HTML", reply_markup=config_kb())
    await callback.answer()


@dp.callback_query(F.data == "svc:panel")
async def choose_panel_service(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    categories = await db.get_panel_categories()
    if not categories:
        await callback.answer("در حال حاضر پنلی برای نمایندگی ثبت نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        "🖥 <b>خرید پنل نمایندگی</b>\nپنل مورد نظر رو انتخاب کنید:", parse_mode="HTML", reply_markup=panel_categories_kb(categories)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("panelcat:"))
async def choose_panel_category(callback: CallbackQuery, state: FSMContext):
    category_id = int(callback.data.split(":")[1])
    category = await db.get_panel_category(category_id)
    items = await db.get_panel_items(category_id)
    if not category or not items:
        await callback.answer("در حال حاضر گزینه‌ای برای این پنل ثبت نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🖥 <b>{category['name']}</b>\nگزینه مورد نظر رو انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=panel_items_kb(items),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("panelitem:"))
async def choose_panel_item(callback: CallbackQuery, state: FSMContext):
    """کاربر یک گزینه از پنل رو انتخاب کرد -> حالا باید حجم (گیگ) مورد نظرش رو انتخاب کنه."""
    item_id = int(callback.data.split(":")[1])
    item = await db.get_panel_item(item_id)
    if not item or not item["active"]:
        await callback.answer("این گزینه دیگر موجود نیست.", show_alert=True)
        return
    category = await db.get_panel_category(item["category_id"])
    cat_name = category["name"] if category else "پنل نمایندگی"

    await state.clear()
    await callback.message.edit_text(
        f"🖥 <b>{cat_name}</b>\n📦 {item['title']}\n\nحجم مورد نظرتون رو انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=panel_volume_kb(item_id, item["price"], item["category_id"]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("panelvol:"))
async def choose_panel_volume(callback: CallbackQuery, state: FSMContext):
    """کاربر حجم مورد نظرش رو انتخاب کرد -> سفارش با قیمت (حجم × نرخ هر گیگ) ثبت میشه."""
    _, item_id_str, volume_str = callback.data.split(":")
    item_id = int(item_id_str)
    volume_gb = int(volume_str)

    item = await db.get_panel_item(item_id)
    if not item or not item["active"]:
        await callback.answer("این گزینه دیگر موجود نیست.", show_alert=True)
        return
    if volume_gb not in PANEL_VOLUME_TIERS_GB:
        await callback.answer("حجم انتخاب‌شده نامعتبره.", show_alert=True)
        return

    category = await db.get_panel_category(item["category_id"])
    cat_name = category["name"] if category else "پنل نمایندگی"
    price = volume_gb * item["price"]
    plan_name = f"🖥 {cat_name} - {item['title']} - {volume_gb:,} گیگ"

    order_id = await db.create_order(
        user_id=callback.from_user.id,
        username=callback.from_user.username or "",
        full_name=callback.from_user.full_name,
        plan_id=item_id,
        plan_name=plan_name,
        price=price,
    )

    await state.clear()
    order = await db.get_order(order_id)
    await callback.message.edit_text(
        order_summary_text(order), parse_mode="HTML", reply_markup=await build_order_summary_kb(order)
    )
    await callback.answer()


@dp.callback_query(F.data == "svc:trial")
async def choose_trial_service(callback: CallbackQuery, state: FSMContext):
    """شروع مسیر «دریافت اکانت تست رایگان» - کاربر پنل و کانفیگ مورد نظرش رو انتخاب می‌کنه."""
    await state.clear()
    if await db.has_requested_trial(callback.from_user.id):
        await callback.answer(
            "شما قبلاً یک اکانت تست دریافت کرده‌اید. هر کاربر فقط یک‌بار می‌تونه تست بگیره.",
            show_alert=True,
        )
        return

    categories = await db.get_panel_categories()
    if not categories:
        await callback.answer("در حال حاضر پنلی برای تست ثبت نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        "🎁 <b>دریافت اکانت تست رایگان</b>\nکدوم پنل رو می‌خواید امتحان کنید؟",
        parse_mode="HTML",
        reply_markup=trial_categories_kb(categories),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("trialcat:"))
async def choose_trial_category(callback: CallbackQuery, state: FSMContext):
    category_id = int(callback.data.split(":")[1])
    category = await db.get_panel_category(category_id)
    items = await db.get_panel_items(category_id)
    if not category or not items:
        await callback.answer("در حال حاضر گزینه‌ای برای این پنل ثبت نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🎁 <b>تست {category['name']}</b>\nکدوم کانفیگ رو می‌خواید تست کنید؟",
        parse_mode="HTML",
        reply_markup=trial_items_kb(items),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("trialitem:"))
async def choose_trial_item(callback: CallbackQuery, state: FSMContext):
    """کاربر کانفیگ مورد نظرش برای تست رو انتخاب کرد -> درخواست مستقیم برای ادمین ارسال میشه."""
    if await db.has_requested_trial(callback.from_user.id):
        await callback.answer(
            "شما قبلاً یک اکانت تست دریافت کرده‌اید. هر کاربر فقط یک‌بار می‌تونه تست بگیره.",
            show_alert=True,
        )
        return

    item_id = int(callback.data.split(":")[1])
    item = await db.get_panel_item(item_id)
    if not item or not item["active"]:
        await callback.answer("این گزینه دیگر موجود نیست.", show_alert=True)
        return
    category = await db.get_panel_category(item["category_id"])
    cat_name = category["name"] if category else "پنل نمایندگی"
    plan_name = f"🎁 تست - {cat_name} - {item['title']}"

    order_id = await db.create_trial_order(
        user_id=callback.from_user.id,
        username=callback.from_user.username or "",
        full_name=callback.from_user.full_name,
        plan_id=item_id,
        plan_name=plan_name,
    )

    await state.clear()
    await callback.message.edit_text(
        "✅ درخواست تست شما ثبت شد و برای ادمین ارسال شد.\n"
        "به محض تأیید، اطلاعات اکانت تست‌تون همینجا ارسال میشه.",
        reply_markup=back_menu_kb(),
    )
    await callback.answer()

    caption = (
        f"🎁 <b>درخواست تست جدید</b> #{order_id}\n"
        f"👤 کاربر: {callback.from_user.full_name} (@{callback.from_user.username or '-'})\n"
        f"🆔 آیدی عددی: {callback.from_user.id}\n"
        f"📦 پنل/کانفیگ درخواستی: {cat_name} - {item['title']}\n\n"
        f"ℹ️ این یک درخواست تست رایگانه (بدون پرداخت)."
    )
    for admin_id in await get_all_admin_ids():
        try:
            await bot.send_message(admin_id, caption, parse_mode="HTML", reply_markup=admin_decision_kb(order_id))
        except Exception as e:
            logging.warning(f"Could not notify admin {admin_id} about trial request: {e}")


@dp.callback_query(F.data.startswith("cancelorder:"))
async def cancel_order_and_go_back(callback: CallbackQuery, state: FSMContext):
    """کاربر از صفحه خلاصه سفارش «بازگشت» رو زده -> سفارش لغو میشه و به لیست تعرفه‌های همون سرویس برمی‌گرده."""
    _, order_id_str, kind = callback.data.split(":", 2)
    order_id = int(order_id_str)
    order = await db.get_order(order_id)
    if order and order["user_id"] == callback.from_user.id and order["status"] in ("awaiting_receipt", "pending"):
        await db.set_order_status(order_id, "cancelled")
    await state.clear()

    if kind == "gaming":
        plans = await db.get_gaming_plans()
        if not plans:
            await callback.answer("در حال حاضر تعرفه‌ای برای این سرویس ثبت نشده.", show_alert=True)
            return
        await callback.message.edit_text(
            "🎮 <b>کانفیگ گیم</b>\nحجم مورد نظر رو انتخاب کنید:", parse_mode="HTML", reply_markup=gaming_plans_kb(plans)
        )
    elif kind == "multi":
        plans = await db.get_multi_plans()
        if not plans:
            await callback.answer("در حال حاضر تعرفه‌ای برای این سرویس ثبت نشده.", show_alert=True)
            return
        await callback.message.edit_text(
            "🌐 <b>کانفیگ وبگردی (مولتی لوکیشن)</b>\nتعرفه مورد نظر رو انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=multi_plans_kb(plans),
        )
    elif kind.startswith("panelvol-"):
        item_id = int(kind.split("-", 1)[1])
        item = await db.get_panel_item(item_id)
        if not item or not item["active"]:
            await callback.answer("این گزینه دیگر موجود نیست.", show_alert=True)
            return
        category = await db.get_panel_category(item["category_id"])
        cat_name = category["name"] if category else "پنل نمایندگی"
        await callback.message.edit_text(
            f"🖥 <b>{cat_name}</b>\n📦 {item['title']}\n\nحجم مورد نظرتون رو انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=panel_volume_kb(item_id, item["price"], item["category_id"]),
        )
    elif kind.startswith("panel-"):
        category_id = int(kind.split("-", 1)[1])
        category = await db.get_panel_category(category_id)
        items = await db.get_panel_items(category_id)
        if not category or not items:
            await callback.answer("در حال حاضر گزینه‌ای برای این پنل ثبت نشده.", show_alert=True)
            return
        await callback.message.edit_text(
            f"🖥 <b>{category['name']}</b>\nگزینه مورد نظر رو انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=panel_items_kb(items),
        )
    else:
        categories = await db.get_panel_categories()
        if not categories:
            await callback.answer("در حال حاضر پنلی برای نمایندگی ثبت نشده.", show_alert=True)
            return
        await callback.message.edit_text(
            "🖥 <b>خرید پنل نمایندگی</b>\nپنل مورد نظر رو انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=panel_categories_kb(categories),
        )
    await callback.answer()


@dp.callback_query(F.data == "svc:gaming")
async def choose_gaming_service(callback: CallbackQuery, state: FSMContext):
    plans = await db.get_gaming_plans()
    if not plans:
        await callback.answer("در حال حاضر تعرفه‌ای برای این سرویس ثبت نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        "🎮 <b>کانفیگ گیم</b>\nحجم مورد نظر رو انتخاب کنید:", parse_mode="HTML", reply_markup=gaming_plans_kb(plans)
    )
    await callback.answer()


@dp.callback_query(F.data == "svc:multi")
async def choose_multi_service(callback: CallbackQuery, state: FSMContext):
    plans = await db.get_multi_plans()
    if not plans:
        await callback.answer("در حال حاضر تعرفه‌ای برای این سرویس ثبت نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        "🌐 <b>کانفیگ وبگردی (مولتی لوکیشن)</b>\nتعرفه مورد نظر رو انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=multi_plans_kb(plans),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("gplan:"))
async def choose_gaming_plan(callback: CallbackQuery, state: FSMContext):
    plan_id = int(callback.data.split(":")[1])
    plan = await db.get_gaming_plan(plan_id)
    if not plan or not plan["active"]:
        await callback.answer("این تعرفه دیگر موجود نیست.", show_alert=True)
        return

    plan_name = f"🎮 سرویس گیمینگ - {plan['volume_gb']} گیگ"

    order_id = await db.create_order(
        user_id=callback.from_user.id,
        username=callback.from_user.username or "",
        full_name=callback.from_user.full_name,
        plan_id=plan_id,
        plan_name=plan_name,
        price=plan["price"],
    )

    await state.clear()
    order = await db.get_order(order_id)
    await callback.message.edit_text(
        order_summary_text(order), parse_mode="HTML", reply_markup=await build_order_summary_kb(order)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("mplan:"))
async def choose_multi_plan(callback: CallbackQuery, state: FSMContext):
    plan_id = int(callback.data.split(":")[1])
    plan = await db.get_multi_plan(plan_id)
    if not plan or not plan["active"]:
        await callback.answer("این تعرفه دیگر موجود نیست.", show_alert=True)
        return

    plan_name = f"🌍 سرویس مولتی لوکیشن - {plan['label']}"

    order_id = await db.create_order(
        user_id=callback.from_user.id,
        username=callback.from_user.username or "",
        full_name=callback.from_user.full_name,
        plan_id=plan_id,
        plan_name=plan_name,
        price=plan["price"],
    )

    await state.clear()
    order = await db.get_order(order_id)
    await callback.message.edit_text(
        order_summary_text(order), parse_mode="HTML", reply_markup=await build_order_summary_kb(order)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("reqreceipt:"))
async def request_receipt(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("این سفارش پیدا نشد.", show_alert=True)
        return
    if order["status"] not in ("awaiting_receipt", "pending"):
        await callback.answer("این سفارش دیگه در وضعیت ارسال رسید نیست.", show_alert=True)
        return

    await state.update_data(order_id=order_id)
    await state.set_state(BuyStates.waiting_for_receipt)

    await callback.message.edit_text(
        "📸 لطفاً عکس یا فایل رسید پرداخت رو همینجا ارسال کنید.",
        reply_markup=waiting_receipt_kb(order_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("backsummary:"))
async def back_to_order_summary(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("این سفارش پیدا نشد.", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text(
        order_summary_text(order), parse_mode="HTML", reply_markup=await build_order_summary_kb(order)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("applycoupon:"))
async def start_apply_coupon(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("این سفارش پیدا نشد.", show_alert=True)
        return
    if order["status"] not in ("awaiting_receipt", "pending"):
        await callback.answer("این سفارش دیگه قابل ویرایش نیست.", show_alert=True)
        return

    await state.update_data(order_id=order_id)
    await state.set_state(BuyStates.entering_coupon_code)
    await callback.message.edit_text(
        "🎟 کد تخفیف رو وارد کنید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"backsummary:{order_id}")]]
        ),
    )
    await callback.answer()


@dp.message(BuyStates.entering_coupon_code)
async def apply_coupon_code(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    order = await db.get_order(order_id) if order_id else None
    if not order:
        await message.answer("مشکلی پیش اومد، لطفاً دوباره از منو پلن رو انتخاب کنید.")
        await state.clear()
        return

    code = (message.text or "").strip().upper()
    coupon = await db.get_coupon(code)

    if not coupon or not coupon["active"]:
        await message.answer("❌ این کد تخفیف معتبر نیست. یه کد دیگه امتحان کنید یا از دکمه بازگشت استفاده کنید.")
        return
    if coupon["max_uses"] is not None and coupon["used_count"] >= coupon["max_uses"]:
        await message.answer("❌ ظرفیت استفاده از این کد تخفیف تموم شده. یه کد دیگه امتحان کنید.")
        return

    new_price = int(order["original_price"] * (100 - coupon["percent"]) / 100)
    await db.apply_coupon_to_order(order_id, code, new_price)
    await db.increment_coupon_usage(code)
    await state.clear()

    order = await db.get_order(order_id)
    await message.answer(
        f"✅ کد تخفیف {code} ({coupon['percent']}٪) با موفقیت اعمال شد!",
    )
    await message.answer(
        order_summary_text(order), parse_mode="HTML", reply_markup=await build_order_summary_kb(order)
    )


@dp.callback_query(F.data.startswith("removecoupon:"))
async def remove_coupon(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("این سفارش پیدا نشد.", show_alert=True)
        return

    await db.remove_coupon_from_order(order_id)
    await state.clear()
    order = await db.get_order(order_id)
    await callback.message.edit_text(
        order_summary_text(order), parse_mode="HTML", reply_markup=await build_order_summary_kb(order)
    )
    await callback.answer("کد تخفیف حذف شد.")


@dp.callback_query(F.data.startswith("walletpay:"))
async def pay_with_wallet(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("این سفارش پیدا نشد.", show_alert=True)
        return
    if order["status"] not in ("awaiting_receipt", "pending"):
        await callback.answer("این سفارش دیگه قابل پرداخت نیست.", show_alert=True)
        return

    ok = await db.deduct_wallet_balance(order["user_id"], order["price"])
    if not ok:
        await callback.answer("موجودی کیف پول شما کافی نیست.", show_alert=True)
        return

    await db.mark_order_paid_by_wallet(order_id)
    await state.clear()

    await callback.message.edit_text(
        "✅ پرداخت با موفقیت از کیف پول انجام شد.\nسفارش شما برای بررسی و تحویل به ادمین ارسال شد.",
        reply_markup=back_menu_kb(),
    )
    await callback.answer()

    caption = (
        f"🆕 سفارش جدید #{order_id} (💰 پرداخت با کیف پول)\n"
        f"👤 کاربر: {order['full_name']} (@{order['username'] or '-'})\n"
        f"🆔 آیدی عددی: {order['user_id']}\n"
        f"📦 پلن: {order['plan_name']}\n"
        f"💰 مبلغ: {order['price']:,} تومان"
    )
    for admin_id in await get_all_admin_ids():
        try:
            await bot.send_message(admin_id, caption, reply_markup=admin_decision_kb(order_id))
        except Exception as e:
            logging.warning(f"Could not notify admin {admin_id}: {e}")


@dp.message(BuyStates.waiting_for_receipt, F.photo | F.document)
async def receive_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        await message.answer("مشکلی پیش اومد، لطفاً دوباره از منو پلن رو انتخاب کنید.")
        await state.clear()
        return

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    await db.attach_receipt(order_id, file_id)
    order = await db.get_order(order_id)

    await message.answer(
        "🕐 رسید شما دریافت شد و برای بررسی به ادمین ارسال شد. "
        "به محض تأیید، اطلاعات سرویس ارسال میشه.",
        reply_markup=await main_menu_kb(message.from_user.id),
    )
    await state.clear()

    caption = (
        f"🆕 سفارش جدید #{order_id}\n"
        f"👤 کاربر: {order['full_name']} (@{order['username'] or '-'})\n"
        f"🆔 آیدی عددی: {order['user_id']}\n"
        f"📦 پلن: {order['plan_name']}\n"
        f"💰 مبلغ: {order['price']:,} تومان"
    )

    for admin_id in await get_all_admin_ids():
        try:
            if message.photo:
                await bot.send_photo(
                    admin_id, photo=file_id, caption=caption,
                    reply_markup=admin_decision_kb(order_id),
                )
            else:
                await bot.send_document(
                    admin_id, document=file_id, caption=caption,
                    reply_markup=admin_decision_kb(order_id),
                )
        except Exception as e:
            logging.warning(f"Could not notify admin {admin_id}: {e}")


@dp.message(BuyStates.waiting_for_receipt)
async def waiting_receipt_wrong_input(message: Message):
    await message.answer("لطفاً عکس یا فایل رسید پرداخت رو ارسال کنید 📸")


ORDER_STATUS_MAP = {
    "awaiting_receipt": "⏳ در انتظار ارسال رسید",
    "pending": "🕐 در حال بررسی",
    "approved": "✅ تأیید شده",
    "rejected": "❌ رد شده",
    "delivered": "📦 تحویل داده شده",
    "cancelled": "🚫 لغو شده",
}

ORDER_STATUS_ICON = {
    "awaiting_receipt": "⏳",
    "pending": "🕐",
    "approved": "✅",
    "rejected": "❌",
    "delivered": "📦",
    "cancelled": "🚫",
}


def my_orders_kb(orders) -> InlineKeyboardMarkup:
    rows = []
    for o in orders:
        icon = ORDER_STATUS_ICON.get(o["status"], "•")
        rows.append(
            [InlineKeyboardButton(text=f"{icon} #{o['id']} - {o['plan_name']}", callback_data=f"vieworder:{o['id']}")]
        )
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_detail_text(order) -> str:
    price_line = "💰 مبلغ: 🎁 رایگان (تست)" if order["order_type"] == "trial" else f"💰 مبلغ: {order['price']:,} تومان"
    text = (
        f"🆔 <b>سفارش #{order['id']}</b>\n"
        f"—————————————\n"
        f"📦 پلن: {order['plan_name']}\n"
        f"{price_line}\n"
        f"📌 وضعیت: {ORDER_STATUS_MAP.get(order['status'], order['status'])}"
    )
    if order["status"] == "delivered" and order["panel_info"]:
        text += f"\n\n🔑 اطلاعات و کانفیگ سرویس:\n{order['panel_info']}"
    return text


@dp.message(F.text == "🖥 سرویس‌های من")
async def my_orders(message: Message, state: FSMContext):
    await state.clear()
    orders = await db.get_user_orders(message.from_user.id)
    if not orders:
        await message.answer("شما هنوز هیچ سفارشی ثبت نکردید.", reply_markup=back_menu_kb())
        return

    await message.answer(
        "🖥 <b>سرویس‌های من</b>\nبرای مشاهده اطلاعات و کانفیگ هر سفارش، روی اون کلیک کنید:",
        parse_mode="HTML",
        reply_markup=my_orders_kb(orders),
    )


@dp.callback_query(F.data.startswith("vieworder:"))
async def view_order_detail(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("این سفارش پیدا نشد.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت به لیست سرویس‌ها", callback_data="myorders:list")]]
    )
    await callback.message.edit_text(order_detail_text(order), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "myorders:list")
async def back_to_my_orders_list(callback: CallbackQuery):
    orders = await db.get_user_orders(callback.from_user.id)
    if not orders:
        await callback.message.edit_text("شما هنوز هیچ سفارشی ثبت نکردید.")
        await callback.answer()
        return
    await callback.message.edit_text(
        "🖥 <b>سرویس‌های من</b>\nبرای مشاهده اطلاعات و کانفیگ هر سفارش، روی اون کلیک کنید:",
        parse_mode="HTML",
        reply_markup=my_orders_kb(orders),
    )
    await callback.answer()


def topup_summary_text(topup) -> str:
    return (
        f"🧾 <b>شارژ کیف پول</b>\n"
        f"—————————————\n"
        f"💰 مبلغ: {topup['amount']:,} تومان\n"
        f"—————————————\n\n"
        f"💳 شماره کارت: <code>{config.CARD_NUMBER}</code>\n"
        f"👤 به نام: {config.CARD_HOLDER}\n\n"
        f"ℹ️ پس از واریز وجه، روی دکمه «📤 ارسال رسید» بزنید و سپس عکس یا فایل رسید رو ارسال کنید."
    )


def topup_summary_kb(topup_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 ارسال رسید", callback_data=f"topupreq:{topup_id}")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"topupcancel:{topup_id}")],
        ]
    )


def topup_waiting_receipt_kb(topup_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"topupback:{topup_id}")]]
    )


def topup_decision_kb(topup_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأیید و شارژ", callback_data=f"wapprove:{topup_id}"),
                InlineKeyboardButton(text="❌ رد", callback_data=f"wreject:{topup_id}"),
            ]
        ]
    )


@dp.message(F.text == "💰 کیف پول")
async def wallet_handler(message: Message, state: FSMContext):
    await state.clear()
    balance = await db.get_wallet_balance(message.from_user.id)
    threshold = await db.get_wallet_bonus_threshold()
    bonus_percent = await db.get_wallet_bonus_percent()
    text = (
        f"💰 <b>کیف پول شما</b>\n\n"
        f"موجودی فعلی: <b>{balance:,} تومان</b>\n\n"
        f"می‌تونید کیف پولتون رو شارژ کنید و در خریدهای بعدی بدون نیاز به ارسال رسید، از همون پرداخت کنید.\n\n"
        f"🎁 شارژهای <b>{threshold:,} تومان</b> به بالا، <b>{bonus_percent}٪ هدیه اضافه</b> می‌گیرن!"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ شارژ کیف پول", callback_data="topupwallet")],
            [InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")],
        ]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "topupwallet")
async def start_topup(callback: CallbackQuery, state: FSMContext):
    threshold = await db.get_wallet_bonus_threshold()
    bonus_percent = await db.get_wallet_bonus_percent()
    await state.set_state(WalletStates.entering_topup_amount)
    await callback.message.edit_text(
        f"💳 مبلغ مورد نظر برای شارژ کیف پول رو به تومان وارد کنید (فقط عدد، مثال: 200000):\n\n"
        f"🎁 نکته: شارژ {threshold:,} تومان به بالا، {bonus_percent}٪ هدیه اضافه می‌گیره!",
        reply_markup=back_menu_kb(),
    )
    await callback.answer()


@dp.message(WalletStates.entering_topup_amount)
async def receive_topup_amount(message: Message, state: FSMContext):
    text = (message.text or "").replace(",", "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("لطفاً فقط عدد بزرگ‌تر از صفر بفرستید (مثال: 200000)")
        return

    amount = int(text)
    topup_id = await db.create_wallet_topup(
        message.from_user.id, message.from_user.username or "", message.from_user.full_name, amount
    )
    await state.clear()
    topup = await db.get_wallet_topup(topup_id)
    await message.answer(topup_summary_text(topup), parse_mode="HTML", reply_markup=topup_summary_kb(topup_id))


@dp.callback_query(F.data.startswith("topupcancel:"))
async def cancel_topup(callback: CallbackQuery, state: FSMContext):
    topup_id = int(callback.data.split(":")[1])
    topup = await db.get_wallet_topup(topup_id)
    if topup and topup["user_id"] == callback.from_user.id and topup["status"] in ("awaiting_receipt", "pending"):
        await db.set_topup_status(topup_id, "cancelled")
    await state.clear()
    await callback.message.edit_text("🚫 درخواست شارژ کیف پول لغو شد.", reply_markup=back_menu_kb())
    await callback.answer()


@dp.callback_query(F.data.startswith("topupreq:"))
async def request_topup_receipt(callback: CallbackQuery, state: FSMContext):
    topup_id = int(callback.data.split(":")[1])
    topup = await db.get_wallet_topup(topup_id)
    if not topup or topup["user_id"] != callback.from_user.id:
        await callback.answer("این درخواست پیدا نشد.", show_alert=True)
        return
    if topup["status"] not in ("awaiting_receipt", "pending"):
        await callback.answer("این درخواست دیگه در وضعیت ارسال رسید نیست.", show_alert=True)
        return

    await state.update_data(topup_id=topup_id)
    await state.set_state(WalletStates.waiting_for_topup_receipt)
    await callback.message.edit_text(
        "📸 لطفاً عکس یا فایل رسید واریزی رو همینجا ارسال کنید.",
        reply_markup=topup_waiting_receipt_kb(topup_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("topupback:"))
async def back_to_topup_summary(callback: CallbackQuery, state: FSMContext):
    topup_id = int(callback.data.split(":")[1])
    topup = await db.get_wallet_topup(topup_id)
    if not topup or topup["user_id"] != callback.from_user.id:
        await callback.answer("این درخواست پیدا نشد.", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text(
        topup_summary_text(topup), parse_mode="HTML", reply_markup=topup_summary_kb(topup_id)
    )
    await callback.answer()


@dp.message(WalletStates.waiting_for_topup_receipt, F.photo | F.document)
async def receive_topup_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    topup_id = data.get("topup_id")
    if not topup_id:
        await message.answer("مشکلی پیش اومد، لطفاً دوباره از «💰 کیف پول» شروع کنید.")
        await state.clear()
        return

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    await db.attach_topup_receipt(topup_id, file_id)
    topup = await db.get_wallet_topup(topup_id)

    await message.answer(
        "🕐 رسید شما دریافت شد و برای بررسی به ادمین ارسال شد. "
        "به محض تأیید، کیف پولتون شارژ میشه.",
        reply_markup=await main_menu_kb(message.from_user.id),
    )
    await state.clear()

    caption = (
        f"💰 درخواست شارژ کیف پول #{topup_id}\n"
        f"👤 کاربر: {topup['full_name']} (@{topup['username'] or '-'})\n"
        f"🆔 آیدی عددی: {topup['user_id']}\n"
        f"💵 مبلغ: {topup['amount']:,} تومان"
    )
    for admin_id in await get_all_admin_ids():
        try:
            if message.photo:
                await bot.send_photo(
                    admin_id, photo=file_id, caption=caption, reply_markup=topup_decision_kb(topup_id)
                )
            else:
                await bot.send_document(
                    admin_id, document=file_id, caption=caption, reply_markup=topup_decision_kb(topup_id)
                )
        except Exception as e:
            logging.warning(f"Could not notify admin {admin_id}: {e}")


@dp.message(WalletStates.waiting_for_topup_receipt)
async def waiting_topup_receipt_wrong_input(message: Message):
    await message.answer("لطفاً عکس یا فایل رسید واریزی رو ارسال کنید 📸")


@dp.callback_query(F.data.startswith("wapprove:"))
async def admin_approve_topup(callback: CallbackQuery):
    if await get_admin_role(callback.from_user.id) is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return

    topup_id = int(callback.data.split(":")[1])
    topup = await db.get_wallet_topup(topup_id)
    if not topup:
        await callback.answer("این درخواست پیدا نشد.", show_alert=True)
        return
    if topup["status"] == "approved":
        await callback.answer("این درخواست قبلاً تأیید شده.", show_alert=True)
        return

    await db.set_topup_status(topup_id, "approved")

    threshold = await db.get_wallet_bonus_threshold()
    bonus_percent = await db.get_wallet_bonus_percent()
    bonus = 0
    if threshold > 0 and bonus_percent > 0 and topup["amount"] >= threshold:
        bonus = int(topup["amount"] * bonus_percent / 100)

    credit_amount = topup["amount"] + bonus
    await db.add_wallet_balance(topup["user_id"], credit_amount)
    new_balance = await db.get_wallet_balance(topup["user_id"])

    bonus_note = f"\n🎁 چون شارژتون {threshold:,} تومان یا بیشتر بود، {bonus:,} تومان هدیه هم گرفتید!" if bonus > 0 else ""

    try:
        await bot.send_message(
            topup["user_id"],
            f"✅ کیف پول شما به مبلغ {topup['amount']:,} تومان شارژ شد.{bonus_note}\n"
            f"💰 موجودی جدید: {new_balance:,} تومان",
        )
    except Exception as e:
        logging.warning(f"Could not notify user about wallet charge: {e}")

    await callback.message.answer(
        f"✅ شارژ کیف پول #{topup_id} تأیید شد و کیف پول کاربر شارژ شد."
        + (f" (شامل {bonus:,} تومان هدیه پلکانی)" if bonus > 0 else "")
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("wreject:"))
async def admin_reject_topup(callback: CallbackQuery):
    if await get_admin_role(callback.from_user.id) is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return

    topup_id = int(callback.data.split(":")[1])
    topup = await db.get_wallet_topup(topup_id)
    if not topup:
        await callback.answer("این درخواست پیدا نشد.", show_alert=True)
        return

    await db.set_topup_status(topup_id, "rejected")

    try:
        await bot.send_message(
            topup["user_id"],
            f"❌ متأسفانه درخواست شارژ کیف پول شما رد شد.\n"
            f"در صورت وجود اشتباه در واریزی، لطفاً با پشتیبانی در ارتباط باشید.",
        )
    except Exception as e:
        logging.warning(f"Could not notify user: {e}")

    await callback.message.answer(f"❌ شارژ کیف پول #{topup_id} رد شد و به کاربر اطلاع داده شد.")
    await callback.answer()


@dp.message(F.text == "💬 پشتیبانی")
async def support_handler(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📞 ارتباط با پشتیبانی", url=f"https://t.me/{config.SUPPORT_USERNAME}")],
            [InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")],
        ]
    )
    await message.answer("💬 برای ارتباط با پشتیبانی روی دکمه زیر بزنید:", reply_markup=kb)


DEFAULT_RULES_TEXT = (
    "📜 <b>قوانین و مقررات استفاده از ربات</b>\n\n"
    "۱️⃣ خرید سرویس از این ربات به معنی پذیرش کامل این قوانینه.\n"
    "۲️⃣ اطلاعات سرویس (کانفیگ/یوزرنیم/پسورد) فقط برای استفاده شخصی شماست؛ اشتراک‌گذاری یا فروش مجدد اون بدون هماهنگی با پشتیبانی مجاز نیست.\n"
    "۳️⃣ بعد از ارسال رسید یا پرداخت با کیف پول، سفارش شما در سریع‌ترین زمان ممکن توسط ادمین بررسی و تحویل داده میشه.\n"
    "۴️⃣ در صورت واریز اشتباه یا مغایرت مبلغ، سفارش ممکنه رد بشه؛ لطفاً از طریق پشتیبانی پیگیری کنید.\n"
    "۵️⃣ وجه واریزی برای سرویس‌های تحویل‌داده‌شده قابل استرداد نیست، مگر در صورت وجود مشکل فنی از سمت ما.\n"
    "۶️⃣ موجودی کیف پول فقط داخل همین ربات و برای خرید سرویس قابل استفاده است و قابل برداشت نقدی نیست.\n"
    "۷️⃣ استفاده از سرویس‌ها برای فعالیت‌های غیرقانونی یا مخرب (هک، اسپم، آزار دیگران و ...) ممنوعه و در صورت مشاهده، سرویس بدون اطلاع قبلی مسدود میشه.\n"
    "۸️⃣ قیمت‌ها و تعرفه‌ها ممکنه بدون اطلاع قبلی تغییر کنن؛ قیمت لحظه ثبت سفارش ملاک نهایی است.\n"
    "۹️⃣ برای هرگونه سؤال یا مشکل، از بخش «💬 پشتیبانی» با ما در ارتباط باشید.\n\n"
    "با تشکر از اعتماد شما 🙏"
)


@dp.message(F.text == "📜 قوانین")
async def rules_handler(message: Message, state: FSMContext):
    await state.clear()
    custom_rules = await db.get_rules_text()
    text = custom_rules if custom_rules else DEFAULT_RULES_TEXT
    await message.answer(text, parse_mode="HTML", reply_markup=back_menu_kb())


@dp.message(F.text == "🤝 دعوت دوستان")
async def invite_handler(message: Message, state: FSMContext):
    await state.clear()
    referrer_id = message.from_user.id
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{referrer_id}"
    total = await db.count_referrals(referrer_id)
    converted = await db.count_converted_referrals(referrer_id)
    commission_percent = await db.get_referral_commission_percent()
    total_earned = await db.get_total_referral_earnings(referrer_id)

    text = (
        f"🤝 <b>دعوت دوستان</b>\n\n"
        f"لینک اختصاصی شما:\n<code>{link}</code>\n\n"
        f"👥 تعداد افراد دعوت‌شده: {total}\n"
        f"✅ تعداد خریدهای موفق زیرمجموعه: {converted}\n"
        f"💰 مجموع پورسانتی دریافتی تا الان: <b>{total_earned:,} تومان</b>\n\n"
        f"🎁 به‌ازای <b>هر</b> خرید موفق دوستانی که با لینک شما وارد بشن، <b>{commission_percent}٪</b> از "
        f"مبلغ خریدشون بلافاصله و به‌صورت نقدی به کیف پول شما اضافه میشه — برای همیشه و بدون محدودیت تعداد دفعات! 💸"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=back_menu_kb())


# ---------- Admin: management panel ----------
def admin_menu_kb(role: str) -> InlineKeyboardMarkup:
    """منوی اصلی مدیریت - دسته‌بندی‌شده و دو-ستونی تا پشت‌سرهم و درهم نباشه."""
    rows = [[InlineKeyboardButton(text="👥 کاربران", callback_data="ausers:page:0", style="primary")]]
    if role in ("owner", "manager"):
        rows[0].append(InlineKeyboardButton(text="🎟 کدهای تخفیف", callback_data="admincoupons", style="primary"))
    if role == "owner":
        rows += [
            [InlineKeyboardButton(text="💰 قیمت‌گذاری و پنل‌ها", callback_data="admincat:pricing", style="danger")],
            [
                InlineKeyboardButton(text="✉️ محتوای ربات", callback_data="admincat:content", style="primary"),
                InlineKeyboardButton(text="🤝 رفرال و تخفیف", callback_data="admincat:referral", style="success"),
            ],
            [
                InlineKeyboardButton(text="📢 پیام همگانی", callback_data="adminbroadcast", style="primary"),
                InlineKeyboardButton(text="👮 مدیریت ادمین‌ها", callback_data="adminmanage", style="danger"),
            ],
            [InlineKeyboardButton(text="📦 بکاپ / ریستور دیتابیس", callback_data="adminbackup", style="success")],
        ]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_pricing_kb() -> InlineKeyboardMarkup:
    """زیرمنوی «قیمت‌گذاری و پنل‌ها»: تعرفه‌ها و پنل‌های نمایندگی."""
    rows = [
        [InlineKeyboardButton(text="🎮 تعرفه‌های کانفیگ گیم", callback_data="admintariff:gaming", style="primary")],
        [InlineKeyboardButton(text="🌐 تعرفه‌های کانفیگ وبگردی", callback_data="admintariff:multi", style="primary")],
        [InlineKeyboardButton(text="🖥 مدیریت پنل‌های نمایندگی", callback_data="adminpanels", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_content_kb() -> InlineKeyboardMarkup:
    """زیرمنوی «محتوای ربات»: پیام خوش‌آمد و قوانین."""
    rows = [
        [InlineKeyboardButton(text="✉️ پیام خوش‌آمدگویی", callback_data="adminwelcome", style="primary")],
        [InlineKeyboardButton(text="📜 ویرایش قوانین", callback_data="adminrules", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_referral_kb() -> InlineKeyboardMarkup:
    """زیرمنوی «رفرال و تخفیف»: تنظیمات رفرال و تخفیف شارژ کیف پول."""
    rows = [
        [InlineKeyboardButton(text="🤝 تنظیمات رفرال", callback_data="adminreferral", style="success")],
        [InlineKeyboardButton(text="💳 تخفیف شارژ کیف پول", callback_data="adminwalletbonus", style="success")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "admincat:pricing")
async def admin_category_pricing(callback: CallbackQuery, state: FSMContext):
    if await get_admin_role(callback.from_user.id) != "owner":
        await callback.answer("شما دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "💰 <b>قیمت‌گذاری و پنل‌ها</b>\nچی رو می‌خواید تنظیم کنید؟", parse_mode="HTML", reply_markup=admin_pricing_kb()
    )
    await callback.answer()


@dp.callback_query(F.data == "admincat:content")
async def admin_category_content(callback: CallbackQuery, state: FSMContext):
    if await get_admin_role(callback.from_user.id) != "owner":
        await callback.answer("شما دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "✉️ <b>محتوای ربات</b>\nچی رو می‌خواید ویرایش کنید؟", parse_mode="HTML", reply_markup=admin_content_kb()
    )
    await callback.answer()


@dp.callback_query(F.data == "admincat:referral")
async def admin_category_referral(callback: CallbackQuery, state: FSMContext):
    if await get_admin_role(callback.from_user.id) != "owner":
        await callback.answer("شما دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🤝 <b>رفرال و تخفیف</b>\nچی رو می‌خواید تنظیم کنید؟", parse_mode="HTML", reply_markup=admin_referral_kb()
    )
    await callback.answer()


async def gaming_admin_list_kb() -> InlineKeyboardMarkup:
    plans = await db.get_gaming_plans(active_only=False)
    rows = []
    for p in plans:
        status = "✅" if p["active"] else "🚫"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {p['volume_gb']} گیگ - {p['price']:,} تومان",
                    callback_data=f"gpriceedit:{p['id']}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="✏️ متن", callback_data=f"gedittext:{p['id']}"),
                InlineKeyboardButton(
                    text="غیرفعال" if p["active"] else "فعال",
                    callback_data=f"gtoggle:{p['id']}",
                ),
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"gdelete:{p['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ افزودن تعرفه جدید", callback_data="gadd")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def multi_admin_list_kb() -> InlineKeyboardMarkup:
    plans = await db.get_multi_plans(active_only=False)
    rows = []
    for p in plans:
        status = "✅" if p["active"] else "🚫"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {p['label']} - {p['price']:,} تومان",
                    callback_data=f"mpriceedit:{p['id']}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="✏️ متن", callback_data=f"medittext:{p['id']}"),
                InlineKeyboardButton(
                    text="غیرفعال" if p["active"] else "فعال",
                    callback_data=f"mtoggle:{p['id']}",
                ),
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"mdelete:{p['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ افزودن تعرفه جدید", callback_data="madd")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def panel_admin_categories_kb() -> InlineKeyboardMarkup:
    categories = await db.get_panel_categories(active_only=False)
    rows = []
    for c in categories:
        status = "✅" if c["active"] else "🚫"
        rows.append([InlineKeyboardButton(text=f"{status} {c['name']}", callback_data=f"pcatopen:{c['id']}")])
    rows.append([InlineKeyboardButton(text="➕ افزودن سرویس/پنل جدید", callback_data="pcatadd")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def panel_admin_category_detail_kb(category_id: int) -> InlineKeyboardMarkup:
    category = await db.get_panel_category(category_id)
    active = bool(category["active"]) if category else True
    rows = [
        [
            InlineKeyboardButton(text="✏️ تغییر نام پنل", callback_data=f"pcatedit:{category_id}"),
            InlineKeyboardButton(text="غیرفعال" if active else "فعال", callback_data=f"pcattoggle:{category_id}"),
        ],
        [InlineKeyboardButton(text="🗑 حذف کل این پنل", callback_data=f"pcatdelete:{category_id}")],
    ]
    items = await db.get_panel_items(category_id, active_only=False)
    for it in items:
        status = "✅" if it["active"] else "🚫"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {it['title']} - {it['price']:,} تومان/گیگ",
                    callback_data=f"pitempriceedit:{it['id']}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="✏️ متن", callback_data=f"pitemedittext:{it['id']}"),
                InlineKeyboardButton(
                    text="غیرفعال" if it["active"] else "فعال",
                    callback_data=f"pitemtoggle:{it['id']}",
                ),
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"pitemdelete:{it['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ افزودن گزینه جدید", callback_data=f"pitemadd:{category_id}")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به لیست پنل‌ها", callback_data="adminpanels")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


ADMIN_ROOT_TEXT = "⚙️ <b>مدیریت ربات</b>\nچی رو می‌خواید تنظیم کنید؟"


@dp.message(Command("admin"))
@dp.message(F.text == "🛠 مدیریت ربات")
async def admin_panel_entry(message: Message, state: FSMContext):
    role = await get_admin_role(message.from_user.id)
    if role is None:
        return
    await state.clear()
    await message.answer(ADMIN_ROOT_TEXT, parse_mode="HTML", reply_markup=admin_menu_kb(role))


# ---------- Admin: پشتیبان‌گیری و بازگردانی دیتابیس (برای جابه‌جایی بین اکانت‌های هاست) ----------
def backup_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 دریافت فایل بکاپ", callback_data="dobackup")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")],
        ]
    )


BACKUP_TEXT = (
    "📦 <b>بکاپ / ریستور دیتابیس</b>\n\n"
    "با «📥 دریافت فایل بکاپ» یک فایل از کل دیتابیس فعلی (کاربران، سفارش‌ها، کیف پول‌ها، تنظیمات) "
    "براتون ارسال میشه.\n\n"
    "برای <b>بازگردانی</b> دیتابیس (مثلاً روی اکانت/هاست جدید): همون فایل بکاپ رو برای همین ربات "
    "بفرستید و روی همون فایل <b>ریپلای</b> کنید و دستور /restore رو بزنید "
    "(یا موقع ارسال فایل، توی کپشن /restore بنویسید).\n\n"
    "⚠️ بعد از ریستور، حتماً ربات رو یک‌بار ری‌استارت/Redeploy کنید."
)


@dp.callback_query(F.data == "adminbackup")
async def admin_backup_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(BACKUP_TEXT, parse_mode="HTML", reply_markup=backup_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "dobackup")
async def admin_do_backup(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await callback.answer()
    if not os.path.exists(config.DB_PATH):
        await callback.message.answer("❌ فایل دیتابیس پیدا نشد. مسیر DB_PATH رو چک کنید.")
        return
    await callback.message.answer_document(
        FSInputFile(config.DB_PATH),
        caption=(
            "📦 این فایل، دیتابیس فعلی رباته.\n\n"
            "برای بازگردانی روی ربات/اکانت جدید: این فایل رو برای اون ربات بفرستید، "
            "روی خود فایل ریپلای کنید و دستور /restore رو بزنید."
        ),
    )


@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    if not os.path.exists(config.DB_PATH):
        await message.answer("❌ فایل دیتابیس پیدا نشد. مسیر DB_PATH رو چک کنید.")
        return
    await message.answer_document(
        FSInputFile(config.DB_PATH),
        caption="📦 فایل دیتابیس فعلی. برای بازگردانی، روی این فایل ریپلای کنید و /restore بزنید.",
    )


@dp.message(Command("restore"))
async def cmd_restore(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return

    doc = None
    if message.document:
        doc = message.document
    elif message.reply_to_message and message.reply_to_message.document:
        doc = message.reply_to_message.document

    if not doc:
        await message.answer(
            "⚠️ یک فایل دیتابیس (.db) رو همراه دستور /restore بفرستید، "
            "یا روی فایلی که قبلاً فرستادید ریپلای کنید و /restore رو بزنید."
        )
        return

    await message.answer("⏳ در حال بازگردانی دیتابیس...")

    file_info = await bot.get_file(doc.file_id)

    # از دیتابیس فعلی (در صورت وجود) یک نسخه پشتیبان محلی می‌گیریم
    if os.path.exists(config.DB_PATH):
        os.replace(config.DB_PATH, config.DB_PATH + ".bak")

    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    await bot.download_file(file_info.file_path, destination=config.DB_PATH)

    await message.answer(
        "✅ دیتابیس با موفقیت جایگزین شد.\n\n"
        "⚠️ حالا حتماً ربات رو یک‌بار ری‌استارت (Redeploy در Railway) کنید تا اتصال به دیتابیس "
        "جدید از نو برقرار بشه؛ در غیر این صورت ربات همچنان با اتصال قبلی کار می‌کنه."
    )


@dp.callback_query(F.data == "admintariff:root")
async def admintariff_root(callback: CallbackQuery, state: FSMContext):
    role = await get_admin_role(callback.from_user.id)
    if role is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(ADMIN_ROOT_TEXT, parse_mode="HTML", reply_markup=admin_menu_kb(role))
    await callback.answer()


back_to_admin_root_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")]]
)


@dp.callback_query(F.data == "adminwelcome")
async def admin_edit_welcome(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    current = await db.get_welcome_message()
    current_display = current if current else (
        f"(پیش‌فرض) ✨ {config.BRAND_NAME} ✨\n👋 به پلتفرم فروش سرویس {config.BRAND_NAME} خوش اومدید ..."
    )
    await state.set_state(AdminStates.editing_welcome_message)
    await callback.message.edit_text(
        f"✉️ <b>پیام خوش‌آمدگویی فعلی:</b>\n\n{current_display}\n\n"
        f"—————————————\n"
        f"متن جدید رو بفرستید (تگ‌های ساده HTML مثل &lt;b&gt; پشتیبانی میشه):",
        parse_mode="HTML",
        reply_markup=back_to_admin_root_kb,
    )
    await callback.answer()


@dp.message(AdminStates.editing_welcome_message)
async def save_welcome_message(message: Message, state: FSMContext):
    text = message.text or message.caption
    if not text:
        await message.answer("لطفاً یه پیام متنی معتبر بفرستید.")
        return
    await db.set_welcome_message(text)
    await state.clear()
    await message.answer("✅ پیام خوش‌آمدگویی با موفقیت بروزرسانی شد.", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adminrules")
async def admin_edit_rules(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    current = await db.get_rules_text()
    current_display = current if current else f"(پیش‌فرض)\n\n{DEFAULT_RULES_TEXT}"
    await state.set_state(AdminStates.editing_rules_text)
    await callback.message.edit_text(
        f"📜 <b>قوانین فعلی:</b>\n\n{current_display}\n\n"
        f"—————————————\n"
        f"متن جدید قوانین رو بفرستید (تگ‌های ساده HTML مثل &lt;b&gt; پشتیبانی میشه):",
        parse_mode="HTML",
        reply_markup=back_to_admin_root_kb,
    )
    await callback.answer()


@dp.message(AdminStates.editing_rules_text)
async def save_rules_text(message: Message, state: FSMContext):
    text = message.text or message.caption
    if not text:
        await message.answer("لطفاً یه پیام متنی معتبر بفرستید.")
        return
    await db.set_rules_text(text)
    await state.clear()
    await message.answer("✅ قوانین با موفقیت بروزرسانی شد.", reply_markup=admin_menu_kb())


def referral_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ تغییر درصد پورسانتی", callback_data="editrefpercent")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")],
        ]
    )


@dp.callback_query(F.data == "adminreferral")
async def admin_referral_settings(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.clear()
    percent = await db.get_referral_commission_percent()
    await callback.message.edit_text(
        f"🤝 <b>تنظیمات رفرال (پورسانتی دائمی)</b>\n\n"
        f"💸 درصد پورسانتی فعلی: <b>{percent}٪</b>\n\n"
        f"به‌ازای هر خرید موفق (تحویل‌شده) هر کاربری که با لینک یه نفر وارد ربات شده، همین درصد از مبلغ خرید بلافاصله و به‌صورت نقدی به کیف پول دعوت‌کننده اضافه میشه — برای همیشه و بدون محدودیت تعداد دفعات.",
        parse_mode="HTML",
        reply_markup=referral_settings_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "editrefpercent")
async def start_edit_referral_percent(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.editing_referral_percent)
    await callback.message.edit_text(
        "درصد پورسانتی رفرال رو وارد کنید (عدد بین ۱ تا ۱۰۰، مثال: 10):",
        reply_markup=back_to_admin_root_kb,
    )
    await callback.answer()


@dp.message(AdminStates.editing_referral_percent)
async def save_referral_percent(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or not (1 <= int(text) <= 100):
        await message.answer("لطفاً یه عدد صحیح بین ۱ تا ۱۰۰ بفرستید (مثال: 10)")
        return
    await db.set_setting("referral_commission_percent", int(text))
    await state.clear()
    await message.answer("✅ درصد پورسانتی رفرال با موفقیت بروزرسانی شد.", reply_markup=referral_settings_kb())


# ---------- Admin: کد تخفیف ----------
async def coupons_admin_kb() -> InlineKeyboardMarkup:
    coupons = await db.list_coupons()
    rows = []
    for c in coupons:
        status = "✅" if c["active"] else "🚫"
        usage = f"{c['used_count']}/{c['max_uses']}" if c["max_uses"] is not None else f"{c['used_count']}/∞"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {c['code']} - {c['percent']}٪ ({usage})",
                    callback_data=f"coupontoggle:{c['code']}",
                ),
                InlineKeyboardButton(text="🗑", callback_data=f"coupondelete:{c['code']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ ساخت کد تخفیف جدید", callback_data="coupadd")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "admincoupons")
async def admin_coupons_root(callback: CallbackQuery, state: FSMContext):
    role = await get_admin_role(callback.from_user.id)
    if not await can_manage_coupons(role):
        await callback.answer("شما دسترسی به این بخش رو ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🎟 <b>کدهای تخفیف</b>\nروی هر کد بزنید تا فعال/غیرفعال بشه، یا با 🗑 حذفش کنید.",
        parse_mode="HTML",
        reply_markup=await coupons_admin_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("coupontoggle:"))
async def toggle_coupon(callback: CallbackQuery):
    role = await get_admin_role(callback.from_user.id)
    if not await can_manage_coupons(role):
        await callback.answer("شما دسترسی به این بخش رو ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1]
    await db.toggle_coupon_active(code)
    await callback.message.edit_text(
        "🎟 <b>کدهای تخفیف</b>\nروی هر کد بزنید تا فعال/غیرفعال بشه، یا با 🗑 حذفش کنید.",
        parse_mode="HTML",
        reply_markup=await coupons_admin_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("coupondelete:"))
async def delete_coupon_handler(callback: CallbackQuery):
    role = await get_admin_role(callback.from_user.id)
    if not await can_manage_coupons(role):
        await callback.answer("شما دسترسی به این بخش رو ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1]
    await db.delete_coupon(code)
    await callback.message.edit_text(
        "🎟 <b>کدهای تخفیف</b>\nروی هر کد بزنید تا فعال/غیرفعال بشه، یا با 🗑 حذفش کنید.",
        parse_mode="HTML",
        reply_markup=await coupons_admin_kb(),
    )
    await callback.answer("کد تخفیف حذف شد.")


@dp.callback_query(F.data == "coupadd")
async def start_add_coupon(callback: CallbackQuery, state: FSMContext):
    role = await get_admin_role(callback.from_user.id)
    if not await can_manage_coupons(role):
        await callback.answer("شما دسترسی به این بخش رو ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.adding_coupon_code)
    await callback.message.edit_text(
        "کد تخفیف رو وارد کنید (فقط حروف انگلیسی و عدد، بدون فاصله - مثال: SUMMER20):",
        reply_markup=back_to_admin_root_kb,
    )
    await callback.answer()


@dp.message(AdminStates.adding_coupon_code)
async def add_coupon_step_code(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    if not code.isalnum():
        await message.answer("کد باید فقط شامل حروف انگلیسی و عدد باشه، بدون فاصله یا کاراکتر خاص. دوباره امتحان کنید:")
        return
    existing = await db.get_coupon(code)
    if existing:
        await message.answer("این کد قبلاً ثبت شده. یه کد دیگه انتخاب کنید:")
        return
    await state.update_data(coupon_code=code)
    await state.set_state(AdminStates.adding_coupon_percent)
    await message.answer("چند درصد تخفیف بده؟ (عدد بین ۱ تا ۱۰۰):")


@dp.message(AdminStates.adding_coupon_percent)
async def add_coupon_step_percent(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or not (1 <= int(text) <= 100):
        await message.answer("لطفاً یه عدد صحیح بین ۱ تا ۱۰۰ بفرستید:")
        return
    await state.update_data(coupon_percent=int(text))
    await state.set_state(AdminStates.adding_coupon_maxuses)
    await message.answer("حداکثر تعداد استفاده از این کد چقدر باشه؟ (برای نامحدود، عدد 0 رو بفرستید):")


@dp.message(AdminStates.adding_coupon_maxuses)
async def add_coupon_step_maxuses(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید (برای نامحدود، 0 رو بفرستید):")
        return
    max_uses = int(text) if int(text) > 0 else None

    data = await state.get_data()
    code = data.get("coupon_code")
    percent = data.get("coupon_percent")
    await db.create_coupon(code, percent, max_uses)
    await state.clear()

    usage_text = f"{max_uses} بار" if max_uses else "نامحدود"
    await message.answer(
        f"✅ کد تخفیف <b>{code}</b> با {percent}٪ تخفیف و ظرفیت {usage_text} ساخته شد.",
        parse_mode="HTML",
        reply_markup=await coupons_admin_kb(),
    )


# ---------- Admin: تخفیف پلکانی شارژ کیف پول ----------
def wallet_bonus_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ تغییر آستانه مبلغ", callback_data="editwalletthreshold")],
            [InlineKeyboardButton(text="✏️ تغییر درصد هدیه", callback_data="editwalletbonuspercent")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")],
        ]
    )


@dp.callback_query(F.data == "adminwalletbonus")
async def admin_wallet_bonus_settings(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.clear()
    threshold = await db.get_wallet_bonus_threshold()
    percent = await db.get_wallet_bonus_percent()
    await callback.message.edit_text(
        f"💳 <b>تخفیف پلکانی شارژ کیف پول</b>\n\n"
        f"📊 آستانه فعلی: <b>{threshold:,} تومان</b>\n"
        f"🎁 درصد هدیه: <b>{percent}٪</b>\n\n"
        f"یعنی وقتی کاربری {threshold:,} تومان یا بیشتر شارژ کنه، {percent}٪ هدیه اضافه هم به کیف پولش اضافه میشه.",
        parse_mode="HTML",
        reply_markup=wallet_bonus_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "editwalletthreshold")
async def start_edit_wallet_threshold(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.editing_wallet_bonus_threshold)
    await callback.message.edit_text(
        "حداقل مبلغ شارژ برای دریافت هدیه رو به تومان وارد کنید (مثال: 500000):",
        reply_markup=back_to_admin_root_kb,
    )
    await callback.answer()


@dp.message(AdminStates.editing_wallet_bonus_threshold)
async def save_wallet_threshold(message: Message, state: FSMContext):
    text = (message.text or "").replace(",", "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("لطفاً یه عدد صحیح و بزرگ‌تر از صفر بفرستید (مثال: 500000)")
        return
    await db.set_setting("wallet_bonus_threshold", int(text))
    await state.clear()
    await message.answer("✅ آستانه مبلغ با موفقیت بروزرسانی شد.", reply_markup=wallet_bonus_kb())


@dp.callback_query(F.data == "editwalletbonuspercent")
async def start_edit_wallet_bonus_percent(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.editing_wallet_bonus_percent)
    await callback.message.edit_text(
        "درصد هدیه رو وارد کنید (عدد بین ۱ تا ۱۰۰، مثال: 5):",
        reply_markup=back_to_admin_root_kb,
    )
    await callback.answer()


@dp.message(AdminStates.editing_wallet_bonus_percent)
async def save_wallet_bonus_percent(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or not (1 <= int(text) <= 100):
        await message.answer("لطفاً یه عدد صحیح بین ۱ تا ۱۰۰ بفرستید (مثال: 5)")
        return
    await db.set_setting("wallet_bonus_percent", int(text))
    await state.clear()
    await message.answer("✅ درصد هدیه با موفقیت بروزرسانی شد.", reply_markup=wallet_bonus_kb())


# ---------- Admin: ارسال پیام همگانی (اطلاعیه/تبلیغ به سبک کانال) ----------
@dp.callback_query(F.data == "adminbroadcast")
async def admin_broadcast_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.broadcasting_message)
    await callback.message.edit_text(
        "📢 <b>ارسال پیام همگانی</b>\n\n"
        "هر نوع پیامی که می‌خواید (متن، عکس، ویدیو، فایل، صوت و...) رو همینجا بفرستید؛ "
        "دقیقاً همون‌طور که فرستادید (بدون تگ Forwarded، مثل کانال) برای همه‌ی کاربرانی که "
        "ربات رو استارت کردن ارسال میشه.\n\n"
        "⚠️ فقط یه پیام تکی بفرستید (نه آلبوم چند عکسی).",
        parse_mode="HTML",
        reply_markup=back_to_admin_root_kb,
    )
    await callback.answer()


@dp.message(AdminStates.broadcasting_message)
async def admin_broadcast_receive(message: Message, state: FSMContext):
    await state.update_data(broadcast_chat_id=message.chat.id, broadcast_message_id=message.message_id)
    await state.set_state(None)
    total = await db.count_users()
    await message.answer(
        f"👆 پیام بالا دقیقاً همینطوری برای <b>{total:,}</b> کاربر ربات ارسال میشه.\n\nمطمئنید؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ ارسال به همه ({total:,} نفر)", callback_data="broadcastsend")],
                [InlineKeyboardButton(text="❌ انصراف", callback_data="admintariff:root")],
            ]
        ),
    )


@dp.callback_query(F.data == "broadcastsend")
async def admin_broadcast_send(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return

    data = await state.get_data()
    src_chat_id = data.get("broadcast_chat_id")
    src_message_id = data.get("broadcast_message_id")
    if not src_chat_id or not src_message_id:
        await callback.answer("پیامی برای ارسال پیدا نشد. دوباره از منوی «📢 ارسال پیام همگانی» شروع کنید.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text("⏳ در حال ارسال پیام برای همه‌ی کاربران... این ممکنه چند دقیقه طول بکشه.")

    user_ids = await db.get_all_user_ids()
    sent = 0
    failed = 0

    for i, uid in enumerate(user_ids, start=1):
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_message_id)
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_message_id)
                sent += 1
            except Exception:
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1  # کاربر ربات رو بلاک کرده یا چت باهاش وجود نداره
        except Exception:
            failed += 1

        await asyncio.sleep(0.05)  # جلوگیری از برخورد با محدودیت نرخ ارسال تلگرام

        if i % 50 == 0:
            try:
                await callback.message.edit_text(
                    f"⏳ در حال ارسال... {i:,}/{len(user_ids):,}\n📤 موفق: {sent:,} | 🚫 ناموفق: {failed:,}"
                )
            except Exception:
                pass

    await callback.message.answer(
        f"✅ <b>ارسال پیام همگانی تموم شد.</b>\n\n"
        f"📤 ارسال موفق: <b>{sent:,}</b>\n"
        f"🚫 ناموفق (بلاک کرده یا ربات رو حذف کرده): <b>{failed:,}</b>",
        parse_mode="HTML",
        reply_markup=back_to_admin_root_kb,
    )


# ---------- Admin: مدیریت ادمین‌ها (فقط مالک ربات) ----------
async def build_admins_list_text_kb():
    managed = await db.list_admins()
    if not managed:
        text = "👮 <b>مدیریت ادمین‌ها</b>\nهنوز هیچ ادمین مدیریت‌شده‌ای اضافه نکردید."
    else:
        lines = ["👮 <b>مدیریت ادمین‌ها</b>\n"]
        for a in managed:
            label = f"@{a['username']}" if a["username"] else (a["full_name"] or "بدون‌نام")
            role_label = ADMIN_ROLE_LABELS.get(a["role"], a["role"])
            lines.append(f"• {label} | <code>{a['user_id']}</code> — {role_label}")
        text = "\n".join(lines)

    rows = []
    for a in managed:
        label = f"@{a['username']}" if a["username"] else (a["full_name"] or str(a["user_id"]))
        rows.append(
            [InlineKeyboardButton(text=f"🗑 حذف {label}", callback_data=f"adminremove:{a['user_id']}")]
        )
    rows.append([InlineKeyboardButton(text="➕ افزودن ادمین جدید", callback_data="adminaddadmin")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adminmanage")
async def admin_manage_root(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("این بخش فقط برای مالک ربات در دسترسه.", show_alert=True)
        return
    await state.clear()
    text, kb = await build_admins_list_text_kb()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "adminaddadmin")
async def admin_add_admin_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("این بخش فقط برای مالک ربات در دسترسه.", show_alert=True)
        return
    await state.set_state(AdminStates.adding_admin_id)
    await callback.message.edit_text(
        "🆔 آیدی عددی کاربری که می‌خواید ادمین کنید رو بفرستید.\n"
        "(کاربر باید حداقل یه‌بار ربات رو استارت کرده باشه.)",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 انصراف", callback_data="adminmanage")]]
        ),
    )
    await callback.answer()


@dp.message(AdminStates.adding_admin_id)
async def admin_add_admin_receive_id(message: Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_IDS:
        await state.set_state(None)
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط آیدی عددی کاربر رو بفرستید:")
        return
    user_id = int(text)

    if user_id in config.ADMIN_IDS:
        await message.answer("این کاربر از قبل مالک رباته و نیازی به این کار نیست.")
        return

    existing_role = await db.get_admin_role(user_id)
    user = await db.get_user(user_id)
    if not user:
        await message.answer(
            "⚠️ این کاربر هیچ‌وقت ربات رو استارت نکرده، پس نمی‌تونم مشخصاتش رو نشون بدم. "
            "اگه مطمئنید آیدی درسته، بازم می‌تونید ادامه بدید."
        )

    await state.update_data(new_admin_user_id=user_id)
    await state.set_state(None)

    note = f"\n(در حال حاضر: {ADMIN_ROLE_LABELS.get(existing_role, existing_role)})" if existing_role else ""
    await message.answer(
        f"سطح دسترسی این ادمین رو انتخاب کنید:{note}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🥉 سطح ۱ - فقط کاربران", callback_data=f"adminsetrole:{user_id}:support")],
                [
                    InlineKeyboardButton(
                        text="🥈 سطح ۲ - کاربران + کدهای تخفیف", callback_data=f"adminsetrole:{user_id}:manager"
                    )
                ],
                [InlineKeyboardButton(text="🔙 انصراف", callback_data="adminmanage")],
            ]
        ),
    )


@dp.callback_query(F.data.startswith("adminsetrole:"))
async def admin_set_role(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("این بخش فقط برای مالک ربات در دسترسه.", show_alert=True)
        return
    _, user_id_str, role = callback.data.split(":", 2)
    user_id = int(user_id_str)
    if role not in ("support", "manager"):
        await callback.answer("سطح نامعتبر.", show_alert=True)
        return

    await db.add_admin(user_id, role, added_by=callback.from_user.id)

    role_label = ADMIN_ROLE_LABELS.get(role, role)
    try:
        await bot.send_message(
            user_id,
            f"🎉 شما به عنوان «{role_label}» به تیم مدیریت ربات اضافه شدید.\n"
            f"برای ورود به پنل مدیریت، از منو روی «🛠 مدیریت ربات» بزنید.",
        )
    except Exception as e:
        logging.warning(f"Could not notify new admin {user_id}: {e}")

    text, kb = await build_admins_list_text_kb()
    await callback.message.edit_text(
        f"✅ کاربر <code>{user_id}</code> با نقش «{role_label}» اضافه شد.\n\n" + text,
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adminremove:"))
async def admin_remove_admin(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("این بخش فقط برای مالک ربات در دسترسه.", show_alert=True)
        return
    user_id = int(callback.data.split(":", 1)[1])
    await db.remove_admin(user_id)
    try:
        await bot.send_message(user_id, "🚫 دسترسی ادمین شما به ربات لغو شد.")
    except Exception:
        pass
    text, kb = await build_admins_list_text_kb()
    await callback.message.edit_text(f"🗑 ادمین حذف شد.\n\n" + text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ---------- Admin: بخش کاربران (فقط ادمین) ----------
USERS_PAGE_SIZE = 8


def user_row_label(u) -> str:
    name = f"@{u['username']}" if u["username"] else (u["full_name"] or "بدون‌نام")
    return f"{name} | {u['user_id']}"


async def build_users_page(page: int):
    total = await db.count_users()
    users = await db.list_users(limit=USERS_PAGE_SIZE, offset=page * USERS_PAGE_SIZE)

    if total == 0:
        text = "👥 <b>کاربران ربات</b>\nهنوز هیچ کاربری ربات رو استارت نکرده."
    else:
        text = (
            f"👥 <b>کاربران ربات</b>\n"
            f"تعداد کل: <b>{total:,}</b> نفر\n\n"
            f"روی هر کاربر بزنید تا مشخصاتش رو ببینید:"
        )

    rows = [
        [InlineKeyboardButton(text=user_row_label(u), callback_data=f"auser:{u['user_id']}:p{page}")]
        for u in users
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"ausers:page:{page - 1}"))
    if (page + 1) * USERS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"ausers:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔎 جست‌وجوی کاربر", callback_data="ausersearch")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def build_user_search_results(query: str):
    users = await db.search_users(query, limit=30)
    rows = [
        [InlineKeyboardButton(text=user_row_label(u), callback_data=f"auser:{u['user_id']}:s")]
        for u in users
    ]
    rows.append([InlineKeyboardButton(text="🔎 جست‌وجوی دوباره", callback_data="ausersearch")])
    rows.append([InlineKeyboardButton(text="📋 نمایش همه کاربران", callback_data="ausers:page:0")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")])

    safe_query = html.escape(query)
    if users:
        text = f"🔎 <b>نتایج جست‌وجو برای:</b> <code>{safe_query}</code>\nتعداد نتایج: {len(users)}"
    else:
        text = f"🔎 <b>نتایج جست‌وجو برای:</b> <code>{safe_query}</code>\nهیچ کاربری پیدا نشد."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _row_get(row, key: str, default=None):
    """دسترسی امن به یه ستون از sqlite Row؛ اگه ستون وجود نداشته باشه (دیتابیس قدیمی) خطا نمی‌ده."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def user_detail_text(user, wallet_balance: int, orders_count: int) -> str:
    username_val = _row_get(user, "username")
    full_name_val = _row_get(user, "full_name")
    username = f"@{html.escape(username_val)}" if username_val else "—"
    full_name = html.escape(full_name_val) if full_name_val else "—"
    joined = (_row_get(user, "joined_at") or "")[:19].replace("T", " ") or "—"
    last_seen = (_row_get(user, "last_seen") or "")[:19].replace("T", " ") or "—"
    return (
        f"👤 <b>مشخصات کاربر</b>\n"
        f"—————————————\n"
        f"🆔 آیدی عددی: <code>{user['user_id']}</code>\n"
        f"🔖 یوزرنیم: {username}\n"
        f"📝 نام: {full_name}\n"
        f"💰 موجودی کیف پول: <b>{wallet_balance:,} تومان</b>\n"
        f"📦 تعداد سفارش‌ها: <b>{orders_count}</b>\n"
        f"🗓 اولین ورود: {joined}\n"
        f"🕒 آخرین فعالیت: {last_seen}"
    )


def user_detail_kb(user_id: int, ctx: str, role: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🧾 سفارش‌های این کاربر", callback_data=f"auserorders:{user_id}:{ctx}")]]
    if role == "owner":
        rows.append(
            [InlineKeyboardButton(text="✏️ ویرایش موجودی کیف پول", callback_data=f"auserwallet:{user_id}:{ctx}")]
        )
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data=f"auserback:{ctx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_orders_kb(orders, user_id: int, ctx: str) -> InlineKeyboardMarkup:
    rows = []
    for o in orders:
        icon = ORDER_STATUS_ICON.get(o["status"], "•")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} #{o['id']} - {o['plan_name']} - {o['price']:,} تومان",
                    callback_data=f"auserorder:{o['id']}:{user_id}:{ctx}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به مشخصات کاربر", callback_data=f"auser:{user_id}:{ctx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("ausers:page:"))
async def admin_users_page(callback: CallbackQuery, state: FSMContext):
    if await get_admin_role(callback.from_user.id) is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    page = int(callback.data.split(":")[2])
    text, kb = await build_users_page(page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "ausersearch")
async def admin_users_search_prompt(callback: CallbackQuery, state: FSMContext):
    if await get_admin_role(callback.from_user.id) is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.searching_users)
    await callback.message.edit_text(
        "🔎 آیدی عددی یا بخشی از یوزرنیم کاربر مورد نظر رو ارسال کنید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="ausers:page:0")]]
        ),
    )
    await callback.answer()


@dp.message(AdminStates.searching_users)
async def admin_users_search_result(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if not query:
        await message.answer("لطفاً یه آیدی عددی یا بخشی از یوزرنیم بفرستید:")
        return
    await state.update_data(last_user_search=query)
    await state.set_state(None)  # استیت رو خالی می‌کنیم ولی last_user_search رو نگه می‌داریم
    text, kb = await build_user_search_results(query)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("auserback:"))
async def admin_users_back(callback: CallbackQuery, state: FSMContext):
    if await get_admin_role(callback.from_user.id) is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    ctx = callback.data.split(":", 1)[1]
    if ctx == "s":
        data = await state.get_data()
        query = data.get("last_user_search")
        if query:
            text, kb = await build_user_search_results(query)
        else:
            text, kb = await build_users_page(0)
    else:
        page = int(ctx[1:]) if ctx.startswith("p") and ctx[1:].isdigit() else 0
        text, kb = await build_users_page(page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("auser:"))
async def admin_view_user(callback: CallbackQuery, state: FSMContext):
    role = await get_admin_role(callback.from_user.id)
    if role is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    _, user_id_str, ctx = callback.data.split(":", 2)
    user_id = int(user_id_str)
    user = await db.get_user(user_id)
    if not user:
        await callback.answer("این کاربر پیدا نشد.", show_alert=True)
        return
    wallet_balance = await db.get_wallet_balance(user_id)
    orders = await db.get_user_orders(user_id)
    text = user_detail_text(user, wallet_balance, len(orders))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=user_detail_kb(user_id, ctx, role))
    await callback.answer()


@dp.callback_query(F.data.startswith("auserorders:"))
async def admin_user_orders(callback: CallbackQuery):
    if await get_admin_role(callback.from_user.id) is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    _, user_id_str, ctx = callback.data.split(":", 2)
    user_id = int(user_id_str)
    orders = await db.get_user_orders(user_id)
    if not orders:
        await callback.answer("این کاربر هنوز هیچ سفارشی ثبت نکرده.", show_alert=True)
        return
    await callback.message.edit_text(
        "🧾 <b>سفارش‌های این کاربر</b>\nروی هر سفارش بزنید تا جزئیاتش رو ببینید:",
        parse_mode="HTML",
        reply_markup=admin_user_orders_kb(orders, user_id, ctx),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("auserorder:"))
async def admin_view_user_order(callback: CallbackQuery):
    if await get_admin_role(callback.from_user.id) is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    _, order_id_str, user_id_str, ctx = callback.data.split(":", 3)
    order_id = int(order_id_str)
    user_id = int(user_id_str)
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("این سفارش پیدا نشد.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت به لیست سفارش‌ها", callback_data=f"auserorders:{user_id}:{ctx}")]
        ]
    )
    await callback.message.edit_text(order_detail_text(order), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("auserwallet:"))
async def admin_edit_user_wallet_prompt(callback: CallbackQuery, state: FSMContext):
    # عمداً محدود به مالک ربات: هیچ‌کدوم از سطوح ادمین مدیریت‌شده نباید بتونن دستی موجودی کیف پول اضافه کنن
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("این قابلیت فقط برای مالک ربات در دسترسه.", show_alert=True)
        return
    _, user_id_str, ctx = callback.data.split(":", 2)
    user_id = int(user_id_str)
    current = await db.get_wallet_balance(user_id)
    await state.update_data(edit_wallet_user_id=user_id, edit_wallet_ctx=ctx)
    await state.set_state(AdminStates.editing_wallet_balance)
    await callback.message.edit_text(
        f"💰 موجودی فعلی این کاربر: <b>{current:,} تومان</b>\nموجودی جدید رو به تومان وارد کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 انصراف", callback_data=f"auser:{user_id}:{ctx}")]]
        ),
    )
    await callback.answer()


@dp.message(AdminStates.editing_wallet_balance)
async def admin_edit_user_wallet_save(message: Message, state: FSMContext):
    # این استیت فقط از طریق admin_edit_user_wallet_prompt (که مخصوص مالک ربات هست) قابل‌دسترسیه
    if message.from_user.id not in config.ADMIN_IDS:
        await state.set_state(None)
        return
    text = (message.text or "").replace(",", "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد صحیح بفرستید (مثال: 100000):")
        return

    data = await state.get_data()
    user_id = data.get("edit_wallet_user_id")
    ctx = data.get("edit_wallet_ctx", "p0")
    if user_id is None:
        await state.set_state(None)
        await message.answer("مشکلی پیش اومد، دوباره از بخش کاربران وارد شوید.")
        return

    await db.set_wallet_balance(user_id, int(text))
    await state.set_state(None)  # last_user_search رو دست‌نخورده نگه می‌داریم

    user = await db.get_user(user_id)
    wallet_balance = await db.get_wallet_balance(user_id)
    orders = await db.get_user_orders(user_id)
    await message.answer(
        "✅ موجودی کیف پول با موفقیت بروزرسانی شد.\n\n" + user_detail_text(user, wallet_balance, len(orders)),
        parse_mode="HTML",
        reply_markup=user_detail_kb(user_id, ctx, "owner"),
    )


@dp.callback_query(F.data == "admintariff:gaming")
async def admintariff_gaming(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🎮 <b>تعرفه‌های کانفیگ گیم</b>\nروی هر تعرفه بزنید تا قیمتش رو تغییر بدید؛ با «✏️ متن» می‌تونید حجم رو عوض کنید، "
        "با «🗑 حذف» تعرفه رو کاملاً حذف کنید یا فعال/غیرفعالش کنید:",
        parse_mode="HTML",
        reply_markup=await gaming_admin_list_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admintariff:multi")
async def admintariff_multi(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🌐 <b>تعرفه‌های کانفیگ وبگردی</b>\nروی هر تعرفه بزنید تا قیمتش رو تغییر بدید؛ با «✏️ متن» می‌تونید عنوان رو عوض کنید، "
        "با «🗑 حذف» تعرفه رو کاملاً حذف کنید یا فعال/غیرفعالش کنید:",
        parse_mode="HTML",
        reply_markup=await multi_admin_list_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("gtoggle:"))
async def toggle_gaming(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[1])
    await db.toggle_gaming_active(plan_id)
    await callback.message.edit_reply_markup(reply_markup=await gaming_admin_list_kb())
    await callback.answer("وضعیت تعرفه تغییر کرد.")


@dp.callback_query(F.data.startswith("mtoggle:"))
async def toggle_multi(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[1])
    await db.toggle_multi_active(plan_id)
    await callback.message.edit_reply_markup(reply_markup=await multi_admin_list_kb())
    await callback.answer("وضعیت تعرفه تغییر کرد.")


@dp.callback_query(F.data.startswith("gdelete:"))
async def delete_gaming(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[1])
    await db.delete_gaming_plan(plan_id)
    await callback.message.edit_reply_markup(reply_markup=await gaming_admin_list_kb())
    await callback.answer("✅ تعرفه حذف شد.")


@dp.callback_query(F.data.startswith("mdelete:"))
async def delete_multi(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[1])
    await db.delete_multi_plan(plan_id)
    await callback.message.edit_reply_markup(reply_markup=await multi_admin_list_kb())
    await callback.answer("✅ تعرفه حذف شد.")


@dp.callback_query(F.data.startswith("gedittext:"))
async def start_edit_gaming_volume(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[1])
    plan = await db.get_gaming_plan(plan_id)
    if not plan:
        await callback.answer("این تعرفه پیدا نشد.", show_alert=True)
        return
    await state.update_data(plan_id=plan_id)
    await state.set_state(AdminStates.editing_gaming_volume)
    await callback.message.answer(
        f"حجم جدید (به گیگابایت) رو به‌جای «{plan['volume_gb']} گیگ» بفرستید (فقط عدد):"
    )
    await callback.answer()


@dp.message(AdminStates.editing_gaming_volume)
async def save_gaming_volume(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید (مثال: 60)")
        return
    data = await state.get_data()
    plan_id = data.get("plan_id")
    await db.update_gaming_volume(plan_id, int(text))
    await state.clear()
    await message.answer("✅ متن (حجم) تعرفه بروزرسانی شد.", reply_markup=await gaming_admin_list_kb())


@dp.callback_query(F.data.startswith("medittext:"))
async def start_edit_multi_label(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[1])
    plan = await db.get_multi_plan(plan_id)
    if not plan:
        await callback.answer("این تعرفه پیدا نشد.", show_alert=True)
        return
    await state.update_data(plan_id=plan_id)
    await state.set_state(AdminStates.editing_multi_label)
    await callback.message.answer(f"متن جدید رو به‌جای «{plan['label']}» بفرستید:")
    await callback.answer()


@dp.message(AdminStates.editing_multi_label)
async def save_multi_label(message: Message, state: FSMContext):
    label = (message.text or "").strip()
    if not label:
        await message.answer("لطفاً یه متن معتبر بفرستید.")
        return
    data = await state.get_data()
    plan_id = data.get("plan_id")
    await db.update_multi_label(plan_id, label)
    await state.clear()
    await message.answer("✅ متن تعرفه بروزرسانی شد.", reply_markup=await multi_admin_list_kb())


@dp.callback_query(F.data.startswith("gpriceedit:"))
async def start_edit_gaming_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[1])
    plan = await db.get_gaming_plan(plan_id)
    if not plan:
        await callback.answer("این تعرفه پیدا نشد.", show_alert=True)
        return
    await state.update_data(plan_id=plan_id)
    await state.set_state(AdminStates.editing_gaming_price)
    await callback.message.answer(
        f"قیمت جدید برای «{plan['volume_gb']} گیگ» رو به تومان بفرستید (فقط عدد):"
    )
    await callback.answer()


@dp.message(AdminStates.editing_gaming_price)
async def save_gaming_price(message: Message, state: FSMContext):
    text = (message.text or "").replace(",", "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید (مثال: 80000)")
        return
    data = await state.get_data()
    plan_id = data.get("plan_id")
    await db.update_gaming_price(plan_id, int(text))
    await state.clear()
    await message.answer("✅ قیمت با موفقیت بروزرسانی شد.", reply_markup=await gaming_admin_list_kb())


@dp.callback_query(F.data.startswith("mpriceedit:"))
async def start_edit_multi_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[1])
    plan = await db.get_multi_plan(plan_id)
    if not plan:
        await callback.answer("این تعرفه پیدا نشد.", show_alert=True)
        return
    await state.update_data(plan_id=plan_id)
    await state.set_state(AdminStates.editing_multi_price)
    await callback.message.answer(
        f"قیمت جدید برای «{plan['label']}» رو به تومان بفرستید (فقط عدد):"
    )
    await callback.answer()


@dp.message(AdminStates.editing_multi_price)
async def save_multi_price(message: Message, state: FSMContext):
    text = (message.text or "").replace(",", "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید (مثال: 180000)")
        return
    data = await state.get_data()
    plan_id = data.get("plan_id")
    await db.update_multi_price(plan_id, int(text))
    await state.clear()
    await message.answer("✅ قیمت با موفقیت بروزرسانی شد.", reply_markup=await multi_admin_list_kb())


@dp.callback_query(F.data == "gadd")
async def start_add_gaming(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.adding_gaming_volume)
    await callback.message.answer("حجم تعرفه جدید رو به گیگابایت بفرستید (فقط عدد، مثال: 60):")
    await callback.answer()


@dp.message(AdminStates.adding_gaming_volume)
async def add_gaming_volume(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید (مثال: 60)")
        return
    await state.update_data(volume=int(text))
    await state.set_state(AdminStates.adding_gaming_price)
    await message.answer("حالا قیمت این تعرفه رو به تومان بفرستید:")


@dp.message(AdminStates.adding_gaming_price)
async def add_gaming_price(message: Message, state: FSMContext):
    text = (message.text or "").replace(",", "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید (مثال: 400000)")
        return
    data = await state.get_data()
    volume = data.get("volume")
    await db.add_gaming_plan(volume, int(text))
    await state.clear()
    await message.answer("✅ تعرفه جدید اضافه شد.", reply_markup=await gaming_admin_list_kb())


@dp.callback_query(F.data == "madd")
async def start_add_multi(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.adding_multi_label)
    await callback.message.answer("عنوان تعرفه جدید رو بفرستید (مثال: سه کاربره نامحدود یک‌ماهه):")
    await callback.answer()


@dp.message(AdminStates.adding_multi_label)
async def add_multi_label(message: Message, state: FSMContext):
    label = (message.text or "").strip()
    if not label:
        await message.answer("لطفاً یه عنوان معتبر بفرستید.")
        return
    await state.update_data(label=label)
    await state.set_state(AdminStates.adding_multi_price)
    await message.answer("حالا قیمت این تعرفه رو به تومان بفرستید:")


@dp.message(AdminStates.adding_multi_price)
async def add_multi_price(message: Message, state: FSMContext):
    text = (message.text or "").replace(",", "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید (مثال: 300000)")
        return
    data = await state.get_data()
    label = data.get("label")
    await db.add_multi_plan(label, int(text))
    await state.clear()
    await message.answer("✅ تعرفه جدید اضافه شد.", reply_markup=await multi_admin_list_kb())


# ---------- Admin: مدیریت پنل‌های نمایندگی (سرویس‌های دلخواه ادمین) ----------
@dp.callback_query(F.data == "adminpanels")
async def admin_panels_root(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🖥 <b>مدیریت پنل‌های نمایندگی</b>\n"
        "اینجا می‌تونید سرویس‌های جدید (مثل «خرید پنل نمایندگی») بسازید، هرکدوم رو باز کنید و گزینه‌های "
        "داخلش (مثل پنل پاسارگاد، پنل سنایی و...) رو با متن و قیمت دلخواه اضافه/ویرایش/حذف کنید:",
        parse_mode="HTML",
        reply_markup=await panel_admin_categories_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("pcatopen:"))
async def admin_panel_category_open(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    category_id = int(callback.data.split(":")[1])
    category = await db.get_panel_category(category_id)
    if not category:
        await callback.answer("این پنل پیدا نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🖥 <b>{category['name']}</b>\nگزینه‌های این پنل رو مدیریت کنید:",
        parse_mode="HTML",
        reply_markup=await panel_admin_category_detail_kb(category_id),
    )
    await callback.answer()


@dp.callback_query(F.data == "pcatadd")
async def start_add_panel_category(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.adding_panel_category_name)
    await callback.message.answer("نام سرویس/پنل جدید رو بفرستید (مثال: خرید پنل نمایندگی):")
    await callback.answer()


@dp.message(AdminStates.adding_panel_category_name)
async def add_panel_category_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("لطفاً یه نام معتبر بفرستید.")
        return
    await db.add_panel_category(name)
    await state.clear()
    await message.answer("✅ پنل جدید اضافه شد.", reply_markup=await panel_admin_categories_kb())


@dp.callback_query(F.data.startswith("pcatedit:"))
async def start_edit_panel_category(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    category_id = int(callback.data.split(":")[1])
    category = await db.get_panel_category(category_id)
    if not category:
        await callback.answer("این پنل پیدا نشد.", show_alert=True)
        return
    await state.update_data(category_id=category_id)
    await state.set_state(AdminStates.editing_panel_category_name)
    await callback.message.answer(f"نام جدید رو به‌جای «{category['name']}» بفرستید:")
    await callback.answer()


@dp.message(AdminStates.editing_panel_category_name)
async def save_panel_category_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("لطفاً یه نام معتبر بفرستید.")
        return
    data = await state.get_data()
    category_id = data.get("category_id")
    await db.update_panel_category_name(category_id, name)
    await state.clear()
    await message.answer("✅ نام پنل بروزرسانی شد.", reply_markup=await panel_admin_categories_kb())


@dp.callback_query(F.data.startswith("pcattoggle:"))
async def toggle_panel_category(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    category_id = int(callback.data.split(":")[1])
    await db.toggle_panel_category_active(category_id)
    await callback.message.edit_reply_markup(reply_markup=await panel_admin_category_detail_kb(category_id))
    await callback.answer("وضعیت پنل تغییر کرد.")


@dp.callback_query(F.data.startswith("pcatdelete:"))
async def delete_panel_category_handler(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    category_id = int(callback.data.split(":")[1])
    await db.delete_panel_category(category_id)
    await callback.message.edit_text(
        "🖥 <b>مدیریت پنل‌های نمایندگی</b>\nهر سرویس رو از اینجا اضافه/ویرایش/حذف کنید:",
        parse_mode="HTML",
        reply_markup=await panel_admin_categories_kb(),
    )
    await callback.answer("✅ پنل و همه گزینه‌های داخلش حذف شدن.")


@dp.callback_query(F.data.startswith("pitemadd:"))
async def start_add_panel_item(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    category_id = int(callback.data.split(":")[1])
    await state.update_data(category_id=category_id)
    await state.set_state(AdminStates.adding_panel_item_title)
    await callback.message.answer("متن/عنوان گزینه جدید رو بفرستید (مثلاً «لوکیشن آلمان - ۵ سرور»):")
    await callback.answer()


@dp.message(AdminStates.adding_panel_item_title)
async def add_panel_item_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("لطفاً یه متن معتبر بفرستید.")
        return
    await state.update_data(title=title)
    await state.set_state(AdminStates.adding_panel_item_price)
    await message.answer(
        "حالا نرخ «هر گیگ» این گزینه رو به تومان بفرستید (فقط عدد).\n"
        "مثال: اگه بفرستید 2000، حجم ۲۵۰ گیگ خودکار میشه 500,000 تومان (چون 250 × 2000)."
    )


@dp.message(AdminStates.adding_panel_item_price)
async def add_panel_item_price(message: Message, state: FSMContext):
    text = (message.text or "").replace(",", "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید (مثال: 250000)")
        return
    data = await state.get_data()
    category_id = data.get("category_id")
    title = data.get("title")
    await db.add_panel_item(category_id, title, int(text))
    await state.clear()
    await message.answer("✅ گزینه جدید اضافه شد.", reply_markup=await panel_admin_category_detail_kb(category_id))


@dp.callback_query(F.data.startswith("pitempriceedit:"))
async def start_edit_panel_item_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    item_id = int(callback.data.split(":")[1])
    item = await db.get_panel_item(item_id)
    if not item:
        await callback.answer("این گزینه پیدا نشد.", show_alert=True)
        return
    await state.update_data(item_id=item_id)
    await state.set_state(AdminStates.editing_panel_item_price)
    await callback.message.answer(
        f"نرخ جدید «هر گیگ» رو برای «{item['title']}» به تومان بفرستید (فقط عدد؛ حجم‌های ۲۵۰ تا ۳۰۰۰ گیگ خودکار بر این اساس محاسبه میشن):"
    )
    await callback.answer()


@dp.message(AdminStates.editing_panel_item_price)
async def save_panel_item_price(message: Message, state: FSMContext):
    text = (message.text or "").replace(",", "").strip()
    if not text.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید (مثال: 250000)")
        return
    data = await state.get_data()
    item_id = data.get("item_id")
    item = await db.get_panel_item(item_id)
    await db.update_panel_item_price(item_id, int(text))
    await state.clear()
    category_id = item["category_id"] if item else None
    kb = await panel_admin_category_detail_kb(category_id) if category_id else await panel_admin_categories_kb()
    await message.answer("✅ قیمت بروزرسانی شد.", reply_markup=kb)


@dp.callback_query(F.data.startswith("pitemedittext:"))
async def start_edit_panel_item_title(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    item_id = int(callback.data.split(":")[1])
    item = await db.get_panel_item(item_id)
    if not item:
        await callback.answer("این گزینه پیدا نشد.", show_alert=True)
        return
    await state.update_data(item_id=item_id)
    await state.set_state(AdminStates.editing_panel_item_title)
    await callback.message.answer(f"متن جدید رو به‌جای «{item['title']}» بفرستید:")
    await callback.answer()


@dp.message(AdminStates.editing_panel_item_title)
async def save_panel_item_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("لطفاً یه متن معتبر بفرستید.")
        return
    data = await state.get_data()
    item_id = data.get("item_id")
    item = await db.get_panel_item(item_id)
    await db.update_panel_item_title(item_id, title)
    await state.clear()
    category_id = item["category_id"] if item else None
    kb = await panel_admin_category_detail_kb(category_id) if category_id else await panel_admin_categories_kb()
    await message.answer("✅ متن بروزرسانی شد.", reply_markup=kb)


@dp.callback_query(F.data.startswith("pitemtoggle:"))
async def toggle_panel_item(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    item_id = int(callback.data.split(":")[1])
    item = await db.get_panel_item(item_id)
    category_id = item["category_id"] if item else None
    await db.toggle_panel_item_active(item_id)
    kb = await panel_admin_category_detail_kb(category_id) if category_id else await panel_admin_categories_kb()
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("وضعیت گزینه تغییر کرد.")


@dp.callback_query(F.data.startswith("pitemdelete:"))
async def delete_panel_item_handler(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    item_id = int(callback.data.split(":")[1])
    item = await db.get_panel_item(item_id)
    category_id = item["category_id"] if item else None
    await db.delete_panel_item(item_id)
    kb = await panel_admin_category_detail_kb(category_id) if category_id else await panel_admin_categories_kb()
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("✅ گزینه حذف شد.")


# ---------- Admin handlers ----------
@dp.callback_query(F.data.startswith("approve:"))
async def admin_approve(callback: CallbackQuery, state: FSMContext):
    if await get_admin_role(callback.from_user.id) is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return

    await db.set_order_status(order_id, "approved")
    await state.update_data(order_id=order_id)
    await state.set_state(AdminStates.waiting_for_panel_info)

    is_trial = order["order_type"] == "trial"
    label = "درخواست تست" if is_trial else "سفارش"
    await callback.message.answer(
        f"✅ {label} #{order_id} تأیید شد.\n"
        f"حالا لطفاً اطلاعات {'اکانت تست' if is_trial else 'سرویس'} (کانفیگ/یوزر/پس/لینک و ...) رو برای ارسال به مشتری بفرستید:"
    )
    await callback.answer()


@dp.message(AdminStates.waiting_for_panel_info)
async def admin_send_panel_info(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    order = await db.get_order(order_id)
    if not order:
        await message.answer("سفارش پیدا نشد.")
        await state.clear()
        return

    panel_info = message.text or message.caption or ""
    await db.deliver_order(order_id, panel_info)
    await state.clear()

    is_trial = order["order_type"] == "trial"
    try:
        if is_trial:
            await bot.send_message(
                order["user_id"],
                f"🎁 اکانت تست شما (#{order_id}) آماده شد!\n\n"
                f"🔑 اطلاعات اتصال:\n{panel_info}\n\n"
                f"این یک اکانت آزمایشی و محدوده. برای خرید نسخه کامل از «🛍 خرید سرویس» استفاده کنید.",
            )
        else:
            await bot.send_message(
                order["user_id"],
                f"🎉 سفارش شما (#{order_id}) تأیید و تحویل داده شد!\n\n"
                f"🔑 اطلاعات سرویس شما:\n{panel_info}",
            )
        await message.answer(f"✅ اطلاعات {'تست' if is_trial else 'سرویس'} با موفقیت برای مشتری سفارش #{order_id} ارسال شد.")
    except Exception as e:
        await message.answer(f"⚠️ ارسال به کاربر ناموفق بود: {e}")

    # رفرال دائمی پورسانتی: به‌ازای هر خرید موفق (تحویل‌شده) کاربری که با لینک یه نفر دیگه وارد شده،
    # درصدی از مبلغ خرید به‌صورت نقدی به کیف پول دعوت‌کننده اضافه میشه - این کار به تعداد نامحدود تکرار میشه
    # (تحویل اکانت تست چون خرید واقعی نیست، باعث فعال شدن رفرال یا پورسانت نمیشه)
    referral = await db.get_referral_by_referred(order["user_id"]) if not is_trial else None
    if referral:
        if not referral["converted"]:
            await db.mark_referral_converted(order["user_id"])
        referrer_id = referral["referrer_id"]
        if order["price"] and order["price"] > 0:
            commission_percent = await db.get_referral_commission_percent()
            commission_amount = int(order["price"] * commission_percent / 100)
            if commission_amount > 0:
                await db.add_wallet_balance(referrer_id, commission_amount)
                await db.add_referral_commission(referrer_id, order["user_id"], order_id, commission_amount)
                try:
                    await bot.send_message(
                        referrer_id,
                        f"💸 یکی از دوستانی که دعوت کردید خرید کرد!\n"
                        f"مبلغ {commission_amount:,} تومان ({commission_percent}٪ از خریدش) به کیف پول شما اضافه شد. 🎉",
                    )
                except Exception as e:
                    logging.warning(f"Could not notify referrer {referrer_id} about commission: {e}")


@dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(callback: CallbackQuery, state: FSMContext):
    if await get_admin_role(callback.from_user.id) is None:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return

    await db.set_order_status(order_id, "rejected")

    refund_note = ""
    if order["payment_method"] == "wallet" and order["price"] > 0:
        await db.add_wallet_balance(order["user_id"], order["price"])
        refund_note = f"\n💰 مبلغ {order['price']:,} تومان به کیف پول شما برگردونده شد."

    try:
        await bot.send_message(
            order["user_id"],
            f"❌ متأسفانه سفارش شما (#{order_id}) رد شد.{refund_note}\n"
            f"در صورت وجود اشتباه در واریزی، لطفاً با پشتیبانی در ارتباط باشید.",
        )
    except Exception as e:
        logging.warning(f"Could not notify user: {e}")

    await callback.message.answer(f"❌ سفارش #{order_id} رد شد و به کاربر اطلاع داده شد.")
    await callback.answer()


@dp.message(Command("orders_admin"))
async def admin_all_pending(message: Message):
    if await get_admin_role(message.from_user.id) is None:
        return
    # نمایش سریع راهنما - برای گزارش کامل می‌تونید دیتابیس bot.db رو با ابزار SQLite باز کنید
    await message.answer(
        "برای مشاهده کامل سفارش‌ها فایل دیتابیس bot.db رو بررسی کنید، "
        "یا از دستورات تأیید/رد که زیر هر سفارش جدید ارسال میشه استفاده کنید."
    )


# ---------- مدیریت خطاهای پیش‌بینی‌نشده ----------
# بدون این هندلر، اگه توی یه هندلر خطایی رخ بده (مثلاً به‌خاطر HTML نامعتبر)، کاربر هیچ
# واکنشی نمی‌بینه (نه پیام خطا، نه هیچی) و به نظر میاد ربات "هیچ کاری نکرده".
@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.exception("خطای پیش‌بینی‌نشده در پردازش آپدیت", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer("⚠️ خطایی رخ داد. لطفاً دوباره امتحان کنید.", show_alert=True)
        elif update.message:
            await update.message.answer("⚠️ خطایی رخ داد. لطفاً دوباره امتحان کنید.")
    except Exception:
        pass  # اگه ارسال پیام خطا هم شکست خورد، دیگه کاری نمیشه کرد
    return True


# ---------- Startup ----------
async def main():
    global BOT_USERNAME
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده! متغیر محیطی BOT_TOKEN رو ست کنید.")
    if not config.ADMIN_IDS:
        logging.warning("ADMIN_IDS تنظیم نشده! هیچ ادمینی سفارش‌ها رو دریافت نمی‌کنه.")
    if _force_join_channel_username():
        logging.info(
            f"عضویت اجباری در کانال {_force_join_channel_username()} فعاله. "
            f"مطمئن شوید ربات به عنوان ادمین توی این کانال اضافه شده، وگرنه چک عضویت کار نمی‌کنه."
        )

    await db.init_db()
    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    BOT_USERNAME = me.username
    logging.info(f"Bot started as @{BOT_USERNAME}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
