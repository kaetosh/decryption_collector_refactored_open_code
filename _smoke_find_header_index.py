# -*- coding: utf-8 -*-
"""
Смоук: NA-безопасный поиск заголовков (utils.dataframe_utils.find_header_index).

Регрессия, которую ловит:
  ОСВ-процессоры искали колонки через list.index(). Если в шапке выгрузки
  1С есть пустые ячейки, имена колонок содержат настоящие pd.NA, и
  list.index() падал с «TypeError: boolean value of NA is ambiguous»
  (pandas 2.x). Раньше это глушилось except/except: pass либо не
  приводило к внятной диагностике.

Смоук обязан падать при провале: check() увеличивает счётчик FAILED, финальная
строка — SMOKE_OK (PASSED/total) либо SMOKE_FAIL со списком провалов и код
возврата 1.

Запуск: python -u _smoke_find_header_index.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from utils.dataframe_utils import find_header_index

PASSED = 0
FAILED = 0
_failed_messages: list[str] = []


def check(condition: bool, message: str) -> None:
    """Одна проверка с прогрессом (зелёная — счётчик, красная — счётчик)."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[OK] {message}")
    else:
        FAILED += 1
        _failed_messages.append(message)
        print(f"[FAIL] {message}")


def test_pure_strings() -> None:
    columns = pd.Index(['Счет', 'Сальдо на начало периода', 'Обороты за период'])
    check(
        find_header_index(columns, 'Обороты за период') == 2,
        "обычные строки: «Обороты за период» найден на позиции 2",
    )
    check(
        find_header_index(columns, 'Счет') == 0,
        "обычные строки: «Счет» найден на позиции 0",
    )
    check(
        find_header_index(columns, 'Нет такой') is None,
        "обычные строки: отсутствующий заголовок даёт None",
    )


def test_pd_na_columns() -> None:
    """Настоящие pd.NA в именах колонок — источник падения list.index()."""
    columns = pd.Index(
        ['Счет', pd.NA, 'Сальдо на начало периода', 'Обороты за период'],
        dtype=object,
    )
    check(
        find_header_index(columns, 'Обороты за период') == 3,
        f"при pd.NA в шапке: «Обороты за период» на позиции 3 "
        f"(факт: {find_header_index(columns, 'Обороты за период')})",
    )
    check(
        find_header_index(columns, 'Сальдо на начало периода') == 2,
        f"при pd.NA в шапке: «Сальдо на начало периода» на позиции 2 "
        f"(факт: {find_header_index(columns, 'Сальдо на начало периода')})",
    )
    # Поиск pd.NA как значения не должен падать и не должен «найти» его.
    check(
        find_header_index(columns, 'Отсутствует') is None,
        "при pd.NA в шапке: отсутствующий заголовок даёт None, а не падает",
    )


def test_list_index_actually_fails() -> None:
    """Фиксирует исходную причину: list.index() на pd.NA падает."""
    columns = pd.Index(['Счет', pd.NA, 'Обороты за период'], dtype=object)
    try:
        columns.tolist().index('Обороты за период')
    except TypeError as exc:
        check('NA' in str(exc), f"list.index() на pd.NA падает с упоминанием NA: {exc}")
        return
    check(False, 'Ожидался TypeError от list.index() на pd.NA, а его не было')


def test_duplicates_rejected() -> None:
    """Неоднозначный заголовок -> None: выбор был бы произвольным."""
    columns = pd.Index(['Обороты', 'Обороты'])
    check(
        find_header_index(columns, 'Обороты') is None,
        "дубли заголовка дают None (выбор был бы произвольным)",
    )


def test_case_and_space_insensitive() -> None:
    """Шапка 1С пестрит пробелами и регистром."""
    columns = pd.Index(['  Обороты за период  ', 'СЧЕТ'])
    check(
        find_header_index(columns, 'обороты за период') == 0,
        "пробелы и регистр игнорируются: «обороты за период» на позиции 0",
    )
    check(
        find_header_index(columns, 'Счет') == 1,
        "пробелы и регистр игнорируются: «Счет» на позиции 1",
    )


def main() -> None:
    test_pure_strings()
    test_pd_na_columns()
    test_list_index_actually_fails()
    test_duplicates_rejected()
    test_case_and_space_insensitive()

    total = PASSED + FAILED
    label = 'find_header_index (NA-безопасный поиск заголовков)'
    if FAILED:
        print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
        for line in _failed_messages:
            print(f"  [FAIL] {line}")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


if __name__ == '__main__':
    main()