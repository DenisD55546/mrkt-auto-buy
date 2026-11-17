import asyncio
from pyrogram import Client

API_ID = 31267437
API_HASH = "517b90bd9eef9b8f162cd83fef08ff41"
PROXY = {
    "scheme": "socks5", 
    "hostname": "138.59.5.169", 
    "port": 9616,
    "username": "Rzd9Vk",
    "password": "xSnELX"
}

async def main():
    client = Client("test_session", api_id=API_ID, api_hash=API_HASH, proxy=PROXY)
    try:
        print("Подключаюсь...")
        await client.connect()
        print("Подключено!")
        
        # Попытка отправить код
        sent_code = await client.send_code("+79340916257")
        print(f"Код отправлен! phone_code_hash: {sent_code.phone_code_hash}")
        
    except Exception as e:
        print(f"Ошибка: {e}")
    finally:
        await client.disconnect()

asyncio.run(main())