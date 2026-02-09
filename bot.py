import logging
import sqlite3
import hashlib
import qrcode
import os
import sys
import threading
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, HTTPException
import uvicorn
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ================== КОНФИГУРАЦИЯ ДЛЯ RENDER ==================
# Добавляем текущую директорию в путь Python
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Автоматическое определение пути
if 'RENDER' in os.environ:
    # Мы на Render
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_NAME = os.path.join(BASE_DIR, 'evotor_loyalty.db')
    WEBHOOK_URL = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'localhost')}"
    IS_RENDER = True
    print(f"✅ Режим: RENDER, URL: {WEBHOOK_URL}")
elif 'PYTHONANYWHERE_DOMAIN' in os.environ:
    # Мы на PythonAnywhere
    BASE_DIR = '/home/archicux/'
    DB_NAME = os.path.join(BASE_DIR, 'evotor_loyalty.db')
    WEBHOOK_URL = f"https://archicux.pythonanywhere.com"
    IS_RENDER = False
    print("✅ Режим: PythonAnywhere")
else:
    # Локальная разработка
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_NAME = os.path.join(BASE_DIR, 'evotor_loyalty.db')
    WEBHOOK_URL = "http://localhost:8000"
    IS_RENDER = False
    print("✅ Режим: Локальный")

# ================== НАСТРОЙКИ ==================
# Используем переменные окружения на Render
BOT_TOKEN = os.environ.get('BOT_TOKEN', "8200085604:AAHyzg31wBdNHDRFxvSWz_wNkFzp9iRRBD0")
YOUR_TELEGRAM_ID = int(os.environ.get('YOUR_TELEGRAM_ID', 945157249))

# Проверка токена
if not BOT_TOKEN or BOT_TOKEN == "8200085604:AAHyzg31wBdNHDRFxvSWz_wNkFzp9iRRBD0":
    print("⚠️  ВНИМАНИЕ: Используется тестовый токен бота!")

# Настройки лояльности
LOYALTY_SETTINGS = {
    'points_per_purchase': 0.05,  # 5% от покупки
    'discount_per_point': 0.01,  # 1% скидка за балл
    'max_discount': 50,  # Максимальная скидка 50%
    'welcome_bonus': 100,  # Бонус за регистрацию
    'birthday_bonus': 500,  # Бонус на день рождения
}

# Админы по ID Telegram аккаунтов
ADMINS = [YOUR_TELEGRAM_ID]

