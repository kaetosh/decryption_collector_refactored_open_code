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
    assert abs(after - before) < 1e-6, f'Сумма потеряна: было {before}, стало {after}'
    # Строки размножились по контрагентам выручки: 2 расхода × 2 контрагента
    assert len(result) == 4, f'Ожидалось 4 строки, получено {len(result)}'
    assert set(result['контрагент']) == {'ООО Альфа', 'ООО Бетта'}, set(result['контрагент'])


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
    assert abs(after - before) < 1e-6, f'Сумма потеряна: было {before}, стало {after}'
    assert len(result) > 4, 'Остаток должен добавить строки, а не откатить распределение'
    # Остаток помечен как нераспределённый — виден в отчёте и в ОПУ.
    assert (result['контрагент'] == 'не_указано').any()


def test_remainder_restores_sum() -> None:
    """_build_orphan_remainder компенсирует ровно указанную потерю."""
    step = Step17AddOtherIncomeExpensesToOpuStep()
    df_9102 = make_9102()

    remainder = step._build_orphan_remainder(df_9102, {COL_AMOUNT: 7.5})
    assert len(remainder) == 1, len(remainder)
    assert remainder[COL_AMOUNT].iloc[0] == 7.5
    # Прочие суммовые колонки обнулены — иначе сумма двоилась бы.
    assert remainder[COL_AMOUNT_RUB].iloc[0] == 0.0
    assert remainder['контрагент'].iloc[0] == 'не_указано'


def test_tolerance_from_context() -> None:
    """Допуск берётся из «Параметров», при отсутствии — дефолт 0.01."""
    step = Step17AddOtherIncomeExpensesToOpuStep()
    assert step._get_orphan_tolerance(make_context(0.5)) == 0.5
    assert step._get_orphan_tolerance(make_context()) == 0.01
    assert step._get_orphan_tolerance(None) == 0.01


def test_untouched_when_no_orphans() -> None:
    """Расходы в том же документе, что и выручка — распределение не нужно."""
    step = Step17AddOtherIncomeExpensesToOpuStep()
    df_9101, df_9102 = make_9101(), make_9102(document='Продажа-1')
    result = step._distribute_orphan_expenses(
        df_9101, df_9102, asset_sale_types=[ASSET_SALE_TYPE], context=make_context(),
    )
    assert len(result) == 2, len(result)


def main() -> None:
    test_sum_preserved_on_distribution()
    test_no_rollback_on_sum_violation()
    test_remainder_restores_sum()
    test_tolerance_from_context()
    test_untouched_when_no_orphans()
    print('SMOKE_OK 5 scenarios: distribution of 91.02')


if __name__ == '__main__':
    main()
