# -*- coding: utf-8 -*-
"""
Смоук: индивидуальный меппинг компаний (колонка «компания»).

Сценарии:
  1. Загрузка Меппинг_опу / Меппинг_бб конфигом с usecols по ИМЕНАМ:
     обязательные колонки на месте (счет_фо, детализация_субконто, компания) —
     с позиционными индексами они терялись после вставки колонки в лист.
  2. Нет колонки «компания» → кадр без изменений (column_missing).
  3. Индивидуальная строка перекрывает универсальную с тем же ключом листа.
  4. Строки других компаний отбрасываются; компания без правок получает
     исходный кадр (тот же объект).
  5. Неизвестное значение колонки (опечатка в имени компании) видно в диагностике.
  6. Реальный файл: для «ГиагКХП» статья аренды 91.02 в сегменте Птицеводство
     отдаёт «Расходы по процентам аренда», для компании без правок — «Аренда».

Запуск: python _smoke_reference_scope.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import REFERENCE_CONFIGS, REFERENCE_DATA_FILE
from io_module import DataLoader
from pipeline.step_config import BalanceReportConstants, OpuReportConstants
from utils import resolve_company_view


def _load(sheet: str) -> pd.DataFrame:
    """Читает лист справочников так же, как _load_all_references()."""
    return DataLoader.load_reference_data(sheet_name=sheet, **REFERENCE_CONFIGS[sheet])


def test_load_real_references() -> None:
    """Колонки по именам: счет_фо и детализация_субконто больше не теряются."""
    opu = _load('Меппинг_опу')
    for col in ('счет', 'доход_расход', 'вид_дохода_расхода', 'компания',
                'сегмент', 'вид_связи', 'счет_фо'):
        assert col in opu.columns, (col, opu.columns.tolist())
    assert opu['счет_фо'].notna().any(), 'счет_фо должен читаться'

    bb = _load('Меппинг_бб')
    for col in ('счет', 'субконто', 'компания', 'вид_задолженности',
                'детализация_субконто', 'счет_фо', 'отчетность'):
        assert col in bb.columns, (col, bb.columns.tolist())


def _make_ref() -> pd.DataFrame:
    return pd.DataFrame({
        'счет': ['91.02', '91.02'],
        'доход_расход': ['Статья', 'Статья'],
        'вид_дохода_расхода': ['Аренда', 'Расходы по процентам аренда'],
        'компания': ['все', 'ГиагКХП'],
        'сегмент': ['Птицеводство', 'Птицеводство'],
        'вид_связи': ['3 лица', '3 лица'],
        'счет_фо': ['600020103', '1150040103'],
    }).astype('string')


def test_override_and_company_isolation() -> None:
    """Индивидуальная строка перекрывает универсальную только для своей компании."""
    ref = _make_ref()
    key_cols = OpuReportConstants.REFERENCE_ROW_KEY

    view, diag = resolve_company_view(
        ref, 'ГиагКХП', key_cols, reference_name='Меппинг_опу',
        known_companies=['ГиагКХП', 'Другая'],
    )
    assert diag.applied and diag.individual_rows == 1, diag
    assert diag.overridden_rows == 1, diag
    assert list(view['вид_дохода_расхода']) == ['Расходы по процентам аренда']
    assert list(view['счет_фо']) == ['1150040103']
    assert len(view) == 1, view

    other_view, other_diag = resolve_company_view(
        ref, 'Другая', key_cols, reference_name='Меппинг_опу',
        known_companies=['ГиагКХП', 'Другая'],
    )
    assert not other_diag.applied, other_diag
    assert other_view is ref, 'компания без правок получает исходный кадр'
    assert list(other_view['вид_дохода_расхода']) == ['Аренда', 'Расходы по процентам аренда']


def test_missing_column_is_backward_compatible() -> None:
    """Старый Справочники.xlsx без колонки «компания» работает как раньше."""
    ref = _make_ref().drop(columns=['компания'])
    view, diag = resolve_company_view(
        ref, 'ГиагКХП', OpuReportConstants.REFERENCE_ROW_KEY, reference_name='Меппинг_опу',
    )
    assert diag.column_missing, diag
    assert view is ref, 'кадр не должен изменяться'


def test_unknown_scope_value_reported() -> None:
    """Опечатка в имени компании видна в диагностике, а строки не попадают в работу."""
    ref = _make_ref().copy()
    ref.loc[len(ref)] = {
        'счет': '91.02', 'доход_расход': 'Статья', 'вид_дохода_расхода': 'Тип-X',
        'компания': 'ГиагКХП2', 'сегмент': 'Птицеводство',
        'вид_связи': '3 лица', 'счет_фо': '1150040199',
    }
    view, diag = resolve_company_view(
        ref, 'ГиагКХП', OpuReportConstants.REFERENCE_ROW_KEY,
        reference_name='Меппинг_опу', known_companies=['ГиагКХП', 'Другая'],
    )
    assert diag.unknown_scope_values == ['ГиагКХП2'], diag.unknown_scope_values
    assert 'Тип-X' not in set(view['вид_дохода_расхода']), 'чужие строки отброшены'


def test_real_file_company_view() -> None:
    """Реальный кейс ГиагКХП: статья аренды перекрыта на «Расходы по процентам аренда»."""
    if not REFERENCE_DATA_FILE.exists():
        print('  [skip] Справочники.xlsx не найден — сценарий пропущен')
        return

    opu = _load('Меппинг_опу')
    view, diag = resolve_company_view(
        opu, 'ГиагКХП', OpuReportConstants.REFERENCE_ROW_KEY,
        reference_name='Меппинг_опу', known_companies=['ГиагКХП'],
    )
    assert diag.applied and diag.individual_rows == 32, (diag.individual_rows, diag.overridden_rows)
    assert diag.overridden_rows >= 4, diag.overridden_rows

    rent = view[
        view['счет'].astype(str).str.startswith('91.02')
        & view['доход_расход'].astype(str).str.contains('сдачей', na=False)
        & (view['сегмент'].astype(str) == 'Птицеводство')
    ]
    types = set(rent['вид_дохода_расхода'].dropna())
    assert types == {'Расходы по процентам аренда'}, types

    universal_view, _ = resolve_company_view(
        opu, 'Другая', OpuReportConstants.REFERENCE_ROW_KEY, reference_name='Меппинг_опу',
    )
    universal_rent = universal_view[
        universal_view['счет'].astype(str).str.startswith('91.02')
        & universal_view['доход_расход'].astype(str).str.contains('сдачей', na=False)
        & (universal_view['сегмент'].astype(str) == 'Птицеводство')
    ]
    assert 'Аренда' in set(universal_rent['вид_дохода_расхода']), 'универсальный блок цел'

    bb = _load('Меппинг_бб')
    bb_view, bb_diag = resolve_company_view(
        bb, 'ГиагКХП', BalanceReportConstants.MAPPING_KEYS, reference_name='Меппинг_бб',
    )
    assert not bb_diag.applied and bb_view is bb, 'в Меппинг_бб правок пока нет'


def test_wiring_in_executors() -> None:
    """
    Обвязка в executors: _load_all_references + _apply_company_reference_scope.

    Проверяет на реальном файле, что взгляд компании применяется к обоим
    меппингам, сводка попадает в context.data (для титульного листа)
    и создаётся Excel-диагностика в mismatches/.
    """
    import shutil

    from io_module.output_manager import configure_run, get_run_dir
    from pipeline.base import ProcessingContext
    from pipeline.executors import _apply_company_reference_scope, _load_all_references

    configure_run('smoke_scope')
    try:
        context = ProcessingContext(company='ГиагКХП')
        context.references = _load_all_references()
        _apply_company_reference_scope(context)

        summary = context.data.get('reference_scope_summary') or {}
        entries = summary.get('entries') or []
        opu_entry = next(
            (entry for entry in entries if entry['лист'] == 'Меппинг_опу'), None
        )
        assert opu_entry is not None, entries
        assert opu_entry['индивидуальных_строк'] == 32, opu_entry

        opu = context.references['меппинг_опу']
        assert 'счет_фо' in opu.columns and 'компания' in opu.columns
        bb = context.references['меппинг_баланс']
        for col in ('счет_фо', 'детализация_субконто', 'компания'):
            assert col in bb.columns, col

        reports = list((get_run_dir() / 'mismatches').glob('reference_scope_*.xlsx'))
        assert reports, 'Excel-диагностика индивидуального меппинга должна быть создана'
    finally:
        shutil.rmtree(get_run_dir(), ignore_errors=True)


def main() -> None:
    test_load_real_references()
    test_override_and_company_isolation()
    test_missing_column_is_backward_compatible()
    test_unknown_scope_value_reported()
    test_real_file_company_view()
    test_wiring_in_executors()
    print('SMOKE_OK 6 scenarios: reference scope (индивидуальный меппинг компаний)')


if __name__ == '__main__':
    main()
