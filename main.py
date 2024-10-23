import os
import logging
from flask import Flask, request, jsonify
import openai
from langdetect import detect, LangDetectException
import asyncio
from functools import lru_cache
import json
from psycopg2 import pool
import aiohttp
from dotenv import load_dotenv
import requests

# Загрузка переменных окружения из .env файла
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

required_env_vars = ['GREEN_API_URL', 'GREEN_ID_INSTANCE', 'GREEN_API_TOKEN_INSTANCE',
                     'TIMESCALE_CONNECTION_STRING', 'OPENAI_API_KEY', 'GREEN_PHONE_NUMBER']


missing_env_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_env_vars:
    raise ValueError(f"Отсутствуют следующие обязательные переменные окружения: {', '.join(missing_env_vars)}")

# Инициализация ключа OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

app = Flask(__name__)

# Получение Green API клиента
green_api_client = requests.Session()
green_api_headers = {
    "Authorization": f"Bearer {os.getenv('GREEN_API_TOKEN_INSTANCE')}",
    "instanceId": os.getenv('GREEN_ID_INSTANCE')
}


# Глобальный объект ClientSession
global_session = None

# Создание пула соединений с PostgreSQL
connection_pool = pool.ThreadedConnectionPool(
    1,  # minconn
    20,  # maxconn
    host=os.getenv('POSTGRESQL_HOST'),
    database=os.getenv('POSTGRESQL_DB'),
    user=os.getenv('POSTGRESQL_USER'),
    password=os.getenv('POSTGRESQL_PASSWORD')
)


class CompanySettings:
    def __init__(self):
        self.name = ""
        self.description = ""
        self.contact_info = {}
        self.price_list = {}
        self.products = []

    def load_from_db(self):
        query = """
        SELECT name, description, contact_info, price_list, products
        FROM company_settings
        """
        with connection_pool.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                settings = cur.fetchone()

                if settings:
                    self.name = settings['name']
                    self.description = settings['description']
                    self.contact_info = json.loads(settings['contact_info']) if settings['contact_info'] else {}
                    self.price_list = json.loads(settings['price_list']) if settings['price_list'] else {}
                    self.products = json.loads(settings['products']) if settings['products'] else []
                else:
                    logging.warning("Настройки компании не найдены в базе данных")

    def save_to_db(self):
        query = """
        INSERT INTO company_settings (name, description, contact_info, price_list, products)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
        SET name = EXCLUDED.name,
            description = EXCLUDED.description,
            contact_info = EXCLUDED.contact_info,
            price_list = EXCLUDED.price_list,
            products = EXCLUDED.products
        """
        with connection_pool.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (
                    self.name,
                    self.description,
                    json.dumps(self.contact_info),
                    json.dumps(self.price_list),
                    json.dumps(self.products)
                ))
                conn.commit()


class SalesScript:
    def __init__(self):
        self.steps = []

    def load_from_db(self):
        query = """
        SELECT steps
        FROM sales_script
        """
        with connection_pool.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                script = cur.fetchone()

                if script:
                    self.steps = json.loads(script['steps']) if script['steps'] else []
                else:
                    logging.warning("Сценарий продажи не найден в базе данных")

    def save_to_db(self):
        query = """
        INSERT INTO sales_script (steps)
        VALUES (%s)
        ON CONFLICT (id) DO UPDATE
        SET steps = EXCLUDED.steps
        """
        with connection_pool.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (json.dumps(self.steps),))
                conn.commit()


