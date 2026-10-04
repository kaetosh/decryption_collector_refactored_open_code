# -*- coding: utf-8 -*-
"""Смоук: разрешение типа ОПУ в шаге 17 (две ступени), сессия 02.10.2026.

Жёсткое правило: на один _key ('счет[:5] + доход_расход') должен быть ровно
один 'вид_дохода_расхода'. Разрешение -- две ступени:
  1. по ключу ровно один тип -- присваивается;
  2. несколько типов: индивидуальные строки компании (scope != 'все')
     перекрывают универсальные и дают ровно один тип -- берется он.
Иначе -- ReferenceMismatchError со списком допустимых типов.

Ступени "тип ППА по объекту" и "тип из блока сегмента" удалены (сессия
02.10.2026): подстрока "ППА" в имени объекта не является бизнес-признаком, а
сегмент на реальном Меппинг_опу не разрешал НИ ОДНОГО многотипного ключа
(замер: 0/8).

Второй сценарий --composition: ступень 2 работает на кадре, который уже
прошёл utils.reference_scope.resolve_company_view (его вызывает
executors.initialize_context до старта конвейера). Это контракт, на котором
стоит вся ступень 2, поэтому он проверяется явно.

Запуск: python -u _smoke_17_type_resolution.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import REFERENCE_SCOPE_COL
from utils import build_composite_key, resolve_company_view
from pipeline.errors import ReferenceMismatchError
from pipeline.step_config import OpuReportConstants
from pipeline.steps.step_17_add_other_income_and_expenses import (
    Step17AddOtherIncomeExpensesToOpuStep,
)

PASSED = 0
FAILED = 0

ACCOUNT = '91.02'
ARTICLE = 'Статья'
COMPANY = 'ГиагКХП'
RENT_TYPE = 'Аренда'
PPA_INTEREST_TYPE = 'Расходы по процентам аренда'


def check(condition: bool, message: str) -> None:
    """Одна проверка с прогрессом (зелёная — счётчик, красная — счётчик)."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[OK] {message}")
    else:
        FAILED += 1
        print(f"[FAIL] {message}")


def _step() -> Step17AddOtherIncomeExpensesToOpuStep:
    return Step17AddOtherIncomeExpensesToOpuStep()


