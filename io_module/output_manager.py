# -*- coding: utf-8 -*-
"""
Управление выводными данными запуска.

Каждый запуск приложения пишет все свои результаты в отдельную папку
_OUTPUT_DATA/run_ГГГГММДД_ЧЧММСС. Все места записи файлов должны получать
путь через get_output_dir() — это единственная точка формирования путей
вывода (вместо прямого использования OUTPUT_DATA_DIR).

Хранение: последние KEEP_LAST_RUNS папок запусков (см. config.settings);
очистка старых папок выполняется функцией cleanup_old_runs() при старте.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from config.settings import OUTPUT_DATA_DIR

# Префикс имени папки запуска внутри _OUTPUT_DATA
RUN_DIR_PREFIX = "run_"

# Состояние текущего запуска (заполняется configure_run)
_run_id: Optional[str] = None
_run_dir: Optional[Path] = None


def new_run_id() -> str:
    """Возвращает идентификатор запуска: ГГГГММДД_ЧЧММСС."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def configure_run(run_id: Optional[str] = None) -> str:
    """
    Инициализирует вывод для нового запуска.

    Создаёт папку запуска _OUTPUT_DATA/run_<run_id> и запоминает её:
    все последующие вызовы get_output_dir() возвращают пути внутри неё.

    Args:
        run_id: Идентификатор запуска; если не задан — генерируется
            из текущих даты/времени.

    Returns:
        Идентификатор запуска.
    """
    global _run_id, _run_dir

    _run_id = run_id or new_run_id()

    run_dir = OUTPUT_DATA_DIR / f"{RUN_DIR_PREFIX}{_run_id}"
    # Защита от коллизии: два запуска в одну секунду
    suffix = 2
    while run_dir.exists():
        run_dir = OUTPUT_DATA_DIR / f"{RUN_DIR_PREFIX}{_run_id}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=True)

    _run_dir = run_dir
    logger.debug("Папка вывода запуска: {}", _run_dir)
    return _run_id


def get_run_id() -> str:
    """Идентификатор текущего запуска (после configure_run)."""
    if _run_id is None:
        raise RuntimeError(
            "Вывод не инициализирован: сначала вызовите configure_run()"
        )
    return _run_id


def get_run_dir() -> Path:
    """Папка вывода текущего запуска: _OUTPUT_DATA/run_<run_id>."""
    if _run_dir is None:
        raise RuntimeError(
            "Вывод не инициализирован: сначала вызовите configure_run()"
        )
    return _run_dir


def get_output_dir(subfolder: Optional[str] = None) -> Path:
    """
    Папка для сохранения файлов текущего запуска.

    Args:
        subfolder: Дополнительная подпапка внутри папки запуска
            (например 'mismatches', 'warnings').

    Returns:
        Путь к папке (создаётся при необходимости).
    """
    output_dir = get_run_dir()
    if subfolder:
        output_dir = output_dir / subfolder
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir
