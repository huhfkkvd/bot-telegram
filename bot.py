import asyncio
import logging

from aiogram import Bot, Dispatcher, F
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
)

import config
import database as db

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

BOT_USERNAME = ""  # در main() پر میشه


# ---------- States ----------
class BuyStates(StatesGroup):
    waiting_for_receipt = State()


class AdminStates(StatesGroup):
    waiting_for_panel_info = State()
    waiting_for_reject_reason = State()
    editing_gaming_price = State()
    editing_multi_price = State()
    adding_gaming_volume = State()
    adding_gaming_price = State()
    adding_multi_label = State()
    adding_multi_price = State()
    editing_welcome_message = State()
    editing_referral_count = State()
    editing_referral_volume = State()


# ---------- Keyboards ----------
def main_menu_kb(user_id: int | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🛍 خرید سرویس")],
        [KeyboardButton(text="🖥 سرویس‌های من")],
        [KeyboardButton(text="💰 کیف پول"), KeyboardButton(text="💬 پشتیبانی")],
        [KeyboardButton(text="🤝 دعوت دوستان")],
    ]
    if user_id is not None and user_id in config.ADMIN_IDS:
        keyboard.append([KeyboardButton(text="🛠 مدیریت ربات")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")]]
    )


def services_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎮 سرویس گیمینگ", callback_data="svc:gaming")],
        [InlineKeyboardButton(text="🌍 سرویس مولتی لوکیشن (وبگردی)", callback_data="svc:multi")],
        [InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gaming_plans_kb(plans) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for p in plans:
        row.append(
            InlineKeyboardButton(text=f"{p['volume_gb']} گیگ - {p['price']:,} تومان", callback_data=f"gplan:{p['id']}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="back:services")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def multi_plans_kb(plans) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{p['label']} - {p['price']:,} تومان", callback_data=f"mplan:{p['id']}")]
        for p in plans
    ]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="back:services")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_summary_kb(order_id: int, kind: str) -> InlineKeyboardMarkup:
    """کیبورد صفحه خلاصه سفارش: ارسال رسید یا بازگشت (لغو سفارش و برگشت به لیست تعرفه‌ها)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 ارسال رسید", callback_data=f"reqreceipt:{order_id}")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"cancelorder:{order_id}:{kind}")],
        ]
    )


def waiting_receipt_kb(order_id: int) -> InlineKeyboardMarkup:
    """کیبورد صفحه‌ی در انتظار دریافت رسید: فقط بازگشت به خلاصه سفارش."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"backsummary:{order_id}")]]
    )


def order_summary_text(order) -> str:
    return (
        f"🧾 <b>خلاصه سفارش شما</b>\n"
        f"—————————————\n"
        f"📦 {order['plan_name']}\n"
        f"💰 قیمت: {order['price']:,} تومان\n"
        f"—————————————\n\n"
        f"💳 شماره کارت: <code>{config.CARD_NUMBER}</code>\n"
        f"👤 به نام: {config.CARD_HOLDER}\n\n"
        f"ℹ️ پس از واریز وجه، روی دکمه «📤 ارسال رسید» بزنید و سپس عکس یا فایل رسید رو ارسال کنید."
    )


def admin_decision_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأیید", callback_data=f"approve:{order_id}"),
                InlineKeyboardButton(text="❌ رد", callback_data=f"reject:{order_id}"),
            ]
        ]
    )


# ---------- User handlers ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()

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
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb(message.from_user.id))


@dp.message(F.text == "🛍 خرید سرویس")
async def show_services(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛍 لطفاً نوع سرویس مورد نظر رو انتخاب کنید:", reply_markup=services_kb())


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
    await callback.message.edit_text("🛍 لطفاً نوع سرویس مورد نظر رو انتخاب کنید:", reply_markup=services_kb())
    await callback.answer()


@dp.callback_query(F.data.startswith("cancelorder:"))
async def cancel_order_and_go_back(callback: CallbackQuery, state: FSMContext):
    """کاربر از صفحه خلاصه سفارش «بازگشت» رو زده -> سفارش لغو میشه و به لیست تعرفه‌های همون سرویس برمی‌گرده."""
    _, order_id_str, kind = callback.data.split(":")
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
            "🎮 <b>سرویس گیمینگ</b>\nحجم مورد نظر رو انتخاب کنید:", parse_mode="HTML", reply_markup=gaming_plans_kb(plans)
        )
    else:
        plans = await db.get_multi_plans()
        if not plans:
            await callback.answer("در حال حاضر تعرفه‌ای برای این سرویس ثبت نشده.", show_alert=True)
            return
        await callback.message.edit_text(
            "🌍 <b>سرویس مولتی لوکیشن (وبگردی)</b>\nتعرفه مورد نظر رو انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=multi_plans_kb(plans),
        )
    await callback.answer()


