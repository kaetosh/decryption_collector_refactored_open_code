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

Смоук обязан падать при провале: check() увеличивает счётчик FAILED, финальная
строка — SMOKE_OK (PASSED/total) либо SMOKE_FAIL со списком провалов и код
возврата 1 (как в _smoke_98_account_level.py и _smoke_17_type_resolution.py).

Запуск: python -u _smoke_reference_scope.py
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


def skip(message: str) -> None:
    """Проверка неприменима (нет данных) — в прогоне её нет, и это не ошибка."""
    print(f"[SKIP] {message}")


def test_load_real_references() -> None:
    """Колонки по именам: счет_фо и детализация_субконто больше не теряются."""
    opu = _load('Меппинг_опу')
    for col in ('счет', 'доход_расход', 'вид_дохода_расхода', 'компания',
                'сегмент', 'вид_связи', 'счет_фо'):
        check(col in opu.columns, f"Меппинг_опу: колонка «{col}» на месте")
    check(opu['счет_фо'].notna().any(), 'счет_фо в Меппинг_опу читается')

    bb = _load('Меппинг_бб')
    for col in ('счет', 'субконто', 'компания', 'вид_задолженности',
                'детализация_субконто', 'счет_фо', 'отчетность'):
        check(col in bb.columns, f"Меппинг_бб: колонка «{col}» на месте")


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
    check(
        diag.applied and diag.individual_rows == 1,
        f"индивидуальная строка применена (строк: {diag.individual_rows})",
    )
    check(diag.overridden_rows == 1, f"вытеснено универсальных: {diag.overridden_rows}")
    check(
        list(view['вид_дохода_расхода']) == ['Расходы по процентам аренда'],
        f"в кадре тип компании, а не универсальный ({list(view['вид_дохода_расхода'])})",
    )
    check(
        list(view['счет_фо']) == ['1150040103'],
        f"счёт_фо взят из индивидуальной строки ({list(view['счет_фо'])})",
    )
    check(len(view) == 1, f"в кадре осталась одна строка (факт: {len(view)})")

    # У компании без своих строк индивидуальные строки ЧУЖИХ компаний
    # отбрасываются (регресс кейса «ТимПФ», 29.09.2026): раньше здесь стоял
    # ранний выход `if individual.empty: return df`, и весь справочник,
    # включая чужие строки, уезжал в прогон.
    other_view, other_diag = resolve_company_view(
        ref, 'Другая', key_cols, reference_name='Меппинг_опу',
        known_companies=['ГиагКХП', 'Другая'],
    )
    check(not other_diag.applied, 'у «Другая» своих строк нет')
    check(other_diag.individual_rows == 0, 'индивидуальных строк у «Другая» 0')
    check(
        other_diag.dropped_foreign_rows == 1,
        f"строка «ГиагКХП» отброшена для «Другая» (факт: {other_diag.dropped_foreign_rows})",
    )
    check(
        list(other_view['вид_дохода_расхода']) == ['Аренда'],
        f"у «Другая» остался универсальный тип ({list(other_view['вид_дохода_расхода'])})",
    )
    check(
        list(other_view['счет_фо']) == ['600020103'],
        f"счёт_фо остался универсальным ({list(other_view['счет_фо'])})",
    )


def test_reference_without_individual_rows_is_untouched() -> None:
    """Справочник, где все строки «все», возвращается тем же объектом."""
    ref = _make_ref().iloc[:1].copy()
    view, diag = resolve_company_view(
        ref, 'Другая', OpuReportConstants.REFERENCE_ROW_KEY, reference_name='Меппинг_опу',
    )
    check(not diag.applied, 'индивидуальный меппинг не применялся')
    check(
        diag.dropped_foreign_rows == 0,
        f"чужих строк нет, отбрасывать нечего (факт: {diag.dropped_foreign_rows})",
    )
    check(view is ref, 'индивидуального меппинга нет — кадр не меняется вообще')