# Состояния для ConversationHandler
PHONE, NAME, GENDER = range(3)
ADD_PURCHASE, SPEND_POINTS, CHECK_BALANCE = range(3, 6)
ADMIN_MENU, ADMIN_ADD_USER, ADMIN_EDIT_USER = range(6, 9)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ================== БАЗА ДАННЫХ (ОБЪЕДИНЕННАЯ) ==================
class LoyaltyDB:
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self.init_database()
        logger.info(f"База данных инициализирована: {db_name}")

    def init_database(self):
        """Инициализация базы данных"""
        try:
            with sqlite3.connect(self.db_name) as conn:
                cursor = conn.cursor()

                # Таблица пользователей
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        telegram_id INTEGER UNIQUE,
                        name TEXT,
                        phone TEXT,
                        gender TEXT,
                        registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        total_purchases REAL DEFAULT 0,
                        total_points INTEGER DEFAULT 0,
                        current_points INTEGER DEFAULT 0,
                        qr_code TEXT UNIQUE,
                        is_active BOOLEAN DEFAULT 1
                    )
                ''')

                # Таблица транзакций
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS transactions (
                        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        type TEXT NOT NULL,
                        amount REAL,
                        points_change INTEGER,
                        description TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users (user_id)
                    )
                ''')

                # Индексы для быстрого поиска
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_qr ON users(qr_code)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)')

                conn.commit()
                logger.info("Таблицы базы данных созданы/проверены")
        except Exception as e:
            logger.error(f"Ошибка инициализации БД: {e}")
            raise

    def generate_qr_code(self, user_id: int) -> str:
        """Генерация QR кода в формате XXX-XXX"""
        return f"{str(user_id).zfill(3)}-{hashlib.md5(str(user_id).encode()).hexdigest()[:3]}"

    def add_user(self, telegram_id: int, name: str = None, phone: str = None, gender: str = None) -> Tuple[int, str]:
        """Добавление нового пользователя с QR кодом"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()

            # Проверяем, существует ли пользователь
            cursor.execute('SELECT user_id, qr_code FROM users WHERE telegram_id = ?', (telegram_id,))
            existing = cursor.fetchone()

            if existing:
                logger.info(f"Пользователь {telegram_id} уже существует")
                return existing[0], existing[1] or self.generate_qr_code(existing[0])

            # Добавляем нового пользователя
            cursor.execute('''
                INSERT INTO users (telegram_id, name, phone, gender, current_points)
                VALUES (?, ?, ?, ?, ?)
            ''', (telegram_id, name, phone, gender, LOYALTY_SETTINGS['welcome_bonus']))

            user_id = cursor.lastrowid
            qr_code = self.generate_qr_code(user_id)

            # Обновляем QR код
            cursor.execute('UPDATE users SET qr_code = ? WHERE user_id = ?', (qr_code, user_id))

            # Добавляем транзакцию бонуса
            cursor.execute('''
                INSERT INTO transactions (user_id, type, points_change, description)
                VALUES (?, 'bonus', ?, 'Бонус за регистрацию')
            ''', (user_id, LOYALTY_SETTINGS['welcome_bonus']))

            conn.commit()
            logger.info(f"Создан новый пользователь: ID={user_id}, QR={qr_code}")
            return user_id, qr_code

    def get_user_by_qr(self, qr_code: str) -> Optional[Tuple]:
        """Получение пользователя по QR коду"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT telegram_id, current_points, user_id FROM users 
                WHERE qr_code = ? AND is_active = 1
            ''', (qr_code,))
            return cursor.fetchone()

    def add_purchase_by_qr(self, qr_code: str, amount: float) -> Optional[Tuple]:
        """Добавление покупки по QR коду (для webhook)"""
        row = self.get_user_by_qr(qr_code)
        if not row:
            logger.warning(f"Пользователь с QR={qr_code} не найден")
            return None

        telegram_id, current_points, user_id = row
        earned = int(amount * LOYALTY_SETTINGS['points_per_purchase'])

        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()

            # Обновляем баланс пользователя
            cursor.execute('''
                UPDATE users 
                SET total_purchases = total_purchases + ?,
                    total_points = total_points + ?,
                    current_points = current_points + ?
                WHERE qr_code = ?
            ''', (amount, earned, earned, qr_code))

            # Добавляем транзакцию
            cursor.execute('''
                INSERT INTO transactions (user_id, type, amount, points_change, description)
                VALUES (?, 'purchase', ?, ?, ?)
            ''', (user_id, amount, earned, f'Покупка на сумму {amount} руб. (через Эвотор)'))

            conn.commit()

            # Получаем новый баланс
            cursor.execute('SELECT current_points FROM users WHERE user_id = ?', (user_id,))
            new_balance = cursor.fetchone()[0]

            logger.info(f"Начислено баллов: QR={qr_code}, сумма={amount}, баллы={earned}, новый баланс={new_balance}")
            return telegram_id, earned, new_balance

    def add_purchase(self, user_id: int, amount: float) -> Tuple[int, float]:
        """Добавление покупки через бота"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            points_earned = int(amount * LOYALTY_SETTINGS['points_per_purchase'])

            cursor.execute('''
                UPDATE users 
                SET total_purchases = total_purchases + ?,
                    total_points = total_points + ?,
                    current_points = current_points + ?
                WHERE user_id = ?
            ''', (amount, points_earned, points_earned, user_id))

            cursor.execute('''
                INSERT INTO transactions (user_id, type, amount, points_change, description)
                VALUES (?, 'purchase', ?, ?, ?)
            ''', (user_id, amount, points_earned, f'Покупка на сумму {amount} руб.'))

            conn.commit()

            cursor.execute('SELECT current_points FROM users WHERE user_id = ?', (user_id,))
            new_balance = cursor.fetchone()[0]

            return points_earned, new_balance

    def spend_points(self, user_id: int, points_to_spend: int, purchase_amount: float = None) -> Tuple[
        bool, int, float]:
        """Списание баллов"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT current_points FROM users WHERE user_id = ?', (user_id,))
            current_points = cursor.fetchone()[0]

            if current_points < points_to_spend:
                return False, current_points, 0.0

            # Рассчитываем максимальное количество баллов для скидки
            max_points_for_discount = 0
            if purchase_amount:
                max_discount_amount = purchase_amount * LOYALTY_SETTINGS['max_discount'] / 100
                max_points_for_discount = int(max_discount_amount / LOYALTY_SETTINGS['discount_per_point'])

            if purchase_amount and points_to_spend > max_points_for_discount:
                points_to_spend = max_points_for_discount

            # Рассчитываем скидку
            discount = points_to_spend * LOYALTY_SETTINGS['discount_per_point']
            if purchase_amount:
                discount_amount = purchase_amount * discount / 100
                discount = min(discount, LOYALTY_SETTINGS['max_discount'])

            # Списание баллов
            cursor.execute('''
                UPDATE users 
                SET current_points = current_points - ? 
                WHERE user_id = ?
            ''', (points_to_spend, user_id))

            # Добавляем транзакцию
            cursor.execute('''
                INSERT INTO transactions (user_id, type, points_change, description)
                VALUES (?, 'spend', ?, ?)
            ''', (user_id, -points_to_spend, f'Списание {points_to_spend} баллов, скидка {discount:.1f}%'))

            conn.commit()

            cursor.execute('SELECT current_points FROM users WHERE user_id = ?', (user_id,))
            new_balance = cursor.fetchone()[0]

            return True, new_balance, discount

    def get_user_info(self, telegram_id: int) -> Optional[dict]:
        """Получение информации о пользователе"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT user_id, name, phone, gender, total_purchases, 
                       total_points, current_points, registration_date, qr_code
                FROM users 
                WHERE telegram_id = ? AND is_active = 1
            ''', (telegram_id,))
            row = cursor.fetchone()

            if not row:
                return None

            return {
                'user_id': row[0],
                'name': row[1],
                'phone': row[2],
                'gender': row[3],
                'total_purchases': row[4],
                'total_points': row[5],
                'current_points': row[6],
                'registration_date': row[7],
                'qr_code': row[8]
            }

    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        """Получение пользователя по ID"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()

            if not row:
                return None

            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))

    def get_user_transactions(self, user_id: int, limit: int = 10) -> list:
        """Получение последних транзакций"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT type, amount, points_change, description, timestamp
                FROM transactions 
                WHERE user_id = ? 
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (user_id, limit))

            return [
                {
                    'type': row[0],
                    'amount': row[1],
                    'points_change': row[2],
                    'description': row[3],
                    'timestamp': row[4]
                }
                for row in cursor.fetchall()
            ]

    def get_all_users(self, limit: int = 100, offset: int = 0) -> Tuple[List[dict], int]:
        """Получение всех пользователей (для админа)"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT user_id, telegram_id, name, phone, total_purchases, 
                       current_points, registration_date, qr_code
                FROM users 
                WHERE is_active = 1 
                ORDER BY registration_date DESC 
                LIMIT ? OFFSET ?
            ''', (limit, offset))

            users = []
            for row in cursor.fetchall():
                users.append({
                    'user_id': row[0],
                    'telegram_id': row[1],
                    'name': row[2],
                    'phone': row[3],
                    'total_purchases': row[4],
                    'current_points': row[5],
                    'registration_date': row[6],
                    'qr_code': row[7]
                })

            cursor.execute('SELECT COUNT(*) FROM users WHERE is_active = 1')
            total = cursor.fetchone()[0]
            return users, total

    def update_user_points(self, user_id: int, points: int,
                           description: str = "Изменение баланса администратором") -> bool:
        """Изменение баланса пользователя (админ)"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    UPDATE users 
                    SET current_points = current_points + ?,
                        total_points = total_points + ?
                    WHERE user_id = ?
                ''', (points, max(0, points), user_id))

                cursor.execute('''
                    INSERT INTO transactions (user_id, type, points_change, description)
                    VALUES (?, 'admin', ?, ?)
                ''', (user_id, points, description))

                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Ошибка обновления баллов: {e}")
                return False

    def get_system_stats(self) -> dict:
        """Получение статистики системы"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            stats = {}

            cursor.execute('''
                SELECT COUNT(*) as total_users,
                       SUM(total_purchases) as total_sales,
                       SUM(current_points) as total_points,
                       AVG(total_purchases) as avg_purchase
                FROM users 
                WHERE is_active = 1
            ''')

            row = cursor.fetchone()
            stats.update({
                'total_users': row[0] or 0,
                'total_sales': row[1] or 0,
                'total_points': row[2] or 0,
                'avg_purchase': row[3] or 0
            })

            return stats


# Инициализация базы данных
db = LoyaltyDB()

# ================== FASTAPI ВЕБ-ПРИЛОЖЕНИЕ ==================
app = FastAPI(title="Система лояльности Эвотор", version="1.0")


@app.get("/")
async def root():
    """Корневая страница для проверки работы сервера"""
    return {
        "status": "online",
        "service": "Evotor Loyalty System",
        "webhook": f"{WEBHOOK_URL}/evotor/webhook",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/health")
async def health_check():
    """Проверка здоровья сервера"""
    try:
        # Проверяем подключение к БД
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


@app.post("/evotor/webhook")
async def evotor_webhook(request: Request):
    """Обработчик вебхука от Эвотор"""
    try:
        # Пробуем разные форматы данных
        try:
            data = await request.json()
        except:
            # Иногда данные приходят как текст
            body = await request.body()
            data = json.loads(body.decode())
        
        logger.info(f"Получен вебхук: {data}")
        
        # Разные форматы данных от Эвотор
        receipt = data.get("document") or data.get("receipt") or data
        
        # Ищем total в разных местах
        total = receipt.get("total") or receipt.get("sum") or receipt.get("amount")
        
        # Ищем QR код
        extra = receipt.get("extra") or receipt.get("additional") or {}
        qr_code = extra.get("clientCode") or extra.get("qrCode") or data.get("clientCode")
        
        if not qr_code:
            # Пробуем найти в items или других полях
            qr_code = receipt.get("clientCode") or data.get("clientCode")
        
        if not qr_code or not total:
            logger.warning(f"Нет QR кода или суммы: qr_code={qr_code}, total={total}")
            return {"status": "ignored", "message": "Missing QR code or total"}
        
        # Конвертируем в float
        try:
            total_float = float(total)
        except:
            return {"status": "error", "message": "Invalid total format"}
        
        result = db.add_purchase_by_qr(qr_code, total_float)
        if not result:
            logger.warning(f"Пользователь не найден: QR={qr_code}")
            return {"status": "not_found", "message": "Client not found"}
        
        telegram_id, earned, balance = result
        
        # Отправляем уведомление пользователю через бота
        if application and hasattr(application, 'bot'):
            try:
                await application.bot.send_message(
                    chat_id=telegram_id,
                    text=f"🧾 Покупка: {total_float} ₽\n"
                         f"🎁 Начислено: {earned} баллов\n"
                         f"💰 Баланс: {balance}"
                )
                logger.info(f"Уведомление отправлено пользователю {telegram_id}")
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")
        
        return {
            "status": "ok",
            "points": earned,
            "balance": balance,
            "message": "Points added successfully"
        }
        
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


# ==================== TELEGRAM БОТ ====================

# ==================== КНОПКИ И КЛАВИАТУРЫ ====================
def get_main_keyboard():
    """Основная клавиатура для пользователей"""
    keyboard = [
        ["💰 Мой баланс", "📊 История операций"],
        ["➕ Добавить покупку", "🎁 Использовать баллы"],
        ["👤 Мой профиль", "📋 Правила"],
        ["🆘 Помощь"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_admin_keyboard():
    """Клавиатура для админов"""
    keyboard = [
        ["📊 Статистика", "👥 Пользователи"],
        ["➕ Добавить баллы", "✏️ Редактировать пользователя"],
        ["📋 Экспорт данных", "⚙️ Настройки"],
        ["🔙 В главное меню"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_cancel_keyboard():
    """Клавиатура для отмены"""
    keyboard = [["❌ Отмена"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ==================== ОСНОВНЫЕ ФУНКЦИИ БОТА ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало регистрации"""
    user = update.effective_user
    logger.info(f"Пользователь {user.id} ({user.username}) запустил бота")

    # Проверяем, является ли пользователь админом
    if user.id in ADMINS:
        await update.message.reply_text(
            f"👑 Привет, администратор {user.first_name}!\n"
            f"Используйте /admin для доступа к панели управления",
            reply_markup=get_admin_keyboard()
        )
        return ConversationHandler.END

    # Проверяем, зарегистрирован ли уже пользователь
    user_info = db.get_user_info(user.id)
    if user_info:
        qr_text = f"📲 Ваш код для кассы:\n`{user_info['qr_code']}`" if user_info.get('qr_code') else ""
        await update.message.reply_text(
            f"👋 Привет, {user_info['name']}!\n"
            f"Вы уже зарегистрированы.\n"
            f"Ваш баланс: {user_info['current_points']} баллов\n\n"
            f"{qr_text}\n\n"
            f"Используйте кнопки ниже:",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

        # Отправляем QR код если он есть
        if user_info.get('qr_code'):
            try:
                img = qrcode.make(user_info['qr_code'])
                img_path = f"/tmp/qr_{user.id}.png"
                img.save(img_path)
                with open(img_path, 'rb') as photo:
                    await update.message.reply_photo(photo=photo)
                os.remove(img_path)
            except Exception as e:
                logger.error(f"Ошибка генерации QR: {e}")
                await update.message.reply_text(
                    f"QR код: `{user_info['qr_code']}`\n"
                    f"Покажите этот код на кассе для начисления баллов",
                    parse_mode="Markdown"
                )

        return ConversationHandler.END

    # Начинаем регистрацию
    contact_button = KeyboardButton(
        text="📱 Поделиться контактом",
        request_contact=True
    )
    reply_markup = ReplyKeyboardMarkup(
        [[contact_button]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n"
        f"Добро пожаловать в программу лояльности!\n\n"
        f"Для регистрации нажмите кнопку ниже:",
        reply_markup=reply_markup
    )
    return PHONE


async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка номера телефона"""
    if update.message.contact:
        phone_number = update.message.contact.phone_number
    else:
        phone_number = update.message.text

    context.user_data['phone'] = phone_number
    await update.message.reply_text(
        "📝 Теперь напишите своё имя:",
        reply_markup=ReplyKeyboardRemove()
    )
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка имени"""
    name = update.message.text
    context.user_data['name'] = name

    gender_keyboard = [
        ["👨 Мужской", "👩 Женский"]
    ]
    reply_markup = ReplyKeyboardMarkup(
        gender_keyboard,
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await update.message.reply_text(
        f"👋 Приятно познакомиться, {name}!\n"
        f"Теперь выберите ваш пол:",
        reply_markup=reply_markup
    )
    return GENDER


async def get_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Завершение регистрации"""
    user = update.effective_user
    gender_text = update.message.text

    if "Мужской" in gender_text:
        gender = "мужской"
    elif "Женский" in gender_text:
        gender = "женский"
    else:
        gender = gender_text

    name = context.user_data.get('name')
    phone = context.user_data.get('phone')

    user_id, qr_code = db.add_user(user.id, name, phone, gender)
    user_info = db.get_user_info(user.id)

    # Генерируем QR код
    try:
        img = qrcode.make(qr_code)
        img_path = f"/tmp/qr_{user.id}.png"
        img.save(img_path)
    except Exception as e:
        logger.error(f"Ошибка генерации QR: {e}")
        img_path = None

    registration_message = (
        "✅ *Регистрация завершена!*\n\n"
        f"*Ваши данные:*\n"
        f"👤 Имя: {user_info['name']}\n"
        f"📱 Телефон: {user_info['phone']}\n"
        f"⚤ Пол: {user_info['gender']}\n"
        f"🎁 Бонус за регистрацию: {LOYALTY_SETTINGS['welcome_bonus']} баллов\n"
        f"💰 Текущий баланс: {user_info['current_points']} баллов\n"
        f"📲 Ваш код для кассы:\n`{qr_code}`\n\n"
        f"*Как использовать:*\n"
        f"1. Покажите QR код на кассе\n"
        f"2. Получайте баллы за покупки\n"
        f"3. Используйте баллы для скидок\n\n"
        f"Используйте кнопки ниже:"
    )

    await update.message.reply_text(
        registration_message,
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

    # Отправляем QR код
    if img_path and os.path.exists(img_path):
        with open(img_path, 'rb') as photo:
            await update.message.reply_photo(photo=photo)
        os.remove(img_path)

    return ConversationHandler.END


# ==================== ОБРАБОТКА КНОПОК ====================
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок"""
    user = update.effective_user
    text = update.message.text
    user_info = db.get_user_info(user.id)

    if text == "💰 Мой баланс":
        if not user_info:
            await update.message.reply_text(
                "❌ Вы не зарегистрированы. Используйте /start",
                reply_markup=get_main_keyboard()
            )
            return

        await update.message.reply_text(
            f"💰 *Ваш баланс:* {user_info['current_points']} баллов\n"
            f"🎯 *Доступная скидка:* {user_info['current_points'] * LOYALTY_SETTINGS['discount_per_point']:.1f}%\n"
            f"📊 *Всего накоплено:* {user_info['total_points']} баллов\n"
            f"🛒 *Сумма покупок:* {user_info['total_purchases']:.2f} руб.\n\n"
            f"*QR код:* `{user_info['qr_code']}`",
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )

    elif text == "📊 История операций":
        if not user_info:
            await update.message.reply_text(
                "❌ Вы не зарегистрированы. Используйте /start",
                reply_markup=get_main_keyboard()
            )
            return

        transactions = db.get_user_transactions(user_info['user_id'], limit=5)
        if not transactions:
            history_message = "📜 *История операций:*\n\nОпераций пока нет"
        else:
            history_message = "📜 *Последние операции:*\n\n"
            for trans in transactions:
                try:
                    date_str = datetime.strptime(trans['timestamp'], '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M')
                except:
                    date_str = str(trans['timestamp'])
                points = trans['points_change']
                points_str = f"+{points}" if points > 0 else str(points)
                history_message += f"• {date_str}: {trans['description']} ({points_str} баллов)\n"

        await update.message.reply_text(
            history_message,
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )

    elif text == "➕ Добавить покупку":
        if not user_info:
            await update.message.reply_text(
                "❌ Вы не зарегистрированы. Используйте /start",
                reply_markup=get_main_keyboard()
            )
            return

        await update.message.reply_text(
            "💵 Введите сумму покупки в рублях (например: 1500.50):",
            reply_markup=get_cancel_keyboard()
        )
        return ADD_PURCHASE

    elif text == "🎁 Использовать баллы":
        if not user_info:
            await update.message.reply_text(
                "❌ Вы не зарегистрированы. Используйте /start",
                reply_markup=get_main_keyboard()
            )
            return

        await update.message.reply_text(
            f"🎁 Ваш текущий баланс: {user_info['current_points']} баллов\n"
            f"Максимальная скидка: {LOYALTY_SETTINGS['max_discount']}%\n"
            f"Введите количество баллов для использования:",
            reply_markup=get_cancel_keyboard()
        )
        return SPEND_POINTS

    elif text == "👤 Мой профиль":
        if not user_info:
            await update.message.reply_text(
                "❌ Вы не зарегистрированы. Используйте /start",
                reply_markup=get_main_keyboard()
            )
            return

        registration_date = user_info['registration_date']
        if isinstance(registration_date, str):
            date_str = registration_date.split()[0] if ' ' in registration_date else registration_date
        else:
            date_str = "Неизвестно"

        qr_text = f"📱 QR код: `{user_info['qr_code']}`" if user_info.get('qr_code') else ""

        profile_message = (
            "👤 *Ваш профиль:*\n\n"
            f"📛 Имя: {user_info['name']}\n"
            f"📱 Телефон: {user_info['phone']}\n"
            f"⚤ Пол: {user_info.get('gender', 'Не указан')}\n"
            f"📅 Дата регистрации: {date_str}\n"
            f"{qr_text}\n\n"
            f"💰 *Статистика:*\n"
            f"• Текущий баланс: {user_info['current_points']} баллов\n"
            f"• Всего накоплено: {user_info['total_points']} баллов\n"
            f"• Общая сумма покупок: {user_info['total_purchases']:.2f} руб.\n"
            f"• Доступная скидка: {user_info['current_points'] * LOYALTY_SETTINGS['discount_per_point']:.1f}%\n"
            f"• Максимальная скидка: {LOYALTY_SETTINGS['max_discount']}%"
        )

        await update.message.reply_text(
            profile_message,
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )

    elif text == "📋 Правила":
        rules_message = (
            "📋 *Правила программы лояльности:*\n\n"
            f"🎁 *Начисление баллов:*\n"
            f"• За каждый рубль покупки: {LOYALTY_SETTINGS['points_per_purchase'] * 100}% от суммы\n"
            f"• Бонус за регистрацию: {LOYALTY_SETTINGS['welcome_bonus']} баллов\n\n"
            f"💰 *Использование баллов:*\n"
            f"• 1 балл = {LOYALTY_SETTINGS['discount_per_point'] * 100}% скидки\n"
            f"• Максимальная скидка: {LOYALTY_SETTINGS['max_discount']}%\n"
            f"• Баллы не имеют срока действия\n\n"
            f"📱 *Как использовать:*\n"
            f"1. Покажите QR код на кассе для начисления баллов\n"
            f"2. Добавляйте покупки через кнопку '➕ Добавить покупку'\n"
            f"3. Копите баллы\n"
            f"4. Используйте баллы через кнопку '🎁 Использовать баллы'"
        )

        await update.message.reply_text(
            rules_message,
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )

    elif text == "🆘 Помощь":
        help_message = (
            "🆘 *Помощь по боту:*\n\n"
            "📋 *Основные функции:*\n"
            "• 💰 Мой баланс - просмотр баланса баллов\n"
            "• 📊 История операций - последние транзакции\n"
            "• ➕ Добавить покупку - добавить новую покупку\n"
            "• 🎁 Использовать баллы - потратить баллы на скидку\n"
            "• 👤 Мой профиль - ваши данные\n"
            "• 📋 Правила - правила программы\n\n"
            "👑 *Для администраторов:*\n"
            "Используйте /admin для доступа к панели управления\n\n"
            f"📞 *Поддержка:*\n"
            f"Сервер: {WEBHOOK_URL}"
        )

        await update.message.reply_text(
            help_message,
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )

    elif text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Действие отменено.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END


# ==================== ОБРАБОТКА ПОКУПОК И БАЛЛОВ ====================
async def add_purchase_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка добавления покупки"""
    user = update.effective_user
    text = update.message.text

    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Добавление покупки отменено.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError

        user_info = db.get_user_info(user.id)
        points_earned, new_balance = db.add_purchase(user_info['user_id'], amount)

        response = (
            f"✅ *Покупка зарегистрирована!*\n\n"
            f"💵 Сумма покупки: {amount:.2f} руб.\n"
            f"🎁 Начислено баллов: {points_earned}\n"
            f"💰 Новый баланс: {new_balance} баллов\n"
            f"🎯 Доступная скидка: {new_balance * LOYALTY_SETTINGS['discount_per_point']:.1f}%"
        )

        await update.message.reply_text(
            response,
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат суммы. Введите число (например: 1500.50):",
            reply_markup=get_cancel_keyboard()
        )
        return ADD_PURCHASE


async def spend_points_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка списания баллов"""
    user = update.effective_user
    text = update.message.text

    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Использование баллов отменено.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END

    try:
        points_to_spend = int(text)
        if points_to_spend <= 0:
            raise ValueError

        user_info = db.get_user_info(user.id)
        if points_to_spend > user_info['current_points']:
            await update.message.reply_text(
                f"❌ Недостаточно баллов. Ваш баланс: {user_info['current_points']}\n"
                f"Введите меньшее количество:",
                reply_markup=get_cancel_keyboard()
            )
            return SPEND_POINTS

        context.user_data['points_to_spend'] = points_to_spend
        context.user_data['user_id'] = user_info['user_id']

        await update.message.reply_text(
            "💵 Введите сумму покупки в рублях для расчета скидки:",
            reply_markup=get_cancel_keyboard()
        )
        return CHECK_BALANCE

    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Введите целое число баллов:",
            reply_markup=get_cancel_keyboard()
        )
        return SPEND_POINTS


async def calculate_discount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Расчет скидки"""
    text = update.message.text

    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Использование баллов отменено.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END

    try:
        purchase_amount = float(text)
        if purchase_amount <= 0:
            raise ValueError

        points_to_spend = context.user_data.get('points_to_spend')
        user_id = context.user_data.get('user_id')

        success, new_balance, discount = db.spend_points(
            user_id, points_to_spend, purchase_amount
        )

        if success:
            discount_amount = purchase_amount * discount / 100
            final_amount = purchase_amount - discount_amount

            response = (
                f"✅ *Баллы успешно списаны!*\n\n"
                f"🎁 Списано баллов: {points_to_spend}\n"
                f"📉 Скидка: {discount:.1f}% ({discount_amount:.2f} руб.)\n"
                f"💰 К оплате: {final_amount:.2f} руб.\n"
                f"💳 Изначальная сумма: {purchase_amount:.2f} руб.\n"
                f"📊 Новый баланс: {new_balance} баллов\n\n"
                f"💡 *Совет:* Покажите это сообщение кассиру для применения скидки"
            )

            await update.message.reply_text(
                response,
                parse_mode='Markdown',
                reply_markup=get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "❌ Ошибка при списании баллов.",
                reply_markup=get_main_keyboard()
            )

        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат суммы. Введите число (например: 1500.50):",
            reply_markup=get_cancel_keyboard()
        )
        return CHECK_BALANCE


# ==================== АДМИН ПАНЕЛЬ ====================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вход в админ панель"""
    user = update.effective_user

    if user.id not in ADMINS:
        await update.message.reply_text(
            "❌ Доступ только для администраторов!",
            reply_markup=get_main_keyboard()
        )
        return

    await update.message.reply_text(
        f"👑 *Панель администратора*\n"
        f"Добро пожаловать, {user.first_name}!\n\n"
        f"Сервер: {WEBHOOK_URL}\n"
        f"База данных: {DB_NAME}\n\n"
        f"Выберите действие:",
        parse_mode='Markdown',
        reply_markup=get_admin_keyboard()
    )
    return ADMIN_MENU


async def handle_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок админ панели"""
    user = update.effective_user
    text = update.message.text

    if user.id not in ADMINS:
        await update.message.reply_text(
            "❌ Доступ только для администраторов!",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END

    if text == "📊 Статистика":
        stats = db.get_system_stats()
        stats_message = (
            "📊 *Статистика системы:*\n\n"
            f"👥 Пользователей: {stats['total_users']}\n"
            f"💰 Общий оборот: {stats['total_sales']:.2f} руб.\n"
            f"🎁 Всего баллов в системе: {stats['total_points']}\n"
            f"📈 Средний чек: {stats['avg_purchase']:.2f} руб.\n\n"
            f"⚙️ *Настройки:*\n"
            f"• Баллов за рубль: {LOYALTY_SETTINGS['points_per_purchase'] * 100}%\n"
            f"• Скидка за балл: {LOYALTY_SETTINGS['discount_per_point'] * 100}%\n"
            f"• Макс. скидка: {LOYALTY_SETTINGS['max_discount']}%\n"
            f"• Бонус за регистрацию: {LOYALTY_SETTINGS['welcome_bonus']}\n\n"
            f"🌐 *Сервер:*\n"
            f"• URL: {WEBHOOK_URL}\n"
            f"• Webhook: {WEBHOOK_URL}/evotor/webhook\n"
            f"• База данных: {DB_NAME}"
        )

        await update.message.reply_text(
            stats_message,
            parse_mode='Markdown',
            reply_markup=get_admin_keyboard()
        )

    elif text == "👥 Пользователи":
        users, total = db.get_all_users(limit=10)
        if not users:
            message = "📭 *Пользователей пока нет*"
        else:
            message = f"👥 *Пользователи (всего: {total}):*\n\n"
            for i, user_data in enumerate(users, start=1):
                reg_date = user_data['registration_date']
                date_str = reg_date.split()[0] if reg_date and ' ' in reg_date else reg_date or "Неизвестно"
                qr_text = f" | 📱 {user_data['qr_code']}" if user_data.get('qr_code') else ""
                message += (
                    f"{i}. *{user_data['name']}*\n"
                    f"   🆔 ID: {user_data['user_id']}\n"
                    f"   📱 {user_data['phone']}{qr_text}\n"
                    f"   💰 {user_data['current_points']} баллов\n"
                    f"   🛒 {user_data['total_purchases']:.2f} руб.\n"
                    f"   📅 {date_str}\n\n"
                )

        await update.message.reply_text(
            message,
            parse_mode='Markdown',
            reply_markup=get_admin_keyboard()
        )

    elif text == "➕ Добавить баллы":
        await update.message.reply_text(
            "🎁 Введите ID пользователя и количество баллов через пробел:\n\n"
            "Пример: `1 500` - добавит 500 баллов пользователю с ID 1\n"
            "Пример: `1 -100` - вычтет 100 баллов",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )
        return ADMIN_ADD_USER

    elif text == "✏️ Редактировать пользователя":
        await update.message.reply_text(
            "✏️ Введите ID пользователя для редактирования:",
            reply_markup=get_cancel_keyboard()
        )
        return ADMIN_EDIT_USER

    elif text == "📋 Экспорт данных":
        stats = db.get_system_stats()
        export_text = (
            f"Экспорт данных системы лояльности\n"
            f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"📊 Статистика:\n"
            f"- Пользователей: {stats['total_users']}\n"
            f"- Общий оборот: {stats['total_sales']:.2f} руб.\n"
            f"- Всего баллов: {stats['total_points']}\n"
            f"- Средний чек: {stats['avg_purchase']:.2f} руб.\n\n"
            f"⚙️ Настройки:\n"
            f"- Баллов за рубль: {LOYALTY_SETTINGS['points_per_purchase'] * 100}%\n"
            f"- Скидка за балл: {LOYALTY_SETTINGS['discount_per_point'] * 100}%\n"
            f"- Макс. скидка: {LOYALTY_SETTINGS['max_discount']}%\n"
            f"- Бонус за регистрацию: {LOYALTY_SETTINGS['welcome_bonus']}\n\n"
            f"🌐 Сервер: {WEBHOOK_URL}"
        )

        await update.message.reply_text(
            f"<pre>{export_text}</pre>",
            parse_mode='HTML',
            reply_markup=get_admin_keyboard()
        )

    elif text == "⚙️ Настройки":
        settings_message = (
            "⚙️ *Настройки системы:*\n\n"
            f"🎯 *Текущие настройки:*\n"
            f"• Баллов за рубль: {LOYALTY_SETTINGS['points_per_purchase'] * 100}%\n"
            f"• Скидка за балл: {LOYALTY_SETTINGS['discount_per_point'] * 100}%\n"
            f"• Макс. скидка: {LOYALTY_SETTINGS['max_discount']}%\n"
            f"• Бонус за регистрацию: {LOYALTY_SETTINGS['welcome_bonus']}\n"
            f"• Бонус на день рождения: {LOYALTY_SETTINGS['birthday_bonus']}\n\n"
            f"⚠️ Для изменения настроек требуется редактирование кода.\n\n"
            f"📊 *Техническая информация:*\n"
            f"• Бот токен: {'Установлен' if BOT_TOKEN else 'Не установлен'}\n"
            f"• Админ ID: {YOUR_TELEGRAM_ID}\n"
            f"• База данных: {DB_NAME}\n"
            f"• Вебхук URL: {WEBHOOK_URL}/evotor/webhook"
        )

        await update.message.reply_text(
            settings_message,
            parse_mode='Markdown',
            reply_markup=get_admin_keyboard()
        )

    elif text == "🔙 В главное меню":
        await update.message.reply_text(
            "🔙 Возврат в главное меню...",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END


async def admin_add_points_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка добавления баллов админом"""
    text = update.message.text

    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Добавление баллов отменено.",
            reply_markup=get_admin_keyboard()
        )
        return ADMIN_MENU

    try:
        parts = text.split()
        if len(parts) != 2:
            raise ValueError

        user_id = int(parts[0])
        points = int(parts[1])

        user_info = db.get_user_by_id(user_id)
        if not user_info:
            await update.message.reply_text(
                f"❌ Пользователь с ID {user_id} не найден.",
                reply_markup=get_cancel_keyboard()
            )
            return ADMIN_ADD_USER

        if db.update_user_points(user_id, points, f"Изменение баланса администратором: {points:+d}"):
            new_balance = user_info['current_points'] + points
            await update.message.reply_text(
                f"✅ Пользователю *{user_info['name']}* {'добавлено' if points > 0 else 'списано'} {abs(points)} баллов\n"
                f"💰 Новый баланс: {new_balance} баллов",
                parse_mode='Markdown',
                reply_markup=get_admin_keyboard()
            )
            return ADMIN_MENU
        else:
            await update.message.reply_text(
                "❌ Ошибка при изменении баллов.",
                reply_markup=get_admin_keyboard()
            )
            return ADMIN_MENU

    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Введите: ID пользователя и количество баллов через пробел\n"
            "Пример: 1 500 (добавить 500 баллов)\n"
            "Пример: 1 -100 (убрать 100 баллов)",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )
        return ADMIN_ADD_USER


async def admin_edit_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка редактирования пользователя"""
    text = update.message.text

    if text == "❌ Отмена":
        await update.message.reply_text(
            "❌ Редактирование отменено.",
            reply_markup=get_admin_keyboard()
        )
        return ADMIN_MENU

    try:
        user_id = int(text)
        user_info = db.get_user_by_id(user_id)

        if not user_info:
            await update.message.reply_text(
                f"❌ Пользователь с ID {user_id} не найден.",
                reply_markup=get_cancel_keyboard()
            )
            return ADMIN_EDIT_USER

        await update.message.reply_text(
            f"✏️ *Редактирование пользователя:*\n\n"
            f"👤 Имя: {user_info['name']}\n"
            f"📱 Телефон: {user_info['phone']}\n"
            f"⚤ Пол: {user_info.get('gender', 'Не указан')}\n"
            f"💰 Баланс: {user_info['current_points']} баллов\n"
            f"🛒 Покупок: {user_info['total_purchases']:.2f} руб.\n"
            f"📅 Регистрация: {user_info['registration_date']}\n"
            f"📱 QR код: {user_info.get('qr_code', 'Нет')}\n"
            f"🆔 Telegram ID: {user_info.get('telegram_id', 'Нет')}",
            parse_mode='Markdown',
            reply_markup=get_admin_keyboard()
        )

        await update.message.reply_text(
            "Функция подробного редактирования в разработке.\n"
            "Используйте '➕ Добавить баллы' для изменения баланса.",
            reply_markup=get_admin_keyboard()
        )
        return ADMIN_MENU

    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Введите ID пользователя (число):",
            reply_markup=get_cancel_keyboard()
        )
        return ADMIN_EDIT_USER


# ==================== ОБРАБОТЧИК ОТМЕНЫ ====================
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик отмены"""
    user = update.effective_user

    if user.id in ADMINS:
        await update.message.reply_text(
            "❌ Действие отменено.",
            reply_markup=get_admin_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Действие отменено.",
            reply_markup=get_main_keyboard()
        )
    return ConversationHandler.END


# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================
application = None


# ==================== ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ====================
def main():
    """Запуск бота и вебхука"""
    global application

    print("=" * 60)
    print("🤖 СИСТЕМА ЛОЯЛЬНОСТИ ЭВОТОР")
    print("=" * 60)
    print(f"Python версия: {sys.version}")
    print(f"Токен бота: {'Установлен' if BOT_TOKEN and BOT_TOKEN != '8200085604:AAHyzg31wBdNHDRFxvSWz_wNkFzp9iRRBD0' else 'ТЕСТОВЫЙ'}")
    print(f"Папка: {BASE_DIR}")
    print(f"База данных: {DB_NAME}")
    print(f"WEBHOOK_URL: {WEBHOOK_URL}")
    
    # Проверяем существование requirements.txt
    req_file = os.path.join(BASE_DIR, 'requirements.txt')
    if os.path.exists(req_file):
        print(f"✅ requirements.txt найден: {req_file}")
    else:
        print(f"⚠️  requirements.txt не найден в: {req_file}")
        print("Список файлов в папке:")
        for file in os.listdir(BASE_DIR):
            print(f"  - {file}")

    if BOT_TOKEN == "8200085604:AAHyzg31wBdNHDRFxvSWz_wNkFzp9iRRBD0":
        print("⚠️  ВНИМАНИЕ: Используется тестовый токен!")
        print("⚠️  Получите реальный токен у @BotFather")

    print(f"🔑 Админ ID: {YOUR_TELEGRAM_ID}")
    print(f"👑 Всего админов: {len(ADMINS)}")
    print(f"💾 База данных: {DB_NAME}")
    print(f"🎁 Бонус за регистрацию: {LOYALTY_SETTINGS['welcome_bonus']} баллов")
    print(f"🌐 Сервер: {WEBHOOK_URL}")
    print(f"📱 Вебхук: {WEBHOOK_URL}/evotor/webhook")
    print(f"📊 Настройки: {LOYALTY_SETTINGS['points_per_purchase'] * 100}% баллов за рубль")
    print("=" * 60)

    # Проверяем базу данных
    try:
        stats = db.get_system_stats()
        print(f"📊 Статистика: {stats['total_users']} пользователей, {stats['total_sales']:.2f} руб. оборот")
    except Exception as e:
        print(f"⚠️  Ошибка БД: {e}")

    # Создаем Application для бота
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        print("✅ Telegram бот инициализирован")
    except Exception as e:
        print(f"❌ Ошибка инициализации бота: {e}")
        print("Проверьте токен бота")
        return

    # Обработчик регистрации пользователей
    user_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            PHONE: [
                MessageHandler(
                    filters.CONTACT | filters.TEXT & ~filters.COMMAND,
                    get_phone
                )
            ],
            NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)
            ],
            GENDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_gender)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_handler)],
    )

    # Обработчик покупок и баллов
    purchase_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["➕ Добавить покупку"]), handle_buttons)],
        states={
            ADD_PURCHASE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_purchase_handler)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_handler)],
    )

    points_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["🎁 Использовать баллы"]), handle_buttons)],
        states={
            SPEND_POINTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, spend_points_handler)
            ],
            CHECK_BALANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, calculate_discount_handler)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_handler)],
    )

    # Обработчик админ панели
    admin_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('admin', admin_panel)],
        states={
            ADMIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_buttons)
            ],
            ADMIN_ADD_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_points_handler)
            ],
            ADMIN_EDIT_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_edit_user_handler)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_handler)],
    )

    # Регистрируем обработчики
    application.add_handler(user_conv_handler)
    application.add_handler(purchase_conv_handler)
    application.add_handler(points_conv_handler)
    application.add_handler(admin_conv_handler)

    # Обработчик кнопок (для остальных кнопок)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    # Команда помощи
    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id in ADMINS:
            await update.message.reply_text(
                "👑 *Команды администратора:*\n"
                "/admin - Панель администратора\n"
                "/start - Перезапуск бота\n\n"
                f"🌐 *Сервер:* {WEBHOOK_URL}",
                parse_mode='Markdown',
                reply_markup=get_admin_keyboard()
            )
        else:
            await update.message.reply_text(
                "🆘 *Помощь:*\n"
                "/start - Начать регистрацию\n"
                "/help - Эта справка\n\n"
                "Используйте кнопки для навигации",
                parse_mode='Markdown',
                reply_markup=get_main_keyboard()
            )

    application.add_handler(CommandHandler('help', help_command))

    # Команда для проверки статуса
    async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        stats = db.get_system_stats()

        status_text = (
            f"📊 *Статус системы:*\n\n"
            f"👥 Пользователей: {stats['total_users']}\n"
            f"💰 Оборот: {stats['total_sales']:.2f} руб.\n"
            f"🎁 Баллов в системе: {stats['total_points']}\n"
            f"🌐 Сервер: {WEBHOOK_URL}\n"
            f"✅ Система работает нормально"
        )

        await update.message.reply_text(
            status_text,
            parse_mode='Markdown',
            reply_markup=get_admin_keyboard() if user.id in ADMINS else get_main_keyboard()
        )

    application.add_handler(CommandHandler('status', status_command))

    print("\n✅ Система готова к работе!")
    print("\n📱 *Инструкция:*")
    print("1. Откройте Telegram и найдите вашего бота")
    print("2. Нажмите START или отправьте /start")
    print("3. Следуйте инструкциям регистрации")
    print("4. Получите QR код для кассы")
    print("\n👑 *Админ панель:*")
    print("• Отправьте команду /admin")
    print("• Или используйте Telegram ID:", YOUR_TELEGRAM_ID)
    print("\n🌐 *Интеграция с Эвотор:*")
    print("1. URL вебхука:", f"{WEBHOOK_URL}/evotor/webhook")
    print("2. Установите скрипт на терминал Эвотор")
    print("3. Клиенты показывают QR код на кассе")
    print("4. Баллы начисляются автоматически")
    print("=" * 60)

    try:
        if IS_RENDER or 'PYTHONANYWHERE_DOMAIN' in os.environ:
            print("🌐 Cloud режим: Запуск FastAPI сервера...")
            # На Render запускаем uvicorn
            port = int(os.environ.get("PORT", 10000))
            uvicorn.run(app, host="0.0.0.0", port=port)
        else:
            print("🚀 Локальный запуск: Запуск бота в режиме polling...")
            # Локально запускаем вебхук в отдельном потоке
            webhook_thread = threading.Thread(
                target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000),
                daemon=True
            )
            webhook_thread.start()
            print("✅ Вебхук запущен: http://localhost:8000")
            application.run_polling()
    except Exception as e:
        print(f"❌ Ошибка запуска: {e}")
        print("Проверьте токен и подключение к интернету")


if __name__ == '__main__':
    main()
