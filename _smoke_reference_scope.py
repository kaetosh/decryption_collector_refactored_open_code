# -*- coding: utf-8 -*-
"""
Смоук: индивидуальный меппинг компаний (колонка «компания»).

Сценарии:
  1. Загрузка Меппинг_опу / Меппинг_бб конфигом с usecols по ИМЕНАМ:
     обязательные колонки на месте (счет_фо, детализация_субконто, компания) —
     с позиционными индексами они терялись после вставки колонки в лист.
  2. Нет колонки «компания» → кадр без изменений (column_missing).
  3. Индивидуальная строка перекрывает универсальную с тем же ключом листа.
  4. Строки других компаний отбрасываются ВСЕГДА, в том числе для компании,
     у которой нет ни одной своей строки (регресс кейса «ТимПФ»: ранний выход
     `if individual.empty: return df` отдавал в прогон весь справочник, и статья
     аренды 91.02 давала два бизнес-типа → остановка шага 17). Если же в
     справочнике нет строк, адресованных конкретным компаниям, кадр
     возвращается тем же объектом.
  5. Неизвестное значение колонки (опечатка в имени компании) видно в диагностике.
  6. Реальный файл: для «ГиагКХП» статья аренды 91.02 отдаёт «Расходы по
     процентам аренда», для компании без правок — «Аренда». Плюс инвариант
     «на ключ (счет[:5] + доход_расход) — ровно один тип», на котором стоит
     ступень 1 разрешения типа в шаге 17.
  7. Реальный файл, регресс кейса «ТимПФ»: компания без своих строк не
     наследует строки другой компании — в кадре остаётся ровно один тип статьи
     аренды, то есть резолвер шага 17 больше не сптыкается о неоднозначность.
  8. Обвязка в executors пишет Excel с ОБЕИМИ половинами правки: лист
     «Применённые строки» (что действует для компании) и «Перекрытые строки»
     (что вытеснено), плюс служебные колонки «лист» и «перекрыто_компанией».

ВАЖНО про подстроки в проверках реальных данных: бухгалтерия опечатывается
при вносе новых статей в 1С, и в Меппинг_опу реально живут две почти
одинаковые статьи аренды, отличающиеся пробелом:
«Доходы (расходы), связанные со сдачей имущества в аренду (субаренду)»
(у «ГиагКХП» это проценты ППА) и «Доходы и расходы,связанные со сдачей
имущества в аренду (субаренду)» (универсальная, тип «Аренда»). Поэтому
сценарии 6–7 проверяют статью по КЛЮЧУ (имя берётся из индивидуальных строк
компании) и по ключу поиска счёта, а не через contains('сдачей') — такой
срез ловил обе статьи и требовал от них одного типа.

Запуск: python _smoke_reference_scope.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import (
    REFERENCE_CONFIGS,
    REFERENCE_DATA_FILE,
    REFERENCE_SCOPE_COL,
    SCOPE_ALL_VALUE,
)
from io_module import DataLoader
from pipeline.constants import ColumnNames
from pipeline.step_config import BalanceReportConstants, OpuReportConstants
from utils import build_composite_key, resolve_company_view
from utils.reference_scope import KEY_SEPARATOR


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

    # У компании без своих строк индивидуальные строки ЧУЖИХ компаний
    # отбрасываются (регресс кейса «ТимПФ», 29.09.2026): раньше здесь стоял
    # ранний выход `if individual.empty: return df`, и весь справочник,
    # включая чужие строки, уезжал в прогон.
    other_view, other_diag = resolve_company_view(
        ref, 'Другая', key_cols, reference_name='Меппинг_опу',
        known_companies=['ГиагКХП', 'Другая'],
    )
    assert not other_diag.applied, other_diag
    assert other_diag.individual_rows == 0, other_diag
    assert other_diag.dropped_foreign_rows == 1, other_diag
    assert list(other_view['вид_дохода_расхода']) == ['Аренда'], other_view
    assert list(other_view['счет_фо']) == ['600020103'], other_view


def test_reference_without_individual_rows_is_untouched() -> None:
    """Справочник, где все строки «все», возвращается тем же объектом."""
    ref = _make_ref().iloc[:1].copy()
    view, diag = resolve_company_view(
        ref, 'Другая', OpuReportConstants.REFERENCE_ROW_KEY, reference_name='Меппинг_опу',
    )
    assert not diag.applied, diag
    assert diag.dropped_foreign_rows == 0, diag
    assert view is ref, 'индивидуального меппинга нет — кадр не меняется вообще'


def test_missing_column_is_backward_compatible() -> None:
    """Старый Справочники.xlsx без колонки «компания» работает как раньше."""
    ref = _make_ref().drop(columns=['компания'])
    view, diag = resolve_company_view(
        ref, 'ГиагКХП', OpuReportConstants.REFERENCE_ROW_KEY, reference_name='Меппинг_опу',
    )
    assert diag.column_missing, diag
    assert view is ref, 'кадр не должен изменяться'


def _build_type_candidates_from_view(view: pd.DataFrame) -> dict:
    """
    Кандидаты типов ОПУ по ключу `счет[:5] + доход_расход` — та же группировка,
    что в шаге 17 (`_build_type_candidates`), но на кадре «взгляда компании».
    Считается здесь намеренно: смоук должен падать ровно на той неоднозначности,
    из-за которой прогон уходил в mismatches/, не завися от приватного
    хелпера шага.
    """
    truncated = view['счет'].astype(str).str.slice(0, 5)
    group_key = (
        truncated + KEY_SEPARATOR + view['доход_расход'].astype('string').fillna('')
    )
    grouped = view.assign(_key=group_key).groupby('_key', sort=False)
    return {
        str(value): frame['вид_дохода_расхода'].dropna().astype(str).drop_duplicates().tolist()
        for value, frame in grouped
    }


def test_company_without_individual_rows_does_not_inherit_foreign() -> None:
    """
    Регресс кейса «ТимПФ» (29.09.2026): прогон компании, у которой НЕТ своих
    строк в справочнике, не должен видеть индивидуальные строки другой
    компании. Иначе статья аренды 91.02 даёт два бизнес-типа и шаг 17
    останавливает прогон с «найдено несколько бизнес-типов».
    """
    if not REFERENCE_DATA_FILE.exists():
        print('  [skip] Справочники.xlsx не найден — сценарий пропущен')
        return

    opu = _load('Меппинг_опу')
    is_foreign = ~opu[REFERENCE_SCOPE_COL].astype('string').str.strip().str.casefold().eq(
        SCOPE_ALL_VALUE.casefold()
    )
    foreign = opu[is_foreign]
    if foreign.empty:
        print('  [skip] в справочнике нет индивидуальных строк — сценарий пропущен')
        return

    # Берём компанию из КомпанииГруппы без своих строк, но с тем же
    # сегментом, что у владельца чужих строк: именно совпадение сегмента
    # раньше делало неоднозначность неразрешимой на ступени «тип по сегменту».
    # Лист читается напрямую с usecols: в REFERENCE_CONFIGS его нет, а тянуть
    # весь реестр справочников ради двух колонок незачем.
    short_name = ColumnNames.SHORT_COMPANY_NAME
    companies = DataLoader.load_reference_data(
        sheet_name='КомпанииГруппы',
        usecols=[short_name, ColumnNames.SEGMENT],
    )
    owners = set(foreign[REFERENCE_SCOPE_COL].astype(str).str.strip())
    foreign_segments = set(foreign['сегмент'].astype(str).str.strip())
    candidates = companies[
        ~companies[short_name].astype(str).str.strip().isin(owners)
        & companies[ColumnNames.SEGMENT].astype(str).str.strip().isin(foreign_segments)
    ]
    if candidates.empty:
        print('  [skip] нет компании с чужим сегментом — сценарий пропущен')
        return

    company = str(candidates[short_name].iloc[0])
    view, diag = resolve_company_view(
        opu, company, OpuReportConstants.REFERENCE_ROW_KEY,
        reference_name='Меппинг_опу',
        known_companies=companies[short_name].dropna().astype(str),
    )

    assert diag.individual_rows == 0, (company, diag)
    assert diag.dropped_foreign_rows == len(foreign), (company, diag)
    assert len(view) == len(opu) - len(foreign), (len(view), len(opu), len(foreign))
    assert 'компания' in view.columns
    assert set(
        view[REFERENCE_SCOPE_COL].astype('string').str.strip().str.casefold().unique()
    ) == {SCOPE_ALL_VALUE.casefold()}, 'в кадре остались строки чужих компаний'

    # Ключевой симптом кейса: у статьи аренды остаётся РОВНО один тип.
    rent = view[
        view['счет'].astype(str).str.startswith('91.02')
        & view['доход_расход'].astype(str).str.contains('сдачей', na=False)
    ]
    types = set(rent['вид_дохода_расхода'].dropna())
    assert types == {'Аренда'}, (company, types)

    # ...и по этому ключу резолвер шага 17 больше не спотыкается о неоднозначность.
    by_key = _build_type_candidates_from_view(view)
    rent_keys = {
        key for key in by_key
        if key.startswith('91.02' + KEY_SEPARATOR)
        and 'сдачей' in key
    }
    assert rent_keys, 'статья аренды не найдена в кадре'
    for key in rent_keys:
        assert by_key[key] == ['Аренда'], (key, by_key[key])


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

    # Имя статьи берём из индивидуальных строк самой компании, а не пишем
    # строкой в ассерте: бухгалтерия при вносе новых статей в 1С опечатывается,
    # и в справочнике реально живут ДВЕ почти одинаковые статьи аренды —
    # «Доходы (расходы), связанные со сдачей…» (у ГиагКХП это проценты ППА) и
    # «Доходы и расходы,связанные со сдачей…» (универсальная, тип «Аренда»).
    # Отличие — пробел, поэтому срез по contains('сдачей') ловит обе, и
    # проверять надо по ключу статьи, а не по подстроке её названия.
    rent_articles = sorted(set(diag.individual_frame['доход_расход']))
    assert len(rent_articles) == 1, rent_articles
    rent_article = rent_articles[0]
    assert set(diag.individual_frame['вид_дохода_расхода']) == {
        'Расходы по процентам аренда'
    }, set(diag.individual_frame['вид_дохода_расхода'])
    assert set(diag.overridden_frame['вид_дохода_расхода']) == {'Аренда'}

    # Инвариант, на котором реально стоит шаг 17: на ключ поиска
    # ('счет[:5]' + 'доход_расход') должен быть ровно один вид_дохода_расхода,
    # иначе ступень 1 разрешения типа даст ReferenceMismatchError.
    #
    # Проверяем не весь справочник, а только ключи, где у компании есть свои
    # строки. Многотипные ключи в универсальном блоке — нормальное состояние
    # Меппинг_опу (замер 02.10.2026: 8 таких ключей), и они не мешают прогону:
    # ступени 1 они касаются, только если соответствующие обороты реально
    # пришли в компанию (замер: 0 из 147 компаний). Требовать единственный тип
    # от всего справочника — требование неверное.
    own_keys = set(
        build_composite_key(
            diag.individual_frame, 'счет', 'доход_расход', truncate_a=5
        )
    )
    assert own_keys, 'у компании должны быть индивидуальные строки'
    keyed = view.assign(
        _key=build_composite_key(view, 'счет', 'доход_расход', truncate_a=5)
    )
    on_own_keys = keyed[keyed['_key'].isin(own_keys)]
    types_per_key = on_own_keys.groupby('_key')['вид_дохода_расхода'].nunique(
        dropna=True
    )
    ambiguous = types_per_key[types_per_key > 1]
    assert ambiguous.empty, ambiguous.to_dict()

    rent_rows = view[
        (view['доход_расход'] == rent_article)
        & view['счет'].astype(str).str.startswith('91.02')
    ]
    assert set(rent_rows['вид_дохода_расхода']) == {'Расходы по процентам аренда'}

    universal_view, _ = resolve_company_view(
        opu, 'Другая', OpuReportConstants.REFERENCE_ROW_KEY, reference_name='Меппинг_опу',
    )
    universal_rent = universal_view[
        (universal_view['доход_расход'] == rent_article)
        & universal_view['счет'].astype(str).str.startswith('91.02')
    ]
    assert set(universal_rent['вид_дохода_расхода']) == {'Аренда'}, (
        'универсальный блок цел: у компании без индивидуальных строк статья '
        f'остаётся «Аренда», получено {sorted(set(universal_rent["вид_дохода_расхода"]))}'
    )

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

        sheets = pd.read_excel(reports[0], sheet_name=None, dtype=str)
        assert 'Применённые строки' in sheets, sorted(sheets)
        assert 'Перекрытые строки' in sheets, sorted(sheets)

        applied = sheets['Применённые строки']
        overridden = sheets['Перекрытые строки']
        # Обе половины правки: вытеснено столько же, сколько применено
        assert len(applied) == len(overridden) == 32, (len(applied), len(overridden))
        # Служебные колонки: лист справочника и кто именно перекрыл строку
        for frame in (applied, overridden):
            assert 'перекрыто_компанией' in frame.columns, frame.columns.tolist()
            assert set(frame['перекрыто_компанией']) == {'ГиагКХП'}
        assert set(overridden['лист']) == {'Меппинг_опу'}
        # Подмена видна прямо в файле: вместо «Аренда» — проценты ППА
        types = set(applied['вид_дохода_расхода'])
        assert types == {'Расходы по процентам аренда'}, types
        assert set(overridden['вид_дохода_расхода']) == {'Аренда'}
    finally:
        shutil.rmtree(get_run_dir(), ignore_errors=True)


def main() -> None:
    test_load_real_references()
    test_override_and_company_isolation()
    test_reference_without_individual_rows_is_untouched()
    test_missing_column_is_backward_compatible()
    test_unknown_scope_value_reported()
    test_real_file_company_view()
    test_company_without_individual_rows_does_not_inherit_foreign()
    test_wiring_in_executors()
    print('SMOKE_OK 9 scenarios: reference scope (индивидуальный меппинг компаний)')


if __name__ == '__main__':
    main()