def test_missing_column_is_backward_compatible() -> None:
    """Старый Справочники.xlsx без колонки «компания» работает как раньше."""
    ref = _make_ref().drop(columns=['компания'])
    view, diag = resolve_company_view(
        ref, 'ГиагКХП', OpuReportConstants.REFERENCE_ROW_KEY, reference_name='Меппинг_опу',
    )
    check(diag.column_missing, 'диагностика сообщает об отсутствии колонки')
    check(view is ref, 'кадр без колонки «компания» не изменяется')


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
        skip('Справочники.xlsx не найден — сценарий пропущен')
        return

    opu = _load('Меппинг_опу')
    is_foreign = ~opu[REFERENCE_SCOPE_COL].astype('string').str.strip().str.casefold().eq(
        SCOPE_ALL_VALUE.casefold()
    )
    foreign = opu[is_foreign]
    if foreign.empty:
        skip('в справочнике нет индивидуальных строк — сценарий пропущен')
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
        skip('нет компании с чужим сегментом — сценарий пропущен')
        return

    company = str(candidates[short_name].iloc[0])
    view, diag = resolve_company_view(
        opu, company, OpuReportConstants.REFERENCE_ROW_KEY,
        reference_name='Меппинг_опу',
        known_companies=companies[short_name].dropna().astype(str),
    )

    check(
        diag.individual_rows == 0,
        f"у «{company}» нет индивидуальных строк (факт: {diag.individual_rows})",
    )
    check(
        diag.dropped_foreign_rows == len(foreign),
        f"отброшены все строки чужих компаний: {diag.dropped_foreign_rows} из {len(foreign)}",
    )
    check(
        len(view) == len(opu) - len(foreign),
        f"кадр уменьшился ровно на число чужих строк ({len(view)} = {len(opu)} - {len(foreign)})",
    )
    check('компания' in view.columns, 'колонка «компания» на месте')
    check(
        set(
            view[REFERENCE_SCOPE_COL].astype('string').str.strip().str.casefold().unique()
        ) == {SCOPE_ALL_VALUE.casefold()},
        'в кадре остались строки чужих компаний',
    )

    # Ключевой симптом кейса: у статьи аренды остаётся РОВНО один тип.
    rent = view[
        view['счет'].astype(str).str.startswith('91.02')
        & view['доход_расход'].astype(str).str.contains('сдачей', na=False)
    ]
    types = set(rent['вид_дохода_расхода'].dropna())
    check(types == {'Аренда'}, f"у «{company}» статья аренды — только «Аренда»: {sorted(types)}")

    # ...и по этому ключу резолвер шага 17 больше не спотыкается о неоднозначность.
    by_key = _build_type_candidates_from_view(view)
    rent_keys = {
        key for key in by_key
        if key.startswith('91.02' + KEY_SEPARATOR)
        and 'сдачей' in key
    }
    check(bool(rent_keys), 'статья аренды не найдена в кадре')
    for key in sorted(rent_keys):
        check(by_key[key] == ['Аренда'], f"на ключе «{key}» один тип: {by_key[key]}")


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
    check(
        diag.unknown_scope_values == ['ГиагКХП2'],
        f"опечатка в имени компании попала в диагностику: {diag.unknown_scope_values}",
    )
    check(
        'Тип-X' not in set(view['вид_дохода_расхода']),
        'строка с неизвестной областью действия в работу не попала',
    )


