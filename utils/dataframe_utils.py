# -*- coding: utf-8 -*-
"""
Created on Mon Jun 22 09:29:38 2026

@author: a.karabedyan
"""

# utils/dataframe_utils.py
import pandas as pd
from loguru import logger
from typing import List, Optional

def cast_columns_to_types(df: pd.DataFrame, type_mapping: dict) -> pd.DataFrame:
    """
    Преобразует указанные столбцы DataFrame в заданные типы данных.
    
    Для числовых столбцов выполняет предварительную очистку строковых значений:
    - Удаляет все виды пробелов (обычные, неразрывные, тонкие)
    - Заменяет запятую на точку (российский формат → международный)
    - Удаляет знаки валют (₽, $, €)
    
    Это делает функцию устойчивой к выгрузкам из 1С в TXT-формате.
    Для числовых значений (из Excel) очистка не применяется.
    
    Args:
        df: Исходный DataFrame
        type_mapping: {
            'string': ['col1', 'col2'],
            'numeric': ['col4', 'col5'],
            'datetime': ['Дата']
        }
    """
    df = df.copy()
    
    for target_type, columns in type_mapping.items():
        for col in columns:
            if col not in df.columns:
                continue
                
            if target_type == 'string':
                df[col] = df[col].astype('string')
                
            elif target_type == 'numeric':
                # ★ ОЧИСТКА строковых значений перед приведением к числу
                df[col] = _clean_numeric_series(df[col])
                # Приведение к числовому типу
                df[col] = pd.to_numeric(df[col], errors='coerce')
                
            elif target_type in ('datetime', 'date'):
                try:
                    df[col] = pd.to_datetime(
                        df[col],
                        format='mixed',
                        dayfirst=True,
                        errors='coerce'
                    )
                except (ValueError, TypeError):
                    # Fallback для старых pandas
                    df[col] = pd.to_datetime(
                        df[col], format='%d.%m.%Y %H:%M:%S', errors='coerce'
                    ).fillna(
                        pd.to_datetime(df[col], format='%d.%m.%Y', errors='coerce')
                    )
                
                if target_type == 'date':
                    df[col] = df[col].dt.date
    
    return df


def _clean_numeric_series(series: pd.Series) -> pd.Series:
    """
    Очищает Series для последующего приведения к числовому типу.
    
    Обрабатывает строковые значения:
    - Удаляет все виды пробелов (обычные, неразрывные, тонкие)
    - Заменяет запятую на точку
    - Удаляет знаки валют
    
    Числовые значения возвращает без изменений.
    
    Args:
        series: Series для очистки
        
    Returns:
        Очищенный Series
    """
    import re
    
    def clean_value(value):
        # Числовые значения (int, float) возвращаем как есть
        if isinstance(value, (int, float)):
            return value
        
        # NaN/NaT — пропускаем
        if pd.isna(value):
            return value
        
        # Строковые значения — очищаем
        s = str(value)
        
        # 1. Удаляем все виды пробелов
        # \s — обычные пробелы, \xa0 — неразрывные, \u2009 — тонкие
        s = re.sub(r'[\s\xa0\u2009\u200a\u202f\u205f]+', '', s)
        
        # 2. Удаляем знаки валют
        s = re.sub(r'[₽$€£¥]', '', s)
        
        # 3. Заменяем запятую на точку
        s = s.replace(',', '.')
        
        # 4. Обрабатываем бухгалтерский формат отрицательных чисел (скобки)
        # "(1234.56)" → "-1234.56"
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        
        return s
    
    return series.map(clean_value)

def set_header_from_row(df, search_text='Строка баланса', offset=0):
    """
    Находит строку с указанным текстом и устанавливает её как заголовок.
    
    Args:
        df: DataFrame
        search_text: текст для поиска строки-заголовка
        offset: смещение от найденной строки (0 = сама строка, 1 = следующая)
    """
    # Сбрасываем индекс, чтобы iloc и loc совпадали
    df = df.reset_index(drop=True)
    
    # Поиск строки с текстом
    mask = df.apply(
        lambda row: row.astype(str).str.contains(search_text, na=False).any(), 
        axis=1
    )
    
    if not mask.any():
        raise ValueError(f"Строка с '{search_text}' не найдена")
    
    # Позиция найденной строки
    found_idx = mask.idxmax()
    
    # Индекс строки, которую используем как заголовок
    header_idx = found_idx + offset
    
    if header_idx >= len(df):
        raise ValueError(f"Строка заголовка (индекс {header_idx}) выходит за пределы DataFrame")
    
    # Берём значения строки-заголовка
    new_columns = df.iloc[header_idx].values
    
    # Очищаем имена столбцов
    new_columns = [
        str(col).strip() if pd.notna(col) and str(col).strip() not in ('nan', '') 
        else f'Unnamed_{i}' 
        for i, col in enumerate(new_columns)
    ]
    
    # Проверяем дубликаты имён столбцов
    seen = {}
    unique_columns = []
    for col in new_columns:
        if col in seen:
            seen[col] += 1
            unique_columns.append(f"{col}_{seen[col]}")
        else:
            seen[col] = 0
            unique_columns.append(col)
    
    # Применяем заголовок
    df.columns = unique_columns
    
    # Удаляем строку-заголовок и все строки до неё
    df = df.iloc[header_idx + 1:].reset_index(drop=True)
    
    # Удаляем полностью пустые столбцы
    df = df.dropna(axis=1, how='all')
    
    return df

def get_required_columns_df(df: pd.DataFrame, required_columns: List[str]) -> pd.DataFrame:
    """
    Проверяет наличие обязательных столбцов и возвращает DataFrame только с ними.
    """
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Отсутствуют обязательные столбцы: {missing}")
    return df[required_columns].copy()