"""
Утилиты для обработки текстовых строк.
Очистка, нормализация, лемматизация названий компаний и других текстов.
"""
import re
from typing import Dict
import pandas as pd
from loguru import logger

from config.settings import (
    REMOVE_LEGAL_FORMS,
    LEGAL_FORMS_REGEX,
    REMOVE_DIGITS,
    REMOVE_PUNCTUATION,
    REMOVE_STOPWORDS,
    USE_LEMMATIZATION,
    SORT_WORDS,
)

# =========================================================================
# РУССКИЕ СТОП-СЛОВА
# =========================================================================

# Русские стоп-слова (встроенный список, не требует nltk)
RUSSIAN_STOPWORDS = {
    'и', 'в', 'во', 'не', 'что', 'он', 'на', 'я', 'с', 'со', 'как',
    'а', 'то', 'все', 'она', 'так', 'его', 'но', 'да', 'ты', 'к', 'у',
    'же', 'вы', 'за', 'бы', 'по', 'только', 'ее', 'мне', 'было', 'вот',
    'от', 'меня', 'еще', 'нет', 'о', 'из', 'ему', 'теперь', 'когда',
    'даже', 'ну', 'ли', 'если', 'уже', 'или', 'ни', 'быть', 'был',
    'него', 'до', 'вас', 'нибудь', 'опять', 'уж', 'вам', 'ведь', 'там',
    'потом', 'себя', 'ничего', 'ей', 'может', 'они', 'тут', 'где',
    'есть', 'надо', 'ней', 'для', 'мы', 'тебя', 'их', 'чем', 'была',
    'сам', 'чтоб', 'без', 'будто', 'чего', 'раз', 'тоже', 'себе', 'под',
    'будет', 'ж', 'тогда', 'кто', 'этот', 'того', 'потому', 'этого',
    'какой', 'совсем', 'ним', 'здесь', 'этом', 'один', 'почти', 'мой',
    'тем', 'чтобы', 'нее', 'сейчас', 'были', 'куда', 'зачем', 'всех',
    'никогда', 'можно', 'при', 'наконец', 'два', 'об', 'другой', 'хоть',
    'после', 'над', 'больше', 'тот', 'через', 'эти', 'нас', 'про',
    'всего', 'них', 'какая', 'много', 'разве', 'три', 'эту', 'моя',
    'впрочем', 'хорошо', 'свою', 'этой', 'перед', 'иногда', 'лучше',
    'чуть', 'том', 'нельзя', 'такой', 'им', 'более', 'всегда',
    'конечно', 'всю', 'между'
}
# =========================================================================
# ЛЕММАТИЗАТОР
# =========================================================================

try:
    from pymorphy3 import MorphAnalyzer
    _morph = MorphAnalyzer()
    
    def _lemmatize_word(word: str) -> str:
        """Приводит слово к начальной форме."""
        return _morph.parse(word)[0].normal_form
except ImportError:
    def _lemmatize_word(word: str) -> str:
        """Заглушка, если pymorphy3 не установлен."""
        return word

# =========================================================================
# ОЧИСТКА ТЕКСТА
# =========================================================================

def clean_text_optimized(text: str) -> str:
    """
    Оптимизированная функция очистки текста.
    
    Настраивается через константы в config/settings.py:
    - REMOVE_LEGAL_FORMS: удаление ОПФ (ООО, АО, ИП)
    - REMOVE_DIGITS: удаление цифр
    - REMOVE_PUNCTUATION: удаление знаков препинания
    - REMOVE_STOPWORDS: удаление стоп-слов
    - USE_LEMMATIZATION: лемматизация (приведение к начальной форме)
    - SORT_WORDS: сортировка слов
    
    Args:
        text: Исходная строка
        
    Returns:
        Очищенная и нормализованная строка
    """
    if text is None:
        return ""
    
    text = str(text).lower()
    
    # 1. Удаление организационно-правовых форм
    if REMOVE_LEGAL_FORMS and LEGAL_FORMS_REGEX:
        text = re.sub(LEGAL_FORMS_REGEX, ' ', text)
    
    # 2. Удаление цифр
    if REMOVE_DIGITS:
        text = re.sub(r'\d+', ' ', text)
    
    # 3. Удаление знаков препинания
    if REMOVE_PUNCTUATION:
        text = re.sub(r'[^\w\s]', ' ', text)
    
    # 4. Удаление лишних пробелов
    text = re.sub(r'\s+', ' ', text).strip()
    
    # 5. Токенизация
    words = text.split()
    
    # 6. Удаление стоп-слов
    if REMOVE_STOPWORDS:
        words = [w for w in words if w not in RUSSIAN_STOPWORDS]
    
    # 7. Лемматизация
    if USE_LEMMATIZATION:
        words = [_lemmatize_word(w) for w in words]
    
    # 8. Сортировка слов
    if SORT_WORDS:
        words.sort()
    
    return " ".join(words)

# =========================================================================
# ОЧИСТКА НАЗВАНИЙ КОМПАНИЙ (с кэшированием)
# =========================================================================

_cleaning_cache: Dict[str, str] = {}


