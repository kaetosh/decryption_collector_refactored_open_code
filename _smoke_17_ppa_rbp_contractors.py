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
  8. 'не_указано' в ППА — не значение: строки справочника, где контрагент
     не заполнен (заглушка/пусто), исключаются из меппинга
     (_build_ppa_mapping; регресс 30.09.2026 — та же логика, что для группы
     ОС аренды/лизинга в шаге 6);
  9. РБП есть в ППА, но контрагент не заполнен → строгий режим стопует
     с этим РБП в problem_data, мягкий — продолжает с 'не_указано';
 10. объект ОС ('ос_ппа') есть в ППА, но контрагент не заполнен → строгий
     режим стопует (объект в problem_data), мягкий — замена на '3 лица'.

Запуск: python _smoke_17_ppa_rbp_contractors.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import pipeline.steps._step17_processing as ppa_module
from pipeline.errors import MissingMappingError
from pipeline.step_config import StepConstants
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
    step = _step_no_report()
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


def _step_no_report() -> Step17AddOtherIncomeExpensesToOpuStep:
    """Шаг с заглушкой сохранения отчёта — смоук не пишет в _OUTPUT_DATA."""
    step = _step()
    step._save_reference_mismatch_report = lambda error: None
    return step


def _empty_9101_full() -> pd.DataFrame:
    """Пустой df_9101 со всеми колонками, которые читает _process_ppa."""
    return pd.DataFrame({
        'счет': pd.Series(dtype='string'),
        'вид_дохода_расхода': pd.Series(dtype='string'),
        'Корр.счет': pd.Series(dtype='string'),
        'контрагент': pd.Series(dtype='string'),
        'Субконто Дт_1': pd.Series(dtype='string'),
        'оборот, тыс.ед.': pd.Series(dtype='float64'),
        'оборот, тыс.руб.': pd.Series(dtype='float64'),
    })


def _df_9102_ppa_object(contractor: str, ppa_object: str) -> pd.DataFrame:
    """Строка 91.02 с объектом ОС ППА: Корр.счет 01.09, тип расхода ППА."""
    df = pd.DataFrame({
        'счет': ['91.02'],
        'Корр.счет': ['01.09'],
        'доход_расход': ['Расходы от выбытия прав пользования активами'],
        'вид_дохода_расхода': [StepConstants.PPA_EXPENSE_TYPE],
        'контрагент': [contractor],
        'Субконто Кт_1': [ppa_object],
        'оборот, тыс.ед.': [10.0],
        'оборот, тыс.руб.': [10.0],
        'сегмент': ['Птицеводство'],
    })
    return df.astype({
        'счет': 'string', 'Корр.счет': 'string', 'доход_расход': 'string',
        'вид_дохода_расхода': 'string', 'контрагент': 'string',
        'Субконто Кт_1': 'string', 'сегмент': 'string',
    })


def test_build_mapping_drops_service_values() -> None:
    """'не_указано' в ППА — ни ключ, ни значение (регресс 30.09.2026).

    Регресс: строка ППА с заполненным ключом, но контрагентом-заглушкой,
    считалась рабочей — объект получал контрагента 'не_указано' молча, без
    диагностики (map возвращает строку, а не NaN → isna() = False).
    """
    step = _step()
    ppa = pd.DataFrame({
        'наименование_компании': ['ГиагКХП'] * 4,
        'рбп': ['РБП А', 'РБП Б', 'не_указано', 'РБП В'],
        'ос_ппа': ['ОС 1', 'ОС 2', 'ОС 3', 'ОС 4'],
        'контрагент': ['КА 1', 'не_указано', 'КА 3', ''],
    }).astype('string')

    mapping_rbp = step._build_ppa_mapping(ppa, 'рбп')
    assert mapping_rbp.to_dict() == {'РБП А': 'КА 1'}, mapping_rbp.to_dict()

    mapping_os = step._build_ppa_mapping(ppa, 'ос_ппа')
    assert mapping_os.to_dict() == {'ОС 1': 'КА 1', 'ОС 3': 'КА 3'}, mapping_os.to_dict()

    duplicate = pd.DataFrame({
        'рбп': ['РБП Д', 'РБП Д'],
        'контрагент': ['не_указано', 'КА Д'],
    }).astype('string')
    assert step._build_ppa_mapping(duplicate, 'рбп').to_dict() == {'РБП Д': 'КА Д'}, (
        'строка с реальным контрагентом выигрывает у строки-заглушки'
    )

    no_contractor_column = pd.DataFrame({'рбп': ['РБП Е']}).astype('string')
    assert step._build_ppa_mapping(no_contractor_column, 'рбп').empty, (
        'нет колонки контрагента — пустой маппинг и диагностика, а не KeyError'
    )


