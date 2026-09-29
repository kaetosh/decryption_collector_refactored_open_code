# -*- coding: utf-8 -*-
"""
Смоук: выбор вида дохода/расхода (шаг 17) с учётом сегмента компании.

Три ступени разрешения в _resolve_income_expense_mapping:
  1. по ключу ровно один тип — присваивается (регрессия старого поведения);
  2. подтверждённый объект ППА — тип выбытия прав пользования;
  3. остальные строки — тип из блока СЕГМЕНТА компании, если там ровно один
     (это и есть фикс кейса «ГиагКХП»: в общем блоке статья аренды даёт
     «Аренда», а по сегменту компании — индивидуальная «Расходы по процентам аренда»);
  4. иначе — ReferenceMismatchError (произвольный выбор запрещён).

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

PPA_TYPE = 'Расходы от выбытия прав пользования активами, изменений условий договоров аренды'
RENT_TYPE = 'Расходы по процентам аренда'


def _step() -> Step17AddOtherIncomeExpensesToOpuStep:
    return Step17AddOtherIncomeExpensesToOpuStep()


def _reference(rows: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    ref = pd.DataFrame(rows, columns=['счет', 'доход_расход', 'вид_дохода_расхода', 'сегмент'])
    ref['_key'] = build_composite_key(ref, 'счет', 'доход_расход', truncate_a=5)
    return ref


def _df(rows: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    """(счет, доход_расход, Корр.счет, Субконто Кт_1) — расходы 91.02."""
    df = pd.DataFrame(rows, columns=['счет', 'доход_расход', 'Корр.счет', 'Субконто Кт_1'])
    df['_key'] = build_composite_key(df, 'счет', 'доход_расход', truncate_a=5)
    return df


def test_single_candidate_is_unchanged() -> None:
    """Ступень 1: один тип на ключ — присваивается всем строкам."""
    step = _step()
    ref = _reference([('91.02', 'Статья', 'Аренда', 'Прочие')])
    df = _df([('91.02', 'Статья', '60.01', 'Контрагент-1')])

    step._resolve_income_expense_mapping(
        df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
    )
    assert list(df['вид_дохода_расхода']) == ['Аренда'], df['вид_дохода_расхода']


def test_segment_scoped_type_wins_for_company() -> None:
    """Ступень 3: глобально несколько типов, по сегменту компании — ровно один."""
    step = _step()
    ref = _reference([
        ('91.02', 'Статья', 'Аренда', 'Прочие'),
        ('91.02', 'Статья', 'Расходы по прочим услугам', 'КРС'),
        ('91.02', 'Статья', RENT_TYPE, 'Птицеводство'),
    ])
    df = _df([('91.02', 'Статья', '97.21', 'Проценты ППА Договор аренды № 020823-49')])

    step._resolve_income_expense_mapping(
        df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
    )
    assert list(df['вид_дохода_расхода']) == [RENT_TYPE], df['вид_дохода_расхода']


def test_ppa_rule_keeps_priority() -> None:
    """Ступень 2: подтверждённый объект ППА получает тип выбытия, остальные — по сегменту."""
    step = _step()
    ref = _reference([
        ('91.02', 'Статья', 'Аренда', 'Прочие'),
        ('91.02', 'Статья', PPA_TYPE, 'Растениеводство'),
        ('91.02', 'Статья', RENT_TYPE, 'Птицеводство'),
    ])
    df = _df([
        ('91.02', 'Статья', '01.09', 'ППА Лизинговый объект № 1'),
        ('91.02', 'Статья', '97.21', 'Проценты ППА Договор аренды № 020823-49'),
    ])

    step._resolve_income_expense_mapping(
        df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
    )
    assert list(df['вид_дохода_расхода']) == [PPA_TYPE, RENT_TYPE], df['вид_дохода_расхода'].tolist()


def test_ambiguous_key_is_hard_error() -> None:
    """Ступень 4: ни уникального типа, ни ППА, ни уникального типа по сегменту — стоп."""
    step = _step()
    ref = _reference([
        ('91.02', 'Статья', 'Аренда', 'Птицеводство'),
        ('91.02', 'Статья', 'Расходы по прочим услугам', 'Птицеводство'),
    ])
    df = _df([('91.02', 'Статья', '60.01', 'Контрагент-1')])

    try:
        step._resolve_income_expense_mapping(
            df, ref, step.ACCOUNT_OTHER_EXPENSE, segment='Птицеводство',
        )
    except ReferenceMismatchError as exc:
        assert exc.problem_data is not None, 'в mismatches/ должна уйти диагностика'
        assert exc.reference_name == 'Меппинг_опу'
    else:
        raise AssertionError('ожидался ReferenceMismatchError по неоднозначному ключу')


def test_real_transactions_step17_core() -> None:
    """
    Реальные выгрузки «ГиагКХП»: ядро шага 17 на настоящих проводках.

    Старое поведение: ключ 91.02 + «Доходы (расходы), связанные со сдачей
    имущества в аренду (субаренду)» содержал несколько бизнес-типов →
    ReferenceMismatchError / строки уходили в «продажу активов» (тип «Аренда»
    из ПрочиеДоходыНДС) и давали mismatch_step_17_распределение_расходов_91_02_*.
    Новое: тип — «Расходы по процентам аренда», контрагент процентов ППА — из
    справочника ППА (колонка 'рбп').
    """
    from io_module import DataLoader
    from io_module.output_manager import configure_run, get_run_dir
    from pipeline.base import ProcessingContext
    from pipeline.executors import (
        _apply_company_reference_scope,
        _load_all_references,
    )
    from utils import add_ruble_amount_column
    import shutil

    company = 'ГиагКХП'
    # Реальные проводки в папке сменяются от прогона к прогону: если сейчас
    # там выгрузки другой компании (например ТимПФ), сценарий честно пропускаем
    # — сверять их с ППА «ГиагКХП» бессмысленно (падение на чужих данных)
    from config.settings import ACCOUNT_CARDS_DIR

    prefixes = {
        f.name.split('_отчпровод_')[0]
        for f in ACCOUNT_CARDS_DIR.glob('*_отчпровод_*.txt')
    }
    if prefixes != {company}:
        print(
            f'  SKIP реальный сценарий: в папке отчёты по проводкам {sorted(prefixes)}, '
            f'тест ждёт {company}'
        )
        return
    configure_run('smoke_seg17')
    try:
        context = ProcessingContext(company=company)
        context.references = _load_all_references()
        companies = context.references['компании_группы']
        segment = companies.loc[
            companies['сокращенное_наименование_компании'] == company, 'сегмент'
        ].iloc[0]
        context.segment = segment
        # Как в initialize_context(): взгляд компании — до запуска шагов
        _apply_company_reference_scope(context)

        step = _step()
        refs = step._load_references(company, context)
        transactions = step.clean_whitespace(DataLoader.load_transaction_report())
        # В конвейере «Сумма_руб» появляется на шаге 14 (add_ruble_amount_column);
        # шаг 14 в смоуке не выполняем, поэтому вызываем тот же хелпер напрямую
        transactions = add_ruble_amount_column(transactions, context)

        df_9101, df_9102 = step._filter_91_transactions(
            transactions, refs['mapping_opu'], segment=segment,
        )
        df_9101 = step._extract_contractors(
            df_9101, is_income=True,
            accounts_with_contractors=refs['accounts_with_contractors'],
        )
        df_9102 = step._extract_contractors(
            df_9102, is_income=False,
            accounts_with_contractors=refs['accounts_with_contractors'],
        )
        df_9101, df_9102 = step._process_ppa(df_9101, df_9102, refs['ppa'], company)
        df_9101, df_9102 = step._pull_contractors_from_ppa_by_rbp(
            df_9101, df_9102, refs['ppa'], company,
        )

        assert len(df_9102) > 0, 'выгрузка 91.02 должна быть прочитана'

        rent = df_9102[
            df_9102['доход_расход'].astype(str).str.contains('сдачей', na=False)
        ]
        assert not rent.empty, 'строки статьи аренды есть в 91.02'

        types = set(rent['вид_дохода_расхода'].dropna())
        assert types == {RENT_TYPE}, types
        assert not (types & set(refs['asset_sale_types'])), \
            'проценты ППА не должны уходить в обработку продажи активов'

        ppa_rows = rent[rent['Субконто Кт_1'].astype(str).str.contains('ППА', na=False)]
        assert not ppa_rows.empty, 'строки процентов ППА есть в 91.02'
        contractors = set(ppa_rows['контрагент'].dropna())
        ppa_contractors = set(refs['ppa']['контрагент'].dropna())
        assert contractors, 'контрагент процентов ППА подтянут из справочника ППА'
        assert 'не_указано' not in contractors, contractors
        assert contractors <= ppa_contractors, (contractors, ppa_contractors)
    finally:
        shutil.rmtree(get_run_dir(), ignore_errors=True)


def main() -> None:
    test_single_candidate_is_unchanged()
    test_segment_scoped_type_wins_for_company()
    test_ppa_rule_keeps_priority()
    test_ambiguous_key_is_hard_error()
    test_real_transactions_step17_core()
    print('SMOKE_OK 5 scenarios: выбор типа ОПУ по сегменту (шаг 17)')


if __name__ == '__main__':
    main()
