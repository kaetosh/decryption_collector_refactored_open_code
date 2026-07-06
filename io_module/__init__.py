"""
Пакет для работы с вводом/выводом данных.
Загрузка из 1С, справочников и сохранение результатов.
"""

from .data_io import DataLoader, DataSaver

# Явно определяем публичный API пакета
__all__ = ['DataLoader', 'DataSaver']