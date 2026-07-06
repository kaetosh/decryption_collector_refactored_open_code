# -*- coding: utf-8 -*-
"""
Модуль конфигурации логирования.
Настраивает loguru для всего приложения.
"""

import sys
from loguru import logger
from config.settings import LOG_LEVEL, LOG_FILE


def _truncate_text(text: str, max_length: int = 35) -> str:
    """
    Обрезает текст до max_length символов.
    Если текст длиннее, оставляет правую часть и добавляет '...' в начало.
    """
    if len(text) <= max_length:
        return text
    return f"...{text[-(max_length-3):]}"


def _patch_record(record):
    """
    Добавляет в запись сокращённые имена модулей, функций и сообщений.
    
    Для уровней ERROR и CRITICAL сообщение НЕ обрезается —
    важная диагностическая информация сохраняется полностью.
    """
    # 1. Сокращаем имя модуля (последние 2 части)
    name = record["name"]
    parts = name.split('.')
    record["short_name"] = '.'.join(parts[-2:]) if len(parts) > 2 else name
    
    # 2. Обрезаем имя функции
    record["short_function"] = _truncate_text(record["function"], max_length=35)
    
    # 3. ★ Условная обрезка сообщения
    # Числовые уровни loguru:
    #   DEBUG=10, INFO=20, WARNING=30, ERROR=40, CRITICAL=50
    # Для ERROR (40) и CRITICAL (50) — НЕ обрезаем
    if record["level"].no >= 40:  # ERROR и выше
        record["short_message"] = record["message"]
    else:
        record["short_message"] = _truncate_text(record["message"], max_length=500)
    
    return record


def setup_logger():
    logger.remove()
    logger.configure(patcher=_patch_record)
    
    console_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{short_name:<35}</cyan> | "
        "<cyan>{short_function:<35}</cyan> | "
        "<red>{line:<5}</red> | "
        "<level>{short_message}</level>"
    )
    
    file_format = (
        "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
        "{short_name:<55} | "
        "{short_function:<55} | "
        "{line:<5} | "
        "{short_message}"
    )
    
    logger.add(sys.stderr, format=console_format, level=LOG_LEVEL)
    
    # Лог-файл перезаписывается при каждом запуске
    logger.add(
        str(LOG_FILE),
        format=file_format,
        level='DEBUG',
        mode="w",
        retention=None,
        enqueue=True,
        encoding="utf-8"
    )
    
    return logger