def test_rbp_with_stub_contractor() -> None:
    """РБП есть в ППА, но контрагент по нему не заполнен ('не_указано')."""
    step = _step_no_report()
    original = ppa_module.STRICT_PPA_MAPPING_CHECK
    try:
        ppa = _ppa([(RBP_IN_DATA, 'не_указано')])

        ppa_module.STRICT_PPA_MAPPING_CHECK = True
        df_9102 = _df_9102(['не_указано'], [RBP_IN_DATA])
        try:
            step._pull_contractors_from_ppa_by_rbp(_empty_9101(), df_9102, ppa, 'ГиагКХП')
        except MissingMappingError as exc:
            assert list(exc.problem_data['отсутствующее_значение']) == [RBP_IN_DATA], (
                exc.problem_data
            )
        else:
            raise AssertionError(
                'ожидался MissingMappingError: у РБП не заполнен контрагент'
            )

        ppa_module.STRICT_PPA_MAPPING_CHECK = False
        df_9102 = _df_9102(['не_указано'], [RBP_IN_DATA])
        _, df_result = step._pull_contractors_from_ppa_by_rbp(
            _empty_9101(), df_9102, ppa, 'ГиагКХП',
        )
        assert list(df_result['контрагент']) == ['не_указано'], 'мягкий режим не стопует'
    finally:
        ppa_module.STRICT_PPA_MAPPING_CHECK = original


def test_os_object_with_stub_contractor() -> None:
    """Объект ОС есть в ППА, но контрагент по нему не заполнен.

    Регресс 30.09.2026: такое значение считалось «смапленным», объект не
    попадал в диагностику, а в мягком режиме контрагент-заглушка даже не
    заменялся на '3 лица' (проверялся только isna()).
    """
    step = _step_no_report()
    original = ppa_module.STRICT_PPA_MAPPING_CHECK
    os_object = 'ППА Договор лизинга № 2021/17114 от 30.12.2021'
    ppa = pd.DataFrame({
        'наименование_компании': ['ГиагКХП'],
        'ос_ппа': [os_object],
        'ос_после_перехода_в_собственность': [None],
        'рбп': ['не_указано'],
        'контрагент': ['не_указано'],
    }).astype('string')
    try:
        ppa_module.STRICT_PPA_MAPPING_CHECK = True
        df_9102 = _df_9102_ppa_object('не_указано', os_object)
        try:
            step._process_ppa(_empty_9101_full(), df_9102, ppa, 'ГиагКХП')
        except MissingMappingError as exc:
            assert list(exc.problem_data['отсутствующее_значение']) == [os_object], (
                exc.problem_data
            )
        else:
            raise AssertionError(
                'ожидался MissingMappingError: у объекта ОС не заполнен контрагент'
            )

        ppa_module.STRICT_PPA_MAPPING_CHECK = False
        df_9102 = _df_9102_ppa_object('не_указано', os_object)
        _, df_result = step._process_ppa(_empty_9101_full(), df_9102, ppa, 'ГиагКХП')
        assert list(df_result['контрагент']) == [StepConstants.THIRD_PARTY], (
            'мягкий режим заменяет контрагента-заглушку на 3 лица'
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
    test_build_mapping_drops_service_values()
    test_rbp_with_stub_contractor()
    test_os_object_with_stub_contractor()
    print('SMOKE_OK 9 scenarios: контрагенты ППА по рбп (шаг 17)')


if __name__ == '__main__':
    main()
