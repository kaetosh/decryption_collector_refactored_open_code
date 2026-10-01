# -*- coding: utf-8 -*-
"""
Created on Mon Jun 22 09:29:51 2026

@author: a.karabedyan
"""

# utils/column_utils.py
import pandas as pd
from loguru import logger
from config.settings import ACCOUNTS_OSV_LEASE

# Доля бухгалтерских счетов, при которой столбец Level_* ещё считается
# столбцом счетов. В find_target_column критерий строгий (все значения — счета).
# Здесь тот же критерий служит ПЕРВЫМ шагом, а второй (запасной) не даёт одному
# плохо разобранному файлу остановить весь прогон.
ACCOUNT_COLUMN_FALLBACK_RATIO = 0.95

# Сколько примеров значений показывать в диагностике Level_*-столбцов
LEVEL_DIAGNOSTICS_EXAMPLES = 5


def _level_columns_sorted(df, column_prefix='Level_'):
    """Столбцы с префиксом Level_ слева направо (Level_0, Level_1, ...)."""
    cols = [col for col in df.columns if str(col).startswith(column_prefix)]

    def _index(col):
        parts = str(col).split('_', 1)
        return int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0

    return sorted(cols, key=_index)


def resolve_account_level_column(df,
                                 column_prefix='Level_',
                                 fallback_ratio=ACCOUNT_COLUMN_FALLBACK_RATIO):
    """
    Единая точка поиска столбца Level_* с номерами бухгалтерских счетов.

    Шаг 1 (строгий, как в find_target_column): ВСЕ значения столбца — счета,
    проверка справа налево. Шаг 2 (запасной): ни один столбец не чистый — берём
    самый левый, где доля счетов не меньше fallback_ratio, и пишем WARNING с
    файлами-источниками мусора.

    Запасной шаг нужен из-за регресса от 01.10.2026: в `ББ_осв_98.01` номер счёта
    не лёг на свой уровень, и в сводной таблице не осталось НИ ОДНОГО столбца,
    целиком состоящего из счетов — шаг 1в останавливался с текстом «проверьте
    формат выгрузки», хотя формат был в порядке, а виноват был один файл из
    двадцати. Молча брать «почти правильный» столбец нельзя, но и останавливать
    прогон из-за одного файла тоже нельзя — поэтому WARNING, а не падение.

    Returns:
        Optional[str]: имя столбца счетов либо None, если не нашёлся.
    """
    from data_processors.file_processor import FileProcessor

    cols = _level_columns_sorted(df, column_prefix)
    if not cols:
        logger.debug("Столбцы с префиксом '{}' не найдены", column_prefix)
        return None

    # --- Шаг 1: строгий критерий, справа налево ---
    for col in reversed(cols):
        if FileProcessor._is_accounting_code_vectorized(df[col]).all():
            return col

    # --- Шаг 2: запасной критерий, слева направо ---
    for col in cols:
        mask = FileProcessor._is_accounting_code_vectorized(df[col])
        ratio = float(mask.mean()) if len(mask) else 0.0
        if ratio < fallback_ratio:
            continue

        bad = df.loc[~mask]
        if 'Исх.файл' in bad.columns:
            source = bad['Исх.файл'].value_counts().to_dict()
        else:
            source = {}

        logger.warning(
            "Столбец '{}' выбран как столбец счетов по запасному правилу: "
            "доля бухгалтерских счетов {:.1%} (порог {:.0%}). "
            "Не-счета ({}) пришли из файлов: {}",
            col,
            ratio,
            fallback_ratio,
            len(bad),
            source if source else 'источник неизвестен (нет столбца «Исх.файл»)',
        )
        return col

    logger.debug(
        "Столбец Level_* со счетами не найден (порог доли {:.0%})", fallback_ratio
    )
    return None


