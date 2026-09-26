# -*- coding: utf-8 -*-
"""
Смоук: NA-безопасный поиск заголовков (utils.dataframe_utils.find_header_index).

Регрессия, которую ловит:
  ОСВ-процессоры искали колонки через list.index(). Если в шапке выгрузки
  1С есть пустые ячейки, имена колонок содержат настоящие pd.NA, и
  list.index() падал с «TypeError: boolean value of NA is ambiguous»
  (pandas 2.x). Раньше это глушилось except/except: pass либо не
  приводило к внятной диагностике.

Запуск: python _smoke_find_header_index.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from utils.dataframe_utils import find_header_index


def test_pure_strings() -> None:
    columns = pd.Index(['Счет', 'Сальдо на начало периода', 'Обороты за период'])
    assert find_header_index(columns, 'Обороты за период') == 2
    assert find_header_index(columns, 'Счет') == 0
    assert find_header_index(columns, 'Нет такой') is None


def test_pd_na_columns() -> None:
    """Настоящие pd.NA в именах колонок — источник падения list.index()."""
    columns = pd.Index(
        ['Счет', pd.NA, 'Сальдо на начало периода', 'Обороты за период'],
        dtype=object,
    )
    assert find_header_index(columns, 'Обороты за период') == 3
    assert find_header_index(columns, 'Сальдо на начало периода') == 2
    # Поиск pd.NA как значения не должен падать и не должен «найти» его.
    assert find_header_index(columns, 'Отсутствует') is None


def test_list_index_actually_fails() -> None:
    """Фиксирует исходную причину: list.index() на pd.NA падает."""
    columns = pd.Index(['Счет', pd.NA, 'Обороты за период'], dtype=object)
    try:
        columns.tolist().index('Обороты за период')
    except TypeError as exc:
        assert 'NA' in str(exc), exc
        return
    raise AssertionError('Ожидался TypeError от list.index() на pd.NA')


def test_duplicates_rejected() -> None:
    """Неоднозначный заголовок -> None: выбор был бы произвольным."""
    columns = pd.Index(['Обороты', 'Обороты'])
    assert find_header_index(columns, 'Обороты') is None


def test_case_and_space_insensitive() -> None:
    """Шапка 1С пестрит пробелами и регистром."""
    columns = pd.Index(['  Обороты за период  ', 'СЧЕТ'])
    assert find_header_index(columns, 'обороты за период') == 0
    assert find_header_index(columns, 'Счет') == 1


def main() -> None:
    test_pure_strings()
    test_pd_na_columns()
    test_list_index_actually_fails()
    test_duplicates_rejected()
    test_case_and_space_insensitive()
    print('SMOKE_OK 5 scenarios: find_header_index')


if __name__ == '__main__':
    main()
