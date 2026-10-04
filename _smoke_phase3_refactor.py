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

Запуск: python -u _smoke_phase3_refactor.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from utils import build_composite_key, align_dtypes_to_reference
from pipeline.steps.step_17_add_other_income_and_expenses import (
    Step17AddOtherIncomeExpensesToOpuStep,
)

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


def test_composite_key_without_truncation() -> None:
    """Шаг 14: счет + '_' + ном_группа, счёт обрезается до 5 знаков в маппинге."""
    df = pd.DataFrame({
        'счет': ['90.01', '90.02'],
        'ном_группа': ['Гр-1', 'Гр-2'],
    })
    check(
        list(build_composite_key(df, 'счет', 'ном_группа')) == ['90.01_Гр-1', '90.02_Гр-2'],
        "ключ без обрезки: счёт + '_' + ном_группа",
    )


def test_composite_key_with_truncation() -> None:
    """Шаг 17: счёт режется до 5 знаков, вторая часть — целиком."""
    df = pd.DataFrame({
        'счет': ['91.0200001', '91.01.2'],
        'доход_расход': ['Аренда', 'Аренда'],
    })
    result = build_composite_key(df, 'счет', 'доход_расход', truncate_a=5)
    check(
        list(result) == ['91.02_Аренда', '91.01_Аренда'],
        f"счёт обрезан до 5 знаков, вторая часть целиком (факт: {list(result)})",
    )


def test_composite_key_truncation_is_not_silent() -> None:
    """Без truncate счёт остаётся полным — регрессия среза."""
    df = pd.DataFrame({'счет': ['91.0200001'], 'доход_расход': ['Аренда']})
    result = build_composite_key(df, 'счет', 'доход_расход')
    check(
        list(result) == ['91.0200001_Аренда'],
        f"без truncate счёт остаётся полным (факт: {list(result)})",
    )


def test_composite_key_truncates_second_part_too() -> None:
    """truncate_b применяется ко второй части."""
    df = pd.DataFrame({'счет': ['90.01'], 'доход_расход': ['Длинный тип']})
    result = build_composite_key(df, 'счет', 'доход_расход', truncate_b=3)
    check(
        list(result) == ['90.01_Дли'],
        f"truncate_b применяется ко второй части (факт: {list(result)})",
    )


def test_composite_key_custom_separator() -> None:
    """Разделитель настраивается."""
    df = pd.DataFrame({'счет': ['90.01'], 'ном_группа': ['Гр-1']})
    check(
        list(build_composite_key(df, 'счет', 'ном_группа', sep='|')) == ['90.01|Гр-1'],
        'разделитель настраивается (sep="|")',
    )


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

    check(
        list(df['вид_дохода_расхода']) == ['Расходы по аренде'] * 2,
        "при одном кандидате на ключ тип проставлен всем строкам "
        f"(факт: {df['вид_дохода_расхода'].tolist()})",
    )


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
    check(
        orphans['контрагент'].dtype == object,
        f"исходная сборка из list/numpy даёт object (факт: {orphans['контрагент'].dtype})",
    )

    aligned = align_dtypes_to_reference(orphans, main, columns=['контрагент', 'ном_группа'])
    result = pd.concat([main, aligned], ignore_index=True)

    check(
        str(result['контрагент'].dtype) == 'string',
        f"после concat контрагент остался string (факт: {result['контрагент'].dtype})",
    )
    check(
        str(result['ном_группа'].dtype) == 'string',
        f"после concat ном_группа остался string (факт: {result['ном_группа'].dtype})",
    )
    check(
        list(result['контрагент']) == ['ООО Альфа', '3 лица'],
        f"значения не потеряны при выравнивании ({list(result['контрагент'])})",
    )


def test_align_dtypes_without_helper_downgrades() -> None:
    """
    Контрольная точка: без выравнивания concat реально понижает тип.
    Иначе тест выше проходил бы вхолостую.
    """
    main = pd.DataFrame({'контрагент': pd.Series(['ООО Альфа'], dtype='string')})
    orphans = pd.DataFrame({'контрагент': pd.Series(['3 лица'], dtype=object)})
    naive = pd.concat([main, orphans], ignore_index=True)
    check(
        str(naive['контрагент'].dtype) != 'string',
        f"без выравнивания concat понижает тип (факт: {naive['контрагент'].dtype}) — "
        f"иначе проверка выше проходила бы вхолостую",
    )


def test_align_dtypes_keeps_reference_untouched() -> None:
    """align_dtypes возвращает копию и не меняет исходные DataFrame."""
    main = pd.DataFrame({'контрагент': pd.Series(['А'], dtype='string')})
    orphans = pd.DataFrame({'контрагент': pd.Series(['Б'], dtype=object)})
    align_dtypes_to_reference(orphans, main)
    check(
        orphans['контрагент'].dtype == object,
        f"исходный orphans не изменён (факт: {orphans['контрагент'].dtype})",
    )
    check(
        str(main['контрагент'].dtype) == 'string',
        f"эталон не изменён (факт: {main['контрагент'].dtype})",
    )


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
    total = PASSED + FAILED
    label = 'фаза 3: составные ключи и выравнивание типов'
    if FAILED:
        print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
        for line in _failed_messages:
            print(f"  [FAIL] {line}")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


if __name__ == '__main__':
    main()
