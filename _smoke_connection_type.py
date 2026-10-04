# -*- coding: utf-8 -*-
"""
Смоук: единый Step._calculate_connection_type (pipeline/base.py).

Регрессии, которые ловит:
  1. Копия в base_expenses_step.py возвращала Series БЕЗ index=df.index.
     np.select отдаёт numpy-массив, поэтому Series получала RangeIndex, и
     при нестандартном индексе DataFrame значения разъезжались по строкам.
  2. Три копии правила разошлись: в копиях шагов 14/15/16 не было явной
     ветки группа_ка == 'не_указано'. Поведение теперь одно — как в шаге 17.
  3. Шаг 17 передаёт сегмент компании скаляром (у счетов 91 сегмент
     единый), шаги 14/15/16 — колонкой 'сегмент'. Оба режима поддержаны.

Запуск: python -u _smoke_connection_type.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.base import Step
from pipeline.steps.base_expenses_step import StepAddExpensesToOpuBase
from pipeline.steps.step_14_build_opu_foundation import (
    Step14BuildOpuFoundationStep,
)
from pipeline.steps.step_17_add_other_income_and_expenses import (
    Step17AddOtherIncomeExpensesToOpuStep,
)

GROUPS = ['не_указано', '3 лица', 'Прочие ГАП', 'ГСК', 'ГСК']

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


class ProbeStep(Step):
    """Минимальный шаг: нужен только доступ к унаследованному хелперу."""

    def _validate_input(self, context):
        return None

    def _process(self, context):
        return context


def make_step() -> ProbeStep:
    return ProbeStep('probe')


def make_df(index=None) -> pd.DataFrame:
    """Все сочетания группа_ка / сегмент_ка в одном DataFrame."""
    return pd.DataFrame({
        'группа_ка': GROUPS,
        'сегмент_ка': [
            'Розница',      # не_указано
            'Розница',      # 3 лица
            'Розница',      # Прочие ГАП
            'Розница',      # ГСК внутри сегмента
            'Опт',          # ГСК между сегментами
        ],
        'сегмент': ['Розница'] * len(GROUPS),
    }, index=index)


EXPECTED = [
    'не_указано',
    '3 лица',
    'Прочие ГАП',
    'ГСК внутрисегмент.',
    'ГСК межсегмент.',
]


def test_rule_from_segment_column() -> None:
    """Режим шагов 14/15/16: эталон берётся из колонки 'сегмент'."""
    result = make_step()._calculate_connection_type(make_df())
    check(
        list(result) == EXPECTED,
        f"вид связи по колонке «сегмент» (факт: {list(result)})",
    )
    check(str(result.dtype) == 'string', f"dtype результата = string (факт: {result.dtype})")


def test_rule_from_scalar_segment() -> None:
    """Режим шага 17: эталон — скалярный сегмент компании."""
    result = make_step()._calculate_connection_type(make_df(), 'Розница')
    check(
        list(result) == EXPECTED,
        f"вид связи по скалярному сегменту (факт: {list(result)})",
    )


def test_scalar_overrides_column() -> None:
    """Явный сегмент важнее колонки 'сегмент' (у 91 сегмент единый)."""
    df = make_df()
    df.loc[3, 'сегмент'] = 'Опт'   # колонка «врёт», сегмент компании — «Розница»
    result = make_step()._calculate_connection_type(df, 'Розница')
    check(
        result.iloc[3] == 'ГСК внутрисегмент.',
        f"скалярный сегмент важнее колонки (факт: {result.iloc[3]})",
    )


def test_index_preserved() -> None:
    """
    Регрессия: Series без index=df.index съезжала на RangeIndex.
    np.select отдаёт numpy-массив, поэтому индекс нужно проставлять явно.
    """
    df = make_df(index=pd.Index([10, 20, 30, 40, 50], name='id'))
    result = make_step()._calculate_connection_type(df)
    check(
        list(result.index) == [10, 20, 30, 40, 50],
        f"индекс кадра сохранён (факт: {list(result.index)})",
    )
    check(result.index.name == 'id', f"имя индекса сохранено (факт: {result.index.name})")


def test_all_steps_share_one_implementation() -> None:
    """Копий метода в шагах больше нет — все используют базовый."""
    for step_class in (StepAddExpensesToOpuBase, Step14BuildOpuFoundationStep,
                       Step17AddOtherIncomeExpensesToOpuStep):
        name = step_class.__name__
        check(
            '_calculate_connection_type' not in vars(step_class),
            f"{name}: своей копии метода нет",
        )
        check(
            step_class._calculate_connection_type is Step._calculate_connection_type,
            f"{name}: использует общий хелпер базового шага",
        )


def test_unknown_group_falls_back() -> None:
    """Неизвестная группа_ка -> 'не_указано', а не молчаливая потеря строки."""
    df = make_df()
    df.loc[0, 'группа_ка'] = 'Новая группа'
    result = make_step()._calculate_connection_type(df)
    check(
        result.iloc[0] == 'не_указано',
        f"неизвестная группа_ка -> «не_указано» (факт: {result.iloc[0]})",
    )


def main() -> None:
    test_rule_from_segment_column()
    test_rule_from_scalar_segment()
    test_scalar_overrides_column()
    test_index_preserved()
    test_all_steps_share_one_implementation()
    test_unknown_group_falls_back()

    total = PASSED + FAILED
    label = 'вид связи (общий хелпер шагов 14/15/16/17)'
    if FAILED:
        print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
        for line in _failed_messages:
            print(f"  [FAIL] {line}")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


if __name__ == '__main__':
    main()