def describe_level_columns(df, column_prefix='Level_'):
    """
    Диагностика Level_*-столбцов для сообщения об ошибке.

    Нужна, чтобы ошибка называла виновника, а не отправляла проверять формат:
    видно, что именно лежит в каждом столбце и сколько значений не являются
    счетами.
    """
    from data_processors.file_processor import FileProcessor

    parts = []
    for col in _level_columns_sorted(df, column_prefix):
        mask = FileProcessor._is_accounting_code_vectorized(df[col])
        bad = df.loc[~mask, col].dropna().unique().tolist()
        if not bad:
            parts.append(f"{col}: все значения — счета")
            continue
        examples = ', '.join(map(str, bad[:LEVEL_DIAGNOSTICS_EXAMPLES]))
        parts.append(
            f"{col}: {len(bad)} значений не являются счетами (примеры: {examples})"
        )

    return '; '.join(parts) if parts else 'столбцы Level_* отсутствуют'


def find_target_column(df,
                       column_prefix='Level_',
                       search_direction='rightmost',
                       account_type='all_accounts',
                       shift=0):
    """
    Находит столбец с указанным префиксом по заданным условиям и возвращает столбец со сдвигом.
    """
    from data_processors.file_processor import FileProcessor
    
    # Получаем все столбцы с указанным префиксом
    columns_with_prefix = [col for col in df.columns if col.startswith(column_prefix)]
    
    if not columns_with_prefix:
        logger.debug("Столбцы с префиксом '{}' не найдены", column_prefix)
        return None
    
    # Определяем порядок проверки
    if search_direction == 'rightmost':
        check_order = columns_with_prefix[::-1]
    elif search_direction == 'leftmost':
        check_order = columns_with_prefix
    else:
        logger.warning("Некорректное значение search_direction: {}", search_direction)
        return None
    
    # Ищем столбец, удовлетворяющий условию
    found_column = None
    found_index = None
    
    for col in check_order:
        try:
            # ★ ИСПРАВЛЕНИЕ: используем оригинальный метод из FileProcessor
            is_all_account = FileProcessor._is_accounting_code_vectorized(df[col])
            
            if account_type == 'all_accounts':
                condition_met = is_all_account.all()
            elif account_type == 'no_accounts':
                condition_met = (~is_all_account).all()
            else:
                logger.warning("Некорректное значение account_type: {}", account_type)
                return None
            
            if condition_met:
                found_column = col
                found_index = columns_with_prefix.index(col)
                break
                
        except Exception as e:
            logger.debug("Ошибка при проверке столбца {}: {}", col, e)
            continue
    
    if found_column is None:
        logger.debug("Столбец с условием '{}' не найден", account_type)
        return None
    
    # Применяем сдвиг
    target_index = found_index + shift
    
    if target_index < 0 or target_index >= len(columns_with_prefix):
        logger.warning("Сдвиг {} выходит за границы списка столбцов", shift)
        return None
    
    return columns_with_prefix[target_index]

def process_account(acc) -> str:
    """
    Нормализует номер счета (скалярная версия):
    - 98.x -> оставляет как есть
    - Счета из ACCOUNTS_OSV_LEASE (и их субсчета) -> приводит к базовому счету
    - Остальные -> обрезает до первого уровня (2 символа)
    """
    if pd.isna(acc):
        return ''

    acc_str = str(acc).strip()

    # Специальная обработка для 98-го счета
    if acc_str.startswith('98.'):
        return acc_str

    # ★ Динамическая проверка по списку ACCOUNTS_OSV_LEASE
    # Сортируем по длине (убывание), чтобы '76.05.3' проверялся раньше, чем '76.05'
    for lease_acc in sorted(ACCOUNTS_OSV_LEASE, key=len, reverse=True):
        if acc_str == lease_acc or acc_str.startswith(lease_acc + '.'):
            return lease_acc

    # Остальные счета -> первые 2 символа
    return acc_str[:2] if len(acc_str) >= 2 else acc_str


def normalize_account(series: pd.Series) -> pd.Series:
    """
    Векторизованная нормализация счетов для Series (синтетический уровень).
    По умолчанию 2 символа, для счетов 90 и 91 - 5 символов.

    Отличается от process_account():
    - process_account: скалярная, с логикой ACCOUNTS_OSV_LEASE
    - normalize_account: векторная, для реконциляции проводок (Дт/Кт)

    Работает в разы быстрее, чем apply с lambda.
    """
    s = series.astype(str)
    res = s.str[:2].copy()
    mask_90_91 = s.str.startswith(('90', '91'))
    res.loc[mask_90_91] = s.loc[mask_90_91].str[:5]
    return res
