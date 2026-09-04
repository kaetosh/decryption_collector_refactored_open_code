# -- coding: utf-8 --
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
    Если текст длиннее, сохраняет НАЧАЛО text и добавляет '...' в конец.
    Начало сообщения информативнее хвоста: заголовок и первые элементы списка,
    а не обрезанный хвост с '...' впереди (непонятно, что и с чем).
    """
    if len(text) <= max_length:
        return text
    return f"{text[:max_length - 3]}..."

def _patch_record(record):
    """
    Добавляет в запись сокращённые имена модулей, функций и сообщений.
    Для уровней ERROR и CRITICAL сообщение НЕ обрезается —
    важная диагностическая информация сохраняется полностью.
    """
    name = record["name"]
    parts = name.split('.')
    short_name = '.'.join(parts[-2:]) if len(parts) > 2 else name
    
    # Отдельные сокращения для файла
    record["file_short_name"] = _truncate_text(short_name, max_length=55)
    record["short_function"] = _truncate_text(record["function"], max_length=55)
    
    if record["level"].no >= 40:
        record["short_message"] = record["message"]
    else:
        record["short_message"] = _truncate_text(record["message"], max_length=500)
        
    return record

def setup_logger(console_level: str = LOG_LEVEL) -> None:
    logger.remove()
    logger.configure(patcher=_patch_record)
    
    # Формат для консоли: только дата/время, уровень и сообщение
    console_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<level>{short_message}</level>"
    )
    
    # Формат для файла (содержит все технические детали и ПОЛНОЕ сообщение —
    # без short_message, чтобы длинные диагностики (списки, стеки) не обрезались)
    file_format = (
        "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
        "{file_short_name:<55} | "
        "{short_function:<55} | "
        "{line:<5} | "
        "{message}"
    )
    
    logger.add(sys.stderr, format=console_format, level=console_level)
    
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
