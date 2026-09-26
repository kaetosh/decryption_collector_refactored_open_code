# -*- coding: utf-8 -*-
"""
Смоук: рефакторинг Phase 3 (шаги 14 и 17).

Что ловит:
  1. build_composite_key — единая сборка составного ключа вместо трёх
     инлайн-версий (шаг 14: счет+ном_группа; шаг 17: счет[:5]+доход_расход).
     Срез первой части меняет результат — это главный риск хелпера.
  2. _resolve_income_expense_mapping больше не строит '_key' сам: ключ
     приходит от вызывающего кода. Регрессия — KeyError/пустые кандидаты,
     если бы один из путей забыл построить ключ.
  3. align_dtypes_to_reference перед concat сирот в шаге 14: 'object'
     из python-списка понижал весь concat до object, и валидация выхода
     шага падала (регрессия 14.09.2026).

Запуск: python _smoke_phase3_refactor.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from utils import build_composite_key, align_dtypes_to_reference
from pipeline.steps.step_17_add_other_income_and_expenses import (
    Step17AddOtherIncomeExpensesToOpuStep,
)


def test_composite_key_without_truncation() -> None:
    """Шаг 14: счет + '_' + ном_группа, счёт обрезается до 5 знаков в маппинге."""
    df = pd.DataFrame({
        'счет': ['90.01', '90.02'],
        'ном_группа': ['Гр-1', 'Гр-2'],
    })
    assert list(build_composite_key(df, 'счет', 'ном_группа')) == ['90.01_Гр-1', '90.02_Гр-2']


def test_composite_key_with_truncation() -> None:
    """Шаг 17: счёт режется до 5 знаков, вторая часть — целиком."""
    df = pd.DataFrame({
        'счет': ['91.0200001', '91.01.2'],
        'доход_расход': ['Аренда', 'Аренда'],
    })
    result = build_composite_key(df, 'счет', 'доход_расход', truncate_a=5)
    assert list(result) == ['91.02_Аренда', '91.01_Аренда'], list(result)


def test_composite_key_truncation_is_not_silent() -> None:
    """Без truncate счёт остаётся полным — регрессия среза."""
    df = pd.DataFrame({'счет': ['91.0200001'], 'доход_расход': ['Аренда']})
    result = build_composite_key(df, 'счет', 'доход_расход')
    assert list(result) == ['91.0200001_Аренда'], list(result)


def test_composite_key_truncates_second_part_too() -> None:
    """truncate_b применяется ко второй части."""
    df = pd.DataFrame({'счет': ['90.01'], 'доход_расход': ['Длинный тип']})
    result = build_composite_key(df, 'счет', 'доход_расход', truncate_b=3)
    assert list(result) == ['90.01_Дли'], list(result)


def test_composite_key_custom_separator() -> None:
    """Разделитель настраивается."""
    df = pd.DataFrame({'счет': ['90.01'], 'ном_группа': ['Гр-1']})
    assert list(build_composite_key(df, 'счет', 'ном_группа', sep='|')) == ['90.01|Гр-1']


def test_resolve_uses_caller_key() -> None:
    """
    Ключ строится вызывающим кодом, а resolve только читает его.
    При одном кандидате на ключ тип ОПУ проставляется всем строкам.
    """
    step = Step17AddOtherIncomeExpensesToOpuStep()

    reference = pd.DataFrame({
        'счет': ['91.02', '91.02.1'],
        'доход_расход': ['Аренда', 'Аренда'],
        'вид_дохода_расхода': ['Расходы по аренде', 'Расходы по аренде'],
    })
    reference['_key'] = build_composite_key(
        reference, 'счет', 'доход_расход', truncate_a=5
    )

    df = pd.DataFrame({
        'счет': ['91.02', '91.0200005'],
        'доход_расход': ['Аренда', 'Аренда'],
        'Корр.счет': ['60.01', '76.05.3'],
        'Субконто Кт_1': ['ППА-1', 'Контрагент-1'],
    })
    df['_key'] = build_composite_key(df, 'счет', 'доход_расход', truncate_a=5)

    step._resolve_income_expense_mapping(df, reference, step.ACCOUNT_OTHER_EXPENSE)

    assert list(df['вид_дохода_расхода']) == ['Расходы по аренде'] * 2, df[
        'вид_дохода_расхода'
    ].tolist()


def test_align_dtypes_before_concat() -> None:
    """
    object из python-списка не должен понижать concat до object.
    Регрессия 14.09.2026: journal_df['контрагент'] = object.
    """
    main = pd.DataFrame({
        'контрагент': pd.Series(['ООО Альфа'], dtype='string'),
        'ном_группа': pd.Series(['Гр-1'], dtype='string'),
        'сумма': [10.0],
    })
    # object-колонки — ровно то, что даёт сборка строк из python/numpy:
    # шаг строит сироты из list и numpy-массива, где dtype выводится как object.
    orphans = pd.DataFrame({
        'контрагент': pd.Series(['3 лица'], dtype=object),
        'ном_группа': pd.Series(['Гр-9'], dtype=object),
        'сумма': [0.0],
    })
    assert orphans['контрагент'].dtype == object, orphans['контрагент'].dtype

    aligned = align_dtypes_to_reference(orphans, main, columns=['контрагент', 'ном_группа'])
    result = pd.concat([main, aligned], ignore_index=True)

    assert str(result['контрагент'].dtype) == 'string', result['контрагент'].dtype
    assert str(result['ном_группа'].dtype) == 'string', result['ном_группа'].dtype
    assert list(result['контрагент']) == ['ООО Альфа', '3 лица']


def test_align_dtypes_without_helper_downgrades() -> None:
    """
    Контрольная точка: без выравнивания concat реально понижает тип.
    Иначе тест выше проходил бы вхолостую.
    """
    main = pd.DataFrame({'контрагент': pd.Series(['ООО Альфа'], dtype='string')})
    orphans = pd.DataFrame({'контрагент': pd.Series(['3 лица'], dtype=object)})
    naive = pd.concat([main, orphans], ignore_index=True)
    assert str(naive['контрагент'].dtype) != 'string', naive['контрагент'].dtype


def test_align_dtypes_keeps_reference_untouched() -> None:
    """align_dtypes возвращает копию и не меняет исходные DataFrame."""
    main = pd.DataFrame({'контрагент': pd.Series(['А'], dtype='string')})
    orphans = pd.DataFrame({'контрагент': pd.Series(['Б'], dtype=object)})
    align_dtypes_to_reference(orphans, main)
    assert orphans['контрагент'].dtype == object, orphans['контрагент'].dtype
    assert str(main['контрагент'].dtype) == 'string', main['контрагент'].dtype


def main() -> None:
    test_composite_key_without_truncation()
    test_composite_key_with_truncation()
    test_composite_key_truncation_is_not_silent()
    test_composite_key_truncates_second_part_too()
    test_composite_key_custom_separator()
    test_resolve_uses_caller_key()
    test_align_dtypes_before_concat()
    test_align_dtypes_without_helper_downgrades()
    test_align_dtypes_keeps_reference_untouched()
    print('SMOKE_OK 9 scenarios: phase 3 refactor')


if __name__ == '__main__':
    main()
