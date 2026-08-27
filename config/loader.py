# -*- coding: utf-8 -*-
"""
Created on Thu Aug 27 11:47:11 2026

@author: a.karabedyan
"""

# config/loader.py
import pandas as pd
from loguru import logger
from config.defaults import DEFAULTS, SCHEMA
from pipeline.base import ProcessingContext

def load_params(context: ProcessingContext) -> dict:
    """
    Загружает «Параметры» с валидацией и fallback на дефолты.
    """
    params = DEFAULTS.copy()

    try:
        df = context.references["параметры"]
        
    except (KeyError, ValueError):
        logger.warning(
            "Справочник «Параметры» не найден — работаем с дефолтами"
        )
        return params

    if df is None or df.empty:
        logger.warning(
            "Справочник «Параметры» пуст — работаем с дефолтами"
        )
        return params

    # Делаем копию, чтобы не менять исходный DataFrame
    df = df.copy()

    # Приводим названия колонок к единому виду
    df.columns = df.columns.astype(str).str.strip().str.lower()

    required_columns = {"параметр", "значение"}
    if not required_columns.issubset(df.columns):
        logger.warning(
            "В справочнике «Параметры» отсутствуют обязательные колонки {} — "
            "работаем с дефолтами",
            sorted(required_columns),
        )
        return params

    # Нормализуем имена параметров
    df["параметр"] = df["параметр"].astype(str).str.strip()

    # Можно отбросить строки без имени параметра
    df = df[df["параметр"] != ""]
    
    
    
    for _, row in df.iterrows():
        name = row["параметр"]
        raw_value = row["значение"]
        
        if name not in SCHEMA:
            # Неизвестный параметр — игнорируем
            logger.warning('⚠ Неизвестный параметр {} игнорируем', name)
            continue

        expected_type, min_val, max_val, nullable = SCHEMA[name]

        # Обработка пустых значений
        if pd.isna(raw_value):
            if nullable:
                params[name] = None
            # Если не nullable — оставляем дефолт
            logger.warning('⚠ Параметр {} не nullable — оставляем дефолт', name)
            continue

        # Приведение типа
        try:
            value = _cast(raw_value, expected_type)
        except (ValueError, TypeError):
            logger.warning(
                "⚠ Параметр '{}': не удалось привести '{}' к типу {}. "
                "Используется значение по умолчанию: {}",
                name,
                raw_value,
                expected_type.__name__,
                DEFAULTS[name],
            )
            continue

        # Проверка границ
        if min_val is not None and value < min_val:
            logger.warning(
                "⚠ Параметр '{}': значение {} меньше допустимого минимума {}. "
                "Используется дефолт.",
                name,
                value,
                min_val,
            )
            continue

        if max_val is not None and value > max_val:
            logger.warning(
                "⚠ Параметр '{}': значение {} больше допустимого максимума {}. "
                "Используется дефолт.",
                name,
                value,
                max_val,
            )
            continue

        params[name] = value
    
    return params

def _cast(value, target_type):
    """Приводит значение к целевому типу."""
    if target_type is float:
        # Pandas иногда читает числа как int, иногда как str с запятой
        return float(str(value).replace(",", ".").replace(" ", ""))
    if target_type is int:
        return int(float(str(value).replace(",", ".").replace(" ", "")))
    if target_type is str:
        return str(value).strip()
    raise TypeError(f"Неподдерживаемый тип: {target_type}")