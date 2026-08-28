# -*- coding: utf-8 -*-
"""
Аргументы командной строки и утилиты интерфейса.

Содержит функции для разбора аргументов командной строки
и интерактивных вопросов пользователю (например, о traceback).
"""

import argparse


def parse_arguments() -> argparse.Namespace:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Decryption Collector - сбор расшифровки баланса из ОСВ",
        add_help=True
    )

    parser.add_argument(
        '-t', '--traceback',
        action='store_true',
        help='Выводить полную трассировку стека при ошибках'
    )

    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Подробный режим логирования (DEBUG уровень)'
    )

    parser.add_argument(
        '--no-interactive',
        action='store_true',
        help='Не задавать интерактивных вопросов (для автоматического режима)'
    )

    # parse_known_args вместо parse_args - не падает, если аргументы не распознаны
    args, unknown = parser.parse_known_args()
    return args


def ask_user_about_traceback() -> bool:
    """
    Интерактивно спрашивает пользователя, нужен ли traceback.

    Используется в IDE (Spyder), когда аргументы не переданы через командную строку.
    """
    try:
        print("\n" + "=" * 80)
        print("[DIAG] Режим диагностики")
        print("=" * 80)
        print("Хотите выводить полную трассировку стека при ошибках?")
        print("  [enter] - нет (по умолчанию, чистый вывод)")
        print("  [y]   - да (полный traceback для отладки)")
        print("=" * 80)

        POSITIVE_RESPONSES = {'y', 'yes', 'д', 'да'}
        response = input("Ваш выбор: ").strip().lower()
        return response in POSITIVE_RESPONSES
    except (EOFError, KeyboardInterrupt):
        # Если stdin недоступен (например, при запуске из cron)
        return False
