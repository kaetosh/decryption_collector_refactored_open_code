# -*- coding: utf-8 -*-
"""
Пакет CLI: аргументы командной строки, точка входа и утилиты интерфейса.
"""

from .arguments import parse_arguments
from .main import main, entry_point

__all__ = [
    "parse_arguments",
    "main",
    "entry_point",
]

