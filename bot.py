import asyncio
import logging
from datetime import datetime
import os
import json
import time
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramAPIError

from pyrogram import Client as TGClient
from pyrogram.raw.functions.messages import RequestAppWebView
from pyrogram.raw.types import InputBotAppShortName
from urllib.parse import unquote
from curl_cffi import requests

# Импортируем существующие модули
from auth_telethon import telethon_auth 
from db_manager import DatabaseManager
from main import (
    MRKTBuyer, get_fresh_token, save_token, 
    load_saved_token, error_stats, seen_nfts, check_new_nfts,
    extract_price, format_price
)

# Конфигурация
API_ID = 31267437
API_HASH = "517b90bd9eef9b8f162cd83fef08ff41"
MARKET_API_URL = "https://api.tgmrkt.io/api/v1"
BOT_TOKEN = "8632355778:AAGyi2cQDlmMmGmDsbHsCuQIbkKkjJUbp94"

ALLOWED_USER_IDS = [2017681719, 984910437, 6306950956] 

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Глобальные переменные
db = DatabaseManager()
monitoring_tasks: Dict[int, asyncio.Task] = {}
monitoring_status: Dict[int, bool] = {}
user_tokens: Dict[int, str] = {}
user_clients: Dict[int, TGClient] = {}
pending_confirmations: Dict[str, Dict] = {}  # Для хранения подтверждений покупок


# Классы состояний FSM
class AddCollection(StatesGroup):
    waiting_for_name = State()
    waiting_for_price = State()


class UpdatePrice(StatesGroup):
    waiting_for_price = State()


class Settings(StatesGroup):
    waiting_for_interval = State()
    waiting_for_retries = State()
    waiting_for_retry_delay = State()
    waiting_for_timeout = State()
    waiting_for_max_purchases = State()


class AuthStates(StatesGroup):
    waiting_manual_token = State() 
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa = State()


def is_user_allowed(user_id: int) -> bool:
    """Проверяет, разрешен ли доступ пользователю"""
    return user_id in ALLOWED_USER_IDS

class AccessMiddleware:
    async def __call__(self, handler, event: Message, data: Dict):
        user_id = event.from_user.id
        
        # Пропускаем команду /start для всех (чтобы новые пользователи видели приветствие)
        if event.text and event.text.startswith('/start'):
            if not is_user_allowed(user_id):
                await event.answer(
                    "❌ <b>Доступ запрещен</b>\n\n"
                    "Этот бот предназначен только для определенных пользователей.\n"
                    f"Ваш ID: <code>{user_id}</code>",
                    parse_mode="HTML"
                )
                return
            return await handler(event, data)
        
        # Для всех остальных сообщений проверяем доступ
        if not is_user_allowed(user_id):
            await event.answer(
                "❌ <b>Доступ запрещен</b>\n\n"
                f"Ваш ID: <code>{user_id}</code>",
                parse_mode="HTML"
            )
            return
        
        return await handler(event, data)

# Регистрируем мидлварь
dp.message.middleware(AccessMiddleware())

# ==================== КЛАВИАТУРЫ ====================

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔑 Войти"),  # Изменено
            KeyboardButton(text="🔐 Ввести токен вручную")],  # Новая кнопка
            [
                KeyboardButton(text="📋 Мои коллекции"),
                KeyboardButton(text="⚙️ Настройки")
            ],
            [
                KeyboardButton(text="▶️ Запустить мониторинг"),
                KeyboardButton(text="⏹️ Остановить мониторинг")
            ],
            [
                KeyboardButton(text="📊 Статистика"),
                KeyboardButton(text="🛒 История покупок")
            ],
            [KeyboardButton(text="🚪 Выйти")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )
    return keyboard

