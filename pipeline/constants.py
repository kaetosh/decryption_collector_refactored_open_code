# -*- coding: utf-8 -*-
"""
Константы, используемые в пайплайне обработки данных.

Этот модуль централизует все константы, чтобы избежать дублирования
и упростить поддержку.
"""


class ColumnNames:
    """Названия колонок, используемые в обработке."""
    BALANCE = 'сальдо, тыс.ед.'
    BALANCE_RUB = 'сальдо, тыс.руб.'
    
    # Константы из main.py
    SHORT_COMPANY_NAME = "сокращенное_наименование_компании"
    SEGMENT = "сегмент"
    PERIOD_TYPE = "тип_периода"
    DECRYPTION_FILENAME = "название_файла_расшифровки"
    CURRENCY = "валюта"


class DataTypes:
    """Типы данных pandas."""
    OBJECT = 'object'
    STRING = 'string'


class Prefixes:
    """Префиксы для специальных колонок."""
    LEVEL = 'level_'


class Values:
    """Специальные значения."""
    UNSPECIFIED = 'не_указано'