@dp.callback_query(F.data == "svc:gaming")
async def choose_gaming_service(callback: CallbackQuery, state: FSMContext):
    plans = await db.get_gaming_plans()
    if not plans:
        await callback.answer("در حال حاضر تعرفه‌ای برای این سرویس ثبت نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        "🎮 <b>سرویس گیمینگ</b>\nحجم مورد نظر رو انتخاب کنید:", parse_mode="HTML", reply_markup=gaming_plans_kb(plans)
    )
    await callback.answer()


@dp.callback_query(F.data == "svc:multi")
async def choose_multi_service(callback: CallbackQuery, state: FSMContext):
    plans = await db.get_multi_plans()
    if not plans:
        await callback.answer("در حال حاضر تعرفه‌ای برای این سرویس ثبت نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        "🌍 <b>سرویس مولتی لوکیشن (وبگردی)</b>\nتعرفه مورد نظر رو انتخاب کنید:",
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
        order_summary_text(order), parse_mode="HTML", reply_markup=order_summary_kb(order_id, "gaming")
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
        order_summary_text(order), parse_mode="HTML", reply_markup=order_summary_kb(order_id, "multi")
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
    kind = "gaming" if str(order["plan_name"]).startswith("🎮") else "multi"
    await callback.message.edit_text(
        order_summary_text(order), parse_mode="HTML", reply_markup=order_summary_kb(order_id, kind)
    )
    await callback.answer()


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
        reply_markup=main_menu_kb(message.from_user.id),
    )
    await state.clear()

    caption = (
        f"🆕 سفارش جدید #{order_id}\n"
        f"👤 کاربر: {order['full_name']} (@{order['username'] or '-'})\n"
        f"🆔 آیدی عددی: {order['user_id']}\n"
        f"📦 پلن: {order['plan_name']}\n"
        f"💰 مبلغ: {order['price']:,} تومان"
    )

    for admin_id in config.ADMIN_IDS:
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
    text = (
        f"🆔 <b>سفارش #{order['id']}</b>\n"
        f"—————————————\n"
        f"📦 پلن: {order['plan_name']}\n"
        f"💰 مبلغ: {order['price']:,} تومان\n"
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


@dp.message(F.text == "💰 کیف پول")
async def wallet_handler(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "💰 <b>کیف پول شما</b>\n\n"
        "موجودی فعلی: 0 تومان\n\n"
        "🔜 این بخش به‌زودی برای شارژ کیف پول و پرداخت خودکار فعال میشه."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=back_menu_kb())


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


@dp.message(F.text == "🤝 دعوت دوستان")
async def invite_handler(message: Message, state: FSMContext):
    await state.clear()
    referrer_id = message.from_user.id
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{referrer_id}"
    total = await db.count_referrals(referrer_id)
    converted = await db.count_converted_referrals(referrer_id)
    claimed = await db.has_claimed_reward(referrer_id)
    required_count = await db.get_referral_required_count()
    reward_volume = await db.get_referral_reward_volume()

    text = (
        f"🤝 <b>دعوت دوستان</b>\n\n"
        f"لینک اختصاصی شما:\n<code>{link}</code>\n\n"
        f"👥 تعداد افراد دعوت‌شده: {total}\n"
        f"✅ تعداد خریدهای موفق: {converted}\n\n"
        f"🎁 اگر <b>{required_count} نفر</b> با لینک شما وارد بشن و خرید کنن، "
        f"یک سرویس گیمینگ <b>{reward_volume} گیگ</b> به‌صورت <b>رایگان</b> بهتون تعلق می‌گیره! 🎉"
    )

    if converted >= required_count and not claimed:
        text += "\n\n🎉 شما واجد شرایط دریافت هدیه هستید!"
        rows = [
            [InlineKeyboardButton(text=f"🎁 دریافت سرویس گیمینگ {reward_volume} گیگ رایگان", callback_data="claimref")],
            [InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")],
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    else:
        kb = back_menu_kb()

    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "claimref")
async def claim_referral_reward(callback: CallbackQuery, state: FSMContext):
    referrer_id = callback.from_user.id
    converted = await db.count_converted_referrals(referrer_id)
    already_claimed = await db.has_claimed_reward(referrer_id)
    required_count = await db.get_referral_required_count()
    reward_volume = await db.get_referral_reward_volume()

    if converted < required_count or already_claimed:
        await callback.answer("شرایط دریافت هدیه رو ندارید یا قبلاً دریافتش کردید.", show_alert=True)
        return

    await db.set_reward_claimed(referrer_id)
    plan_name = f"🎮 سرویس گیمینگ - {reward_volume} گیگ (هدیه رفرال 🎁)"

    order_id = await db.create_order(
        user_id=referrer_id,
        username=callback.from_user.username or "",
        full_name=callback.from_user.full_name,
        plan_id=0,
        plan_name=plan_name,
        price=0,
    )
    await db.set_order_status(order_id, "pending")

    await callback.message.edit_text(
        f"🎉 درخواست هدیه شما (سرویس گیمینگ {reward_volume} گیگ) ثبت شد.\n"
        f"به‌زودی توسط ادمین بررسی و تحویل داده میشه."
    )
    await callback.answer()

    caption = (
        f"🎁 درخواست هدیه رفرال - سفارش #{order_id}\n"
        f"👤 کاربر: {callback.from_user.full_name} (@{callback.from_user.username or '-'})\n"
        f"🆔 آیدی عددی: {referrer_id}\n"
        f"📦 پلن: {plan_name}\n"
        f"💰 مبلغ: رایگان (هدیه رفرال)"
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, caption, reply_markup=admin_decision_kb(order_id))
        except Exception as e:
            logging.warning(f"Could not notify admin {admin_id}: {e}")


# ---------- Admin: management panel ----------
def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎮 تعرفه‌های گیمینگ", callback_data="admintariff:gaming")],
            [InlineKeyboardButton(text="🌍 تعرفه‌های مولتی لوکیشن", callback_data="admintariff:multi")],
            [InlineKeyboardButton(text="✉️ پیام خوش‌آمدگویی", callback_data="adminwelcome")],
            [InlineKeyboardButton(text="🤝 تنظیمات رفرال", callback_data="adminreferral")],
            [InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back:menu")],
        ]
    )


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
                ),
                InlineKeyboardButton(
                    text="غیرفعال" if p["active"] else "فعال",
                    callback_data=f"gtoggle:{p['id']}",
                ),
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
                ),
                InlineKeyboardButton(
                    text="غیرفعال" if p["active"] else "فعال",
                    callback_data=f"mtoggle:{p['id']}",
                ),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ افزودن تعرفه جدید", callback_data="madd")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