# Новый обработчик для ручного ввода токена
@dp.message(F.text == "🔐 Ввести токен вручную")
async def handle_manual_token(message: Message, state: FSMContext):
    """Начало процесса ручного ввода токена"""
    user_id = message.from_user.id
    
    # Проверяем, может уже авторизован
    if user_id in user_tokens:
        await message.answer(
            "✅ <b>Вы уже авторизованы!</b>\n\n"
            "Если хотите сменить токен, сначала выйдите.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return
    
    await state.set_state(AuthStates.waiting_manual_token)
    
    await message.answer(
        "🔐 <b>Ручной ввод токена MRKT</b>\n\n"
        "Вставьте ваш токен MRKT:\n\n"
        "<i>Где взять токен?</i>\n"
        "1. Откройте @mrkt бот\n"
        "2. Откройте инструменты разработчика (F12)\n"
        "3. Найдите запросы к api.tgmrkt.io\n"
        "4. Скопируйте token из запроса auth\n\n"
        "Или нажмите «❌ Отмена» для возврата",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(AuthStates.waiting_manual_token, F.text)
async def process_manual_token(message: Message, state: FSMContext):
    """Обработка ручного ввода токена"""
    user_id = message.from_user.id
    text = message.text.strip()
    token = message.text.strip()
    
    # Проверяем, не нажал ли пользователь "Отмена"
    if text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "❌ Ввод токена отменен.",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Простейшая проверка токена (он должен быть достаточно длинным)
    if len(token) < 20:
        await message.answer(
            "❌ <b>Неверный формат токена</b>\n\n"
            "Токен должен быть длинной строкой. Попробуйте еще раз:",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    # Пробуем проверить токен, сделав тестовый запрос к API
    status_msg = await message.answer(
        "🔄 <b>Проверка токена...</b>",
        parse_mode="HTML"
    )
    
    try:
        # ИСПРАВЛЕНИЕ: Используем другой эндпоинт для проверки
        # Например, запрос информации о пользователе или просто HEAD запрос
        headers = {
            "Authorization": token,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        # Вариант 2: Если не работает, можно попробовать запрос
        response = requests.get(
            f"{MARKET_API_URL}/me",
            headers=headers,
            impersonate="chrome",
            timeout=10
        )
        
        if response.status_code == 200:
            # Токен работает
            await status_msg.delete()
            
            # Сохраняем токен
            save_token(token)
            user_tokens[user_id] = token
            
            # Очищаем старые сессии Telegram если они были
            await telethon_auth.cleanup_temp_data(user_id)
            
            await message.answer(
                f"✅ <b>Токен успешно сохранен!</b>\n\n"
                f"📋 <b>Доступные действия:</b>\n"
                f"• Добавить коллекции для отслеживания через «📋 Мои коллекции»\n"
                f"• Настроить параметры в «⚙️ Настройки»\n"
                f"• Запустить мониторинг кнопкой «▶️ Запустить мониторинг»",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
            
        elif response.status_code == 429:
            # Специальная обработка rate limit
            await status_msg.delete()
            await message.answer(
                f"⚠️ <b>Слишком много запросов к API</b>\n\n"
                f"Подождите немного и попробуйте снова.\n"
                f"Код ответа: {response.status_code}\n\n"
                f"<i>Совет: используйте токен, который не участвует в мониторинге</i>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
            
        else:
            await status_msg.delete()
            await message.answer(
                f"❌ <b>Токен не работает</b>\n\n"
                f"Код ответа: {response.status_code}\n"
                f"Попробуйте еще раз или войдите через Telegram:",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
            
    except requests.RequestsError as e:
        await status_msg.delete()
        await message.answer(
            f"❌ <b>Ошибка соединения с API</b>\n\n"
            f"{str(e)[:200]}\n\n"
            f"Попробуйте еще раз:",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )
    except Exception as e:
        await status_msg.delete()
        await message.answer(
            f"❌ <b>Ошибка при проверке токена</b>\n\n"
            f"{str(e)[:200]}\n\n"
            f"Попробуйте еще раз:",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )

def get_phone_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура для ввода номера телефона"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить контакт", request_contact=True)],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Или введите номер вручную (+79123456789)..."
    )
    return keyboard


def get_code_keyboard(entered_code: str = "") -> InlineKeyboardMarkup:
    """Создает инлайн-клавиатуру для ввода кода"""
    builder = InlineKeyboardBuilder()
    
    # Кнопки цифр 1-9
    for i in range(1, 10):
        builder.button(text=str(i), callback_data=f"code_digit_{i}")
    
    # Последний ряд: backspace, 0, confirm
    builder.button(text="⌫", callback_data="code_digit_backspace")
    builder.button(text="0", callback_data="code_digit_0")
    builder.button(text="✅ Подтвердить", callback_data="code_digit_confirm")
    
    # Кнопка отмены
    builder.button(text="❌ Отмена", callback_data="cancel_auth")
    
    # Распределяем по рядам: 3x3, потом 3, потом 1
    builder.adjust(3, 3, 3, 3, 1)
    
    return builder.as_markup()


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура только с кнопкой отмены"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )
    return keyboard


def get_collections_keyboard(collections: List[Dict]) -> InlineKeyboardMarkup:
    """Клавиатура для списка коллекций"""
    builder = InlineKeyboardBuilder()
    
    for col in collections[:10]:  # Показываем первые 10
        status = "✅" if col['enabled'] else "❌"
        builder.button(
            text=f"{status} {col['name'][:30]}",
            callback_data=f"collection_{col['name']}"
        )
    
    builder.button(text="➕ Добавить коллекцию", callback_data="add_collection")
    builder.button(text="🔙 Назад", callback_data="back_to_main")
    builder.adjust(1)  # По одной кнопке в ряд
    
    return builder.as_markup()


def get_collection_actions_keyboard(collection_name: str) -> InlineKeyboardMarkup:
    """Клавиатура действий с коллекцией"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="🔄 Вкл/Выкл", callback_data=f"toggle_{collection_name}")
    builder.button(text="💰 Изменить цену", callback_data=f"price_{collection_name}")
    builder.button(text="❌ Удалить", callback_data=f"delete_{collection_name}")
    builder.button(text="🔙 Назад", callback_data="back_to_collections")
    builder.adjust(2, 1, 1)
    
    return builder.as_markup()


def get_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="⏱ Интервал проверки", callback_data="setting_interval")
    builder.button(text="💰 Настройки автопокупки", callback_data="setting_autobuy")
    builder.button(text="🔄 Повторы при ошибках", callback_data="setting_retries")
    builder.button(text="📊 Сбросить настройки", callback_data="setting_reset")
    builder.button(text="🔙 Назад", callback_data="back_to_main")
    builder.adjust(2, 2, 1)
    
    return builder.as_markup()


def get_autobuy_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек автопокупки"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="🔄 Автопокупка", callback_data="autobuy_toggle")
    builder.button(text="💰 При изменении цены", callback_data="pricechange_toggle")
    builder.button(text="✅ Подтверждение", callback_data="confirm_toggle")
    builder.button(text="📊 Макс. покупок/день", callback_data="set_max_purchases")
    builder.button(text="🔙 Назад", callback_data="back_to_settings")
    builder.adjust(2, 2, 1)
    
    return builder.as_markup()


def get_confirm_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура подтверждения"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data="confirm_yes")
    builder.button(text="❌ Нет", callback_data="confirm_no")
    builder.adjust(2)
    return builder.as_markup()


def get_purchase_confirm_keyboard(gift_id: str) -> InlineKeyboardMarkup:
    """Клавиатура для подтверждения покупки NFT"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Купить", callback_data=f"buy_confirm_{gift_id}")
    builder.button(text="❌ Отмена", callback_data=f"buy_cancel_{gift_id}")
    builder.adjust(2)
    return builder.as_markup()


# ==================== ОБРАБОТЧИКИ АВТОРИЗАЦИИ ====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    
    welcome_text = (
        f"👋 *Привет, {first_name}!*\n\n"
        "🤖 *MRKT Monitoring Bot*\n\n"
        "Этот бот поможет вам отслеживать новые NFT на MRKT\n\n"
        "📌 *Что нужно сделать:*\n"
        "1️⃣ Войти (кнопка ниже)\n"
        "2️⃣ Добавить коллекции для отслеживания\n"
        "3️⃣ Запустить мониторинг\n\n"
        "Используйте кнопки меню для навигации."
    )
    
    await message.answer(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


@dp.message(F.text == "🔑 Войти")
async def handle_login(message: Message, state: FSMContext):
    """Начало процесса авторизации"""
    user_id = message.from_user.id
    
    print(f"[BOT] handle_login для user_id {user_id}")
    
    # Проверяем, может уже авторизован
    status = await telethon_auth.check_auth_status(user_id)
    
    if status['is_authorized']:
        user_info = status['user_info']
        await message.answer(
            f"✅ <b>Вы уже авторизованы!</b>\n\n"
            f"👤 <b>Аккаунт:</b> {user_info['first_name']}\n"
            f"📱 <b>Телефон:</b> {user_info['phone']}\n"
            f"🆔 <b>ID:</b> {user_info['id']}\n\n"
            f"Можете запустить мониторинг или настроить коллекции.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Начинаем процесс авторизации
    await state.set_state(AuthStates.waiting_phone)
    
    await message.answer(
        "📱 <b>Авторизация</b>\n\n"
        "<b>Как ввести:</b>\n"
        "1️⃣ Нажмите кнопку «📱 Отправить контакт»\n"
        "2️⃣ Или введите номер вручную в формате <code>+79123456789</code>\n\n"
        "<i>🔒 Ваш номер используется только для регистрацит и не передается третьим лицам.</i>",
        parse_mode="HTML",
        reply_markup=get_phone_keyboard()
    )


# Обработка контакта
@dp.message(AuthStates.waiting_phone, F.contact)
async def process_contact(message: Message, state: FSMContext):
    """Обработка полученного контакта"""
    user_id = message.from_user.id
    contact = message.contact
    
    print(f"[BOT] process_contact для user_id {user_id}")
    
    # Проверяем, что контакт принадлежит пользователю
    if contact.user_id != user_id:
        await message.answer(
            "❌ Пожалуйста, отправьте свой собственный контакт.",
            reply_markup=get_phone_keyboard()
        )
        return
    
    phone = contact.phone_number
    if not phone.startswith('+'):
        phone = '+' + phone
    
    await message.answer(
        "✅ Контакт получен! Обрабатываю номер...",
        reply_markup=ReplyKeyboardRemove()
    )
    
    await process_phone_number(message, phone, state)


# Обработка ручного ввода номера
@dp.message(AuthStates.waiting_phone, F.text)
async def process_phone_text(message: Message, state: FSMContext):
    """Обработка ручного ввода номера"""
    user_id = message.from_user.id
    phone = message.text.strip()
    
    print(f"[BOT] process_phone_text для user_id {user_id}, phone={phone}")
    
    # Проверяем, не нажал ли пользователь "Отмена"
    if phone == "❌ Отмена":
        await state.clear()
        await message.answer(
            "❌ Ввод номера отменен.",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Убираем пробелы, дефисы, скобки
    phone = phone.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    
    # Проверяем формат номера
    if not phone.startswith('+'):
        phone = '+' + phone
    
    # Базовая валидация
    if not phone[1:].isdigit() or len(phone) < 10:
        await message.answer(
            "❌ <b>Неверный формат номера</b>\n\n"
            "Правильный формат: <code>+79123456789</code>\n"
            "Попробуйте еще раз или нажмите «❌ Отмена»:",
            parse_mode="HTML",
            reply_markup=get_phone_keyboard()
        )
        return
    
    await message.answer(
        "✅ Номер принят! Отправляю запрос ...",
        reply_markup=ReplyKeyboardRemove()
    )
    
    await process_phone_number(message, phone, state)


async def process_phone_number(message: Message, phone: str, state: FSMContext):
    """Обработка номера телефона и отправка кода"""
    user_id = message.from_user.id
    
    # Показываем статус
    status_msg = await message.answer(
        "🔄 <b>Отправка шифра...</b>",
        parse_mode="HTML"
    )
    
    # Запускаем процесс авторизации через Pyrogram
    result = await telethon_auth.start_phone_auth(user_id, phone)
    
    print(f"[BOT] result from start_phone_auth: {result}")
    
    if not result['success']:
        await status_msg.delete()
        await message.answer(
            f"{result['error']}\n\n"
            f"Попробуйте еще раз или нажмите «Отмена».",
            parse_mode="HTML",
            reply_markup=get_phone_keyboard()
        )
        return
    
    if result.get('already_authorized'):
        await status_msg.delete()
        await message.answer(
            f"✅ <b>Авторизация успешна!</b>\n\n"
            f"Можете запустить мониторинг или настроить коллекции.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    # Удаляем статусное сообщение
    await status_msg.delete()
    
    # Сохраняем данные в состоянии FSM
    await state.update_data(
        phone=phone,
        phone_code_hash=result['phone_code_hash'],
        timeout=result.get('timeout', 60)
    )
    
    # Переходим к состоянию ввода кода
    await state.set_state(AuthStates.waiting_code)
    
    print(f"[BOT] Переходим к вводу кода для user_id {user_id}")
    
    # Сразу показываем инлайн-клавиатуру для ввода кода
    await message.answer(
        f"📲 <b>Введите шифр</b>\n\n"
        f"Код отправлен на номер <code>{phone}</code>\n\n"
        f"<b>Код:</b> <code>—</code>\n\n"
        f"<i>Нажимайте цифры на клавиатуре ниже</i>\n",
        parse_mode="HTML",
        reply_markup=get_code_keyboard()
    )


# Обработчик нажатий на кнопки кода
@dp.callback_query(AuthStates.waiting_code, F.data.startswith("code_digit_"))
async def process_code_digit(callback: CallbackQuery, state: FSMContext):
    """Обработка нажатий на кнопки кода"""
    user_id = callback.from_user.id
    action = callback.data.replace("code_digit_", "")
    
    print(f"[BOT] process_code_digit для user_id {user_id}, action={action}")
    
    data = await state.get_data()
    current_code = data.get('entered_code', '')
    
    if action == "backspace":
        current_code = current_code[:-1]
    elif action == "confirm":
        if len(current_code) < 4:
            await callback.answer("❌ Код слишком короткий (минимум 4 цифры)", show_alert=True)
            return
        
        # Подтверждаем код
        await state.update_data(entered_code=current_code)
        
        # Сохраняем chat_id для отправки сообщения
        chat_id = callback.message.chat.id
        
        print(f"[BOT] Подтверждение кода {current_code} для user_id {user_id}")
        
        # Удаляем сообщение с клавиатурой
        await callback.message.delete()
        
        # Отправляем новое сообщение о проверке кода
        status_msg = await callback.bot.send_message(
            chat_id, 
            "🔄 <b>Проверка...</b>",
            parse_mode="HTML"
        )
        
        # Проверяем код с правильным user_id
        result = await telethon_auth.verify_code(user_id, current_code)
        
        print(f"[BOT] verify_code result: {result}")
        
        if not result['success']:
            await status_msg.delete()
            
            # Проверяем, не истекла ли сессия
            if "сессия устарела" in result['error'].lower():
                await state.clear()
                await callback.bot.send_message(
                    chat_id,
                    f"{result['error']}\n\n"
                    f"Нажмите «🔑 Войти» чтобы начать заново.",
                    parse_mode="HTML",
                    reply_markup=get_main_keyboard()
                )
                await callback.answer()
                return
            
            # Возвращаемся к вводу кода
            data = await state.get_data()
            await callback.bot.send_message(
                chat_id,
                f"{result['error']}\n\n"
                f"Попробуйте еще раз:",
                parse_mode="HTML",
                reply_markup=get_code_keyboard()
            )
            await callback.answer()
            return
        
        if result.get('requires_2fa'):
            # Требуется 2FA пароль
            await state.set_state(AuthStates.waiting_2fa)
            hint = result.get('hint', '')
            hint_text = f"\n💡 <b>Подсказка:</b> <code>{hint}</code>" if hint else ""
            
            await status_msg.delete()
            await callback.bot.send_message(
                chat_id,
                f"🔐 <b>Требуется доп. данные</b>{hint_text}\n\n"
                f"Введите:",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard()
            )
            await callback.answer()
            return
        
        # Успешная авторизация
        await status_msg.delete()
        await finalize_login(callback, user_id, state, chat_id)
        await callback.answer()
        return
        
    elif action == "0" or action.isdigit():
        if len(current_code) < 10:
            current_code += action
    
    # Обновляем состояние
    await state.update_data(entered_code=current_code)
    
    # Обновляем сообщение с отображением введенного кода
    display_code = current_code or "—"
    await callback.message.edit_text(
        f"📲 <b>Введите шифр</b>\n\n"
        f"<b>Код:</b> <code>{display_code}</code>\n\n"
        f"<i>Нажимайте цифры на клавиатуре ниже</i>\n",
        parse_mode="HTML",
        reply_markup=get_code_keyboard(current_code)
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel_auth")
async def cancel_auth(callback: CallbackQuery, state: FSMContext):
    """Отмена авторизации"""
    user_id = callback.from_user.id
    
    print(f"[BOT] cancel_auth для user_id {user_id}")
    
    # Очищаем временные данные
    await telethon_auth.cleanup_temp_data(user_id)
    
    await state.clear()
    
    await callback.message.edit_text(
        "❌ Авторизация отменена."
    )
    await callback.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()


@dp.message(AuthStates.waiting_2fa, F.text)
async def process_2fa(message: Message, state: FSMContext):
    """Обработка 2FA пароля"""
    user_id = message.from_user.id
    password = message.text.strip()
    
    print(f"[BOT] process_2fa для user_id {user_id}")
    
    # Скрываем введенный пароль (удаляем сообщение пользователя)
    await message.delete()
    
    status_msg = await message.answer(
        "🔄 <b>Проверка...</b>",
        parse_mode="HTML"
    )
    
    result = await telethon_auth.verify_password(user_id, password)
    
    print(f"[BOT] verify_password result: {result}")
    
    if not result['success']:
        await status_msg.delete()
        
        # Проверяем, не истекла ли сессия
        if "сессия устарела" in result['error'].lower():
            await state.clear()
            await message.answer(
                f"{result['error']}\n\n"
                f"Нажмите «🔑 Войти» чтобы начать заново.",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
            return
        
        # Пробуем снова
        await message.answer(
            f"{result['error']}\n\n"
            f"Попробуйте еще раз:",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    # Успешная авторизация
    await status_msg.delete()
    await finalize_login(message, user_id, state)


@dp.message(F.text == "❌ Отмена")
async def handle_cancel(message: Message, state: FSMContext):
    """Обработка кнопки отмены из reply-клавиатуры"""
    current_state = await state.get_state()
    
    if current_state is None:
        await message.answer(
            "❌ Нет активного процесса для отмены.",
            reply_markup=get_main_keyboard()
        )
        return
    
    user_id = message.from_user.id
    
    # Очищаем временные данные авторизации если есть
    await telethon_auth.cleanup_temp_data(user_id)
    
    await state.clear()
    
    await message.answer(
        "❌ Действие отменено.",
        reply_markup=get_main_keyboard()
    )


async def finalize_login(callback_or_message, user_id: int, state: FSMContext, chat_id: int = None):
    """Завершение авторизации"""
    
    print(f"[BOT] finalize_login для user_id {user_id}")
    
    # Определяем, откуда вызвана функция
    if isinstance(callback_or_message, CallbackQuery):
        bot_instance = callback_or_message.bot
        if chat_id is None:
            chat_id = callback_or_message.message.chat.id
    else:
        bot_instance = callback_or_message.bot
        if chat_id is None:
            chat_id = callback_or_message.chat.id
    
    # Даем небольшую задержку для сохранения токена
    await asyncio.sleep(1)
    
    # Получаем токен из хранилища
    token = telethon_auth.user_tokens.get(user_id)
    
    print(f"[BOT] token from telethon_auth.user_tokens: {token}")
    
    if token:
        # Сохраняем токен
        save_token(token)
        user_tokens[user_id] = token
        
        # Получаем информацию о пользователе для приветствия
        status = await telethon_auth.check_auth_status(user_id)
        user_info = status.get('user_info', {})
        first_name = user_info.get('first_name', 'Пользователь')
        
        await bot_instance.send_message(
            chat_id,
            f"✅ <b>Авторизация успешна!</b>\n\n"
            f"👋 Добро пожаловать, {first_name}!\n\n"
            f"📋 <b>Доступные действия:</b>\n"
            f"• Добавить коллекции для отслеживания через «📋 Мои коллекции»\n"
            f"• Настроить параметры в «⚙️ Настройки»\n"
            f"• Запустить мониторинг кнопкой «▶️ Запустить мониторинг»",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    else:
        # Пробуем еще раз через 2 секунды
        await asyncio.sleep(2)
        token = telethon_auth.user_tokens.get(user_id)
        
        if token:
            save_token(token)
            user_tokens[user_id] = token
            await bot_instance.send_message(
                chat_id,
                f"✅ <b>Авторизация успешна!</b>\n\n"
                f"👋 Добро пожаловать!\n\n"
                f"📋 Используйте меню для навигации.",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
        else:
            await bot_instance.send_message(
                chat_id,
                f"✅ <b>Авторизация в Telegram успешна!</b>\n\n"
                f"⚠️ Но не удалось получить токен MRKT.\n"
                f"Попробуйте позже или войдите снова.",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
    
    await state.clear()


@dp.message(F.text == "🚪 Выйти")
async def handle_logout(message: Message):
    """Выход из аккаунта"""
    user_id = message.from_user.id
    
    confirm_keyboard = InlineKeyboardBuilder()
    confirm_keyboard.button(text="✅ Да, выйти", callback_data="confirm_logout")
    confirm_keyboard.button(text="❌ Нет", callback_data="cancel_logout")
    confirm_keyboard.adjust(2)
    
    await message.answer(
        "⚠️ <b>Вы уверены, что хотите выйти?</b>\n\n",
        parse_mode="HTML",
        reply_markup=confirm_keyboard.as_markup()
    )


@dp.callback_query(F.data == "confirm_logout")
async def confirm_logout(callback: CallbackQuery):
    """Подтверждение выхода"""
    user_id = callback.from_user.id
    
    await callback.message.edit_text(
        "🔄 <b>Выполняется выход...</b>",
        parse_mode="HTML"
    )
    
    success = await telethon_auth.logout_user(user_id)
    
    # Очищаем старые данные
    if user_id in user_tokens:
        del user_tokens[user_id]
    
    if success:
        await callback.message.edit_text(
            "✅ <b>Вы успешно вышли из аккаунта</b>\n\n"
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка при выходе</b>\n\n"
            "Но данные пользователя очищены."
        )
    
    await callback.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel_logout")
async def cancel_logout(callback: CallbackQuery):
    """Отмена выхода"""
    await callback.message.delete()
    await callback.message.answer(
        "Выход отменен.",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()


# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С ПОКУПКАМИ ====================

async def request_purchase_confirmation(user_id: int, gift_data: Dict, price: float, gift_id: str) -> bool:
    """Запросить подтверждение покупки у пользователя"""
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Купить", callback_data=f"buy_confirm_{gift_id}")
        builder.button(text="❌ Отмена", callback_data=f"buy_cancel_{gift_id}")
        builder.adjust(2)
        
        # Сохраняем данные для подтверждения
        pending_confirmations[gift_id] = {
            'user_id': user_id,
            'gift_data': gift_data,
            'price': price,
            'timestamp': time.time()
        }
        
        text = (
            f"🛒 <b>Подтверждение покупки</b>\n\n"
            f"📦 <b>Коллекция:</b> {gift_data.get('collectionName', 'Неизвестно')}\n"
            f"📦 <b>Модель:</b> {gift_data.get('modelName', 'Неизвестно')}\n"
            f"💰 <b>Цена:</b> {price} TON\n"
            f"🔢 <b>Номер:</b> #{gift_data.get('number', '?')}\n\n"
            f"Подтвердите покупку:"
        )
        
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=builder.as_markup())
        return True
    except Exception as e:
        logger.error(f"Failed to send confirmation request: {e}")
        return False


async def send_purchase_notification(user_id: int, purchase_data: Dict = None, success: bool = False, error_msg: str = None):
    """Отправка уведомления о результате покупки"""
    try:
        if success and purchase_data:
            text = (
                f"✅ <b>NFT УСПЕШНО КУПЛЕН!</b>\n\n"
                f"📦 <b>Коллекция:</b> {purchase_data.get('collection_name', 'Неизвестно')}\n"
                f"📦 <b>Модель:</b> {purchase_data.get('model_name', 'Неизвестно')}\n"
                f"💰 <b>Цена:</b> {purchase_data.get('price_ton', 0)} TON\n"
                f"🔢 <b>Номер:</b> #{purchase_data.get('number', '?')}\n"
                f"🔗 <b>Ссылка:</b> {purchase_data.get('link', '#')}"
            )
        else:
            if error_msg and ("balance" in error_msg.lower() or "insufficient" in error_msg.lower() or "баланс" in error_msg.lower()):
                error_text = "❌ Недостаточно баланса TON для покупки"
            elif error_msg and ("already" in error_msg.lower() or "уже" in error_msg.lower()):
                error_text = "❌ NFT уже куплен или не в продаже"
            else:
                error_text = f"❌ Не удалось купить NFT: {error_msg if error_msg else 'Неизвестная ошибка'}"
            
            text = error_text
        
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to send purchase notification: {e}")

@dp.callback_query(F.data.startswith("buy_confirm_"))
async def process_buy_confirm(callback: CallbackQuery):
    """Подтверждение покупки NFT"""
    gift_id = callback.data.replace("buy_confirm_", "")
    user_id = callback.from_user.id
    
    # Получаем данные из временного хранилища
    confirm_data = pending_confirmations.get(gift_id)
    if not confirm_data or confirm_data['user_id'] != user_id:
        await callback.answer("❌ Запрос устарел или не найден", show_alert=True)
        await callback.message.delete()
        return
    
    # Удаляем из хранилища
    del pending_confirmations[gift_id]
    
    await callback.message.edit_text("🔄 Выполняю покупку...")
    
    # Получаем токен пользователя
    token = user_tokens.get(user_id)
    if not token:
        await callback.message.edit_text("❌ Ошибка авторизации. Войдите в аккаунт заново.")
        return
    
    # Создаем покупателя и пробуем купить
    buyer = MRKTBuyer(token, db)
    price_nano = int(confirm_data['price'] * 1_000_000_000)
    
    try:
        success, purchase_data, error_msg = await buyer.buy_nft(gift_id, price_nano)
        # Отправляем уведомление
        await send_purchase_notification(user_id, purchase_data if success else None, success, error_msg)
    except Exception as e:
        await send_purchase_notification(user_id, None, False, str(e))
    
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_cancel_"))
async def process_buy_cancel(callback: CallbackQuery):
    """Отмена покупки NFT"""
    gift_id = callback.data.replace("buy_cancel_", "")
    user_id = callback.from_user.id
    
    # Удаляем из хранилища
    if gift_id in pending_confirmations:
        del pending_confirmations[gift_id]
    
    await callback.message.edit_text("❌ Покупка отменена.")
    await callback.answer()


# ==================== ОБРАБОТЧИКИ КОЛЛЕКЦИЙ ====================

@dp.message(F.text == "📋 Мои коллекции")
async def show_collections(message: Message):
    """Показать список коллекций"""
    collections = db.get_all_collections()
    
    if not collections:
        text = (
            "📋 *У вас нет добавленных коллекций*\n\n"
            "Добавьте коллекцию, нажав кнопку ниже.\n\n"
            "Формат: сначала название, потом цена\n"
            "Пример: `Lunar Snake:4.04` или просто название"
        )
        
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Добавить коллекцию", callback_data="add_collection")
        builder.button(text="🔙 Назад", callback_data="back_to_main")
        builder.adjust(1)
        
        await message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        return
    
    text = "📋 *Ваши коллекции*\n\n"
    
    for col in collections:
        status = "✅" if col['enabled'] else "❌"
        price = col['max_price']
        if price == float('inf'):
            price_str = "∞"
        else:
            price_str = f"{price} TON"
        text += f"{status} *{col['name']}* - {price_str}\n"
    
    await message.answer(
        text,
        parse_mode="Markdown",
        reply_markup=get_collections_keyboard(collections)
    )


@dp.callback_query(F.data == "add_collection")
async def add_collection_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления коллекции"""
    await state.set_state(AddCollection.waiting_for_name)
    await callback.message.edit_text(
        "📝 *Добавление коллекции*\n\n"
        "Введите *название коллекции*:\n"
        "Например: `Lunar Snake`",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(AddCollection.waiting_for_name)
async def add_collection_name(message: Message, state: FSMContext):
    """Получение названия коллекции"""
    collection_name = message.text.strip()
    
    # Проверяем, не существует ли уже такая коллекция
    if db.collection_exists(collection_name):
        await message.answer(
            f"❌ Коллекция *{collection_name}* уже существует!",
            parse_mode="Markdown"
        )
        await state.clear()
        return
    
    # Сохраняем название в состоянии
    await state.update_data(collection_name=collection_name)
    await state.set_state(AddCollection.waiting_for_price)
    
    await message.answer(
        f"📝 Коллекция: *{collection_name}*\n\n"
        f"Введите *максимальную цену* в TON:\n"
        f"• Если цена не важна, введите `0` или `-`\n"
        f"• Например: `4.04`",
        parse_mode="Markdown"
    )


@dp.message(AddCollection.waiting_for_price)
async def add_collection_price(message: Message, state: FSMContext):
    """Получение цены коллекции"""
    price_text = message.text.strip()
    
    # Обработка цены
    if price_text in ['0', '-', '∞', 'inf']:
        max_price = float('inf')
    else:
        try:
            max_price = float(price_text.replace(',', '.'))
        except ValueError:
            await message.answer(
                "❌ Неверный формат цены. Введите число (например, 4.04)\n"
                "Или введите 0, если цена не важна:"
            )
            return
    
    # Получаем название из состояния
    data = await state.get_data()
    collection_name = data['collection_name']
    
    # Добавляем коллекцию в БД
    if db.add_collection(collection_name, max_price, True):
        await message.answer(
            f"✅ Коллекция *{collection_name}* добавлена!\n"
            f"💰 Макс. цена: `{max_price if max_price != float('inf') else '∞'} TON`",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"❌ Не удалось добавить коллекцию *{collection_name}*",
            parse_mode="Markdown"
        )
    
    await state.clear()
    
    # Показываем обновленный список коллекций
    collections = db.get_all_collections()
    await message.answer(
        "📋 *Ваши коллекции*",
        parse_mode="Markdown",
        reply_markup=get_collections_keyboard(collections)
    )


@dp.callback_query(F.data.startswith("collection_"))
async def collection_action(callback: CallbackQuery):
    """Действие с конкретной коллекцией"""
    collection_name = callback.data.replace("collection_", "")
    collection = db.get_collection(collection_name)
    
    if not collection:
        await callback.answer("❌ Коллекция не найдена", show_alert=True)
        return
    
    # Получаем статистику покупок по этой коллекции
    purchases = db.get_purchases_by_collection(collection_name, 5)
    
    text = f"📋 *Коллекция: {collection_name}*\n\n"
    text += f"• *Макс. цена:* "
    if collection['max_price'] == float('inf'):
        text += "`∞`\n"
    else:
        text += f"`{collection['max_price']} TON`\n"
    text += f"• *Статус:* {'✅ Активна' if collection['enabled'] else '❌ Неактивна'}\n"
    
    if purchases:
        text += f"\n*Последние покупки:*\n"
        for p in purchases[:3]:
            text += f"• #{p['number']} - `{p['price_ton']} TON`\n"
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_collection_actions_keyboard(collection_name)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_collection(callback: CallbackQuery):
    """Включение/выключение коллекции"""
    collection_name = callback.data.replace("toggle_", "")
    
    new_status = db.toggle_collection(collection_name)
    
    if new_status is not None:
        status_text = "включена" if new_status else "выключена"
        await callback.answer(f"✅ Коллекция {status_text}", show_alert=False)
    else:
        await callback.answer("❌ Коллекция не найдена", show_alert=True)
    
    # Обновляем отображение
    await collection_action(callback)


@dp.callback_query(F.data.startswith("price_"))
async def price_collection_start(callback: CallbackQuery, state: FSMContext):
    """Начало изменения цены коллекции"""
    collection_name = callback.data.replace("price_", "")
    collection = db.get_collection(collection_name)
    
    if not collection:
        await callback.answer("❌ Коллекция не найдена", show_alert=True)
        return
    
    await state.set_state(UpdatePrice.waiting_for_price)
    await state.update_data(collection_name=collection_name)
    
    current_price = collection['max_price']
    price_str = f"{current_price} TON" if current_price != float('inf') else "∞"
    
    await callback.message.edit_text(
        f"💰 *Изменение цены*\n\n"
        f"Коллекция: *{collection_name}*\n"
        f"Текущая цена: `{price_str}`\n\n"
        f"Введите новую цену в TON:\n"
        f"• Например: `5.5`\n"
        f"• Или `0` если цена не важна",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(UpdatePrice.waiting_for_price)
async def price_collection_update(message: Message, state: FSMContext):
    """Обновление цены коллекции"""
    price_text = message.text.strip()
    
    # Обработка цены
    if price_text in ['0', '-', '∞', 'inf']:
        new_price = float('inf')
    else:
        try:
            new_price = float(price_text.replace(',', '.'))
        except ValueError:
            await message.answer(
                "❌ Неверный формат цены. Введите число (например, 5.5)\n"
                "Или введите 0, если цена не важна:"
            )
            return
    
    # Получаем название из состояния
    data = await state.get_data()
    collection_name = data['collection_name']
    
    # Обновляем цену
    if db.update_collection(collection_name, max_price=new_price):
        price_str = f"{new_price} TON" if new_price != float('inf') else "∞"
        await message.answer(
            f"✅ Цена для *{collection_name}* обновлена до `{price_str}`",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"❌ Не удалось обновить цену для *{collection_name}*",
            parse_mode="Markdown"
        )
    
    await state.clear()
    
    # Показываем обновленный список коллекций
    collections = db.get_all_collections()
    await message.answer(
        "📋 *Ваши коллекции*",
        parse_mode="Markdown",
        reply_markup=get_collections_keyboard(collections)
    )


@dp.callback_query(F.data.startswith("delete_"))
async def delete_collection(callback: CallbackQuery):
    """Удаление коллекции"""
    collection_name = callback.data.replace("delete_", "")
    
    # Запрашиваем подтверждение
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"confirm_delete_{collection_name}")
    builder.button(text="❌ Нет", callback_data=f"collection_{collection_name}")
    builder.adjust(2)
    
    await callback.message.edit_text(
        f"⚠️ *Вы уверены, что хотите удалить коллекцию*\n"
        f"*{collection_name}*?",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_collection(callback: CallbackQuery):
    """Подтверждение удаления коллекции"""
    collection_name = callback.data.replace("confirm_delete_", "")
    
    if db.delete_collection(collection_name):
        await callback.answer(f"✅ Коллекция {collection_name} удалена", show_alert=True)
    else:
        await callback.answer("❌ Не удалось удалить коллекцию", show_alert=True)
    
    # Возвращаемся к списку коллекций
    await back_to_collections(callback)


# ==================== ОБРАБОТЧИКИ НАСТРОЕК ====================

@dp.message(F.text == "⚙️ Настройки")
async def show_settings(message: Message):
    """Показать настройки"""
    settings = db.get_all_settings()
    
    text = "⚙️ *Текущие настройки*\n\n"
    
    setting_names = {
        'check_interval': '⏱ Интервал проверки',
        'auto_buy': '💰 Автопокупка',
        'auto_buy_on_price_change': '📈 Покупка при изменении цены',
        'confirm_before_purchase': '✅ Подтверждение перед покупкой',
        'max_daily_purchases': '📊 Макс. покупок в день',
        'max_retries': '🔄 Макс. повторов',
        'retry_delay': '⏳ Задержка повтора',
        'connection_timeout': '⏱ Таймаут подключения'
    }
    
    for key, display_name in setting_names.items():
        if key in settings:
            value = settings[key]
            if isinstance(value, bool):
                value = "✅ Вкл" if value else "❌ Выкл"
            elif isinstance(value, float):
                if key in ['check_interval', 'retry_delay']:
                    value = f"{value} сек"
                elif key == 'connection_timeout':
                    value = f"{value} сек"
                else:
                    value = f"{value}"
            text += f"• *{display_name}*: `{value}`\n"
    
    await message.answer(
        text,
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard()
    )


@dp.callback_query(F.data == "setting_interval")
async def setting_interval(callback: CallbackQuery, state: FSMContext):
    """Изменение интервала проверки"""
    await state.set_state(Settings.waiting_for_interval)
    await callback.message.edit_text(
        "⏱ *Настройка интервала проверки*\n\n"
        f"Текущее значение: `{db.get_setting('check_interval', 0.5)} сек`\n\n"
        "Введите новый интервал в секундах (например: 0.5, 1, 2):",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(Settings.waiting_for_interval)
async def setting_interval_update(message: Message, state: FSMContext):
    """Обновление интервала проверки"""
    try:
        interval = float(message.text.strip().replace(',', '.'))
        if interval <= 0:
            raise ValueError("Интервал должен быть положительным")
        
        db.update_setting('check_interval', interval)
        
        await message.answer(
            f"✅ Интервал проверки обновлен до `{interval} сек`",
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введите положительное число (например: 0.5, 1, 2):"
        )
        return
    
    await state.clear()
    await show_settings(message)


@dp.callback_query(F.data == "setting_autobuy")
async def setting_autobuy(callback: CallbackQuery):
    """Меню настроек автопокупки"""
    settings = db.get_all_settings()
    
    text = "💰 *Настройки автопокупки*\n\n"
    text += f"• *Автопокупка:* `{'✅ Вкл' if settings.get('auto_buy', False) else '❌ Выкл'}`\n"
    text += f"• *Покупка при изменении цены:* `{'✅ Вкл' if settings.get('auto_buy_on_price_change', False) else '❌ Выкл'}`\n"
    text += f"• *Подтверждение перед покупкой:* `{'✅ Вкл' if settings.get('confirm_before_purchase', False) else '❌ Выкл'}`\n"
    text += f"• *Макс. покупок в день:* `{settings.get('max_daily_purchases', 10)}`\n"
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_autobuy_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "autobuy_toggle")
async def toggle_autobuy_setting(callback: CallbackQuery):
    """Переключение автопокупки"""
    current = db.get_setting('auto_buy', False)
    db.update_setting('auto_buy', not current)
    
    await callback.answer(
        f"✅ Автопокупка {'включена' if not current else 'выключена'}",
        show_alert=False
    )
    await setting_autobuy(callback)


@dp.callback_query(F.data == "pricechange_toggle")
async def toggle_price_change_setting(callback: CallbackQuery):
    """Переключение покупки при изменении цены"""
    current = db.get_setting('auto_buy_on_price_change', False)
    db.update_setting('auto_buy_on_price_change', not current)
    
    await callback.answer(
        f"✅ Покупка при изменении цены {'включена' if not current else 'выключена'}",
        show_alert=False
    )
    await setting_autobuy(callback)


@dp.callback_query(F.data == "confirm_toggle")
async def toggle_confirm_setting(callback: CallbackQuery):
    """Переключение подтверждения перед покупкой"""
    current = db.get_setting('confirm_before_purchase', False)
    db.update_setting('confirm_before_purchase', not current)
    
    await callback.answer(
        f"✅ Подтверждение {'включено' if not current else 'выключено'}",
        show_alert=False
    )
    await setting_autobuy(callback)


@dp.callback_query(F.data == "set_max_purchases")
async def set_max_purchases_start(callback: CallbackQuery, state: FSMContext):
    """Начало настройки максимального количества покупок"""
    await state.set_state(Settings.waiting_for_max_purchases)
    await callback.message.edit_text(
        "📊 *Максимальное количество покупок в день*\n\n"
        f"Текущее значение: `{db.get_setting('max_daily_purchases', 10)}`\n\n"
        "Введите новое значение (целое число):",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(Settings.waiting_for_max_purchases)
async def set_max_purchases_update(message: Message, state: FSMContext):
    """Обновление максимального количества покупок"""
    try:
        max_purchases = int(message.text.strip())
        if max_purchases <= 0:
            raise ValueError("Количество должно быть положительным")
        
        db.update_setting('max_daily_purchases', max_purchases)
        
        await message.answer(
            f"✅ Макс. покупок в день обновлено до `{max_purchases}`",
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введите целое положительное число:"
        )
        return
    
    await state.clear()
    await show_settings(message)


@dp.callback_query(F.data == "setting_retries")
async def setting_retries(callback: CallbackQuery, state: FSMContext):
    """Настройка повторов при ошибках"""
    text = (
        "🔄 *Настройки повторов при ошибках*\n\n"
        f"• *Макс. количество повторов:* `{db.get_setting('max_retries', 5)}`\n"
        f"• *Задержка между повторами:* `{db.get_setting('retry_delay', 1.0)} сек`\n"
        f"• *Таймаут подключения:* `{db.get_setting('connection_timeout', 15)} сек`\n\n"
        "Выберите параметр для изменения:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Макс. повторов", callback_data="set_retries_count")
    builder.button(text="⏳ Задержка повтора", callback_data="set_retry_delay")
    builder.button(text="⏱ Таймаут", callback_data="set_timeout")
    builder.button(text="🔙 Назад", callback_data="back_to_settings")
    builder.adjust(2, 2, 1)
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data == "set_retries_count")
async def set_retries_count_start(callback: CallbackQuery, state: FSMContext):
    """Начало настройки количества повторов"""
    await state.set_state(Settings.waiting_for_retries)
    await callback.message.edit_text(
        "🔄 *Максимальное количество повторов*\n\n"
        f"Текущее значение: `{db.get_setting('max_retries', 5)}`\n\n"
        "Введите новое значение (целое число, например: 3, 5, 10):",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(Settings.waiting_for_retries)
async def set_retries_count_update(message: Message, state: FSMContext):
    """Обновление количества повторов"""
    try:
        retries = int(message.text.strip())
        if retries <= 0:
            raise ValueError("Количество должно быть положительным")
        
        db.update_setting('max_retries', retries)
        
        await message.answer(
            f"✅ Макс. количество повторов обновлено до `{retries}`",
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введите целое положительное число:"
        )
        return
    
    await state.clear()
    await show_settings(message)


@dp.callback_query(F.data == "set_retry_delay")
async def set_retry_delay_start(callback: CallbackQuery, state: FSMContext):
    """Начало настройки задержки повтора"""
    await state.set_state(Settings.waiting_for_retry_delay)
    await callback.message.edit_text(
        "⏳ *Задержка между повторами*\n\n"
        f"Текущее значение: `{db.get_setting('retry_delay', 1.0)} сек`\n\n"
        "Введите новое значение в секундах (например: 0.5, 1, 2):",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(Settings.waiting_for_retry_delay)
async def set_retry_delay_update(message: Message, state: FSMContext):
    """Обновление задержки повтора"""
    try:
        delay = float(message.text.strip().replace(',', '.'))
        if delay <= 0:
            raise ValueError("Задержка должна быть положительной")
        
        db.update_setting('retry_delay', delay)
        
        await message.answer(
            f"✅ Задержка повтора обновлена до `{delay} сек`",
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введите положительное число (например: 0.5, 1, 2):"
        )
        return
    
    await state.clear()
    await show_settings(message)


@dp.callback_query(F.data == "set_timeout")
async def set_timeout_start(callback: CallbackQuery, state: FSMContext):
    """Начало настройки таймаута"""
    await state.set_state(Settings.waiting_for_timeout)
    await callback.message.edit_text(
        "⏱ *Таймаут подключения*\n\n"
        f"Текущее значение: `{db.get_setting('connection_timeout', 15)} сек`\n\n"
        "Введите новое значение в секундах (например: 10, 15, 30):",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(Settings.waiting_for_timeout)
async def set_timeout_update(message: Message, state: FSMContext):
    """Обновление таймаута"""
    try:
        timeout = int(message.text.strip())
        if timeout <= 0:
            raise ValueError("Таймаут должен быть положительным")
        
        db.update_setting('connection_timeout', timeout)
        
        await message.answer(
            f"✅ Таймаут подключения обновлен до `{timeout} сек`",
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введите целое положительное число (например: 10, 15, 30):"
        )
        return
    
    await state.clear()
    await show_settings(message)


@dp.callback_query(F.data == "setting_reset")
async def setting_reset(callback: CallbackQuery):
    """Сброс настроек к значениям по умолчанию"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, сбросить", callback_data="confirm_reset")
    builder.button(text="❌ Нет", callback_data="back_to_settings")
    builder.adjust(2)
    
    await callback.message.edit_text(
        "⚠️ *Вы уверены, что хотите сбросить все настройки к значениям по умолчанию?*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data == "confirm_reset")
async def confirm_reset(callback: CallbackQuery):
    """Подтверждение сброса настроек"""
    db.reset_settings_to_default()
    
    await callback.answer("✅ Настройки сброшены к значениям по умолчанию", show_alert=True)
    await back_to_settings(callback)


# ==================== ОБРАБОТЧИКИ CALLBACK ====================

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    """Возврат в главное меню"""
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "back_to_collections")
async def back_to_collections(callback: CallbackQuery):
    """Возврат к списку коллекций"""
    collections = db.get_all_collections()
    
    text = "📋 *Ваши коллекции*\n\n"
    for col in collections:
        status = "✅" if col['enabled'] else "❌"
        price = col['max_price']
        price_str = f"{price} TON" if price != float('inf') else "∞"
        text += f"{status} *{col['name']}* - {price_str}\n"
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_collections_keyboard(collections)
    )
    await callback.answer()


@dp.callback_query(F.data == "back_to_settings")
async def back_to_settings(callback: CallbackQuery):
    """Возврат к настройкам"""
    settings = db.get_all_settings()
    
    text = "⚙️ *Текущие настройки*\n\n"
    setting_names = {
        'check_interval': '⏱ Интервал проверки',
        'auto_buy': '💰 Автопокупка',
        'auto_buy_on_price_change': '📈 Покупка при изменении цены',
        'confirm_before_purchase': '✅ Подтверждение перед покупкой',
        'max_daily_purchases': '📊 Макс. покупок в день',
        'max_retries': '🔄 Макс. повторов',
        'retry_delay': '⏳ Задержка повтора',
        'connection_timeout': '⏱ Таймаут подключения'
    }
    
    for key, display_name in setting_names.items():
        if key in settings:
            value = settings[key]
            if isinstance(value, bool):
                value = "✅ Вкл" if value else "❌ Выкл"
            elif isinstance(value, float):
                if key in ['check_interval', 'retry_delay']:
                    value = f"{value} сек"
                elif key == 'connection_timeout':
                    value = f"{value} сек"
            text += f"• *{display_name}*: `{value}`\n"
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard()
    )
    await callback.answer()


# ==================== ФУНКЦИИ МОНИТОРИНГА ====================

@dp.message(F.text == "▶️ Запустить мониторинг")
async def start_monitoring(message: Message):
    """Запуск мониторинга"""
    user_id = message.from_user.id
    
    # Проверяем авторизацию
    if user_id not in user_tokens:
        await message.answer(
            "❌ *Сначала необходимо войти в аккаунт!*\n\n"
            "Нажмите кнопку '🔑 Войти'",
            parse_mode="Markdown"
        )
        return
    
    # Проверяем наличие коллекций
    collections = db.get_enabled_collections()
    if not collections:
        await message.answer(
            "❌ *Нет активных коллекций для отслеживания!*\n\n"
            "Добавьте коллекции через '📋 Мои коллекции'",
            parse_mode="Markdown"
        )
        return
    
    # Проверяем, не запущен ли уже мониторинг
    if monitoring_status.get(user_id, False):
        await message.answer(
            "⚠️ *Мониторинг уже запущен!*",
            parse_mode="Markdown"
        )
        return
    
    # Отправляем подтверждение
    text = (
        "🔄 *Запуск мониторинга...*\n\n"
        f"📊 *Отслеживаемые коллекции:* {len(collections)}\n"
        f"⏱ *Интервал проверки:* {db.get_setting('check_interval', 0.5)} сек\n"
        f"💰 *Автопокупка:* {'✅' if db.get_setting('auto_buy', False) else '❌'}\n\n"
        "Мониторинг будет работать в фоновом режиме. "
        "О найденных NFT вы будете получать уведомления."
    )
    
    await message.answer(text, parse_mode="Markdown")
    
    # Запускаем мониторинг
    token = user_tokens[user_id]
    monitoring_status[user_id] = True
    
    # Очищаем кэш перед запуском
    global seen_nfts
    seen_nfts.clear()
    
    # Запускаем мониторинг
    task = asyncio.create_task(run_monitoring(user_id, token))
    monitoring_tasks[user_id] = task


@dp.message(F.text == "⏹️ Остановить мониторинг")
async def stop_monitoring(message: Message):
    """Остановка мониторинга"""
    user_id = message.from_user.id
    
    if not monitoring_status.get(user_id, False):
        await message.answer(
            "⚠️ *Мониторинг не запущен*",
            parse_mode="Markdown"
        )
        return
    
    # Отменяем задачу
    if user_id in monitoring_tasks:
        monitoring_tasks[user_id].cancel()
        monitoring_status[user_id] = False
        del monitoring_tasks[user_id]
    
    await message.answer(
        "✅ *Мониторинг остановлен*",
        parse_mode="Markdown"
    )


async def run_monitoring(user_id: int, token: str):
    """Запуск мониторинга для пользователя"""
    try:
        # Создаем экземпляр покупателя если нужна автопокупка
        auto_buy = db.get_setting("auto_buy", False)
        confirm_before_purchase = db.get_setting("confirm_before_purchase", False)
        
        # Модифицируем функцию check_new_nfts для отправки уведомлений
        from main import check_new_nfts as original_check_new_nfts
        
        # Создаем обертку для check_new_nfts с отправкой уведомлений
        async def check_new_nfts_with_notifications(token, db, buyer):
            global seen_nfts
            
            auto_buy = db.get_setting("auto_buy", False)
            confirm_before_purchase = db.get_setting("confirm_before_purchase", True)
            
            gifts, _, rate_limited = await get_gifts_page_with_retry(token, limit=20, cursor="", db=db)
            
            if rate_limited:
                print("\n⏳ Rate limit, ждем 2 секунды...")
                await asyncio.sleep(2)
                return
            
            if gifts is None:
                return
            
            if not gifts:
                return
            
            new_nfts = []
            matched_nfts = []
            
            for gift in gifts:
                gift_id = gift.get('id')
                if gift_id and gift_id not in seen_nfts:
                    new_nfts.append(gift)
                    seen_nfts.add(gift_id)
                    
                    collection_name = gift.get('collectionName', '')
                    price_value = extract_price(gift)
                    
                    if price_value is not None:
                        is_price_match, max_price = check_price_against_db(collection_name, price_value, db)
                        
                        if is_price_match:
                            matched_nfts.append({
                                'gift': gift,
                                'price': price_value,
                                'max_price': max_price,
                                'gift_id': gift_id
                            })
            
            collection_names = db.get_collection_names(only_enabled=True)
            collection_count = len(collection_names)
            
            if matched_nfts and auto_buy:
                for match in matched_nfts:
                    gift = match['gift']
                    price = match['price']
                    gift_id = match['gift_id']
                    
                    if confirm_before_purchase:
                        # Запрашиваем подтверждение
                        await request_purchase_confirmation(user_id, gift, price, gift_id)
                    else:
                        # Покупаем сразу
                        buyer = MRKTBuyer(token, db)
                        price_nano = int(price * 1_000_000_000)
                        success, purchase_data, error_msg = await buyer.buy_nft(gift_id, price_nano)
                        
                        # Отправляем уведомление
                        await send_purchase_notification(user_id, purchase_data if success else None, success, error_msg)
        
        # Получаем интервал проверки
        interval = db.get_setting("check_interval", 0.5)
        
        # Отправляем уведомление о запуске
        await send_notification(
            user_id,
            f"✅ *Мониторинг запущен*\n"
            f"🔄 Проверка каждые {interval} сек\n"
            f"📊 Коллекций в фильтре: {len(db.get_enabled_collections())}"
        )
        
        counter = 0
        # Основной цикл мониторинга
        while monitoring_status.get(user_id, False):
            try:
                # Используем функцию для проверки NFT
                await check_new_nfts_with_notifications(token, db, None)
                
                counter += 1
                
                # Отправляем статистику каждые 100 итераций
                if counter % 100 == 0:
                    purchases_count = db.get_purchases_count()
                    await send_notification(
                        user_id,
                        f"📊 *Статистика мониторинга*\n"
                        f"• Всего покупок: {purchases_count}\n"
                        f"• В кэше NFT: {len(seen_nfts)}\n"
                        f"• Ошибок подряд: {error_stats.get('consecutive_failures', 0)}"
                    )
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                logger.error(f"Monitoring error for user {user_id}: {e}")
                await asyncio.sleep(interval * 2)
                
    except asyncio.CancelledError:
        logger.info(f"Monitoring cancelled for user {user_id}")
    except Exception as e:
        logger.error(f"Fatal monitoring error for user {user_id}: {e}")
        await send_notification(
            user_id,
            f"❌ *Ошибка мониторинга*\n{str(e)[:200]}"
        )
    finally:
        monitoring_status[user_id] = False
        if user_id in monitoring_tasks:
            del monitoring_tasks[user_id]


# Вспомогательные функции из main.py
def check_price_against_db(collection_name, price, db: DatabaseManager):
    """Проверяет, соответствует ли цена NFT порогу из БД"""
    collections = db.get_enabled_collections()
    
    for collection in collections:
        config_name = collection["name"].lower().strip()
        current_name = collection_name.lower().strip()
        
        if (config_name in current_name or current_name in config_name):
            max_price = collection.get("max_price", float('inf'))
            return price <= max_price, max_price
    
    return False, None


async def get_gifts_page_with_retry(token: str, limit: int = 20, cursor: str = "", db: DatabaseManager = None):
    """Получает одну страницу NFT с повторными попытками при ошибках"""
    from main import get_gifts_page
    
    max_retries = db.get_setting("max_retries", 3) if db else 3
    retry_delay = db.get_setting("retry_delay", 1.0) if db else 1.0
    connection_timeout = db.get_setting("connection_timeout", 10) if db else 10
    
    collection_names = db.get_collection_names(only_enabled=True) if db else []
    
    for attempt in range(max_retries + 1):
        try:
            gifts, new_cursor, rate_limited = await get_gifts_page(token, limit, cursor, connection_timeout, collection_names)
            
            if gifts is not None or rate_limited:
                return gifts, new_cursor, rate_limited
            
            if attempt < max_retries:
                wait_time = retry_delay * (2 ** attempt)
                await asyncio.sleep(wait_time)
            else:
                return None, None, False
                
        except Exception as e:
            if attempt < max_retries:
                wait_time = retry_delay * (2 ** attempt)
                await asyncio.sleep(wait_time)
            else:
                return None, None, False


async def send_notification(user_id: int, text: str):
    """Отправка уведомления пользователю"""
    try:
        await bot.send_message(user_id, text, parse_mode="Markdown")
    except TelegramAPIError as e:
        logger.error(f"Failed to send notification to {user_id}: {e}")


@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    """Показать статистику"""
    user_id = message.from_user.id
    
    purchases_count = db.get_purchases_count()
    collections_count = len(db.get_all_collections())
    active_collections = len(db.get_enabled_collections())
    
    # Получаем информацию о последних покупках
    recent = db.get_recent_purchases(5)
    
    text = f"📊 *Статистика*\n\n"
    text += f"• *Всего покупок:* `{purchases_count}`\n"
    text += f"• *Коллекций всего:* `{collections_count}`\n"
    text += f"• *Активных коллекций:* `{active_collections}`\n"
    text += f"• *Мониторинг:* `{'🟢 Активен' if monitoring_status.get(user_id, False) else '🔴 Остановлен'}`\n"
    
    if error_stats.get('consecutive_failures', 0) > 0:
        text += f"• *Ошибок подряд:* `{error_stats['consecutive_failures']}`\n"
    
    if recent:
        text += f"\n🛒 *Последние покупки:*\n"
        for p in recent:
            text += f"• #{p['number']} {p['collection_name']} - `{p['price_ton']} TON`\n"
    
    await message.answer(text, parse_mode="Markdown")


@dp.message(F.text == "🛒 История покупок")
async def show_purchases(message: Message):
    """Показать историю покупок"""
    purchases = db.get_recent_purchases(20)
    
    if not purchases:
        await message.answer(
            "📭 *История покупок пуста*",
            parse_mode="Markdown"
        )
        return
    
    text = "🛒 *История покупок*\n\n"
    
    for i, p in enumerate(purchases[:10], 1):
        text += f"{i}. *{p['collection_name']}* #{p['number']}\n"
        text += f"   💰 `{p['price_ton']} TON`\n"
        text += f"   🔗 [Ссылка]({p['link']})\n\n"
    
    if len(purchases) > 10:
        text += f"*...и еще {len(purchases) - 10} покупок*"
    
    await message.answer(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# ==================== ЗАПУСК БОТА ====================

async def on_startup():
    """Действия при запуске бота"""
    logger.info("Bot started!")
    
    # Проверяем наличие коллекций в БД
    collections = db.get_all_collections()
    if not collections:
        logger.info("No collections in database. Add some collections to start monitoring.")
    
    # Импортируем существующие коллекции из config.json если есть
    if os.path.exists("config.json") and not collections:
        try:
            with open("config.json", 'r', encoding='utf-8') as f:
                old_config = json.load(f)
                for collection in old_config.get("collections", []):
                    db.add_collection(
                        name=collection["name"],
                        max_price=collection["max_price"],
                        enabled=collection.get("enabled", True)
                    )
                logger.info(f"Imported {len(old_config.get('collections', []))} collections from config.json")
        except Exception as e:
            logger.error(f"Failed to import config.json: {e}")


async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("Bot shutting down...")
    
    # Останавливаем все задачи мониторинга
    for user_id, task in monitoring_tasks.items():
        task.cancel()
        monitoring_status[user_id] = False
    
    # Закрываем все клиенты Pyrogram
    for user_id, client in user_clients.items():
        try:
            await client.stop()
        except:
            pass
    
    # Закрываем сессию бота
    await bot.session.close()


async def main():
    """Основная функция запуска"""
    logger.info("Starting MRKT Monitoring Bot...")
    
    # Регистрируем обработчики
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Запускаем бота
    try:
        await dp.start_polling(bot)
    finally:
        await on_shutdown()


if __name__ == "__main__":
    asyncio.run(main())