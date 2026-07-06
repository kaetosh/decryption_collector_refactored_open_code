# -*- coding: utf-8 -*-
"""
Created on Mon Jun 22 09:29:51 2026

@author: a.karabedyan
"""

# utils/column_utils.py
import pandas as pd
from loguru import logger
from config.settings import ACCOUNTS_OSV_LEASE

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
        logger.debug(f"Столбцы с префиксом '{column_prefix}' не найдены")
        return None
    
    # Определяем порядок проверки
    if search_direction == 'rightmost':
        check_order = columns_with_prefix[::-1]
    elif search_direction == 'leftmost':
        check_order = columns_with_prefix
    else:
        logger.warning(f"Некорректное значение search_direction: {search_direction}")
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
                logger.warning(f"Некорректное значение account_type: {account_type}")
                return None
            
            if condition_met:
                found_column = col
                found_index = columns_with_prefix.index(col)
                break
                
        except Exception as e:
            logger.debug(f"Ошибка при проверке столбца {col}: {e}")
            continue
    
    if found_column is None:
        logger.debug(f"Столбец с условием '{account_type}' не найден")
        return None
    
    # Применяем сдвиг
    target_index = found_index + shift
    
    if target_index < 0 or target_index >= len(columns_with_prefix):
        logger.warning(f"Сдвиг {shift} выходит за границы списка столбцов")
        return None
    
    return columns_with_prefix[target_index]

def process_account(acc) -> str:
    """
    Нормализует номер счета:
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