def _reference(rows):
    """Справочник Меппинг_опу: (счёт, доход_расход, вид_дохода_расхода, область)."""
    ref = pd.DataFrame(
        rows,
        columns=['счет', 'доход_расход', 'вид_дохода_расхода', REFERENCE_SCOPE_COL],
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
    ref = _reference([(ACCOUNT, ARTICLE, RENT_TYPE, 'все')])
    df = _df([(ACCOUNT, ARTICLE, '60.01')])
    step._resolve_income_expense_mapping(
        df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
    )
    check(
        list(df['вид_дохода_расхода']) == [RENT_TYPE],
        f"ступень 1: единственный тип присвоен ({list(df['вид_дохода_расхода'])})",
    )


def test_individual_rows_resolve_ambiguous_key():
    """Ступень 2: индивидуальная строка компании перекрывает универсальную."""
    step = _step()
    ref = _reference([
        (ACCOUNT, ARTICLE, RENT_TYPE, 'все'),
        (ACCOUNT, ARTICLE, PPA_INTEREST_TYPE, COMPANY),
    ])
    df = _df([(ACCOUNT, ARTICLE, '60.01')])
    step._resolve_income_expense_mapping(
        df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
    )
    check(
        list(df['вид_дохода_расхода']) == [PPA_INTEREST_TYPE],
        'ступень 2: тип взят из индивидуальной строки компании, а не из '
        f"универсальной ({list(df['вид_дохода_расхода'])})",
    )


def test_composition_with_company_view():
    """Ступень 2 на кадре после resolve_company_view (как в реальном прогоне)."""
    step = _step()
    raw = _reference([
        (ACCOUNT, ARTICLE, RENT_TYPE, 'все'),
        (ACCOUNT, ARTICLE, PPA_INTEREST_TYPE, COMPANY),
    ])
    # Ключ строки справочника требует сегмент и вид связи — добавляем заглушки
    raw['сегмент'] = 'Птицеводство'
    raw['вид_связи'] = '3 лица'
    view, diag = resolve_company_view(
        raw, COMPANY, OpuReportConstants.REFERENCE_ROW_KEY,
        reference_name='Меппинг_опу',
    )
    check(diag.applied, 'composition: индивидуальные строки распознаны')

    df = _df([(ACCOUNT, ARTICLE, '60.01')])
    step._resolve_income_expense_mapping(
        df, view, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
    )
    check(
        list(df['вид_дохода_расхода']) == [PPA_INTEREST_TYPE],
        'composition: на кадре компании неоднозначности не осталось',
    )


def test_multiple_types_raises_error():
    """Два типа scope='все' --> неоднозначность --> ошибка с причиной."""
    step = _step()
    ref = _reference([
        (ACCOUNT, ARTICLE, RENT_TYPE, 'все'),
        (ACCOUNT, ARTICLE, PPA_INTEREST_TYPE, 'все'),
    ])
    df = _df([(ACCOUNT, ARTICLE, '60.01')])
    try:
        step._resolve_income_expense_mapping(
            df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
        )
    except ReferenceMismatchError as exc:
        check(exc.problem_data is not None, 'ошибка несёт problem_data')
        check(exc.reference_name == 'Меппинг_опу', 'ошибка называет справочник')
        reason = exc.problem_data['причина'].iloc[0]
        check(
            reason == 'нет индивидуальных строк компании',
            f'причина в диагностике: {reason!r}',
        )
    else:
        check(False, 'ожидался ReferenceMismatchError, строки не разрешились')


def test_two_individual_types_still_raise():
    """Индивидуальных строк больше одной и типов в них больше одного."""
    step = _step()
    ref = _reference([
        (ACCOUNT, ARTICLE, RENT_TYPE, COMPANY),
        (ACCOUNT, ARTICLE, PPA_INTEREST_TYPE, COMPANY),
    ])
    df = _df([(ACCOUNT, ARTICLE, '60.01')])
    try:
        step._resolve_income_expense_mapping(
            df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
        )
    except ReferenceMismatchError as exc:
        reason = exc.problem_data['причина'].iloc[0]
        check(
            reason == 'в индивидуальных строках больше одного типа',
            f'причина в диагностике: {reason!r}',
        )
    else:
        check(False, 'ожидался ReferenceMismatchError при двух типах компании')


def test_ppa_marker_removed():
    """PPA_OBJECT_MARKER больше не используется."""
    from pipeline.step_config import StepConstants
    check(
        not hasattr(StepConstants, 'PPA_OBJECT_MARKER'),
        'PPA_OBJECT_MARKER удалена из StepConstants',
    )


def test_candidates_builders():
    """_build_type_candidates и _build_individual_type_candidates."""
    step = _step()
    ref = _reference([
        (ACCOUNT, ARTICLE, 'Тип-A', 'все'),
        (ACCOUNT, ARTICLE, 'Тип-B', COMPANY),
        ('91.01', 'Выручка', 'Продажа', 'все'),
    ])
    candidates = step._build_type_candidates(ref)
    check(
        set(candidates.get('91.02_Статья', [])) == {'Тип-A', 'Тип-B'},
        f"_build_type_candidates видит оба типа ({candidates.get('91.02_Статья')})",
    )
    check(
        candidates.get('91.01_Выручка') == ['Продажа'],
        '_build_type_candidates: на ключе с одним типом — список из одного',
    )

    individual = step._build_individual_type_candidates(ref)
    check(
        individual.get('91.02_Статья') == ['Тип-B'],
        f"индивидуальные кандидаты только свои ({individual.get('91.02_Статья')})",
    )
    check(
        '91.01_Выручка' not in individual,
        'универсальные строки в индивидуальные кандидаты не попадают',
    )

    no_scope = step._build_individual_type_candidates(
        ref.drop(columns=[REFERENCE_SCOPE_COL])
    )
    check(no_scope == {}, 'без колонки «компания» индивидуальных кандидатов нет')


def test_no_column_is_backward_compatible():
    """Старый Справочники.xlsx без колонки «компания» — ступени не мешает."""
    step = _step()
    ref = _reference([(ACCOUNT, ARTICLE, RENT_TYPE, 'все')]).drop(
        columns=[REFERENCE_SCOPE_COL]
    )
    df = _df([(ACCOUNT, ARTICLE, '60.01')])
    step._resolve_income_expense_mapping(
        df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
    )
    check(
        list(df['вид_дохода_расхода']) == [RENT_TYPE],
        'без колонки «компания» тип разрешается как раньше',
    )


def main():
    test_single_candidate_is_unchanged()
    test_individual_rows_resolve_ambiguous_key()
    test_composition_with_company_view()
    test_multiple_types_raises_error()
    test_two_individual_types_still_raise()
    test_no_column_is_backward_compatible()
    test_ppa_marker_removed()
    test_candidates_builders()

    total = PASSED + FAILED
    if FAILED:
        print(f"SMOKE_FAIL ({FAILED}/{total}) — разрешение типа ОПУ, шаг 17")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — разрешение типа ОПУ (две ступени), шаг 17")
    sys.exit(0)


if __name__ == '__main__':
    main()