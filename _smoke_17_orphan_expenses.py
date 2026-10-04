# -*- coding: utf-8 -*-
"""
Смоук: распределение расходов 91.02 по выручке 91.01 (шаг 17).

Регрессии, которые ловит:
  1. Раньше при нарушении инварианта суммы метод возвращал ИСХОДНЫЙ
     df_9102, выбрасывая всё корректно распределённое. Теперь потерянная
     сумма дописывается строкой-остатком, данные не теряются.
  2. Допуск читается из context.tolerance_params (лист «Параметры»),
     а не захардкожен в StepConstants.

Запуск: python _smoke_17_orphan_expenses.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.base import ProcessingContext
from pipeline.steps.step_17_add_other_income_and_expenses import (
    Step17AddOtherIncomeExpensesToOpuStep,
)

ASSET_SALE_TYPE = 'Доходы и расходы от продажи активов'
COL_TYPE = 'вид_дохода_расхода'
COL_AMOUNT = 'оборот, тыс.ед.'
COL_AMOUNT_RUB = 'оборот, тыс.руб.'

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


def make_9101() -> pd.DataFrame:
    """Выручка по продаже активов: два контрагента в одном документе."""
    return pd.DataFrame({
        'Документ': ['Продажа-1', 'Продажа-1'],
        COL_TYPE: [ASSET_SALE_TYPE, ASSET_SALE_TYPE],
        'контрагент': ['ООО Альфа', 'ООО Бетта'],
        COL_AMOUNT: [600.0, 400.0],
        COL_AMOUNT_RUB: [6000.0, 4000.0],
    })


def make_9102(document: str = 'Списание-1') -> pd.DataFrame:
    """
    Расходы по продаже активов, оформленные ОТДЕЛЬНЫМ документом.

    Это и есть «осиротевший» случай: выручка по этой статье есть,
    но в другом документе, поэтому расход невозможно смаппить напрямую
    и он распределяется пропорционально долям контрагентов выручки.
    """
    return pd.DataFrame({
        'Документ': [document, document],
        COL_TYPE: [ASSET_SALE_TYPE, ASSET_SALE_TYPE],
        'контрагент': [pd.NA, pd.NA],
        'Субконто Кт_1': ['Актив-1', 'Актив-2'],
        'Сумма': [500.0, 500.0],
        'Сумма_руб': [5000.0, 5000.0],
        COL_AMOUNT: [500.0, 500.0],
        COL_AMOUNT_RUB: [5000.0, 5000.0],
    })


def make_context(tolerance: float | None = None) -> ProcessingContext:
    return ProcessingContext(
        company='Тест',
        period='2026',
        tolerance_params={} if tolerance is None else {'tolerance_orphan_distribution': tolerance},
    )


def test_sum_preserved_on_distribution() -> None:
    """Штатный случай: сумма 91.02 до = после распределения."""
    step = Step17AddOtherIncomeExpensesToOpuStep()
    df_9101, df_9102 = make_9101(), make_9102()
    before = df_9102[COL_AMOUNT].sum()

    result = step._distribute_orphan_expenses(
        df_9101, df_9102, asset_sale_types=[ASSET_SALE_TYPE], context=make_context(),
    )

    after = result[COL_AMOUNT].sum()
    check(
        abs(after - before) < 1e-6,
        f"сумма 91.02 сохранена при распределении (было {before}, стало {after})",
    )
    # Строки размножились по контрагентам выручки: 2 расхода × 2 контрагента
    check(len(result) == 4, f"2 расхода × 2 контрагента = 4 строки (факт: {len(result)})")
    check(
        set(result['контрагент']) == {'ООО Альфа', 'ООО Бетта'},
        f"расходы разнесены по контрагентам выручки ({sorted(set(result['контрагент']))})",
    )


def test_no_rollback_on_sum_violation() -> None:
    """
    Нарушение инварианта суммы: потерянная сумма дописывается остатком,
    а не откатом всего распределения.
    """
    step = Step17AddOtherIncomeExpensesToOpuStep()

    # Доли 1/9 и 8/9 не представимы в двоичной системе -> при умножении
    # на 1000 накапливается реальная погрешность (~1.1e-13).
    df_9101 = pd.DataFrame({
        'Документ': ['Продажа-1', 'Продажа-1'],
        COL_TYPE: [ASSET_SALE_TYPE, ASSET_SALE_TYPE],
        'контрагент': ['ООО Альфа', 'ООО Бетта'],
        COL_AMOUNT: [1.0, 8.0],
        COL_AMOUNT_RUB: [10.0, 80.0],
    })
    df_9102 = make_9102()
    df_9102[COL_AMOUNT] = [1000.0, 0.0]
    df_9102[COL_AMOUNT_RUB] = [10000.0, 0.0]
    before = df_9102[COL_AMOUNT].sum()

    # Допуск 0.0 — любая погрешность деления считается нарушением.
    result = step._distribute_orphan_expenses(
        df_9101, df_9102, asset_sale_types=[ASSET_SALE_TYPE], context=make_context(0.0),
    )

    after = result[COL_AMOUNT].sum()
    check(
        abs(after - before) < 1e-6,
        f"сумма 91.02 сохранена при дописывании остатка (было {before}, стало {after})",
    )
    check(
        len(result) > 4,
        f"остаток добавил строки, а не откатил распределение (строк: {len(result)})",
    )
    # Остаток помечен как нераспределённый — виден в отчёте и в ОПУ.
    check(
        (result['контрагент'] == 'не_указано').any(),
        "остаток помечен контрагентом «не_указано»",
    )


def test_remainder_restores_sum() -> None:
    """_build_orphan_remainder компенсирует ровно указанную потерю."""
    step = Step17AddOtherIncomeExpensesToOpuStep()
    df_9102 = make_9102()

    remainder = step._build_orphan_remainder(df_9102, {COL_AMOUNT: 7.5})
    check(len(remainder) == 1, f"остаток — одна строка (факт: {len(remainder)})")
    check(
        remainder[COL_AMOUNT].iloc[0] == 7.5,
        f"остаток равен потере 7.5 (факт: {remainder[COL_AMOUNT].iloc[0]})",
    )
    # Прочие суммовые колонки обнулены — иначе сумма двоилась бы.
    check(
        remainder[COL_AMOUNT_RUB].iloc[0] == 0.0,
        f"прочие суммовые колонки обнулены (факт: {remainder[COL_AMOUNT_RUB].iloc[0]})",
    )
    check(
        remainder['контрагент'].iloc[0] == 'не_указано',
        f"остаток без контрагента (факт: {remainder['контрагент'].iloc[0]})",
    )


def test_tolerance_from_context() -> None:
    """Допуск берётся из «Параметров», при отсутствии — дефолт 0.01."""
    step = Step17AddOtherIncomeExpensesToOpuStep()
    check(
        step._get_orphan_tolerance(make_context(0.5)) == 0.5,
        "допуск взят из «Параметров»",
    )
    check(step._get_orphan_tolerance(make_context()) == 0.01, "без параметра — дефолт 0.01")
    check(step._get_orphan_tolerance(None) == 0.01, "без контекста — дефолт 0.01")


def test_untouched_when_no_orphans() -> None:
    """Расходы в том же документе, что и выручка — распределение не нужно."""
    step = Step17AddOtherIncomeExpensesToOpuStep()
    df_9101, df_9102 = make_9101(), make_9102(document='Продажа-1')
    result = step._distribute_orphan_expenses(
        df_9101, df_9102, asset_sale_types=[ASSET_SALE_TYPE], context=make_context(),
    )
    check(
        len(result) == 2,
        f"без «осиротевших» расходов строки не размножаются (факт: {len(result)})",
    )


def main() -> None:
    test_sum_preserved_on_distribution()
    test_no_rollback_on_sum_violation()
    test_remainder_restores_sum()
    test_tolerance_from_context()
    test_untouched_when_no_orphans()

    total = PASSED + FAILED
    label = 'распределение расходов 91.02 по выручке продажи активов'
    if FAILED:
        print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
        for line in _failed_messages:
            print(f"  [FAIL] {line}")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


if __name__ == '__main__':
    main()
