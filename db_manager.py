import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, List, Any
import os
import json
import time

DB_FILE = "mrkt_data.db"


class DatabaseManager:
    """Класс для работы с SQLite базой данных MRKT бота"""
    
    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self.init_db()
    
    @contextmanager
    def get_connection(self):
        """Контекстный менеджер с WAL режимом и таймаутом"""
        conn = sqlite3.connect(self.db_path, timeout=30)  # Увеличили таймаут
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # Включаем WAL режим
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def init_db(self):
        """Инициализация структуры базы данных"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    max_price REAL NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT 1
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gift_id TEXT NOT NULL,
                    collection_name TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    number INTEGER,
                    price_nano INTEGER NOT NULL,
                    price_ton REAL NOT NULL,
                    link TEXT NOT NULL
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_purchases_gift_id ON purchases(gift_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_collections_enabled ON collections(enabled)')
            
            self._init_default_settings()
    
    def _init_default_settings(self):
        """Инициализация настроек по умолчанию"""
        default_settings = {
            'check_interval': '0.5',
            'log_matches': 'true',
            'max_retries': '5',
            'retry_delay': '1.0',
            'connection_timeout': '15',
            'use_server_filter': 'true',
            'auto_buy': 'true',
            'auto_buy_on_price_change': 'false',
            'confirm_before_purchase': 'false',
            'max_daily_purchases': '10'
        }
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for key, value in default_settings.items():
                cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    
    # ==================== КОЛЛЕКЦИИ ====================
    
    def get_all_collections(self) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM collections ORDER BY name')
            return [dict(row) for row in cursor.fetchall()]
    
    def get_enabled_collections(self) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM collections WHERE enabled = 1 ORDER BY name')
            return [dict(row) for row in cursor.fetchall()]
    
    def get_collection_names(self, only_enabled: bool = True) -> List[str]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if only_enabled:
                cursor.execute('SELECT name FROM collections WHERE enabled = 1 ORDER BY name')
            else:
                cursor.execute('SELECT name FROM collections ORDER BY name')
            return [row['name'] for row in cursor.fetchall()]
    
    def get_collection(self, name: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM collections WHERE name = ?', (name,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def add_collection(self, name: str, max_price: float, enabled: bool = True) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('INSERT INTO collections (name, max_price, enabled) VALUES (?, ?, ?)', 
                             (name, max_price, enabled))
                return True
        except sqlite3.IntegrityError:
            return False
    
    def update_collection(self, name: str, max_price: Optional[float] = None, enabled: Optional[bool] = None) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            current = cursor.execute('SELECT * FROM collections WHERE name = ?', (name,)).fetchone()
            if not current:
                return False
            
            new_max_price = max_price if max_price is not None else current['max_price']
            new_enabled = enabled if enabled is not None else bool(current['enabled'])
            
            cursor.execute('UPDATE collections SET max_price = ?, enabled = ? WHERE name = ?',
                         (new_max_price, new_enabled, name))
            return True
    
    def delete_collection(self, name: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM collections WHERE name = ?', (name,))
            return cursor.rowcount > 0
    
    def toggle_collection(self, name: str) -> Optional[bool]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute('SELECT enabled FROM collections WHERE name = ?', (name,)).fetchone()
            if not row:
                return None
            new_status = not bool(row['enabled'])
            cursor.execute('UPDATE collections SET enabled = ? WHERE name = ?', (new_status, name))
            return new_status
    
    def collection_exists(self, name: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            return cursor.execute('SELECT 1 FROM collections WHERE name = ?', (name,)).fetchone() is not None
    
    # ==================== НАСТРОЙКИ ====================
    
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
            if not row:
                return default
            value = row['value']
            if value.lower() in ('true', 'false'):
                return value.lower() == 'true'
            try:
                return float(value) if '.' in value else int(value)
            except ValueError:
                return value
    
    def get_all_settings(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            settings = {}
            for row in cursor.execute('SELECT key, value FROM settings').fetchall():
                key, value = row['key'], row['value']
                if value.lower() in ('true', 'false'):
                    settings[key] = value.lower() == 'true'
                else:
                    try:
                        settings[key] = float(value) if '.' in value else int(value)
                    except ValueError:
                        settings[key] = value
            return settings
    
    def update_setting(self, key: str, value: Any) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            str_value = str(value).lower() if isinstance(value, bool) else str(value)
            cursor.execute('UPDATE settings SET value = ? WHERE key = ?', (str_value, key))
            return cursor.rowcount > 0
    
    def reset_settings_to_default(self) -> None:
        """Сбросить все настройки к значениям по умолчанию"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM settings')
            # Просто вставляем заново
            default_settings = {
                'check_interval': '0.5',
                'log_matches': 'true',
                'max_retries': '5',
                'retry_delay': '1.0',
                'connection_timeout': '15',
                'use_server_filter': 'true',
                'auto_buy': 'true',
                'auto_buy_on_price_change': 'false',
                'confirm_before_purchase': 'false',
                'max_daily_purchases': '10'
            }
            for key, value in default_settings.items():
                cursor.execute('INSERT INTO settings (key, value) VALUES (?, ?)', (key, value))
    
    # ==================== ПОКУПКИ ====================
    
    def add_purchase(self, gift_data: dict, price_nano: int) -> Dict:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            price_ton = price_nano / 1_000_000_000
            gift_id = gift_data.get('id')
            link = f"https://t.me/mrkt/app?startapp=gift-{gift_id}"
            
            cursor.execute('''
                INSERT INTO purchases (gift_id, collection_name, model_name, number, price_nano, price_ton, link)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                gift_data.get('giftIdString') or gift_id,
                gift_data.get('collectionName'),
                gift_data.get('modelName'),
                gift_data.get('number'),
                price_nano,
                price_ton,
                link
            ))
            
            return dict(cursor.execute('SELECT * FROM purchases WHERE id = ?', (cursor.lastrowid,)).fetchone())
    
    def get_purchases(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM purchases ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_purchases_by_collection(self, collection_name: str, limit: int = 50) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM purchases WHERE collection_name = ? ORDER BY id DESC LIMIT ?',
                         (collection_name, limit))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_purchases_count(self) -> int:
        with self.get_connection() as conn:
            return conn.execute('SELECT COUNT(*) as count FROM purchases').fetchone()['count']
    
    def get_recent_purchases(self, limit: int = 10) -> List[Dict]:
        return self.get_purchases(limit=limit)