# -*- coding: utf-8 -*-
"""
Смоук: контрагенты из справочника ППА по колонке 'рбп' (шаг 17).

Кейс: строки 91.02 вида «Доходы (расходы), связанные со сдачей имущества в
аренду» с объектом «Проценты ППА Договор аренды № …» — в проводке на стороне Кт
стоит 97.21, поэтому обычное извлечение контрагентов даёт 'не_указано'.
Источник контрагента для них — колонка 'рбп' листа ППА.

Сценарии:
  1. Объект с признаком ППА найден в 'рбп' → контрагент подтянут;
  2. уже проставленный контрагент не перезаписывается;
  3. строки без признака ППА не затрагиваются;
  4. нет в 'рбп' + STRICT_PPA_MAPPING_CHECK=True → MissingMappingError
     с problem_data (отчёт mismatches/);
  5. тот же случай при флаге False → шаг продолжается, отчёт сохраняется;
  6. амортизация ОС (Корр.счет 02.03, объект «ППА …») — не РБП-строка:
     строгий режим НЕ стопует, контрагент остаётся 'не_указано';
  7. объект известен справочнику как ОС ('ос_ппа'), Корр.счет 97.21 —
     в диагностику 'рбп' не попадает, строгий режим не стопует.

Запуск: python _smoke_17_ppa_rbp_contractors.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import pipeline.steps._step17_processing as ppa_module
from pipeline.errors import MissingMappingError
from pipeline.steps.step_17_add_other_income_and_expenses import (
    Step17AddOtherIncomeExpensesToOpuStep,
)

RBP_IN_DATA = 'Проценты ППА Договор аренды № 020823-49 от 23.08.2003'
RBP_OTHER = 'Проценты ППА Договор аренды №120621-47 от 21.06.2012'
CONTRACTOR = 'Администрация МО Гиагинский район'


def _step() -> Step17AddOtherIncomeExpensesToOpuStep:
    return Step17AddOtherIncomeExpensesToOpuStep()


def _ppa(rows: list[tuple[str, str]]) -> pd.DataFrame:
    """(рбп, контрагент) по компании ГиагКХП."""
    return pd.DataFrame({
        'наименование_компании': ['ГиагКХП'] * len(rows),
        'рбп': [row[0] for row in rows],
        'контрагент': [row[1] for row in rows],
    }).astype('string')


def _df_9102(contractors: list[str], objects: list[str]) -> pd.DataFrame:
    size = len(contractors)
    df = pd.DataFrame({
        'счет': ['91.02'] * size,
        'Корр.счет': ['97.21'] * size,
        'доход_расход': ['Доходы (расходы), связанные со сдачей имущества в аренду (субаренду)'] * size,
        'вид_дохода_расхода': ['Расходы по процентам аренда'] * size,
        'контрагент': contractors,
        'Субконто Кт_1': objects,
        'оборот, тыс.ед.': [12.275] * size,
        'оборот, тыс.руб.': [12.275] * size,
    })
    return df.astype({
        'счет': 'string', 'Корр.счет': 'string', 'доход_расход': 'string',
        'вид_дохода_расхода': 'string', 'контрагент': 'string',
        'Субконто Кт_1': 'string',
    })


def _empty_9101() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        'счет', 'контрагент', 'Субконто Дт_1',
        'оборот, тыс.ед.', 'оборот, тыс.руб.',
    ]).astype('string')


def test_contractor_filled_from_ppa() -> None:
    step = _step()
    ppa = _ppa([(RBP_IN_DATA, CONTRACTOR), (RBP_OTHER, 'Арендодатель-2')])
    df_9102 = _df_9102(['не_указано'], [RBP_IN_DATA])

    step._pull_contractors_from_ppa_by_rbp(_empty_9101(), df_9102, ppa, 'ГиагКХП')
    assert list(df_9102['контрагент']) == [CONTRACTOR], df_9102['контрагент'].tolist()


def test_existing_contractor_kept() -> None:
    step = _step()
    ppa = _ppa([(RBP_IN_DATA, CONTRACTOR)])
    df_9102 = _df_9102(['ООО Тест'], [RBP_IN_DATA])

    step._pull_contractors_from_ppa_by_rbp(_empty_9101(), df_9102, ppa, 'ГиагКХП')
    assert list(df_9102['контрагент']) == ['ООО Тест'], 'свой контрагент важнее'


def test_rows_without_marker_untouched() -> None:
    step = _step()
    ppa = _ppa([(RBP_IN_DATA, CONTRACTOR)])
    df_9102 = _df_9102(['не_указано'], ['Автомобиль легковой Aydi Q7'])

    step._pull_contractors_from_ppa_by_rbp(_empty_9101(), df_9102, ppa, 'ГиагКХП')
    assert list(df_9102['контрагент']) == ['не_указано'], 'без признака ППА не трогаем'


def test_missing_rbp_strict_and_soft() -> None:
    """Нет объекта в 'рбп': строгий режим стопует, мягкий продолжает с отчётом."""
    step = _step()
    original = ppa_module.STRICT_PPA_MAPPING_CHECK
    try:
        ppa_module.STRICT_PPA_MAPPING_CHECK = True
        ppa = _ppa([(RBP_OTHER, 'Арендодатель-2')])
        df_9102 = _df_9102(['не_указано'], [RBP_IN_DATA])

        try:
            step._pull_contractors_from_ppa_by_rbp(_empty_9101(), df_9102, ppa, 'ГиагКХП')
        except MissingMappingError as exc:
            assert exc.problem_data is not None, 'problem_data обязателен для mismatches/'
            assert 'отсутствующее_значение' in set(exc.problem_data.columns)
            assert list(exc.problem_data['отсутствующее_значение']) == [RBP_IN_DATA]
            assert exc.reference_name == 'ППА'
        else:
            raise AssertionError('ожидался MissingMappingError в строгом режиме')

        ppa_module.STRICT_PPA_MAPPING_CHECK = False
        df_9102 = _df_9102(['не_указано'], [RBP_IN_DATA])
        _, df_result = step._pull_contractors_from_ppa_by_rbp(
            _empty_9101(), df_9102, ppa, 'ГиагКХП',
        )
        assert list(df_result['контрагент']) == ['не_указано'], 'мягкий режим не стопует'
    finally:
        ppa_module.STRICT_PPA_MAPPING_CHECK = original


def test_amortization_not_rbp() -> None:
    """Амортизация ОС (Корр.счет 02.03, объект «ППА …») — не РБП-строка.

    Регресс «ТимПФ»: проводка 91.02 / 02.03 с субконто Кт_1
    «ППА ТЕК.240617.ТИМ.№20/24-ИПС.аренда» ловилась маркером «ППА» и
    требовала контрагента из 'рбп' → ложный MissingMappingError.
    """
    step = _step()
    original = ppa_module.STRICT_PPA_MAPPING_CHECK
    try:
        ppa_module.STRICT_PPA_MAPPING_CHECK = True
        ppa = _ppa([(RBP_OTHER, 'Арендодатель-2')])
        amort_object = 'ППА ТЕК.240617.ТИМ.№20/24-ИПС.аренда'
        df_9102 = _df_9102(['не_указано'], [amort_object])
        df_9102['Корр.счет'] = ['02.03']

        step._pull_contractors_from_ppa_by_rbp(_empty_9101(), df_9102, ppa, 'ТимПФ')
        assert list(df_9102['контрагент']) == ['не_указано'], (
            'амортизация не получает контрагента из рбп'
        )
    finally:
        ppa_module.STRICT_PPA_MAPPING_CHECK = original


def test_os_object_not_rbp() -> None:
    """Объект из 'ос_ппа' на счёте 97.21 не считается недостающим РБП."""
    step = _step()
    original = ppa_module.STRICT_PPA_MAPPING_CHECK
    try:
        ppa_module.STRICT_PPA_MAPPING_CHECK = True
        os_object = 'ППА ТЕК.240618.ТИМ.№33/24-ИПС.субаренда'
        ppa = pd.DataFrame({
            'наименование_компании': ['ГиагКХП'],
            'рбп': [RBP_OTHER],
            'ос_ппа': [os_object],
            'контрагент': ['Арендодатель-2'],
        }).astype('string')
        df_9102 = _df_9102(['не_указано'], [os_object])

        step._pull_contractors_from_ppa_by_rbp(_empty_9101(), df_9102, ppa, 'ГиагКХП')
        assert list(df_9102['контрагент']) == ['не_указано'], (
            'объект ОС не является РБП — контрагент не подтягивается'
        )
    finally:
        ppa_module.STRICT_PPA_MAPPING_CHECK = original


def main() -> None:
    test_contractor_filled_from_ppa()
    test_existing_contractor_kept()
    test_rows_without_marker_untouched()
    test_missing_rbp_strict_and_soft()
    test_amortization_not_rbp()
    test_os_object_not_rbp()
    print('SMOKE_OK 6 scenarios: контрагенты ППА по рбп (шаг 17)')


if __name__ == '__main__':
    main()
