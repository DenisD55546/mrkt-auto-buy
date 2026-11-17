import asyncio
from aiogram import Bot
from typing import Optional
from pyrogram import Client
from pyrogram.raw.functions.messages import RequestAppWebView
from pyrogram.raw.types import InputBotAppShortName
from urllib.parse import unquote
from curl_cffi import requests
from curl_cffi.requests import RequestsError
import os
import json
from datetime import datetime
import time
from typing import Optional, Dict, List, Tuple
import sys

# Импортируем менеджер базы данных
from db_manager import DatabaseManager

bot_instance = None
bot_chat_id = None
# Конфигурация
API_ID = 31267437
API_HASH = "517b90bd9eef9b8f162cd83fef08ff41"
SESSION_NAME = "mrkt_session"
MARKET_API_URL = "https://api.tgmrkt.io/api/v1"
TOKEN_FILE = "mrkt_token.json"

# Глобальный кэш для отслеживания уже увиденных NFT
seen_nfts = set()

# Статистика ошибок
error_stats = {
    'connection_errors': 0,
    'timeout_errors': 0,
    'rate_limit_errors': 0,
    'other_errors': 0,
    'last_error_time': None,
    'consecutive_failures': 0
}

async def verify_token(token: str) -> bool:
    """Проверяет, работает ли токен"""
    try:
        headers = {"Authorization": token}
        response = requests.get(
            f"{MARKET_API_URL}/gifts/saling?limit=1",
            headers=headers,
            impersonate="chrome",
            timeout=10
        )
        return response.status_code == 200
    except:
        return False

