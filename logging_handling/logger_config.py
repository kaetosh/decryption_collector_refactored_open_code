# -- coding: utf-8 --
"""
Модуль конфигурации логирования.
Настраивает loguru для всего приложения.
"""
import sys
import warnings
from collections import Counter
from loguru import logger
from config.settings import LOG_LEVEL, LOG_FILE

# Ключ в record["extra"]: запись помечена как «только в файл» и не
# показывается в консоли. Служебные предупреждения библиотек (pandas,
# openpyxl) несут технический текст, не понятный пользователю, поэтому
# в консоль не попадают, но остаются в app.log для разбора.
_FILE_ONLY_KEY = "file_only"

# Сводка предупреждений за прогон: {текст: сколько раз встретился}.
# Заполняется sink-ом _collect_warning_sink. Нужна, чтобы в конце прогона
# показать все предупреждения одной таблицей (консоль) и перечислить их
# на титульном листе отчёта, а не искать глазами среди сотен строк лога.
_collected_warnings: Counter = Counter()

# Накопление строк текущего предупреждения (группировка по префиксу "[!]").
_current_warning_group: list[str] = []


def _route_warnings_to_log_file() -> None:
    """
    Перенаправляет warnings в loguru вместо прямой печати в stderr.

    По умолчанию warnings.showwarning пишет в sys.stderr, минуя loguru:
    сообщения не попадают в app.log и засоряют консоль. Здесь каждое
    предупреждение становится записью уровня WARNING с меткой
    file_only=True, которую фильтр консольного sink отбрасывает,
    а файловый sink (уровень DEBUG, без фильтра) сохраняет.
    """

    def _showwarning(message, category, filename, lineno, file=None, line=None):
        logger.bind(**{_FILE_ONLY_KEY: True}).warning(
            "[{category}] {filename}:{lineno}: {message}",
            category=category.__name__,
            filename=filename,
            lineno=lineno,
            message=message,
        )

    warnings.showwarning = _showwarning


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

def _collect_warning_sink(message) -> None:
    """
    Sink loguru уровня WARNING: складывает тексты предупреждений в счётчик.

    Сообщение схлопывается в одну строку: часть предупреждений
    формируется многострочными f-строками (списки файлов, позиций),
    и в сводке каждое из них должно занимать ровно одну строку.
    
    Группировка: строки, начинающиеся с "[!]" — новое сгруппированное предупреждение.
    Последующие строки без "[!]" — продолжение ТОГО ЖЕ сгруппированного предупреждения.
    Предупреждения БЕЗ "[!]" — учитываются по отдельности (старое поведение).
    """
    text = ' '.join(str(message).split())
    if not text:
        return
    
    global _current_warning_group
    
    if text.startswith("[!]"):
        # Новое сгруппированное предупреждение — сбрасываем предыдущую группу
        if _current_warning_group:
            key = _current_warning_group[0]
            _collected_warnings[key] += 1
        _current_warning_group = [text]
    elif _current_warning_group:
        # Продолжение текущего сгруппированного предупреждения
        _current_warning_group.append(text)
    else:
        # Обычное предупреждение без группировки — сразу в счётчик
        _collected_warnings[text] += 1


def get_collected_warnings() -> list[tuple[str, int]]:
    """
    Предупреждения, собранные за прогон: [(текст, сколько раз), ...].

    Сортировка — по убыванию частоты, затем по алфавиту: сначала то,
    что повторялось и важнее проверить, порядок запуска не важен.
    """
    global _current_warning_group
    if _current_warning_group:
        key = _current_warning_group[0]
        _collected_warnings[key] += 1
        _current_warning_group = []
    return sorted(_collected_warnings.items(), key=lambda item: (-item[1], item[0]))


def reset_collected_warnings() -> None:
    """Очищает сводку — вызывается при настройке логирования (старт запуска)."""
    _collected_warnings.clear()
    global _current_warning_group
    _current_warning_group = []


def format_warnings_summary(limit: int = None, max_length: int = 200) -> list[str]:
    """
    Готовая сводка предупреждений в виде строк для показа пользователю.

    Текст обрезается до max_length символов: часть предупреждений
    перечисляет позиции (группы, счета, файлы) и занимает сотни символов —
    в сводке достаточно первых символов, полный текст остаётся в app.log.

    Args:
        limit: Оставить только первые N самых частых (None — без ограничения).
        max_length: Максимальная длина текста предупреждения.

    Returns:
        Строки вида «текст предупреждения» или «текст (×12)».
    """
    items = get_collected_warnings()
    if limit is not None:
        items = items[:limit]
    return [
        _truncate_text(text, max_length=max_length) + (
            f" (×{count})" if count > 1 else ""
        )
        for text, count in items
    ]


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
    
    # Консольный sink пропускает записи, помеченные как file_only
    # (см. _route_warnings_to_log_file). Чтобы вернуть предупреждения
    # в консоль, достаточно убрать filter=.
    logger.add(
        sys.stderr,
        format=console_format,
        level=console_level,
        filter=lambda record: not record["extra"].get(_FILE_ONLY_KEY, False),
    )
    
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

    # Сборщик сводки предупреждений. Фильтр отбрасывает записи file_only
    # (технические предупреждения библиотек) — в сводке они только шумят.
    # Вызывается после logger.configure(patcher=...): сообщение приходит
    # уже с подставленными аргументами, без обрезки short_message.
    reset_collected_warnings()
    logger.add(
        _collect_warning_sink,
        level='WARNING',
        format='{message}',
        filter=lambda record: not record["extra"].get(_FILE_ONLY_KEY, False),
    )

    # Предупреждения библиотек — только в файл (вызывается последним,
    # чтобы записи гарантированно попали в оба sink-а)
    _route_warnings_to_log_file()

    return logger
