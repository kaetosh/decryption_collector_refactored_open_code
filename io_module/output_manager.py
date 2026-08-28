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

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from config.settings import KEEP_LAST_RUNS, OUTPUT_DATA_DIR

# Префикс имени папки запуска внутри _OUTPUT_DATA
RUN_DIR_PREFIX = "run_"

# Верхняя граница параметра KEEP_LAST_RUNS
MAX_KEEP_LAST_RUNS = 5

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


def cleanup_old_runs(keep_last: int = KEEP_LAST_RUNS) -> None:
    """
    Удаляет папки прошлых запусков, кроме последних keep_last.

    Безопасность:
    - удаляются ТОЛЬКО подпапки _OUTPUT_DATA с именами run_*;
    - любые другие файлы и папки (в том числе оставшиеся от прежних
      версий программы) никогда не затрагиваются;
    - текущая папка запуска — самая новая по имени, поэтому учитывается
      в keep_last и не удаляется.

    Args:
        keep_last: Сколько последних папок запусков хранить (1..MAX).
            Значение вне диапазона заменяется на максимум с предупреждением.
    """
    if not (1 <= keep_last <= MAX_KEEP_LAST_RUNS):
        logger.warning(
            "[!] KEEP_LAST_RUNS = {} вне допустимого диапазона 1..{}, "
            "используем {}",
            keep_last,
            MAX_KEEP_LAST_RUNS,
            MAX_KEEP_LAST_RUNS,
        )
        keep_last = MAX_KEEP_LAST_RUNS

    if not OUTPUT_DATA_DIR.exists():
        return

    run_dirs = sorted(
        (
            d
            for d in OUTPUT_DATA_DIR.iterdir()
            if d.is_dir() and d.name.startswith(RUN_DIR_PREFIX)
        ),
        key=lambda d: d.name,
    )

    # Имя run_ГГГГММДД_ЧЧММСС сортируется лексикографически = хронологически,
    # поэтому последние keep_last папок — это срез [: -keep_last]
    to_delete = run_dirs[:-keep_last]

    for old_dir in to_delete:
        try:
            shutil.rmtree(old_dir)
            logger.debug("Удалена папка прошлого запуска: {}", old_dir.name)
        except PermissionError:
            logger.warning(
                "[!] Не удалось удалить папку прошлого запуска {}: файлы "
                "открыты в другой программе (Excel?). Папка будет удалена "
                "при одном из следующих запусков.",
                old_dir.name,
            )
        except OSError as e:
            logger.warning(
                "[!] Не удалось удалить папку прошлого запуска {}: {}",
                old_dir.name,
                e,
            )
