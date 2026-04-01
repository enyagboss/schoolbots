import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
import sqlite3
import random
import time
import threading
import datetime
import logging
import re
import json
from typing import Optional, List, Dict, Any
import zoneinfo
from datetime import datetime

# Часовой пояс бота (например, Москва)
TIMEZONE = zoneinfo.ZoneInfo("Europe/Moscow")

# ==================== КОНФИГУРАЦИЯ ====================
GROUP_TOKEN = "vk1.a.DIHs3tDsCYSMzetUdq6Yxkr9q8LLFgxOkERo-n3ffmiG41yCgQfV1mnbqjt94iKHShAKCfUcEwLGMUMw3zTWzd9-oWdQDyZaV9GruPKBuY2mV7q-mwxvkBQwQSEJH_HzCE6Tt67cbWCBSO065fj44d94Ki_gDQizWnAeh4hmC-5vyzf8a08O28ueyPDC57NlihPcxlbAIseqZjwoMSa9bQ"
GROUP_ID = 236907251
PSYCHOLOGIST_IDS = [159256205]

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
vk_session = vk_api.VkApi(token=GROUP_TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, GROUP_ID)

# ==================== БАЗА ДАННЫХ ====================
conn = sqlite3.connect('bot_database.db', check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.Lock()

cursor.executescript('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    role TEXT DEFAULT 'user',
    reminders_enabled INTEGER DEFAULT 0,
    reminder_time TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    contact TEXT,
    timestamp TEXT,
    answered INTEGER DEFAULT 0,
    answer_text TEXT DEFAULT NULL,
    answer_timestamp TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    time TEXT,
    repeat_type TEXT DEFAULT 'once',
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS daily_motivation (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER DEFAULT 0,
    time TEXT DEFAULT '08:00'
);

CREATE TABLE IF NOT EXISTS user_states (
    user_id INTEGER PRIMARY KEY,
    state TEXT,
    updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS game_stats (
    user_id INTEGER,
    game_type TEXT,
    games_played INTEGER DEFAULT 0,
    correct_answers INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, game_type)
);
''')
conn.commit()

# ==================== ЗАГРУЗКА ВОПРОСОВ ДЛЯ ИГР ====================
def load_questions_emojis():
    try:
        with open('questions_emojis.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("Файл questions_emojis.json не найден")
        return []

def load_questions_scenarios():
    try:
        with open('questions_scenarios.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("Файл questions_scenarios.json не найден")
        return []

QUESTIONS_EMOJIS = load_questions_emojis()
QUESTIONS_SCENARIOS = load_questions_scenarios()

# Хранилище активных таймеров (в памяти)
active_timers = {}

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_keyboard(role: str = 'user') -> VkKeyboard:
    keyboard = VkKeyboard(one_time=False)
    if role == 'user':
        keyboard.add_button('📚 Помощь по темам', color=VkKeyboardColor.PRIMARY)
        keyboard.add_button('📊 Тесты', color=VkKeyboardColor.PRIMARY)
        keyboard.add_line()
        keyboard.add_button('💡 Мотивация', color=VkKeyboardColor.POSITIVE)
        keyboard.add_button('🆘 Совет', color=VkKeyboardColor.POSITIVE)
        keyboard.add_line()
        keyboard.add_button('🎮 Игры', color=VkKeyboardColor.PRIMARY)
        keyboard.add_button('📝 Обратиться к психологу', color=VkKeyboardColor.NEGATIVE)
        keyboard.add_line()
        keyboard.add_button('⏰ Напомнить о событии', color=VkKeyboardColor.SECONDARY)
        keyboard.add_button('☀️ Ежедневные советы', color=VkKeyboardColor.SECONDARY)
    elif role == 'psychologist':
        keyboard.add_button('📋 Список обращений', color=VkKeyboardColor.PRIMARY)
        keyboard.add_button('📖 Инструкция', color=VkKeyboardColor.PRIMARY)
    return keyboard

def send_message(user_id: int, text: str, keyboard: Optional[VkKeyboard] = None, attempts: int = 3):
    for i in range(attempts):
        try:
            vk.messages.send(
                user_id=user_id,
                message=text[:4096],
                random_id=random.randint(1, 2**31),
                keyboard=keyboard.get_keyboard() if keyboard else None
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения {user_id}: {e}, попытка {i+1}")
            time.sleep(1)
    return False

def save_state(user_id: int, state: Dict[str, Any]):
    """Сохраняет состояние пользователя в БД (исключая несериализуемые объекты)"""
    # Создаём копию, удаляем timer, если есть
    copy_state = state.copy()
    with db_lock:
        cursor.execute('''
            INSERT OR REPLACE INTO user_states (user_id, state, updated)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, json.dumps(copy_state, ensure_ascii=False)))
        conn.commit()

def get_state(user_id: int) -> Optional[Dict[str, Any]]:
    with db_lock:
        cursor.execute('SELECT state FROM user_states WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
    return None

def clear_state(user_id: int):
    with db_lock:
        cursor.execute('DELETE FROM user_states WHERE user_id = ?', (user_id,))
        conn.commit()

def update_game_stats(user_id: int, game_type: str, correct: int = 0, games_increment: int = 0):
    with db_lock:
        cursor.execute('''
            INSERT INTO game_stats (user_id, game_type, games_played, correct_answers)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, game_type) DO UPDATE SET
                games_played = games_played + ?,
                correct_answers = correct_answers + ?
        ''', (user_id, game_type, games_increment, correct, games_increment, correct))
        conn.commit()

def get_game_stats(user_id: int) -> Dict[str, Dict[str, int]]:
    with db_lock:
        cursor.execute('SELECT game_type, games_played, correct_answers FROM game_stats WHERE user_id = ?', (user_id,))
        rows = cursor.fetchall()
        stats = {}
        for game_type, games_played, correct_answers in rows:
            stats[game_type] = {'games_played': games_played, 'correct_answers': correct_answers}
        return stats

def save_appeal(user_id: int, text: str, contact: Optional[str] = None):
    with db_lock:
        cursor.execute('''
            INSERT INTO appeals (user_id, text, contact, timestamp)
            VALUES (?, ?, ?, ?)
        ''', (user_id, text, contact, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        appeal_id = cursor.lastrowid
    for psych_id in PSYCHOLOGIST_IDS:
        send_message(psych_id, f"📩 Новое обращение #{appeal_id} от пользователя. Используйте /список для просмотра.")
    return appeal_id

def get_unanswered_appeals() -> List[tuple]:
    with db_lock:
        cursor.execute('''
            SELECT id, user_id, text, contact, timestamp FROM appeals
            WHERE answered = 0 ORDER BY timestamp ASC
        ''')
        return cursor.fetchall()

def answer_appeal(appeal_id: int, answer_text: str, psychologist_id: int):
    with db_lock:
        cursor.execute('SELECT answered, user_id FROM appeals WHERE id = ?', (appeal_id,))
        row = cursor.fetchone()
        if not row or row[0] == 1:
            return False
        user_id = row[1]
        cursor.execute('''
            UPDATE appeals SET answered = 1, answer_text = ?, answer_timestamp = ?
            WHERE id = ?
        ''', (answer_text, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), appeal_id))
        conn.commit()
    send_message(user_id, f"🎓 Ответ психолога:\n{answer_text}")
    return True

# ==================== СЦЕНАРИИ ДЛЯ ПОЛЬЗОВАТЕЛЯ ====================
def handle_help_themes(user_id: int):
    keyboard = VkKeyboard(one_time=False)
    topics = ['Стресс', 'Конфликты', 'Мотивация к учебе', 'Здоровый образ жизни',
              'Буллинг', 'Тревога', 'Сон', 'Организация пространства']
    for i in range(0, len(topics), 4):
        for topic in topics[i:i+4]:
            keyboard.add_button(topic, color=VkKeyboardColor.PRIMARY)
        keyboard.add_line()
    keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
    send_message(user_id, "Выберите тему:", keyboard)

def handle_stress_menu(user_id: int):
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('Пройти тест на стресс', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('Советы при стрессе', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('Дыхательное упражнение', color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
    send_message(user_id, "Что вы хотите сделать?", keyboard)

def handle_stress_tips(user_id: int):
    tips = [
        "Глубокое дыхание: вдох на 4 счета, задержка на 7, выдох на 8. Повтори 3-5 раз.",
        "Прогулка на свежем воздухе хотя бы 10 минут помогает снизить уровень кортизола.",
        "Попробуй метод «5-4-3-2-1»: назови 5 предметов вокруг, 4 звука, 3 тактильных ощущения, 2 запаха, 1 вкус.",
        "Запиши свои мысли в блокнот — это помогает структурировать тревогу."
    ]
    send_message(user_id, random.choice(tips))

def handle_breathing_exercise(user_id: int):
    text = ("🧘 Простое дыхательное упражнение:\n"
            "1. Сядь удобно и закрой глаза.\n"
            "2. Медленно вдохни через нос на 4 секунды.\n"
            "3. Задержи дыхание на 7 секунд.\n"
            "4. Медленно выдохни через рот на 8 секунд.\n"
            "Повтори 3 раза. Это поможет успокоиться.")
    send_message(user_id, text)

def handle_conflict_menu(user_id: int):
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('Как разрешить конфликт?', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('Помощь в диалоге', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('Что делать при буллинге?', color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
    send_message(user_id, "Выберите вариант:", keyboard)

def handle_conflict_resolution(user_id: int):
    text = ("Если возник конфликт:\n"
            "1. Сохраняй спокойствие, не отвечай агрессией.\n"
            "2. Попробуй поговорить наедине, используя «Я-сообщения» (мне обидно, когда...).\n"
            "3. Слушай собеседника, не перебивай.\n"
            "4. Если не получается — обратись к учителю или психологу.")
    send_message(user_id, text)

def handle_dialog_help(user_id: int):
    send_message(user_id, "Напиши, что ты хочешь сказать человеку, а я помогу оформить сообщение вежливо.")
    save_state(user_id, {'scenario': 'compose_message', 'step': 'get_text'})

def handle_bullying_advice(user_id: int):
    text = ("Что делать, если ты столкнулся с буллингом:\n"
            "• Не молчи, расскажи взрослым (учителю, родителям, психологу).\n"
            "• Поддерживай тех, кого обижают.\n"
            "• Не участвуй в травле.\n"
            "• Записывай факты (даты, имена, свидетели).\n"
            "• Обратись за помощью в школьную службу медиации.")
    send_message(user_id, text)

def handle_tests_menu(user_id: int):
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('Тест на стресс', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('Тест на тревожность', color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
    send_message(user_id, "Выберите тест:", keyboard)

def start_stress_test(user_id: int):
    questions = [
        "Ты часто чувствуешь усталость?",
        "Тебе сложно сосредоточиться на уроках?",
        "Ты испытываешь раздражение без причины?",
        "Тебе трудно заснуть или ты просыпаешься ночью?",
        "Ты чувствуешь тревогу или беспокойство?"
    ]
    save_state(user_id, {'scenario': 'stress_test', 'step': 0, 'answers': [], 'questions': questions})
    send_message(user_id, f"Вопрос 1/5: {questions[0]} (Ответь Да/Нет)")

def start_anxiety_test(user_id: int):
    questions = [
        "Я чувствую себя напряжённым.",
        "Я испытываю беспокойство без причины.",
        "Мне трудно сосредоточиться из-за тревоги.",
        "Я боюсь, что что-то пойдёт не так.",
        "У меня бывают проблемы со сном из-за переживаний."
    ]
    save_state(user_id, {'scenario': 'anxiety_test', 'step': 0, 'answers': [], 'questions': questions})
    send_message(user_id, f"Вопрос 1/5: {questions[0]} (Ответь Да/Нет)")

def handle_motivation(user_id: int):
    quotes = [
        "Каждый день — новая возможность стать лучше.",
        "Не бойся трудностей — они делают тебя сильнее.",
        "Ты способен на большее, чем думаешь.",
        "Улыбка — самый простой способ изменить мир вокруг.",
        "Верь в себя и свои силы."
    ]
    send_message(user_id, random.choice(quotes))

def handle_advice(user_id: int):
    tips = [
        "Если чувствуешь усталость — сделай перерыв, глубоко вдохни и выдохни.",
        "Не бойся просить помощи у взрослых — учителей, родителей, психолога.",
        "Старайся поддерживать дружелюбные отношения с одноклассниками.",
        "Занимайся спортом и гуляй на свежем воздухе — это помогает справиться со стрессом.",
        "Ошибки — часть обучения, не бойся их делать и учиться на них."
    ]
    send_message(user_id, random.choice(tips))

def handle_appeal_start(user_id: int):
    send_message(user_id, "Напиши своё обращение. Если хочешь оставить контакт (email или телефон), укажи его в конце сообщения через пробел. Или напиши 'анонимно', чтобы остаться полностью анонимным.")
    save_state(user_id, {'scenario': 'appeal', 'step': 'get_text'})

def handle_reminder_start(user_id: int):
    send_message(user_id, "Напиши текст напоминания (не более 200 символов).")
    save_state(user_id, {'scenario': 'reminder', 'step': 'get_text'})

def handle_daily_motivation_menu(user_id: int):
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('Включить', color=VkKeyboardColor.POSITIVE)
    keyboard.add_button('Выключить', color=VkKeyboardColor.NEGATIVE)
    keyboard.add_button('Изменить время', color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
    send_message(user_id, "Настройка ежедневных мотивационных сообщений:", keyboard)

def set_daily_motivation(user_id: int, enabled: bool):
    with db_lock:
        cursor.execute('''
            INSERT OR REPLACE INTO daily_motivation (user_id, enabled, time)
            VALUES (?, ?, COALESCE((SELECT time FROM daily_motivation WHERE user_id=?), '08:00'))
        ''', (user_id, 1 if enabled else 0, user_id))
        conn.commit()
    status = "включены" if enabled else "выключены"
    send_message(user_id, f"Ежедневные советы {status}.")

def change_daily_motivation_time(user_id: int):
    send_message(user_id, "Введите новое время в формате ЧЧ:ММ (например, 09:30).")
    save_state(user_id, {'scenario': 'change_daily_time'})

def update_daily_time(user_id: int, time_str: str):
    if re.match(r'^\d{2}:\d{2}$', time_str):
        with db_lock:
            cursor.execute('UPDATE daily_motivation SET time = ? WHERE user_id = ?', (time_str, user_id))
            conn.commit()
        send_message(user_id, f"Время ежедневных советов изменено на {time_str}.")
    else:
        send_message(user_id, "Неверный формат. Используйте ЧЧ:ММ.")
    clear_state(user_id)

# ==================== ИГРЫ ====================
def games_menu(user_id: int):
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('🎴 Закон в картинках', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('⚖️ Правонарушение или нет?', color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button('📊 Статистика', color=VkKeyboardColor.SECONDARY)
    keyboard.add_button('🏠 Главное меню', color=VkKeyboardColor.SECONDARY)
    send_message(user_id, "🎮 **Выберите игру:**", keyboard)

def show_stats(user_id: int):
    stats = get_game_stats(user_id)
    if not stats:
        send_message(user_id, "📊 Вы ещё не играли ни в одну игру. Начните через меню 'Игры'.")
        return
    msg = "📊 **Ваша статистика игр:**\n\n"
    for game_type, data in stats.items():
        if game_type == 'emojis':
            msg += f"🎴 Закон в картинках:\n"
        else:
            msg += f"⚖️ Правонарушение или нет?:\n"
        msg += f"   Сыграно игр: {data['games_played']}\n"
        msg += f"   Правильных ответов: {data['correct_answers']}\n"
        if data['games_played'] > 0:
            avg = data['correct_answers'] / data['games_played']
            msg += f"   Среднее за игру: {avg:.1f}\n"
        msg += "\n"
    send_message(user_id, msg)

def start_game_emojis(user_id: int):
    if not QUESTIONS_EMOJIS:
        send_message(user_id, "❌ Вопросы для игры не загружены. Обратитесь к администратору.")
        return
    save_state(user_id, {
        'scenario': 'game_emojis',
        'questions': QUESTIONS_EMOJIS,
        'current_question': 0,
        'score': 0,
        'total': len(QUESTIONS_EMOJIS)
    })
    send_question_emojis(user_id)

def send_question_emojis(user_id: int):
    state = get_state(user_id)
    if not state or state.get('scenario') != 'game_emojis':
        return
    q_index = state['current_question']
    if q_index >= state['total']:
        finish_game_emojis(user_id)
        return
    q = state['questions'][q_index]
    keyboard = VkKeyboard(one_time=False, inline=True)
    # Каждый вариант ответа на новой строке
    for opt in q['options']:
        keyboard.add_button(opt, color=VkKeyboardColor.PRIMARY)
        keyboard.add_line()
    # Кнопки управления
    keyboard.add_button('⏩ Пропустить', color=VkKeyboardColor.SECONDARY)
    keyboard.add_line()
    keyboard.add_button('🏁 Завершить игру', color=VkKeyboardColor.NEGATIVE)
    msg = f"🎨 **Вопрос {q_index+1}/{state['total']}**\n\n{q['emoji_scene']}\n\nВыберите правильный вариант:"
    send_message(user_id, msg, keyboard)

def handle_emojis_answer(user_id: int, answer_index: int):
    state = get_state(user_id)
    if not state or state.get('scenario') != 'game_emojis':
        return
    q_index = state['current_question']
    if q_index >= state['total']:
        finish_game_emojis(user_id)
        return
    q = state['questions'][q_index]
    correct = q['correct']
    if answer_index == correct:
        state['score'] += 1
        result_msg = f"✅ Правильно! {q['explanation']}"
    else:
        correct_text = q['options'][correct]
        result_msg = f"❌ Неправильно. Правильный ответ: {correct_text}\n\n{q['explanation']}"
    send_message(user_id, result_msg)
    state['current_question'] += 1
    save_state(user_id, state)
    send_question_emojis(user_id)

def finish_game_emojis(user_id: int):
    state = get_state(user_id)
    if not state:
        return
    total = state['total']
    score = state['score']
    update_game_stats(user_id, 'emojis', correct=score, games_increment=1)
    msg = f"🏆 **Игра окончена!**\nПравильных ответов: {score}/{total}\n"
    if score == total:
        msg += "🎉 Отлично! Вы знаток законов!"
    elif score > total//2:
        msg += "👍 Хороший результат, повторите материал."
    else:
        msg += "📚 Рекомендуем изучить законы ещё раз."
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('🎮 Игры', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('🏠 Главное меню', color=VkKeyboardColor.SECONDARY)
    send_message(user_id, msg, keyboard)
    clear_state(user_id)

def skip_emojis_question(user_id: int):
    state = get_state(user_id)
    if not state or state.get('scenario') != 'game_emojis':
        return
    state['current_question'] += 1
    save_state(user_id, state)
    send_question_emojis(user_id)

def scenario_timeout(user_id: int):
    logger.info(f"Таймер сработал для {user_id}")
    # Удаляем таймер из словаря (он уже отработал)
    if user_id in active_timers:
        del active_timers[user_id]

    state = get_state(user_id)
    if not state or state.get('scenario') != 'game_scenarios':
        return
    if state.get('timeout_processed'):
        return
    state['timeout_processed'] = True
    q_index = state['current_question']
    if q_index >= state['total']:
        return
    q = state['questions'][q_index]
    correct_answer = "нарушение" if q['is_offense'] else "не нарушение"
    explanation = q['explanation']
    send_message(user_id, f"⏰ Время вышло!\nПравильный ответ: {correct_answer}\n\n{explanation}")
    state['current_question'] += 1
    save_state(user_id, state)
    if state['current_question'] < state['total']:
        send_scenario_question(user_id)
    else:
        finish_game_scenarios(user_id)

def start_game_scenarios(user_id: int):
    if not QUESTIONS_SCENARIOS:
        send_message(user_id, "❌ Вопросы для игры не загружены. Обратитесь к администратору.")
        return
    save_state(user_id, {
        'scenario': 'game_scenarios',
        'questions': QUESTIONS_SCENARIOS,
        'current_question': 0,
        'score': 0,
        'total': len(QUESTIONS_SCENARIOS),
        'timeout_processed': False
        # timer не сохраняем
    })
    send_scenario_question(user_id)

def send_scenario_question(user_id: int):
    state = get_state(user_id)
    if not state or state.get('scenario') != 'game_scenarios':
        return
    q_index = state['current_question']
    if q_index >= state['total']:
        finish_game_scenarios(user_id)
        return
    q = state['questions'][q_index]
    keyboard = VkKeyboard(one_time=False, inline=True)
    keyboard.add_button('⚠️ Нарушение', color=VkKeyboardColor.PRIMARY, payload={'choice': 1})
    keyboard.add_button('✅ Не нарушение', color=VkKeyboardColor.PRIMARY, payload={'choice': 0})
    keyboard.add_line()
    keyboard.add_button('🏁 Завершить игру', color=VkKeyboardColor.NEGATIVE, payload={'finish': True})
    msg = f"📖 **Ситуация {q_index+1}/{state['total']}**\n\n{q['situation']}\n\n⏳ У вас есть **10 секунд** на ответ."
    send_message(user_id, msg, keyboard)
    state['timeout_processed'] = False
    save_state(user_id, state)

    # Создаём таймер и сохраняем в глобальном словаре
    timer = threading.Timer(10.0, scenario_timeout, args=[user_id])
    timer.daemon = True
    timer.start()
    active_timers[user_id] = timer

def handle_scenario_answer(user_id: int, choice: int):
    state = get_state(user_id)
    if not state or state.get('scenario') != 'game_scenarios':
        return
    if state.get('timeout_processed'):
        return

    # Отменяем таймер, если он ещё есть
    if user_id in active_timers:
        active_timers[user_id].cancel()
        del active_timers[user_id]

    q_index = state['current_question']
    if q_index >= state['total']:
        finish_game_scenarios(user_id)
        return
    q = state['questions'][q_index]
    correct = 1 if q['is_offense'] else 0
    if choice == correct:
        state['score'] += 1
        result_msg = f"✅ Правильно! {q['explanation']}"
    else:
        correct_text = "нарушение" if correct else "не нарушение"
        result_msg = f"❌ Неправильно. Правильный ответ: {correct_text}\n\n{q['explanation']}"
    send_message(user_id, result_msg)
    state['current_question'] += 1
    state['timeout_processed'] = False
    save_state(user_id, state)
    if state['current_question'] < state['total']:
        send_scenario_question(user_id)
    else:
        finish_game_scenarios(user_id)

def finish_game_scenarios(user_id: int):
    # Отменяем таймер, если он ещё активен
    if user_id in active_timers:
        active_timers[user_id].cancel()
        del active_timers[user_id]

    state = get_state(user_id)
    if not state:
        return
    total = state['total']
    score = state['score']
    update_game_stats(user_id, 'scenarios', correct=score, games_increment=1)
    msg = f"🏆 **Игра окончена!**\nПравильных ответов: {score}/{total}\n"
    if score == total:
        msg += "🎉 Отлично! Вы отлично разбираетесь в правонарушениях!"
    elif score > total//2:
        msg += "👍 Хороший результат, изучите сложные моменты."
    else:
        msg += "📚 Рекомендуем изучить административное и уголовное право."
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('🎮 Игры', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('🏠 Главное меню', color=VkKeyboardColor.SECONDARY)
    send_message(user_id, msg, keyboard)
    clear_state(user_id)

# ==================== ОБРАБОТЧИК СООБЩЕНИЙ ПОЛЬЗОВАТЕЛЯ ====================
def handle_user_message(user_id: int, text: str, name: str):
    """Основной обработчик сообщений от обычного пользователя"""
    text_lower = text.lower().strip()
    logger.info(f"Получен текст от {user_id}: {text_lower}")

    state = get_state(user_id)

    # ==================== ОБРАБОТКА АКТИВНЫХ СЦЕНАРИЕВ ====================
    if state and state.get('scenario') in ['stress_test', 'anxiety_test', 'compose_message', 'appeal', 'reminder', 'change_daily_time']:
        scenario = state['scenario']
        if scenario == 'stress_test':
            step = state['step']
            if step < 5:
                answer = 1 if text_lower in ['да', 'yes', '+', 'д'] else 0
                state['answers'].append(answer)
                state['step'] += 1
                if state['step'] < 5:
                    send_message(user_id, f"Вопрос {state['step']+1}/5: {state['questions'][state['step']]}")
                    save_state(user_id, state)
                else:
                    total = sum(state['answers'])
                    if total >= 4:
                        msg = "Высокий уровень стресса. Рекомендую техники расслабления и обратиться к психологу."
                    elif total >= 2:
                        msg = "Умеренный уровень стресса. Обрати внимание на отдых и режим дня."
                    else:
                        msg = "Отлично! Ты хорошо справляешься со стрессом."
                    send_message(user_id, msg)
                    clear_state(user_id)
            return
        elif scenario == 'anxiety_test':
            step = state['step']
            if step < 5:
                answer = 1 if text_lower in ['да', 'yes', '+', 'д'] else 0
                state['answers'].append(answer)
                state['step'] += 1
                if state['step'] < 5:
                    send_message(user_id, f"Вопрос {state['step']+1}/5: {state['questions'][state['step']]}")
                    save_state(user_id, state)
                else:
                    total = sum(state['answers'])
                    if total >= 4:
                        msg = "Высокий уровень тревожности. Рекомендуется консультация психолога."
                    elif total >= 2:
                        msg = "Средний уровень тревожности. Попробуй дыхательные упражнения и ведение дневника."
                    else:
                        msg = "Уровень тревожности в норме."
                    send_message(user_id, msg)
                    clear_state(user_id)
            return
        elif scenario == 'compose_message':
            if state.get('step') == 'get_text':
                save_state(user_id, {'scenario': 'compose_message', 'step': 'compose', 'original': text})
                send_message(user_id, "Вот пример вежливого сообщения:\n\n"
                                       f"«{text}»\n\n"
                                       "Ты можешь отредактировать его или отправить как есть. Если хочешь изменить, напиши новый текст.")
            elif state.get('step') == 'compose':
                send_message(user_id, f"Отлично! Твоё сообщение готово:\n\n{text}\n\nТеперь ты можешь отправить его адресату.")
                clear_state(user_id)
            return
        elif scenario == 'appeal':
            contact = None
            if text_lower.strip() == 'анонимно':
                contact = 'анонимно'
                appeal_text = text
            else:
                email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
                phone_match = re.search(r'[\+\(]?[0-9]{1,3}[\)\-\s]?[\d\-]{6,}', text)
                if email_match:
                    contact = email_match.group()
                    appeal_text = text.replace(contact, '').strip()
                elif phone_match:
                    contact = phone_match.group()
                    appeal_text = text.replace(contact, '').strip()
                else:
                    appeal_text = text
            save_appeal(user_id, appeal_text, contact)
            send_message(user_id, "Спасибо, твоё обращение отправлено. Психолог ответит в ближайшее время.")
            clear_state(user_id)
            return
        elif scenario == 'reminder':
            if state.get('step') == 'get_text':
                save_state(user_id, {'scenario': 'reminder', 'step': 'get_time', 'text': text})
                send_message(user_id, "Теперь напиши время в формате ЧЧ:ММ (например, 15:30).\n"
                                      "Если нужно повторять ежедневно, добавь после времени слово 'ежедневно' (например, 09:00 ежедневно).")
            elif state.get('step') == 'get_time':
                parts = text.split()
                time_str = parts[0]
                repeat = 'daily' if len(parts) > 1 and parts[1].lower() == 'ежедневно' else 'once'
                if re.match(r'^\d{2}:\d{2}$', time_str):
                    with db_lock:
                        cursor.execute('''
                            INSERT INTO reminders (user_id, text, time, repeat_type, active)
                            VALUES (?, ?, ?, ?, 1)
                        ''', (user_id, state['text'], time_str, repeat))
                        conn.commit()
                    send_message(user_id, f"Напоминание установлено на {time_str} с текстом: {state['text']}.\n"
                                          f"{'Оно будет повторяться ежедневно.' if repeat == 'daily' else ''}")
                else:
                    send_message(user_id, "Неверный формат времени. Используй ЧЧ:ММ.")
                clear_state(user_id)
            return
        elif scenario == 'change_daily_time':
            update_daily_time(user_id, text)
            return

    # ==================== ОБРАБОТКА КОМАНД И КНОПОК ====================
    if text_lower in ['начать', 'старт', 'привет', 'меню', 'главное меню', '/start', '🏠 главное меню']:
        send_message(user_id, "👋 Привет! Я твой помощник. Выбери, что тебя интересует:", get_keyboard('user'))

    elif text_lower in ['помощь по темам', '📚 помощь по темам']:
        handle_help_themes(user_id)

    elif text_lower in ['тесты', '📊 тесты']:
        handle_tests_menu(user_id)

    elif text_lower in ['мотивация', '💡 мотивация']:
        handle_motivation(user_id)

    elif text_lower in ['совет', '🆘 совет']:
        handle_advice(user_id)

    elif text_lower in ['игры', '🎮 игры']:
        games_menu(user_id)

    elif text_lower in ['обратиться к психологу', '📝 обратиться к психологу']:
        handle_appeal_start(user_id)

    elif text_lower in ['напомнить о событии', '⏰ напомнить о событии']:
        handle_reminder_start(user_id)

    elif text_lower in ['ежедневные советы', '☀️ ежедневные советы']:
        handle_daily_motivation_menu(user_id)

    # Кнопки внутри меню "Помощь по темам"
    elif text_lower == 'стресс':
        handle_stress_menu(user_id)
    elif text_lower == 'конфликты':
        handle_conflict_menu(user_id)
    elif text_lower == 'мотивация к учебе':
        tips = ["Ставь небольшие цели на каждый день — так легче видеть прогресс.",
                "Делай перерывы во время занятий, чтобы не уставать.",
                "Найди интересные способы учиться — видео, игры, проекты.",
                "Помни, зачем тебе нужны знания — это твой путь к мечте!"]
        send_message(user_id, random.choice(tips))
    elif text_lower == 'здоровый образ жизни':
        tips = ["Спи не менее 8 часов в сутки.",
                "Питайся разнообразно и сбалансированно.",
                "Занимайся спортом или просто гуляй на свежем воздухе.",
                "Ограничь время за гаджетами, особенно перед сном.",
                "Пей достаточно воды."]
        send_message(user_id, random.choice(tips))
    elif text_lower == 'буллинг':
        handle_bullying_advice(user_id)
    elif text_lower == 'тревога':
        text = ("Чувствовать тревогу — это нормально. Вот несколько способов справиться:\n"
                "• Сделай дыхательное упражнение.\n"
                "• Отвлекись на приятное занятие.\n"
                "• Поговори с доверенным человеком.\n"
                "• Запиши свои мысли.\n\n"
                "Если тревога сильная, обратись к психологу.")
        send_message(user_id, text)
    elif text_lower == 'сон':
        text = ("Рекомендации для здорового сна:\n"
                "• За 60 минут до сна выключи гаджеты.\n"
                "• За 30 минут займись расслабляющим занятием.\n"
                "• За 10 минут сделай легкую растяжку.\n"
                "• Ложись спать в одно и то же время.")
        send_message(user_id, text)
    elif text_lower == 'организация пространства':
        text = ("Как организовать учебное место:\n"
                "• Убери лишнее со стола.\n"
                "• Обеспечь хорошее освещение.\n"
                "• Держи материалы под рукой.\n"
                "• Удобный стул и правильная высота стола.\n"
                "• Минимизируй отвлекающие факторы.")
        send_message(user_id, text)

    # Кнопки внутри меню "Тесты"
    elif text_lower == 'тест на стресс':
        start_stress_test(user_id)
    elif text_lower == 'тест на тревожность':
        start_anxiety_test(user_id)

    # Кнопки внутри меню "Стресс"
    elif text_lower == 'пройти тест на стресс':
        start_stress_test(user_id)
    elif text_lower == 'советы при стрессе':
        handle_stress_tips(user_id)
    elif text_lower == 'дыхательное упражнение':
        handle_breathing_exercise(user_id)

    # Кнопки внутри меню "Конфликты"
    elif text_lower == 'как разрешить конфликт?':
        handle_conflict_resolution(user_id)
    elif text_lower == 'помощь в диалоге':
        handle_dialog_help(user_id)
    elif text_lower == 'что делать при буллинге?':
        handle_bullying_advice(user_id)

    # Кнопки в меню "Игры"
    elif text_lower in ['закон в картинках', '🎴 закон в картинках']:
        start_game_emojis(user_id)
    elif text_lower in ['правонарушение или нет?', '⚖️ правонарушение или нет?']:
        start_game_scenarios(user_id)
    elif text_lower in ['статистика', 'stats', '/stats', '📊 статистика']:
        show_stats(user_id)
    elif text_lower in ['главное меню', '🏠 главное меню']:
        send_message(user_id, "Главное меню:", get_keyboard('user'))

    # Кнопки в меню "Ежедневные советы"
    elif text_lower == 'включить':
        set_daily_motivation(user_id, True)
    elif text_lower == 'выключить':
        set_daily_motivation(user_id, False)
    elif text_lower == 'изменить время':
        change_daily_motivation_time(user_id)

    # Команды
    elif text_lower == '/restart':
        if state and state.get('scenario') in ('game_emojis', 'game_scenarios'):
            if state['scenario'] == 'game_emojis':
                start_game_emojis(user_id)
            else:
                start_game_scenarios(user_id)
        else:
            send_message(user_id, "Сейчас нет активной игры. Начните игру через меню 'Игры'.")
    elif text_lower == '/help':
        help_text = ("📚 **Справка**\n\n"
                     "Я могу помочь с профилактикой, провести тесты, дать совет.\n"
                     "🔹 Нажмите 'Игры', чтобы поиграть в викторины о законах.\n"
                     "🔹 'Помощь по темам' – советы по стрессу, конфликтам, здоровому образу жизни.\n"
                     "🔹 'Тесты' – проверьте уровень стресса и тревожности.\n"
                     "🔹 'Мотивация' – вдохновляющие цитаты.\n"
                     "🔹 'Обратиться к психологу' – анонимное сообщение.\n"
                     "🔹 'Напомнить о событии' – установите напоминание.\n"
                     "🔹 'Ежедневные советы' – настройте утренние сообщения.\n"
                     "Команды: /stats, /restart, /help")
        send_message(user_id, help_text)

    # Назад
    elif text_lower == '🔙 назад':
        send_message(user_id, "Главное меню:", get_keyboard('user'))

    # ==================== ОБРАБОТКА АКТИВНЫХ ИГР (текстовый ввод) ====================
    else:
        if state and state.get('scenario') == 'game_emojis':
            # Проверка на пропуск и завершение
            if text_lower in ['пропустить', '⏩ пропустить', 'пропустить вопрос']:
                skip_emojis_question(user_id)
            elif text_lower in ['завершить игру', 'закончить', '🏁 завершить игру']:
                finish_game_emojis(user_id)
            else:
                q_index = state.get('current_question', 0)
                if q_index < state.get('total', 0):
                    q = state['questions'][q_index]
                    matched = False
                    # Сравниваем текст варианта
                    for i, opt in enumerate(q['options']):
                        if text_lower == opt.lower():
                            handle_emojis_answer(user_id, i)
                            matched = True
                            break
                    # Если не совпал, пробуем по номеру варианта
                    if not matched and text_lower in ['1', '2', '3']:
                        idx = int(text_lower) - 1
                        if 0 <= idx < len(q['options']):
                            handle_emojis_answer(user_id, idx)
                            matched = True
                    if not matched:
                        send_message(user_id, "Используйте кнопки для ответа или введите текст варианта (например, 'Правила дорожного движения').")
        elif state and state.get('scenario') == 'game_scenarios':
    if text_lower in ['завершить игру', 'закончить', '🏁 завершить игру']:
        finish_game_scenarios(user_id)
    else:
        if state.get('timeout_processed'):
            send_message(user_id, "Время на ответ вышло. Переходим к следующему вопросу.")
            return
        q_index = state['current_question']
        if q_index >= state['total']:
            finish_game_scenarios(user_id)
            return

        # Отменяем таймер, если он ещё есть (пользователь ответил текстом)
        if user_id in active_timers:
            active_timers[user_id].cancel()
            del active_timers[user_id]

        normalized = re.sub(r'[^\w\s]', '', text_lower).strip()
        choice = None
        not_offense_phrases = ['не нарушение', 'не наруш', 'не правонарушение', 'не правонаруш',
                               'законно', 'не является', 'это не нарушение', 'нет']
        if normalized in not_offense_phrases:
            choice = 0
        else:
            offense_phrases = ['нарушение', 'наруш', 'правонарушение', 'правонаруш',
                               'преступление', 'да', 'это нарушение']
            if normalized in offense_phrases:
                choice = 1
            else:
                if 'не' in normalized and ('наруш' in normalized or 'правонаруш' in normalized):
                    choice = 0
                elif 'наруш' in normalized or 'правонаруш' in normalized or 'преступл' in normalized:
                    choice = 1
                else:
                    send_message(user_id, "Введите 'Нарушение' или 'Не нарушение'.")
                    return

        handle_scenario_answer(user_id, choice)

# ==================== ОБРАБОТЧИК ДЛЯ ПСИХОЛОГА ====================
def handle_psychologist_message(user_id: int, text: str):
    text_lower = text.lower().strip()
    if text_lower in ['начать', 'старт', 'привет']:
        send_message(user_id, "Добро пожаловать, психолог! Используйте кнопки.", get_keyboard('psychologist'))
    elif text_lower in ['список обращений', '📋 список обращений']:
        appeals = get_unanswered_appeals()
        if not appeals:
            send_message(user_id, "Новых обращений нет.")
            return
        msg = "Неотвеченные обращения:\n"
        for i, (aid, uid, appeal_text, contact, ts) in enumerate(appeals, 1):
            short_text = appeal_text[:50] + "..." if len(appeal_text) > 50 else appeal_text
            contact_info = f" (контакт: {contact})" if contact and contact != 'анонимно' else ""
            msg += f"{i}. {short_text}{contact_info} (от {ts})\n"
        msg += "\nДля ответа нажмите на кнопку с номером обращения."
        keyboard = VkKeyboard(one_time=False)
        for i in range(1, len(appeals) + 1):
            keyboard.add_button(str(i), color=VkKeyboardColor.PRIMARY)
            if i % 3 == 0:
                keyboard.add_line()
        keyboard.add_line()
        keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
        send_message(user_id, msg, keyboard)
        mapping = {str(i): aid for i, (aid, _, _, _, _) in enumerate(appeals, 1)}
        save_state(user_id, {'psychologist_appeals': mapping})
    elif text_lower == '🔙 назад':
        send_message(user_id, "Главное меню:", get_keyboard('psychologist'))
    elif text_lower in ['инструкция', '📖 инструкция']:
        instr = ("Инструкция для психолога:\n"
                 "1. Нажмите «Список обращений» для просмотра неотвеченных обращений.\n"
                 "2. Выберите номер обращения из предложенных кнопок.\n"
                 "3. Введите текст ответа.\n"
                 "4. Ответ будет отправлен пользователю (контакты скрыты, если пользователь выбрал анонимность).\n"
                 "5. После ответа обращение исчезнет из списка.\n"
                 "Будьте доброжелательны и профессиональны.")
        send_message(user_id, instr)
    elif text_lower.isdigit():
        state = get_state(user_id)
        if state and 'psychologist_appeals' in state:
            appeal_num = text_lower
            appeal_id = state['psychologist_appeals'].get(appeal_num)
            if appeal_id:
                with db_lock:
                    cursor.execute('SELECT answered FROM appeals WHERE id = ?', (appeal_id,))
                    row = cursor.fetchone()
                    if row and row[0] == 1:
                        send_message(user_id, f"Обращение #{appeal_num} уже было отвечено. Обновите список.")
                        clear_state(user_id)
                        return
                save_state(user_id, {'answering_appeal': appeal_id})
                send_message(user_id, f"Введите текст ответа на обращение #{appeal_num}:")
            else:
                send_message(user_id, "Обращение не найдено.")
        else:
            send_message(user_id, "Сначала получите список обращений (кнопка «Список обращений»).")
    else:
        state = get_state(user_id)
        if state and 'answering_appeal' in state:
            appeal_id = state['answering_appeal']
            if answer_appeal(appeal_id, text, user_id):
                send_message(user_id, f"Ответ на обращение #{appeal_id} отправлен.")
                clear_state(user_id)
                send_message(user_id, "Чтобы посмотреть оставшиеся обращения, нажмите «Список обращений».", get_keyboard('psychologist'))
            else:
                clear_state(user_id)
                send_message(user_id, "Не удалось отправить ответ (обращение уже закрыто). Обновите список.", get_keyboard('psychologist'))
        else:
            send_message(user_id, "Используйте кнопки меню.", get_keyboard('psychologist'))

# ==================== ПЛАНИРОВЩИК ====================
def reminder_scheduler():
    """Фоновый поток для отправки напоминаний и мотивационных сообщений"""
    while True:
        # Получаем текущее время в локальном часовом поясе
        now_local = datetime.now(TIMEZONE)
        now_str = now_local.strftime('%H:%M')

        with db_lock:
            # Одноразовые напоминания
            cursor.execute('''
                SELECT id, user_id, text FROM reminders
                WHERE active = 1 AND repeat_type = 'once' AND time = ?
            ''', (now_str,))
            once_reminders = cursor.fetchall()
            for rem_id, user_id, text in once_reminders:
                send_message(user_id, f"⏰ Напоминание: {text}")
                cursor.execute('UPDATE reminders SET active = 0 WHERE id = ?', (rem_id,))

            # Ежедневные
            cursor.execute('''
                SELECT user_id, text FROM reminders
                WHERE active = 1 AND repeat_type = 'daily' AND time = ?
            ''', (now_str,))
            daily_reminders = cursor.fetchall()
            for user_id, text in daily_reminders:
                send_message(user_id, f"⏰ Напоминание: {text}")

            # Ежедневные мотивационные сообщения
            cursor.execute('''
                SELECT user_id FROM daily_motivation
                WHERE enabled = 1 AND time = ?
            ''', (now_str,))
            users = cursor.fetchall()
            for (user_id,) in users:
                quotes = [
                    "Каждый день — новая возможность стать лучше.",
                    "Не бойся трудностей — они делают тебя сильнее.",
                    "Ты способен на большее, чем думаешь.",
                    "Улыбка — самый простой способ изменить мир вокруг.",
                    "Верь в себя и свои силы."
                ]
                send_message(user_id, f"☀️ Доброе утро! {random.choice(quotes)}")
            conn.commit()
        time.sleep(60)

threading.Thread(target=reminder_scheduler, daemon=True).start()

# ==================== ОБРАБОТКА INLINE-СОБЫТИЙ ====================
def handle_message_event(event):
    payload = event.object.payload
    user_id = event.object.user_id
    logger.info(f"MESSAGE_EVENT: user_id={user_id}, payload={payload}")
    state = get_state(user_id)
    if not state:
        logger.warning(f"Нет состояния для {user_id}")
        return
    scenario = state.get('scenario')
    if scenario == 'game_emojis':
        if 'answer' in payload:
            handle_emojis_answer(user_id, payload['answer'])
        elif 'skip' in payload:
            skip_emojis_question(user_id)
        elif 'finish' in payload:
            finish_game_emojis(user_id)
    elif scenario == 'game_scenarios':
        if 'choice' in payload:
            # Отменяем таймер, если он ещё активен
            if user_id in active_timers:
                active_timers[user_id].cancel()
                del active_timers[user_id]
            if state.get('timeout_processed'):
                return
            handle_scenario_answer(user_id, payload['choice'])
        elif 'finish' in payload:
            finish_game_scenarios(user_id)

# ==================== ГЛАВНЫЙ ЦИКЛ ====================
logger.info("Бот запущен")
for event in longpoll.listen():
    if event.type == VkBotEventType.MESSAGE_NEW:
        msg = event.object.message
        user_id = msg['from_id']
        text = msg.get('text', '').strip()
        if not text:
            continue
        try:
            user_info = vk.users.get(user_ids=user_id)[0]
            name = f"{user_info['first_name']} {user_info['last_name']}"
        except:
            name = str(user_id)

        if user_id in PSYCHOLOGIST_IDS:
            handle_psychologist_message(user_id, text)
        else:
            with db_lock:
                cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
                if not cursor.fetchone():
                    cursor.execute('INSERT INTO users (user_id, name) VALUES (?, ?)', (user_id, name))
                    conn.commit()
            handle_user_message(user_id, text, name)

    elif event.type == VkBotEventType.MESSAGE_EVENT:
        handle_message_event(event)