class MRKTBuyer:
    """Класс для покупки NFT на MRKT"""
    
    def __init__(self, token: str, db: DatabaseManager):
        self.token = token
        self.db = db
        self.headers = {
            "Authorization": token,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "Origin": "https://app.tgmrkt.io",
            "Referer": "https://app.tgmrkt.io/",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Connection": "keep-alive"
        }
    
    async def get_gift_details(self, gift_id: str) -> Optional[Dict]:
        """Получает детальную информацию о конкретном NFT"""
        try:
            print(f"🔍 Получаем детали NFT {gift_id}...")
            payload = {"ids": [gift_id]}
            
            response = requests.post(
                f"{MARKET_API_URL}/gifts/saling/by-ids",
                headers=self.headers,
                json=payload,
                impersonate="chrome",
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0:
                    return data[0]
            return None
            
        except Exception as e:
            print(f"❌ Ошибка получения деталей NFT: {e}")
            return None
    
    async def buy_nft(self, gift_id: str, expected_price_nano: int = None) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Покупает NFT"""
        try:
            print(f"\n💰 Начинаем процесс покупки NFT {gift_id}...")
            
            gift_details = await self.get_gift_details(gift_id)
            
            if not gift_details:
                print("❌ Не удалось получить информацию о NFT")
                return False, None, "Не удалось получить информацию о NFT"
            
            if not gift_details.get('isOnSale', False):
                print(f"❌ NFT {gift_id} уже не в продаже")
                return False, None, "NFT уже не в продаже"
            
            current_price = gift_details.get('salePrice')
            if not current_price:
                print(f"❌ У NFT {gift_id} нет цены")
                return False, None, "У NFT нет цены"
            
            print(f"💰 Текущая цена: {current_price/1e9} TON")
            
            if expected_price_nano and current_price != expected_price_nano:
                auto_buy_on_price_change = self.db.get_setting("auto_buy_on_price_change", False)
                if not auto_buy_on_price_change:
                    print("⏭️ Покупка отменена из-за изменения цены")
                    return False, None, "Цена изменилась"
            
            purchase_payload = {
                "ids": [gift_id],
                "prices": {
                    gift_id: current_price
                }
            }
            
            response = requests.post(
                f"{MARKET_API_URL}/gifts/buy",
                headers=self.headers,
                json=purchase_payload,
                impersonate="chrome",
                timeout=30
            )
            
            if response.status_code == 200:
                purchase_result = response.json()
                
                if purchase_result and len(purchase_result) > 0:
                    result = purchase_result[0]
                    
                    if result.get('type') == 'gift':
                        purchased_gift = result.get('userGift', {})
                        
                        # Сохраняем в БД через менеджер
                        purchase_record = self.db.add_purchase(purchased_gift, current_price)
                        
                        print(f"\n{'✅'*20}")
                        print(f"✅ NFT УСПЕШНО КУПЛЕН!")
                        print(f"📦 Коллекция: {purchased_gift.get('collectionName')}")
                        print(f"📦 Модель: {purchased_gift.get('modelName')}")
                        print(f"💰 Цена: {current_price/1e9} TON")
                        print(f"🔢 Номер: #{purchased_gift.get('number')}")
                        print(f"🔗 Ссылка: https://t.me/mrkt/app?startapp=gift-{gift_id}")
                        print(f"{'✅'*20}\n")
                        
                        return True, purchase_record, None
                    else:
                        print(f"❌ Неожиданный ответ: {result}")
                        return False, None, f"Неожиданный ответ: {result}"
                else:
                    print(f"❌ Пустой ответ при покупке")
                    return False, None, "Пустой ответ от сервера"
            else:
                error_text = f"Ошибка HTTP {response.status_code}"
                print(f"❌ Ошибка при покупке: {response.status_code}")
                print(f"  Текст ошибки: {response.text[:500]}")
                return False, None, error_text
                
        except Exception as e:
            print(f"❌ Ошибка при покупке NFT: {e}")
            import traceback
            traceback.print_exc()
            return False, None, str(e)


def load_saved_token():
    """Загружает сохраненный токен из файла"""
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f:
                data = json.load(f)
                return data.get('token')
        except:
            pass
    return None


def save_token(token):
    """Сохраняет токен в файл"""
    with open(TOKEN_FILE, 'w') as f:
        json.dump({
            'token': token,
            'saved_at': datetime.now().isoformat()
        }, f)


def extract_price(gift):
    """Извлекает цену из объекта подарка и конвертирует из нано TON в TON"""
    try:
        price_in_nano = None
        
        if 'listing' in gift and gift['listing'] is not None:
            listing = gift['listing']
            for price_field in ['price', 'amount', 'value']:
                if price_field in listing and listing[price_field] is not None:
                    price_value = listing[price_field]
                    if isinstance(price_value, str):
                        price_value = price_value.replace(',', '.')
                    try:
                        price_in_nano = float(price_value)
                        break
                    except (ValueError, TypeError):
                        continue
        
        if price_in_nano is None:
            for root_field in ['price', 'salePrice', 'lastSalePrice']:
                if root_field in gift and gift[root_field] is not None:
                    price_value = gift[root_field]
                    if isinstance(price_value, str):
                        price_value = price_value.replace(',', '.')
                    try:
                        price_in_nano = float(price_value)
                        break
                    except (ValueError, TypeError):
                        continue
        
        if price_in_nano is not None:
            return price_in_nano / 1_000_000_000
        
        return None
    except Exception as e:
        print(f"Ошибка при извлечении цены: {e}")
        return None


def format_price(price):
    """Форматирует цену для отображения"""
    if price is None:
        return "—"
    
    try:
        price_float = float(price)
        if price_float == 0:
            return "0"
        elif price_float < 0.001:
            return f"{price_float:.6f}"
        elif price_float < 0.01:
            return f"{price_float:.4f}"
        else:
            return f"{price_float:.2f}"
    except (ValueError, TypeError):
        return str(price)


async def get_fresh_token(client: Client):
    """Получает новый токен через Pyrogram"""
    bot_entity = await client.get_users("mrkt")
    peer = await client.resolve_peer("mrkt")
    
    bot_app = InputBotAppShortName(
        bot_id=await client.resolve_peer(bot_entity.id),
        short_name="app"
    )
    
    web_view = await client.invoke(
        RequestAppWebView(
            peer=peer,
            app=bot_app,
            platform="android",
            write_allowed=True
        )
    )
    
    url_parts = web_view.url.split("tgWebAppData=")
    if len(url_parts) < 2:
        raise Exception("Не удалось найти tgWebAppData в URL")
    
    init_data = unquote(url_parts[1].split("&tgWebAppVersion", 1)[0])
    
    auth_data = {"data": init_data}
    response = requests.post(
        url=f"{MARKET_API_URL}/auth",
        json=auth_data,
        impersonate="chrome",
        timeout=30
    )
    
    result = response.json()
    token = result.get("token")
    
    if not token:
        raise Exception(f"Не удалось получить токен: {result}")
    
    return token


def build_gifts_request(limit=20, cursor="", collection_names=None):
    """Строит запрос для получения подарков с фильтрацией по коллекциям"""
    request_data = {
        "collectionNames": collection_names if collection_names else [],
        "modelNames": [],
        "backdropNames": [],
        "symbolNames": [],
        "lowToHigh": False,
        "ordering": "None",
        "maxPrice": None,
        "minPrice": None,
        "mintable": None,
        "number": None,
        "count": min(limit, 20),
        "query": None,
        "promotedFirst": False,
    }
    
    if cursor:
        request_data["cursor"] = cursor
    
    return request_data


async def get_gifts_page_with_retry(token: str, limit: int = 20, cursor: str = "", db: DatabaseManager = None):
    """Получает одну страницу NFT с повторными попытками при ошибках"""
    global error_stats
    
    max_retries = db.get_setting("max_retries", 3) if db else 3
    retry_delay = db.get_setting("retry_delay", 1.0) if db else 1.0
    connection_timeout = db.get_setting("connection_timeout", 10) if db else 10
    
    # Всегда используем фильтрацию на сервере
    collection_names = db.get_collection_names(only_enabled=True) if db else []
    
    for attempt in range(max_retries + 1):
        try:
            result = await get_gifts_page(token, limit, cursor, connection_timeout, collection_names)
            
            if result[0] is not None or result[2]:
                error_stats['consecutive_failures'] = 0
                return result
            
            if attempt < max_retries:
                wait_time = retry_delay * (2 ** attempt)
                print(f"\n⏳ Повторная попытка {attempt + 1}/{max_retries} через {wait_time:.1f}с...")
                await asyncio.sleep(wait_time)
            else:
                return result
                
        except Exception as e:
            error_msg = str(e)
            error_stats['consecutive_failures'] += 1
            error_stats['last_error_time'] = datetime.now()
            
            if "Failed to connect" in error_msg or "Connection refused" in error_msg:
                error_stats['connection_errors'] += 1
                error_type = "подключения"
            elif "Timeout" in error_msg:
                error_stats['timeout_errors'] += 1
                error_type = "таймаута"
            elif "429" in error_msg:
                error_stats['rate_limit_errors'] += 1
                error_type = "rate limit"
            else:
                error_stats['other_errors'] += 1
                error_type = "другая"
            
            if attempt < max_retries:
                wait_time = retry_delay * (2 ** attempt)
                print(f"\n⚠️ Ошибка {error_type} (попытка {attempt + 1}/{max_retries}), ждем {wait_time:.1f}с...")
                await asyncio.sleep(wait_time)
            else:
                print(f"\n❌ Превышено количество попыток. Последняя ошибка: {error_msg}")
                return None, None, False


async def get_gifts_page(token: str, limit: int = 20, cursor: str = "", timeout: int = 10, collection_names=None):
    """Получает одну страницу NFT с фильтрацией по коллекциям"""
    headers = {
        "Authorization": token,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://app.tgmrkt.io',
        'Referer': 'https://app.tgmrkt.io/',
        'Content-Type': 'application/json',
        'Connection': 'keep-alive',
        'Keep-Alive': 'timeout=5'
    }
    
    json_data = build_gifts_request(limit, cursor, collection_names)
    
    try:
        session = requests.Session()
        response = session.post(
            url=f"{MARKET_API_URL}/gifts/saling",
            headers=headers,
            json=json_data,
            impersonate="chrome",
            timeout=timeout
        )
        
        if response.status_code == 429:
            print("\n❌ Получен 429 ошибка - слишком много запросов")
            error_stats['rate_limit_errors'] += 1
            return None, None, True
            
        if response.status_code != 200:
            print(f"\n❌ Ошибка API: {response.status_code}")
            print(f"Ответ: {response.text}")
            return None, None, False
        
        data = response.json()
        gifts = data.get("gifts", [])
        new_cursor = data.get("cursor")
        
        with open("last_success.txt", "w") as f:
            f.write(str(time.time()))
        
        return gifts, new_cursor, False
        
    except RequestsError as e:
        error_msg = str(e)
        if "Failed to connect" in error_msg:
            raise Exception(f"Failed to connect to server: {e}")
        elif "Timeout" in error_msg:
            raise Exception(f"Timeout error: {e}")
        else:
            raise Exception(f"Request error: {e}")
    except Exception as e:
        raise Exception(f"Unexpected error: {e}")


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


async def check_new_nfts(token: str, db: DatabaseManager, buyer: Optional[MRKTBuyer] = None, user_id: int = None):
    """Проверяет новые NFT с фильтрацией по коллекциям из БД"""
    global seen_nfts, bot_instance, bot_chat_id
    
    auto_buy = db.get_setting("auto_buy", False)
    # Убираем confirm_before_purchase - всегда покупаем без подтверждения
    
    gifts, _, rate_limited = await get_gifts_page_with_retry(token, limit=20, cursor="", db=db)
    
    if rate_limited:
        print("\n⏳ Rate limit, ждем 2 секунды...")
        await asyncio.sleep(2)
        return
    
    if gifts is None:
        if error_stats['consecutive_failures'] > 0:
            print(f"\n📊 Статистика ошибок: {error_stats['consecutive_failures']} подряд | "
                  f"Всего: {error_stats['connection_errors']} подключений, "
                  f"{error_stats['timeout_errors']} таймаутов, "
                  f"{error_stats['rate_limit_errors']} rate limit")
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
    
    if new_nfts:
        current_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"\n{'='*60}")
        print(f"🆕 ПОЛУЧЕНО {len(new_nfts)} NFT ИЗ ОТСЛЕЖИВАЕМЫХ КОЛЛЕКЦИЙ [{current_time}]")
        print(f"📊 Коллекций в фильтре: {collection_count}")
        print(f"{'='*60}")
        
        for i, gift in enumerate(new_nfts, 1):
            collection = gift.get('collectionName', 'Без названия')
            model = gift.get('modelName', 'Не указана')
            backdrop = gift.get('backdropName', 'Нет фона')
            symbol = gift.get('symbolName', 'Нет узора')
            
            price_value = extract_price(gift)
            price = format_price(price_value)
            
            number = gift.get('number', '?')
            gift_id = gift.get('id', '')
            owners = gift.get('ownersCount', 0)
            
            print(f"\n{i}. {collection}")
            print(f"   📦 Модель: {model}")
            print(f"   🎨 Фон: {backdrop}")
            print(f"   ✨ Узор: {symbol}")
            print(f"   💰 Цена: {price} TON")
            print(f"   🔢 Номер: #{number}")
            print(f"   📊 Владельцев: {owners}")
            print(f"   🔗 Ссылка: https://t.me/mrkt/app?startapp=gift-{gift_id}")
    
    if matched_nfts:
        print(f"\n{'🔥'*30}")
        print(f"🎯 НАЙДЕНЫ NFT ПО ЦЕЛЕВОЙ ЦЕНЕ ({len(matched_nfts)} шт.)")
        print(f"{'🔥'*30}")
        
        for i, match in enumerate(matched_nfts, 1):
            gift = match['gift']
            collection = gift.get('collectionName', 'Без названия')
            model = gift.get('modelName', 'Не указана')
            number = gift.get('number', '?')  
            price = match['price']
            max_price = match['max_price']
            discount = ((max_price - price) / max_price * 100) if max_price > 0 and max_price != float('inf') else 0
            
            gift_id = match['gift_id']
            
            print(f"\n{'💎'*10} СОВПАДЕНИЕ #{i} {'💎'*10}")
            print(f"📌 Коллекция: {collection}")
            print(f"   Модель: {model}")
            print(f"   🔢 Номер: #{number}") 
            print(f"   Цена: {format_price(price)} TON")
            if max_price != float('inf'):
                print(f"   Макс. цена: {format_price(max_price)} TON")
                print(f"   Экономия: {discount:.1f}%")
            print(f"   🔗 Ссылка: https://t.me/mrkt/app?startapp=gift-{gift_id}")
            print(f"{'💎'*35}")
            
            # Автоматическая покупка (без подтверждения)
            if auto_buy and buyer:
                print(f"\n🔄 Автоматическая покупка активирована...")
                
                # Покупаем сразу без подтверждения
                price_nano = int(price * 1_000_000_000)
                success, purchase_data = await buyer.buy_nft(gift_id, price_nano)
                
                # Отправляем уведомление в бот
                if user_id and 'bot_instance' in globals() and bot_instance:
                    try:
                        if success:
                            # Формируем сообщение об успешной покупке
                            msg = (
                                f"✅ <b>NFT УСПЕШНО КУПЛЕН!</b>\n\n"
                                f"📦 <b>Коллекция:</b> {collection}\n"
                                f"📦 <b>Модель:</b> {model}\n"
                                f"💰 <b>Цена:</b> {format_price(price)} TON\n"
                                f"🔢 <b>Номер:</b> #{number}\n"
                                f"🔗 <b>Ссылка:</b> <a href='https://t.me/mrkt/app?startapp=gift-{gift_id}'>открыть в MRKT</a>"
                            )
                        else:
                            # Формируем сообщение о неудачной покупке
                            msg = (
                                f"❌ <b>Не удалось купить NFT</b>\n\n"
                                f"📦 <b>Коллекция:</b> {collection}\n"
                                f"📦 <b>Модель:</b> {model}\n"
                                f"💰 <b>Цена:</b> {format_price(price)} TON\n"
                                f"🔢 <b>Номер:</b> #{number}\n"
                                f"🔗 <b>Ссылка:</b> <a href='https://t.me/mrkt/app?startapp=gift-{gift_id}'>открыть в MRKT</a>\n\n"
                                f"⚠️ <b>Возможная причина:</b> недостаточно баланса"
                            )
                        
                        await bot_instance.send_message(user_id, msg, parse_mode="HTML")
                    except Exception as e:
                        print(f"[DEBUG] Ошибка отправки уведомления: {e}")
                
                if success:
                    print(f"✅ NFT успешно куплен!")
                else:
                    print(f"❌ Не удалось купить NFT (возможно недостаточно баланса)")
            
        print(f"\n{'🔥'*30}\n")
        
    else:
        print(".", end="", flush=True)


async def monitor_nfts(token: str, db: DatabaseManager, user_id: int = None):
    """Мониторит новые NFT с заданным интервалом"""
    interval = db.get_setting("check_interval", 0.5)
    auto_buy = db.get_setting("auto_buy", False)
    
    print(f"\n🔍 Начинаем мониторинг NFT (проверка каждые {interval} секунд)")
    
    buyer = MRKTBuyer(token, db) if auto_buy else None
    
    collection_names = db.get_collection_names(only_enabled=True)
    
    if collection_names:
        print("📋 Фильтрация на сервере по коллекциям:")
        for name in collection_names:
            print(f"   - {name}")
        print(f"📊 Всего коллекций в фильтре: {len(collection_names)}")
    else:
        print("⚠️ Нет активных коллекций в БД!")
    
    if auto_buy:
        print("💰 РЕЖИМ: АВТОМАТИЧЕСКАЯ ПОКУПКА ВКЛЮЧЕНА")
        print("⚠️ Внимание! Скрипт будет автоматически покупать NFT!")
    
    print("=" * 60)
    
    print("📦 Загружаем существующие NFT в кэш...")
    gifts, _, _ = await get_gifts_page_with_retry(token, limit=20, cursor="", db=db)
    if gifts:
        for gift in gifts:
            gift_id = gift.get('id')
            if gift_id:
                seen_nfts.add(gift_id)
        print(f"✅ Загружено {len(gifts)} NFT в кэш")
    else:
        print("⚠️ Не удалось загрузить начальный кэш, но мониторинг продолжается")
    
    print("🚀 Мониторинг запущен (нажмите Ctrl+C для остановки)")
    print("=" * 60)
    
    counter = 0
    last_check = time.time()
    last_stats_time = time.time()
    
    try:
        while True:
            # Передаем user_id в check_new_nfts для отправки уведомлений
            await check_new_nfts(token, db, buyer, user_id)
            
            counter += 1
            if counter % 120 == 0:
                elapsed = time.time() - last_stats_time
                success_rate = 120 / elapsed if elapsed > 0 else 0
                
                total_purchases = db.get_purchases_count()
                
                print(f"\n📊 Статус: {len(seen_nfts)} NFT в кэше")
                print(f"   Скорость: {success_rate:.1f} запрос/сек")
                print(f"   Покупок всего: {total_purchases}")
                
                if error_stats['consecutive_failures'] > 0:
                    print(f"   ⚠️ Последних ошибок подряд: {error_stats['consecutive_failures']}")
                
                if error_stats['connection_errors'] > 0 or error_stats['timeout_errors'] > 0:
                    print(f"   📈 Ошибки: подключений {error_stats['connection_errors']}, "
                          f"таймаутов {error_stats['timeout_errors']}, "
                          f"rate limit {error_stats['rate_limit_errors']}")
                
                last_stats_time = time.time()
            
            await asyncio.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n\n👋 Мониторинг остановлен пользователем")
        print(f"📊 Всего уникальных NFT в кэше: {len(seen_nfts)}")
        print(f"⏱️  Время работы: {time.time() - last_check:.1f} секунд")
        print(f"📈 Итоговая статистика ошибок:")
        print(f"   - Ошибок подключения: {error_stats['connection_errors']}")
        print(f"   - Таймаутов: {error_stats['timeout_errors']}")
        print(f"   - Rate limit: {error_stats['rate_limit_errors']}")
        print(f"   - Других ошибок: {error_stats['other_errors']}")
        
        purchases = db.get_recent_purchases(limit=10)
        if purchases:
            print(f"\n📦 Последние покупки:")
            for purchase in purchases:
                print(f"   - {purchase['collection_name']} #{purchase['number']} за {purchase['price_ton']} TON")
  

async def main():
    print("🚀 Запуск программы...")
    print("🎯 Режим: мониторинг и автоматическая покупка NFT")
    
    # Инициализация менеджера базы данных
    db = DatabaseManager()
    
    # Проверка, есть ли коллекции в БД
    collections = db.get_all_collections()
    if not collections:
        print("\n📝 База данных пуста. Добавьте коллекции для отслеживания.")
        print("\nПримеры добавления коллекций через Python код:")
        print("  db.add_collection('Lunar Snake', 4.04, True)")
        print("  db.add_collection('Xmas Stocking', 4.04, True)")
        print("  db.add_collection('Ice Cream', 4.06, True)")
        print("\nИли через SQLite командную строку:")
        print("  sqlite3 mrkt_data.db")
        print("  INSERT INTO collections (name, max_price, enabled) VALUES ('Lunar Snake', 4.04, 1);")
        
        # Для удобства добавим коллекции из старого config.json, если он существует
        if os.path.exists("config.json"):
            try:
                with open("config.json", 'r', encoding='utf-8') as f:
                    old_config = json.load(f)
                    for collection in old_config.get("collections", []):
                        db.add_collection(
                            name=collection["name"],
                            max_price=collection["max_price"],
                            enabled=collection.get("enabled", True)
                        )
                    print("\n✅ Импортированы коллекции из config.json")
                    
                    # Покажем импортированные коллекции
                    collections = db.get_all_collections()
                    for col in collections:
                        status = "✅" if col['enabled'] else "❌"
                        print(f"   {status} {col['name']} - макс. {col['max_price']} TON")
            except Exception as e:
                print(f"⚠️ Не удалось импортировать config.json: {e}")
    
    if os.path.exists("last_success.txt"):
        try:
            with open("last_success.txt", "r") as f:
                last_time = float(f.read().strip())
                time_since_last = time.time() - last_time
                if time_since_last < 30:
                    print(f"⏳ Последний запрос был {time_since_last:.0f} секунд назад")
                    wait_time = 30 - time_since_last
                    if wait_time > 0:
                        print(f"⏰ Ожидание {wait_time:.0f} секунд...")
                        time.sleep(wait_time)
        except:
            pass
    
    token = load_saved_token()
    
    if not token:
        print("🔄 Получаем новый токен через Telegram...")
        client = Client(SESSION_NAME, API_ID, API_HASH, proxy={
            "scheme": "socks5", 
            "hostname": "138.59.5.169", 
            "port": 9616,
            "username": "Rzd9Vk",
            "password": "xSnELX"
        })
        
        try:
            await client.start()
            print("✅ Клиент Telegram запущен")
            
            token = await get_fresh_token(client)
            
            if token:
                save_token(token)
                print("✅ Новый токен получен и сохранен")
            else:
                print("❌ Не удалось получить токен")
                return
                
        except Exception as e:
            print(f"❌ Ошибка при получении токена: {e}")
            return
            
        finally:
            await client.stop()
            print("👋 Клиент Telegram отключен")
    
    await monitor_nfts(token, db)


if __name__ == "__main__":
    print("⏳ Запуск через 2 секунды...")
    time.sleep(2)
    asyncio.run(main())