async def create_tables():
    query_create_chat_history_table = """
    CREATE TABLE IF NOT EXISTS chat_history (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        message_role TEXT,
        message_content TEXT,
        timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """
    query_create_users_table = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        whatsapp_id TEXT UNIQUE,
        name TEXT,
        language TEXT DEFAULT 'en'
    );
    """
    query_create_company_settings_table = """
    CREATE TABLE IF NOT EXISTS company_settings (
        id SERIAL PRIMARY KEY,
        name TEXT,
        description TEXT,
        contact_info JSONB,
        price_list JSONB,
        products JSONB
    );
    """
    query_create_sales_script_table = """
    CREATE TABLE IF NOT EXISTS sales_script (
        id SERIAL PRIMARY KEY,
        steps JSONB
    );
    """
    query_add_index = """
    CREATE INDEX IF NOT EXISTS idx_chat_history_user_id ON chat_history(user_id);
    CREATE INDEX IF NOT EXISTS idx_chat_history_timestamp ON chat_history(timestamp);
    """
    execute_query(query_create_chat_history_table)
    execute_query(query_create_users_table)
    execute_query(query_create_company_settings_table)
    execute_query(query_create_sales_script_table)
    execute_query(query_add_index)
    

def execute_query(query, params=None, return_data=True):
    with connection_pool.getconn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            if return_data:
                return cur.fetchall()


async def send_message(to, body):
    try:
        async with aiohttp.ClientSession() as session:
            url = os.getenv('GREEN_API_URL')

            payload = {
                "from": os.getenv('GREEN_PHONE_NUMBER'),
                "to": to,
                "body": body
            }

            headers = {
                "Authorization": f"Bearer {os.getenv('GREEN_API_TOKEN_INSTANCE')}",
                "instanceId": os.getenv('GREEN_ID_INSTANCE')
            }

            async with session.post(url, json=payload, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
                return data['sid']

    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения через Green API: {str(e)}")
        raise Exception(f"Ошибка при отправке сообщения: {str(e)}")


async def save_message(user_id, message_role, message_content):
    query = """
    INSERT INTO chat_history (user_id, message_role, message_content)
    VALUES (%s, %s, %s);
    """
    with connection_pool.getconn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (user_id, message_role, message_content))
            conn.commit()


async def get_chat_history(user_id):
    query = """
    SELECT message_role, message_content 
    FROM chat_history 
    WHERE user_id = %s 
    ORDER BY timestamp DESC 
    LIMIT 10;
    """
    result = execute_query(query, (user_id,), return_data=False)
    return [(role, content) for role, content in result]


@lru_cache(maxsize=128)
def detect_language(message):
    try:
        detected_language = detect(message)
        if detected_language == 'ru':
            return 'ru'
        elif detected_language == 'en':
            return 'en'
        else:
            return 'en'  # По умолчанию возвращаем английский язык
    except LangDetectException as e:
        logging.warning(f"Ошибка при определении языка: {str(e)}")
        return 'en'


async def get_user_info(whatsapp_id):
    query = """
    SELECT name, language FROM users WHERE whatsapp_id = %s;
    """
    result = execute_query(query, (whatsapp_id,))
    return result[0] if result else (None, 'en')


async def save_ai_response(user_id, ai_response):
    try:
        await save_message(user_id, 'ai', ai_response)
    except Exception as e:
        logging.error(f"Ошибка при сохранении ответа AI для пользователя {user_id}: {str(e)}")
        raise


def check_user_exists(whatsapp_id):
    query = """
    SELECT COUNT(*) > 0
    FROM users
    WHERE whatsapp_id = %s;
    """
    result = execute_query(query, (whatsapp_id,))
    return len(result) > 0


def save_user(whatsapp_id, name):
    query = """
    INSERT INTO users (whatsapp_id, name, language)
    VALUES (%s, %s, %s)
    """
    execute_query(query, (whatsapp_id, name, 'en'))


def update_user_language(whatsapp_id, language):
    query = """
    UPDATE users
    SET language = %s
    WHERE whatsapp_id = %s
    """
    execute_query(query, (language, whatsapp_id))


@app.route('/bot', methods=['POST'])
def handle_whatsapp():
    try:
        incoming_msg = request.values.get('Body', '').lower()
        whatsapp_id = request.values.get('From')

        # Сохранение сообщения пользователя
        save_message(whatsapp_id, 'user', incoming_msg)

        # Проверка существования пользователя
        if not check_user_exists(whatsapp_id):
            save_user(whatsapp_id, incoming_msg)

        # Определение языка пользователя
        user_language = detect_language(incoming_msg)

        # Обновление языка пользователя в базе данных
        update_user_language(whatsapp_id, user_language)

        # Получение ответа от ИИ-помощника
        ai_response = get_ai_response(whatsapp_id, incoming_msg)

        # Сохранение ответа ИИ-помощи
        save_ai_response(whatsapp_id, ai_response)

        # Отправка сообщения клиенту
        message_sid = send_message(whatsapp_id, ai_response)

        return jsonify({"message": "Сообщение успешно отправлено.", "sid": message_sid})

    except Exception as e:
        logging.error(f"Ошибка при обработке запроса WhatsApp: {str(e)}")
        return f"Произошла ошибка при обработке вашего запроса: {str(e)}"


async def get_ai_response(whatsapp_id, message):
    global global_session

    if global_session is None:
        global_session = await aiohttp.ClientSession().__aenter__()

    openai_api_url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"
    }
    data = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {
                "role": "system",
                "content": "Вы работаете как ассистент поддержки клиентов."
                           " Ваш ответ должен быть профессиальным, дружелюбным "
                           "и соответствовать предпочтениям языка пользователя."
            },
            {
                "role": "user",
                "content": f"Пользователь {whatsapp_id}: {message}"
            }
        ],
        "temperature": 0.7,
        "max_tokens": 300
    }

    try:
        async with global_session.post(openai_api_url, headers=headers, json=data) as response:
            if response.status == 200:
                result = await response.json()
                return result["choices"][0]["message"]["content"]
            else:
                error_message = await response.text()
                logging.error(f"Ошибка при получении ответа от OpenAI API: {error_message}")
                raise Exception(f"Ошибка API OpenAI: {error_message}")

    except Exception as e:
        logging.error(f"Неожиданная ошибка при получении ответа от OpenAI API: {str(e)}")
        raise Exception(f"Произошла неожиданная ошибка при обработке запроса: {str(e)}")

    finally:
        await global_session.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(create_tables())
    app.run(host='0.0.0.0', port=5000)
