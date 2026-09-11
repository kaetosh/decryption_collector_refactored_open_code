"""
Пакет для работы с вводом/выводом данных.
Загрузка из 1С, справочников, автосортировка inbox и сохранение результатов.
"""

from .data_io import DataLoader, DataSaver
from .auto_sort import (
    build_expected_map,
    normalize_name,
    prepare_general_osv_from_inbox,
    sort_all,
    sort_inbox_by_expected_list,
)

# Явно определяем публичный API пакета
__all__ = [
    'DataLoader',
    'DataSaver',
    'build_expected_map',
    'normalize_name',
    'prepare_general_osv_from_inbox',
    'sort_all',
    'sort_inbox_by_expected_list',
]