def test_real_file_company_view() -> None:
    """Реальный кейс ГиагКХП: статья аренды перекрыта на «Расходы по процентам аренда»."""
    if not REFERENCE_DATA_FILE.exists():
        skip('Справочники.xlsx не найден — сценарий пропущен')
        return

    opu = _load('Меппинг_опу')
    view, diag = resolve_company_view(
        opu, 'ГиагКХП', OpuReportConstants.REFERENCE_ROW_KEY,
        reference_name='Меппинг_опу', known_companies=['ГиагКХП'],
    )
    check(
        diag.applied and diag.individual_rows == 32,
        f"индивидуальные строки «ГиагКХП» применены: {diag.individual_rows} "
        f"(перекрыто {diag.overridden_rows})",
    )
    check(
        diag.overridden_rows >= 4,
        f"вытеснено минимум 4 универсальные строки (факт: {diag.overridden_rows})",
    )

    # Имя статьи берём из индивидуальных строк самой компании, а не пишем
    # строкой в ассерте: бухгалтерия при вносе новых статей в 1С опечатывается,
    # и в справочнике реально живут ДВЕ почти одинаковые статьи аренды —
    # «Доходы (расходы), связанные со сдачей…» (у ГиагКХП это проценты ППА) и
    # «Доходы и расходы,связанные со сдачей…» (универсальная, тип «Аренда»).
    # Отличие — пробел, поэтому срез по contains('сдачей') ловит обе, и
    # проверять надо по ключу статьи, а не по подстроке её названия.
    rent_articles = sorted(set(diag.individual_frame['доход_расход']))
    check(len(rent_articles) == 1, f"индивидуальные строки относятся к одной статье: {rent_articles}")
    rent_article = rent_articles[0] if rent_articles else None
    individual_types = set(diag.individual_frame['вид_дохода_расхода'])
    check(
        individual_types == {'Расходы по процентам аренда'},
        f"все индивидуальные строки — проценты ППА (факт: {sorted(individual_types)})",
    )
    overridden_types = set(diag.overridden_frame['вид_дохода_расхода'])
    check(
        overridden_types == {'Аренда'},
        f"все вытесненные строки были «Аренда» (факт: {sorted(overridden_types)})",
    )

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
    check(bool(own_keys), 'ключи поиска индивидуальных строк построены')
    keyed = view.assign(
        _key=build_composite_key(view, 'счет', 'доход_расход', truncate_a=5)
    )
    on_own_keys = keyed[keyed['_key'].isin(own_keys)]
    types_per_key = on_own_keys.groupby('_key')['вид_дохода_расхода'].nunique(
        dropna=True
    )
    ambiguous = types_per_key[types_per_key > 1]
    check(
        ambiguous.empty,
        f"на ключах компании ровно один тип — ступень 1 шага 17 не споткнётся "
        f"(нарушения: {ambiguous.to_dict()})",
    )

    rent_rows = view[
        (view['доход_расход'] == rent_article)
        & view['счет'].astype(str).str.startswith('91.02')
    ]
    check(
        set(rent_rows['вид_дохода_расхода']) == {'Расходы по процентам аренда'},
        f"в кадре статья аренды — проценты ППА "
        f"(факт: {sorted(set(rent_rows['вид_дохода_расхода']))})",
    )

    universal_view, _ = resolve_company_view(
        opu, 'Другая', OpuReportConstants.REFERENCE_ROW_KEY, reference_name='Меппинг_опу',
    )
    universal_rent = universal_view[
        (universal_view['доход_расход'] == rent_article)
        & universal_view['счет'].astype(str).str.startswith('91.02')
    ]
    check(
        set(universal_rent['вид_дохода_расхода']) == {'Аренда'},
        "универсальный блок цел: у компании без индивидуальных строк статья "
        f"остаётся «Аренда» (факт: {sorted(set(universal_rent['вид_дохода_расхода']))})",
    )

    bb = _load('Меппинг_бб')
    bb_view, bb_diag = resolve_company_view(
        bb, 'ГиагКХП', BalanceReportConstants.MAPPING_KEYS, reference_name='Меппинг_бб',
    )
    check(
        not bb_diag.applied and bb_view is bb,
        'в Меппинг_бб индивидуальных строк «ГиагКХП» пока нет — кадр не тронут',
    )


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
        check(opu_entry is not None, f"в сводке есть запись по Меппинг_опу: {entries}")
        check(
            opu_entry is not None and opu_entry['индивидуальных_строк'] == 32,
            f"в сводке 32 индивидуальные строки (факт: {opu_entry})",
        )

        opu = context.references['меппинг_опу']
        check(
            'счет_фо' in opu.columns and 'компания' in opu.columns,
            'после применения взгляда колонки счет_фо и компания на месте',
        )
        bb = context.references['меппинг_баланс']
        for col in ('счет_фо', 'детализация_субконто', 'компания'):
            check(col in bb.columns, f"Меппинг_бб: колонка «{col}» на месте")

        reports = list((get_run_dir() / 'mismatches').glob('reference_scope_*.xlsx'))
        check(bool(reports), 'Excel-диагностика индивидуального меппинга создана')

        if reports:
            sheets = pd.read_excel(reports[0], sheet_name=None, dtype=str)
            check('Применённые строки' in sheets, f"лист «Применённые строки»: {sorted(sheets)}")
            check('Перекрытые строки' in sheets, f"лист «Перекрытые строки»: {sorted(sheets)}")

            if 'Применённые строки' in sheets and 'Перекрытые строки' in sheets:
                applied = sheets['Применённые строки']
                overridden = sheets['Перекрытые строки']
                # Обе половины правки: вытеснено столько же, сколько применено
                check(
                    len(applied) == len(overridden) == 32,
                    f"вытеснено столько же строк, сколько применено "
                    f"({len(applied)} / {len(overridden)})",
                )
                # Служебные колонки: лист справочника и кто именно перекрыл строку
                for name, frame in (('применённые', applied), ('перекрытые', overridden)):
                    check(
                        'перекрыто_компанией' in frame.columns,
                        f"лист «{name}»: служебная колонка на месте",
                    )
                    check(
                        set(frame.get('перекрыто_компанией', [])) == {'ГиагКХП'},
                        f"лист «{name}»: перекрыто компанией «ГиагКХП»",
                    )
                check(
                    set(overridden['лист']) == {'Меппинг_опу'},
                    f"лист «перекрытые»: источник — Меппинг_опу ({sorted(set(overridden['лист']))})",
                )
                # Подмена видна прямо в файле: вместо «Аренда» — проценты ППА
                applied_types = set(applied['вид_дохода_расхода'])
                check(
                    applied_types == {'Расходы по процентам аренда'},
                    f"в применённых строках проценты ППА (факт: {sorted(applied_types)})",
                )
                overridden_types = set(overridden['вид_дохода_расхода'])
                check(
                    overridden_types == {'Аренда'},
                    f"в перекрытых строках «Аренда» (факт: {sorted(overridden_types)})",
                )
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

    total = PASSED + FAILED
    label = 'reference scope (индивидуальный меппинг компаний)'
    if FAILED:
        print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
        for line in _failed_messages:
            print(f"  [FAIL] {line}")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


if __name__ == '__main__':
    main()
