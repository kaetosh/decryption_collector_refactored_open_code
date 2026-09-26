# -*- coding: utf-8 -*-
"""
Аргументы командной строки.

Содержит функции для разбора аргументов командной строки.
"""

import argparse
import difflib

# Приближение порога подсказки «возможно, имелось в виду» при опечатке
# в названии аргумента. 0.6 — консервативно: подсказываем только при
# явном сходстве, чтобы не предлагать бессмысленные варианты.
_SUGGESTION_CUTOFF = 0.6


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

    parser.add_argument(
        '--no-splash',
        action='store_true',
        help='Не показывать заставку при запуске'
    )

    parser.add_argument(
        '--balance-date',
        type=str,
        default=None,
        help='Дата перевода валютных остатков в рубли для расшифровки баланса, '
             'формат ДД.ММ.ГГГГ (только для компаний с валютой, отличной от RUB; '
             'если не задана — запрашивается интерактивно, а без TTY берётся '
             'последняя дата из справочника курса)'
    )

    # parse_known_args вместо parse_args - не падает, если аргументы не распознаны
    args, unknown = parser.parse_known_args()

    # Неизвестные аргументы не роняем (иначе не откроется ни одна выгрузка),
    # но и не глотаем молча: опечатка вроде «--no-interactiv» иначе тихо
    # оставляет программу в интерактивном режиме с ожиданием ввода.
    # Список отдаём в cli/main — там поднято логирование.
    args.unknown_args = list(unknown)
    args.suggestions = _suggest_known_options(unknown, parser)
    return args


def _suggest_known_options(
    unknown: list,
    parser: argparse.ArgumentParser,
) -> dict[str, str]:
    """
    Подбирает к неизвестному аргументу наиболее похожий известный.

    Возвращает словарь {неизвестный аргумент: подсказка} — только для
    аргументов, не уходящих явно далеко от существующих опций.
    Пример: '--no-interactiv' -> '--no-interactive'.
    """
    known: list[str] = []
    for action in parser._actions:
        known.extend(action.option_strings)

    suggestions: dict[str, str] = {}
    for arg in unknown:
        candidates = difflib.get_close_matches(
            arg, known, n=1, cutoff=_SUGGESTION_CUTOFF,
        )
        if candidates:
            suggestions[arg] = candidates[0]
    return suggestions