def clean_company_name(company_name: str) -> str:
    """
    Нормализует название компании для сравнения.
    
    Использует кэширование для производительности.
    Убирает ОПФ, стоп-слова, лемматизирует и сортирует слова.
    
    Настраивается через константы в config/settings.py.
    
    Examples:
        >>> clean_company_name("ООО Рыба Мясо")
        'мясо рыба'
        >>> clean_company_name("Рыба мясо ООО")
        'мясо рыба'  # Тот же результат — для fuzzy matching
    """
    import pandas as pd
    
    if not company_name or pd.isna(company_name):
        return ""
    
    if company_name in _cleaning_cache:
        return _cleaning_cache[company_name]
    
    normalized = clean_text_optimized(company_name)
    
    _cleaning_cache[company_name] = normalized
    return normalized


def clear_cleaning_cache() -> None:
    """Очищает кэш очистки названий."""
    global _cleaning_cache
    _cleaning_cache.clear()
    
# =========================================================================
# НЕЧЁТКОЕ СРАВНЕНИЕ НАЗВАНИЙ КОМПАНИЙ
# =========================================================================

def find_similar_companies(
    data_a: pd.Series,
    data_b: pd.Series,
    similarity_threshold: int = None,
    limit: int = None,
) -> pd.DataFrame:
    """
    Находит похожие названия компаний между двумя Series через fuzzy matching.
    
    Использует очистку названий и rapidfuzz для быстрого сравнения.
    
    Args:
        data_a: Series с названиями для поиска (например, контрагенты из ОСВ)
        data_b: Series с эталонными названиями (например, справочник)
        similarity_threshold: Порог схожести в % (по умолчанию из settings)
        limit: Максимальное количество совпадений (по умолчанию из settings)
        
    Returns:
        DataFrame с колонками:
        - original_a: исходное название из data_a
        - original_b: похожее название из data_b
        - cleaned_a: очищенное название из data_a
        - cleaned_b: очищенное название из data_b
        - score: процент схожести
    """
    from rapidfuzz import process, fuzz, utils as rf_utils
    from collections import defaultdict
    
    from config.settings import (
        USE_TOKEN_SORT_RATIO,
        CONTRACTOR_SIMILARITY_THRESHOLD,
        CONTRACTOR_SIMILARITY_LIMIT,
    )
    
    # Используем настройки из settings, если не переданы явно
    if similarity_threshold is None:
        similarity_threshold = CONTRACTOR_SIMILARITY_THRESHOLD
    if limit is None:
        limit = CONTRACTOR_SIMILARITY_LIMIT
    
    # Очищаем и дедуплицируем данные
    data_a_clean = data_a.dropna().drop_duplicates()
    data_b_clean = data_b.dropna().drop_duplicates()
    
    if data_a_clean.empty or data_b_clean.empty:
        return pd.DataFrame()
    
    # Очищаем названия
    df_a = data_a_clean.to_frame(name='original_a').copy()
    df_b = data_b_clean.to_frame(name='original_b').copy()
    
    df_a['cleaned_a'] = df_a['original_a'].apply(clean_company_name)
    df_b['cleaned_b'] = df_b['original_b'].apply(clean_company_name)
    
    # Создаём словарь для быстрого поиска оригинальных названий
    b_cleaned_to_original = defaultdict(list)
    for _, row in df_b.iterrows():
        b_cleaned_to_original[row['cleaned_b']].append(row['original_b'])
    
    b_cleaned_list = list(b_cleaned_to_original.keys())
    
    # Предварительная обработка для rapidfuzz
    processed_b = [rf_utils.default_process(x) for x in b_cleaned_list]
    
    # Выбираем скорер на основе настройки
    scorer = fuzz.token_sort_ratio if USE_TOKEN_SORT_RATIO else fuzz.ratio
    
    logger.debug(
        f"Запуск fuzzy matching: {len(df_a)} vs {len(df_b)}, "
        f"порог={similarity_threshold}%, скорер={scorer.__name__}"
    )
    
    results = []
    
    for _, row in df_a.iterrows():
        processed_a = rf_utils.default_process(row['cleaned_a'])
        
        # Ищем похожие через rapidfuzz
        matches = process.extract(
            processed_a,
            processed_b,
            scorer=scorer,
            score_cutoff=similarity_threshold,
            limit=limit,
        )
        
        for matched_cleaned, score, match_idx in matches:
            # Получаем оригинальные названия из data_b
            original_b_values = b_cleaned_to_original[b_cleaned_list[match_idx]]
            
            for original_b in original_b_values:
                results.append({
                    'original_a': row['original_a'],
                    'original_b': original_b,
                    'cleaned_a': row['cleaned_a'],
                    'cleaned_b': matched_cleaned,
                    'score': round(score, 1),
                })
    
    if not results:
        return pd.DataFrame()
    
    # Создаём DataFrame и удаляем дубликаты
    df_output = pd.DataFrame(results).drop_duplicates()
    
    # Сортируем по проценту совпадения (убывание)
    df_output = df_output.sort_values('score', ascending=False).reset_index(drop=True)
    
    # Очищаем кэш после использования
    clear_cleaning_cache()
    
    return df_output