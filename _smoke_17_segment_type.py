# -*- coding: utf-8 -*-
"""Смоук: двухступенчатое разрешение типа ОПУ (шаг 17), сессия 02.10.2026.

Жёсткое правило: на один _key ('счет[:5] + доход_расход') должен быть ровно
один 'вид_дохода_расхода'. Разрешение -- две ступени:
  1. по ключу ровно один тип -- присваивается;
  2. несколько типов: индивидуальные строки компании (scope != 'все')
     перекрывают универсальные и дают ровно один тип -- берется он.
Иначе -- ReferenceMismatchError со списком допустимых типов.

Ступени "тип ППА по объекту" и "тип из блока сегмента" удалены (сессия 02.10.2026):
подстрока "ППА" в имени объекта не является бизнес-признаком, а сегмент на
реальном Меппинг_опу не разрешал НИ ОДНОГО многотипного ключа (замер: 0/8).

Запуск: python _smoke_17_segment_type.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from utils import build_composite_key
from pipeline.errors import ReferenceMismatchError
from pipeline.steps.step_17_add_other_income_and_expenses import (
    Step17AddOtherIncomeExpensesToOpuStep,
)


def _step() -> Step17AddOtherIncomeExpensesToOpuStep:
    return Step17AddOtherIncomeExpensesToOpuStep()


def _reference(rows):
    ref = pd.DataFrame(
        rows, columns=['счет', 'доход_расход', 'вид_дохода_расхода']
    )
    ref['_key'] = build_composite_key(ref, 'счет', 'доход_расход', truncate_a=5)
    return ref


def _df(rows):
    df = pd.DataFrame(rows, columns=['счет', 'доход_расход', 'Корр.счет'])
    df['_key'] = build_composite_key(df, 'счет', 'доход_расход', truncate_a=5)
    return df


def test_single_candidate_is_unchanged():
    """Ступень 1: один тип на ключ -- присваивается всем строкам."""
    step = _step()
    ref = _reference([('91.02', 'Статья', 'Аренда')])
    df = _df([('91.02', 'Статья', '60.01')])
    step._resolve_income_expense_mapping(
        df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
    )
    assert list(df['вид_дохода_расхода']) == ['Аренда'], df['вид_дохода_расхода']


def test_multiple_types_raises_error():
    """Два типа scope=all --> неоднозначность --> ошибка."""
    step = _step()
    ref = _reference([
        ('91.02', 'Статья', 'Аренда'),
        (
            '91.02', 'Статья',
            'Расходы от выбытия прав пользования активами, изменений условий договоров аренды',
        ),
    ])
    df = _df([('91.02', 'Статья', '60.01')])
    try:
        step._resolve_income_expense_mapping(
            df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
        )
    except ReferenceMismatchError as exc:
        assert exc.problem_data is not None, 'diagnostic required'
        assert exc.reference_name == 'Меппинг_опу'
    else:
        raise AssertionError('Expected ReferenceMismatchError')


def test_ppa_marker_removed():
    """PPA_OBJECT_MARKER больше не используется."""
    from pipeline.step_config import StepConstants
    assert not hasattr(StepConstants, 'PPA_OBJECT_MARKER'), (
        'PPA_OBJECT_MARKER должна быть удалена'
    )


def test_build_type_candidates_basic():
    """Базовая проверка _build_type_candidates."""
    step = _step()
    ref = _reference([
        ('91.02', 'Статья', 'Тип-A'),
        ('91.02', 'Статья', 'Тип-B'),
        ('91.01', 'Выручка', 'Продажа'),
    ])
    candidates = step._build_type_candidates(ref)
    assert set(candidates.get('91.02_Статья', [])) == {'Тип-A', 'Тип-B'}
    assert candidates.get('91.01_Выручка') == ['Продажа']


def main():
    test_single_candidate_is_unchanged()
    test_multiple_types_raises_error()
    test_ppa_marker_removed()
    test_build_type_candidates_basic()
    print('SMOKE_OK 4 scenarios: разрешение типа ОПУ, ступени 1+2 (шаг 17)')


if __name__ == '__main__':
    main()