ADMIN_ROOT_TEXT = "⚙️ <b>مدیریت ربات</b>\nچی رو می‌خواید تنظیم کنید؟"


@dp.message(Command("admin"))
@dp.message(F.text == "🛠 مدیریت ربات")
async def admin_panel_entry(message: Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await state.clear()
    await message.answer(ADMIN_ROOT_TEXT, parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "admintariff:root")
async def admintariff_root(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(ADMIN_ROOT_TEXT, parse_mode="HTML", reply_markup=admin_menu_kb())
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


def referral_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ تغییر تعداد دعوت لازم", callback_data="editrefcount")],
            [InlineKeyboardButton(text="✏️ تغییر حجم هدیه (گیگ)", callback_data="editrefvolume")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admintariff:root")],
        ]
    )


@dp.callback_query(F.data == "adminreferral")
async def admin_referral_settings(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.clear()
    required_count = await db.get_referral_required_count()
    reward_volume = await db.get_referral_reward_volume()
    await callback.message.edit_text(
        f"🤝 <b>تنظیمات رفرال</b>\n\n"
        f"👥 تعداد دعوت موفق لازم: <b>{required_count}</b>\n"
        f"🎁 حجم هدیه گیمینگ: <b>{reward_volume} گیگ</b>",
        parse_mode="HTML",
        reply_markup=referral_settings_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "editrefcount")
async def start_edit_referral_count(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.editing_referral_count)
    await callback.message.edit_text(
        "تعداد دعوت موفق لازم برای دریافت هدیه رو بفرستید (فقط عدد):",
        reply_markup=back_to_admin_root_kb,
    )
    await callback.answer()


@dp.message(AdminStates.editing_referral_count)
async def save_referral_count(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("لطفاً یه عدد صحیح و بزرگ‌تر از صفر بفرستید (مثال: 3)")
        return
    await db.set_setting("referral_required_count", int(text))
    await state.clear()
    await message.answer("✅ تعداد دعوت لازم با موفقیت بروزرسانی شد.", reply_markup=referral_settings_kb())


@dp.callback_query(F.data == "editrefvolume")
async def start_edit_referral_volume(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.editing_referral_volume)
    await callback.message.edit_text(
        "حجم هدیه گیمینگ رو به گیگابایت بفرستید (فقط عدد):",
        reply_markup=back_to_admin_root_kb,
    )
    await callback.answer()


@dp.message(AdminStates.editing_referral_volume)
async def save_referral_volume(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("لطفاً یه عدد صحیح و بزرگ‌تر از صفر بفرستید (مثال: 50)")
        return
    await db.set_setting("referral_reward_volume", int(text))
    await state.clear()
    await message.answer("✅ حجم هدیه رفرال با موفقیت بروزرسانی شد.", reply_markup=referral_settings_kb())


@dp.callback_query(F.data == "admintariff:gaming")
async def admintariff_gaming(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🎮 <b>تعرفه‌های سرویس گیمینگ</b>\nروی هر تعرفه بزنید تا قیمتش رو تغییر بدید، یا فعال/غیرفعالش کنید:",
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
        "🌍 <b>تعرفه‌های سرویس مولتی لوکیشن</b>\nروی هر تعرفه بزنید تا قیمتش رو تغییر بدید، یا فعال/غیرفعالش کنید:",
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


# ---------- Admin handlers ----------
@dp.callback_query(F.data.startswith("approve:"))
async def admin_approve(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
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

    await callback.message.answer(
        f"✅ سفارش #{order_id} تأیید شد.\n"
        f"حالا لطفاً اطلاعات سرویس (کانفیگ/یوزر/پس/لینک و ...) رو برای ارسال به مشتری بفرستید:"
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

    try:
        await bot.send_message(
            order["user_id"],
            f"🎉 سفارش شما (#{order_id}) تأیید و تحویل داده شد!\n\n"
            f"🔑 اطلاعات سرویس شما:\n{panel_info}",
        )
        await message.answer(f"✅ اطلاعات سرویس با موفقیت برای مشتری سفارش #{order_id} ارسال شد.")
    except Exception as e:
        await message.answer(f"⚠️ ارسال به کاربر ناموفق بود: {e}")

    # بررسی سیستم رفرال: اگر این کاربر با لینک دعوت وارد شده، این خرید رو "تبدیل‌شده" علامت بزن
    referral = await db.get_referral_by_referred(order["user_id"])
    if referral and not referral["converted"]:
        await db.mark_referral_converted(order["user_id"])
        referrer_id = referral["referrer_id"]
        converted_count = await db.count_converted_referrals(referrer_id)
        required_count = await db.get_referral_required_count()
        reward_volume = await db.get_referral_reward_volume()
        if converted_count == required_count and not await db.has_claimed_reward(referrer_id):
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 تبریک! {required_count} نفر از دوستان شما با لینک دعوتتون خرید کردن.\n"
                    f"یک سرویس گیمینگ {reward_volume} گیگ رایگان بهتون تعلق گرفت 🎁\n"
                    f"برای دریافت، به بخش «🤝 دعوت دوستان» برید و روی دکمه دریافت هدیه بزنید.",
                )
            except Exception as e:
                logging.warning(f"Could not notify referrer {referrer_id} about reward: {e}")


@dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("شما دسترسی ادمین ندارید.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return

    await db.set_order_status(order_id, "rejected")

    try:
        await bot.send_message(
            order["user_id"],
            f"❌ متأسفانه سفارش شما (#{order_id}) رد شد.\n"
            f"در صورت وجود اشتباه در واریزی، لطفاً با پشتیبانی در ارتباط باشید.",
        )
    except Exception as e:
        logging.warning(f"Could not notify user: {e}")

    await callback.message.answer(f"❌ سفارش #{order_id} رد شد و به کاربر اطلاع داده شد.")
    await callback.answer()


@dp.message(Command("orders_admin"))
async def admin_all_pending(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    # نمایش سریع راهنما - برای گزارش کامل می‌تونید دیتابیس bot.db رو با ابزار SQLite باز کنید
    await message.answer(
        "برای مشاهده کامل سفارش‌ها فایل دیتابیس bot.db رو بررسی کنید، "
        "یا از دستورات تأیید/رد که زیر هر سفارش جدید ارسال میشه استفاده کنید."
    )


# ---------- Startup ----------
async def main():
    global BOT_USERNAME
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده! متغیر محیطی BOT_TOKEN رو ست کنید.")
    if not config.ADMIN_IDS:
        logging.warning("ADMIN_IDS تنظیم نشده! هیچ ادمینی سفارش‌ها رو دریافت نمی‌کنه.")

    await db.init_db()
    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    BOT_USERNAME = me.username
    logging.info(f"Bot started as @{BOT_USERNAME}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
