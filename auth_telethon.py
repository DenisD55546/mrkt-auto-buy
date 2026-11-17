# auth_telethon.py
import os
import asyncio
import logging
from typing import Optional, Dict, Any

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.network import ConnectionTcpAbridged
from urllib.parse import unquote
from curl_cffi import requests

from config import API_ID, API_HASH, MARKET_API_URL
from db_manager import DatabaseManager

logger = logging.getLogger(__name__)

# Создаем директорию для сессий
os.makedirs("sessions", exist_ok=True)

# Прокси (можно оставить, можно убрать, Telethon с ним отлично работает)
PROXY = {
    'proxy_type': 'socks5',  # или 'http'
    'addr': '138.59.5.169',
    'port': 9616,
    'username': 'Rzd9Vk',
    'password': 'xSnELX'
}


class TelethonAuth:
    """Класс для авторизации через Telethon с хранением session string"""

    def __init__(self):
        self.user_clients: Dict[int, TelegramClient] = {}
        self.user_tokens: Dict[int, str] = {}
        self.temp_data: Dict[int, Dict[str, Any]] = {}
        self.session_strings: Dict[int, str] = {}
        self.db = DatabaseManager()

    def get_session_path(self, user_id: int) -> str:
        """Получить путь к файлу для session string"""
        return f"sessions/telethon_{user_id}.txt"

    async def save_session_string(self, user_id: int, client: TelegramClient):
        """Сохранить session string в файл"""
        try:
            session_string = client.session.save()  # В Telethon это метод .save()
            self.session_strings[user_id] = session_string

            with open(self.get_session_path(user_id), 'w') as f:
                f.write(session_string)

            print(f"[DEBUG] Session string сохранена для user_id {user_id}")
        except Exception as e:
            print(f"[DEBUG] Ошибка сохранения session string: {e}")

    def load_session_string(self, user_id: int) -> Optional[str]:
        """Загрузить session string из файла"""
        try:
            path = self.get_session_path(user_id)
            if os.path.exists(path):
                with open(path, 'r') as f:
                    return f.read().strip()
        except Exception as e:
            print(f"[DEBUG] Ошибка загрузки session string: {e}")
        return None

    async def create_client(self, user_id: int, session_string: str = None) -> TelegramClient:
        """Создает клиента Telethon"""
        session = StringSession(session_string) if session_string else StringSession()

        client = TelegramClient(
            session,
            API_ID,
            API_HASH,
            connection=ConnectionTcpAbridged,
            proxy=PROXY,  # Telethon принимает прокси напрямую. Если не нужен, закомментируйте.
            connection_retries=5,
            retry_delay=2,
            timeout=30
        )
        return client

    async def start_phone_auth(self, user_id: int, phone_number: str) -> Dict[str, Any]:
        """Начать авторизацию по номеру телефона"""
        try:
            print(f"[DEBUG] НАЧАЛО АВТОРИЗАЦИИ для user_id {user_id}, phone: {phone_number}")

            # 1. Пробуем загрузить сохраненную сессию
            session_string = self.load_session_string(user_id)
            if session_string:
                print(f"[DEBUG] Найдена сохраненная сессия")
                client = await self.create_client(user_id, session_string)
                await client.connect()

                if await client.is_user_authorized():
                    me = await client.get_me()
                    print(f"[DEBUG] Сессия рабочая для {me.phone_number}")
                    token = await self.get_mrkt_token(client)
                    if token:
                        self.user_clients[user_id] = client
                        self.user_tokens[user_id] = token
                        return {
                            'success': True,
                            'already_authorized': True,
                            'token': token
                        }
                else:
                    await client.disconnect()

            # 2. Создаем нового клиента
            print(f"[DEBUG] Создание нового клиента")
            client = await self.create_client(user_id)
            await client.connect()

            # 3. Отправляем код
            print(f"[DEBUG] Отправляем код на {phone_number}")
            sent_code = await client.send_code_request(phone_number)

            # 4. Сохраняем данные
            self.temp_data[user_id] = {
                'client': client,
                'phone': phone_number,
                'phone_code_hash': sent_code.phone_code_hash
            }

            return {
                'success': True,
                'phone_code_hash': sent_code.phone_code_hash,
                'timeout': 60
            }

        except errors.FloodWaitError as e:
            return {'success': False, 'error': f'⏳ Слишком много попыток. Подождите {e.seconds} секунд.'}
        except errors.PhoneNumberInvalidError:
            return {'success': False, 'error': '❌ Неверный формат номера телефона.'}
        except errors.PhoneNumberFloodError:
            return {'success': False, 'error': '❌ Слишком много запросов с этого номера.'}
        except errors.ApiIdInvalidError:
            return {'success': False, 'error': '❌ Неверные API данные (API_ID/API_HASH).'}
        except Exception as e:
            logger.error(f"Auth error: {e}", exc_info=True)
            return {'success': False, 'error': f'❌ Ошибка: {str(e)}'}

    async def verify_code(self, user_id: int, code: str) -> Dict[str, Any]:
        """Подтверждение кода авторизации"""
        try:
            if user_id not in self.temp_data:
                return {'success': False, 'error': '❌ Сессия устарела. Начните заново.'}

            data = self.temp_data[user_id]
            client = data['client']

            try:
                # Пытаемся войти с кодом
                user = await client.sign_in(
                    phone=data['phone'],
                    code=code,
                    phone_code_hash=data['phone_code_hash']
                )

                print(f"[DEBUG] Вход успешен для {user.phone}")

                # Сохраняем сессию
                await self.save_session_string(user_id, client)

                # Получаем токен MRKT
                token = await self.get_mrkt_token(client)

                if token:
                    self.user_clients[user_id] = client
                    self.user_tokens[user_id] = token
                    del self.temp_data[user_id]
                    return {'success': True, 'token': token}

            except errors.SessionPasswordNeededError:
                print(f"[DEBUG] Требуется 2FA")
                self.temp_data[user_id]['awaiting_password'] = True
                return {'success': True, 'requires_2fa': True, 'hint': ''}
            except errors.PhoneCodeInvalidError:
                return {'success': False, 'error': '❌ Неверный код.'}
            except errors.PhoneCodeExpiredError:
                return {'success': False, 'error': '❌ Код истек. Запросите новый.'}
            except Exception as e:
                return {'success': False, 'error': f'❌ Ошибка: {str(e)}'}

        except Exception as e:
            logger.error(f"Code verification error: {e}", exc_info=True)
            return {'success': False, 'error': f'❌ Ошибка: {str(e)}'}

    async def verify_password(self, user_id: int, password: str) -> Dict[str, Any]:
        """Подтверждение 2FA пароля"""
        try:
            if user_id not in self.temp_data:
                return {'success': False, 'error': '❌ Сессия устарела. Начните заново.'}

            client = self.temp_data[user_id]['client']

            try:
                user = await client.sign_in(password=password)

                # Сохраняем сессию
                await self.save_session_string(user_id, client)

                # Получаем токен MRKT
                token = await self.get_mrkt_token(client)

                if token:
                    self.user_clients[user_id] = client
                    self.user_tokens[user_id] = token
                    del self.temp_data[user_id]
                    return {'success': True, 'token': token}

            except errors.PasswordHashInvalidError:
                return {'success': False, 'error': '❌ Неверный пароль.'}
            except Exception as e:
                return {'success': False, 'error': f'❌ Ошибка: {str(e)}'}

        except Exception as e:
            return {'success': False, 'error': f'❌ Ошибка: {str(e)}'}

    async def get_mrkt_token(self, client: TelegramClient) -> Optional[str]:
        """
        Получает токен MRKT через WebView.
        Это правильный способ, описанный в документации .
        """
        try:
            print(f"[DEBUG] Получаем токен MRKT")

            # 1. Получаем сущность бота
            bot_entity = await client.get_entity('mrkt')
            
            # 2. Запрашиваем WebView
            # В Telethon для этого используется метод client.call
            from telethon.tl.functions.messages import RequestAppWebViewRequest
            from telethon.tl.types import InputBotAppShortName, InputUser

            peer = await client.get_input_entity('mrkt')
            bot_id = InputUser(user_id=bot_entity.id, access_hash=bot_entity.access_hash)
            
            web_view = await client(
                RequestAppWebViewRequest(
                    peer=peer,
                    app=InputBotAppShortName(
                        bot_id=bot_id,
                        short_name="app"  # 'app' — это имя приложения
                    ),
                    platform='android',  # или 'ios'/'web', но android работает надёжнее
                    write_allowed=True
                )
            )
            
            # 3. Извлекаем init_data из URL
            if not web_view.url:
                print("[DEBUG] URL WebView пуст")
                return None
                
            # URL выглядит так: https://t.me/mrkt/app?startapp=...&tgWebAppData=...
            # Нас интересует всё, что после tgWebAppData=
            url_parts = web_view.url.split("tgWebAppData=")
            if len(url_parts) < 2:
                print("[DEBUG] Не удалось найти tgWebAppData в URL")
                return None
                
            # Берём вторую часть, и обрезаем до первого & (там дальше идут служебные параметры)
            init_data_encoded = url_parts[1].split("&")[0]
            init_data = unquote(init_data_encoded)
            
            print(f"[DEBUG] init_data получен (первые 50 символов): {init_data[:50]}...")
            
            # 4. Отправляем запрос на MRKT API
            auth_data = {"data": init_data}
            
            response = requests.post(
                url=f"{MARKET_API_URL}/auth",
                json=auth_data,
                impersonate="chrome",  # Важно: без этого MRKT может отклонить запрос
                timeout=30
            )
            
            if response.status_code != 200:
                print(f"[DEBUG] Ошибка HTTP при получении токена: {response.status_code}")
                print(f"[DEBUG] Ответ: {response.text[:200]}")
                return None
                
            result = response.json()
            token = result.get("token")
            
            if token:
                print(f"[DEBUG] Токен MRKT успешно получен: {token[:20]}...")
                return token
            else:
                print(f"[DEBUG] В ответе нет токена: {result}")
                return None
                
        except ImportError as e:
            # Если не хватает импортов telethon.tl...
            logger.error(f"Ошибка импорта Telethon типов: {e}")
            print("[DEBUG] Возможно, нужно обновить telethon: pip install --upgrade telethon")
            return None
        except Exception as e:
            logger.error(f"Failed to get MRKT token: {e}", exc_info=True)
            print(f"[DEBUG] Ошибка получения токена: {type(e).__name__}: {e}")
            return None

    async def check_auth_status(self, user_id: int) -> Dict[str, Any]:
        """Проверить статус авторизации"""
        if user_id in self.user_clients:
            client = self.user_clients[user_id]
            try:
                if await client.is_user_authorized():
                    me = await client.get_me()
                    return {
                        'is_authorized': True,
                        'user_info': {
                            'id': me.id,
                            'first_name': me.first_name,
                            'username': me.username,
                            'phone': me.phone
                        }
                    }
            except Exception as e:
                logger.warning(f"Auth check failed for {user_id}: {e}")

        # Пробуем загрузить сессию
        session_string = self.load_session_string(user_id)
        if session_string:
            try:
                client = await self.create_client(user_id, session_string)
                await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                    self.user_clients[user_id] = client
                    token = await self.get_mrkt_token(client)
                    if token:
                        self.user_tokens[user_id] = token
                    return {
                        'is_authorized': True,
                        'user_info': {
                            'id': me.id,
                            'first_name': me.first_name,
                            'username': me.username,
                            'phone': me.phone
                        }
                    }
            except Exception as e:
                print(f"[DEBUG] Ошибка загрузки сессии: {e}")

        return {'is_authorized': False}

    async def logout_user(self, user_id: int) -> bool:
        """Выход пользователя"""
        try:
            if user_id in self.user_clients:
                await self.user_clients[user_id].disconnect()
                del self.user_clients[user_id]

            if user_id in self.user_tokens:
                del self.user_tokens[user_id]

            if user_id in self.temp_data:
                try:
                    await self.temp_data[user_id]['client'].disconnect()
                except:
                    pass
                del self.temp_data[user_id]

            # Удаляем файл сессии
            session_file = self.get_session_path(user_id)
            if os.path.exists(session_file):
                os.remove(session_file)

            return True
        except Exception as e:
            logger.error(f"Logout error: {e}")
            return False

    async def cleanup_temp_data(self, user_id: int):
        """Очистить временные данные"""
        if user_id in self.temp_data:
            try:
                await self.temp_data[user_id]['client'].disconnect()
            except:
                pass
            del self.temp_data[user_id]


# Создаем глобальный экземпляр
telethon_auth = TelethonAuth()