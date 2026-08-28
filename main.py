# -*- coding: utf-8 -*-
"""
Точка входа приложения Decryption Collector (совместимый запускатель).

Этот модуль сохраняет обратную совместимость с запуском `python main.py`.
Вся логика приложения находится в пакете `cli` (см. cli/main.py).
"""

import sys
from pathlib import Path

# Добавляем корневую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent))

from cli.main import main, entry_point  # noqa: E402

# Псевдонимы для обратной совместимости (если кто-то импортирует main.main)
MAIN = main
APP_MAIN = main


if __name__ == "__main__":
    sys.exit(entry_point())

