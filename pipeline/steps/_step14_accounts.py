"""
Mixin с обработкой счетов 90.01/90.02 для Шага 14.

Методы:
    _process_revenue_9001: обработка выручки (90.01)
    _extract_vat_9003: сбор НДС по продажам из отчёта по проводкам 90.03
    _subtract_vat_from_revenue: вычитание НДС документа из строк 90.01
    _warn_unmatched_vat: диагностика НДС, не сопоставленного с выручкой
    _validate_revenue_against_osv: сверка выручки с ОСВ
    _process_cost_9002: обработка себестоимости (90.02)
    _process_reassessment: обработка переоценки (Дт 90.02 Кт 16)
    _validate_cost_against_osv: сверка себестоимости с ОСВ
    _distribute_cost_to_buyers: распределение себестоимости пропорционально выручке
"""
from typing import Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from pipeline.errors import ConvergenceError, MissingMappingError, ReferenceMismatchError
from utils import align_dtypes_to_reference


class Step14AccountsMixin:
    """Миксин обработки счетов 90.01/90.02 для Шага 14."""

    def _process_revenue_9001(
        self,
        transactions_all_df: pd.DataFrame,
        osv_df: pd.DataFrame,
        context,
    ) -> pd.DataFrame:
        """Обрабатывает выручку по счету 90.01."""
        logger.debug("Обработка выручки (90.01)")

        mask_account = transactions_all_df['Кт'].str.startswith("90.01", na=False)
        mask_file = transactions_all_df['Имя_файла'].str.contains("_90.01_", na=False)
        df9001 = transactions_all_df.loc[mask_account & mask_file].copy()

        df9001 = df9001.rename(columns={
            'Субконто Дт_1': 'контрагент',
            'Субконто Кт_1': 'ном_группа',
            'Кт': 'счет'
        })

        df9001.loc[df9001['Дт'].str.startswith('76.15'), 'контрагент'] = 'пайщики'

        # Выручка 90.01 валовая (с НДС), а в ОПУ идёт без НДС. Сумму НДС берём
        # из отчёта по проводкам 90.03 — готовую сумму по каждому документу, а
        # не ставку из субконто: у иностранных компаний ставка текстовая
        # («Exempt from Tax», «Без НДС») или пустая, а одна номенклатурная
        # группа может облагаться разными ставками одновременно, и любое
        # восстановление ставки по «аналогам» здесь ошибается.
        df9001, unmatched_vat_df = self._subtract_vat_from_revenue(
            df9001, self._extract_vat_9003(transactions_all_df)
        )

        df9001['выручка_без_ндс_тыс_ед'] = (
            df9001['Сумма'] / 1000 - df9001['ндс_тыс_ед']
        )
        df9001['выручка_без_ндс_тыс_руб'] = (
            df9001['Сумма_руб'] / 1000 - df9001['ндс_тыс_руб']
        )

        df9001 = df9001.loc[:, ['Документ', 'контрагент', 'ном_группа', 'выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']]
        df9001 = df9001.groupby(
            ['Документ', 'контрагент', 'ном_группа'],
            as_index=False
        )[['выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']].sum()

        self._validate_revenue_against_osv(
            df9001, osv_df, context, unmatched_vat_df
        )

        logger.debug("Выручка обработана: {} строк", len(df9001))

        return df9001

    def _extract_vat_9003(
        self,
        transactions_all_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Собирает НДС по продажам из отчёта по проводкам 90.03.

        Источник НДС — весь дебетовый оборот счёта 90.03 в сводном отчёте по
        проводкам, агрегированный по 'Документ'. Фильтр по корр.счёту (68.02)
        НЕ ставится намеренно: сверка выручки с ОСВ тоже опирается на весь
        дебетовый оборот 90.03, а НДС по экспорту (Кт 68.07/68.11) сидит в
        валовой выручке 90.01 и тоже должен быть вычтен.

        Returns:
            DataFrame с колонками 'Документ', 'ндс_тыс_ед', 'ндс_тыс_руб'
            (пустой, если оборотов по 90.03 нет).
        """
        mask_account = transactions_all_df['Дт'].str.startswith(
            self.ACCOUNT_VAT, na=False
        )
        mask_file = transactions_all_df['Имя_файла'].str.contains(
            f"_{self.ACCOUNT_VAT}_", na=False
        )
        df9003 = transactions_all_df.loc[mask_account & mask_file]

        if df9003.empty:
            logger.info(
                "НДС 90.03: оборотов по счёту {} в отчёте по проводкам нет — "
                "выручка 90.01 остаётся валовой (НДС не начислялся)",
                self.ACCOUNT_VAT,
            )
            return pd.DataFrame(columns=['Документ', 'ндс_тыс_ед', 'ндс_тыс_руб'])

        vat_df = df9003.groupby('Документ', as_index=False)[
            ['Сумма', 'Сумма_руб']
        ].sum()
        vat_df['ндс_тыс_ед'] = vat_df['Сумма'] / 1000
        vat_df['ндс_тыс_руб'] = vat_df['Сумма_руб'] / 1000

        logger.debug(
            "НДС 90.03: {} документов, сумма {:.2f} тыс.ед.",
            len(vat_df), float(vat_df['ндс_тыс_ед'].sum()),
        )

        return vat_df[['Документ', 'ндс_тыс_ед', 'ндс_тыс_руб']]

    def _subtract_vat_from_revenue(
        self,
        df9001: pd.DataFrame,
        vat_df: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Вычитает НДС документа из строк выручки 90.01.

        НДС документа распределяется по его строкам РОВНО ОДИН РАЗ
        (Step._distribute_vat_by_rows — тот же хелпер, что и в шаге 17 для
        91.01): у документа бывает несколько строк 90.01, и прямое вычитание
        НДС из каждой многократно завышало бы выручку. Документы без НДС в
        90.03 получают нулевой НДС — так сама собой получается нулевая ставка
        (освобождение, «Без НДС», «Exempt from Tax»), ничего распознавать
        не нужно.

        Returns:
            Tuple[df9001 с колонками 'ндс_тыс_ед'/'ндс_тыс_руб',
                  DataFrame с НДС, не сопоставленным ни одной строкой 90.01].
        """
        df9001 = df9001.copy()

        if vat_df.empty:
            df9001['ндс_тыс_ед'] = 0.0
            df9001['ндс_тыс_руб'] = 0.0
            return df9001, pd.DataFrame()

        vat_by_doc = vat_df.set_index('Документ')
        df9001['ндс_тыс_ед'] = self._distribute_vat_by_rows(
            df9001, vat_by_doc['ндс_тыс_ед'], 'Сумма'
        )
        df9001['ндс_тыс_руб'] = self._distribute_vat_by_rows(
            df9001, vat_by_doc['ндс_тыс_руб'], 'Сумма_руб'
        )

        unmatched = vat_df.loc[
            ~vat_df['Документ'].isin(df9001['Документ'].unique())
        ].copy()

        self._warn_unmatched_vat(unmatched, df9001)

        return df9001, unmatched

    def _warn_unmatched_vat(
        self,
        unmatched: pd.DataFrame,
        df9001: pd.DataFrame,
    ) -> None:
        """
        Сообщает о НДС 90.03, которому не нашлось строки выручки 90.01.

        Такой НДС вычесть некуда: он остался бы в выручке, а сверка с ОСВ
        показала бы расхождение без указания причины. Поэтому WARNING уходит
        ДО остановки, а сами строки попадают в problem_data того же
        ConvergenceError (см. _validate_revenue_against_osv) — получатель видит
        не «расхождение 12 705», а конкретные документы.
        """
        if unmatched.empty:
            return

        logger.warning(
            "НДС 90.03: {} на {:.2f} тыс.ед. не сопоставлено ни с одним "
            "документом выручки 90.01 (документов выручки: {}) — НДС не вычтен. "
            "Проверьте, что отчёты по проводкам 90.01 и 90.03 выгружены "
            "за один период.",
            len(unmatched), float(unmatched['ндс_тыс_ед'].sum()),
            int(df9001['Документ'].nunique()),
        )

    def _validate_revenue_against_osv(
        self,
        df9001: pd.DataFrame,
        osv_df: pd.DataFrame,
        context,
        unmatched_vat_df: Optional[pd.DataFrame] = None,
    ) -> None:
        """Проверяет сходимость выручки с общей ОСВ.

        unmatched_vat_df — НДС 90.03, которому не нашлось строки выручки 90.01
        (см. _warn_unmatched_vat). Добавляется в problem_data: без него
        получатель видит только цифру расхождения и не понимает, что именно
        не сошлось.
        """
        revenue_osv_9001 = osv_df.loc[
            osv_df['Счет'].str.startswith('90.01'), 'Кредит_оборот'
        ].sum()
        revenue_osv_9003 = osv_df.loc[
            osv_df['Счет'].str.startswith('90.03'), 'Дебет_оборот'
        ].sum()
        revenue_without_vat = (revenue_osv_9001 - revenue_osv_9003) / 1000

        revenue_from_df9001 = df9001['выручка_без_ндс_тыс_ед'].sum()

        difference = abs(revenue_without_vat - revenue_from_df9001)

        if difference > context.tolerance_params['tolerance_reconciliation']:
            problem_data = pd.DataFrame([{
                'проверка': 'выручка_против_ОСВ',
                'из_проводок, тыс.ед.': round(revenue_from_df9001, 2),
                'из_ОСВ, тыс.ед.': round(revenue_without_vat, 2),
                'разница, тыс.ед.': round(difference, 2),
                'допуск, тыс.ед.': context.tolerance_params['tolerance_reconciliation'],
            }])
            if unmatched_vat_df is not None and not unmatched_vat_df.empty:
                problem_data = pd.concat(
                    [problem_data, unmatched_vat_df], ignore_index=True
                )
            raise ConvergenceError(
                f"Выручка из отчёта по проводкам ({revenue_from_df9001:,.2f} тыс.ед.) "
                f"отличается от общей ОСВ ({revenue_without_vat:,.2f} тыс.ед.) "
                f"на {difference:,.2f} тыс.ед. (допуск: {context.tolerance_params['tolerance_reconciliation']})",
                problem_data=problem_data,
                reference_name='выручка_против_ОСВ',
            )

        logger.debug(
            "[OK] Сходимость выручки: ОСВ={:,.2f}, отчёт={:,.2f}, разница={:,.2f}",
            revenue_without_vat,
            revenue_from_df9001,
            difference,
        )

    def _process_cost_9002(
        self,
        transactions_all_df: pd.DataFrame,
        osv_df: pd.DataFrame,
        name_company: str,
        company_directory_df: pd.DataFrame,
        context,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Обрабатывает себестоимость по счету 90.02.

        Выделяет отдельно переоценку активов (Дт 90.02 Кт 16) в df9002_16.

        Returns:
            Tuple[df9002, df9002_16]: Основная себестоимость и переоценка
        """
        logger.debug("Обработка себестоимости (90.02)")

        mask_account = transactions_all_df['Дт'].str.startswith("90.02", na=False)
        mask_file = transactions_all_df['Имя_файла'].str.contains("_90.02_", na=False)
        df9002 = transactions_all_df.loc[mask_account & mask_file].copy()

        df9002 = df9002.rename(columns={
            'Субконто Дт_1': 'ном_группа',
            'Дт': 'счет'
        })

        mask_16 = df9002['Кт'].astype(str).str.startswith('16', na=False)

        if mask_16.any():
            df9002_16 = self._process_reassessment(df9002[mask_16].copy(), name_company, company_directory_df)
            df9002 = df9002[~mask_16].copy()
            turn_df9002_16 = df9002_16['оборот, тыс.ед.'].sum()
        else:
            df9002_16 = pd.DataFrame()
            turn_df9002_16 = 0

        df9002['себестоимость_тыс_ед'] = df9002.loc[:, 'Сумма'] / 1000
        df9002['себестоимость_тыс_руб'] = df9002.loc[:, 'Сумма_руб'] / 1000
        df9002 = df9002.loc[:, ['Документ', 'ном_группа', 'себестоимость_тыс_ед', 'себестоимость_тыс_руб']]
        df9002 = df9002.groupby(
            ['Документ', 'ном_группа'],
            as_index=False
        )[['себестоимость_тыс_ед', 'себестоимость_тыс_руб']].sum()

        self._validate_cost_against_osv(df9002, osv_df, turn_df9002_16, context)

        logger.debug(
            "Себестоимость обработана: {} строк основной, {} строк переоценки",
            len(df9002),
            len(df9002_16),
        )

        return df9002, df9002_16

    def _process_reassessment(
        self,
        df9002_16: pd.DataFrame,
        name_company: str,
        company_directory_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Обрабатывает переоценку активов (Дт 90.02 Кт 16)."""
        logger.debug("Обработка переоценки активов (Дт 90.02 Кт 16)")

        matching_rows = company_directory_df[
            company_directory_df['сокращенное_наименование_компании'] == name_company
        ]

        if matching_rows.empty:
            problem_data = (
                company_directory_df[['сокращенное_наименование_компании']]
                .drop_duplicates()
                .rename(columns={'сокращенное_наименование_компании': 'компания_в_справочнике'})
            )
            raise ReferenceMismatchError(
                message=f"Компания '{name_company}' не найдена в справочнике",
                problem_data=problem_data,
                reference_name="КомпанииГруппы",
                searched_company=name_company,
            )

        if len(matching_rows) > 1:
            raise ReferenceMismatchError(
                message=(
                    f"У компании '{name_company}' найдено {len(matching_rows)} записей. "
                    f"Ожидается одна."
                ),
                problem_data=matching_rows.copy(),
                reference_name="КомпанииГруппы",
                duplicate_count=len(matching_rows),
            )

        df9002_16['оборот, тыс.ед.'] = df9002_16.loc[:, 'Сумма'] / 1000
        df9002_16['оборот, тыс.руб.'] = df9002_16.loc[:, 'Сумма_руб'] / 1000
        df9002_16 = df9002_16.groupby(
            ['ном_группа'], as_index=False
        )[['оборот, тыс.ед.', 'оборот, тыс.руб.']].sum()

        segment = matching_rows['сегмент'].iloc[0]

        for col_name, value in [
            ('счет', '90.02'),
            ('доход_расход', 'Изменение в оценке'),
            ('сегмент', segment),
            ('вид_связи', 'не_указано'),
        ]:
            df9002_16[col_name] = pd.Series([value] * len(df9002_16), dtype='string')

        products_reassessment_type = matching_rows['вид_продукции_переоценки'].iloc[0]
        df9002_16['вид_дохода_расхода'] = products_reassessment_type

        logger.debug(
            "Переоценка: {} строк, вид_дохода_расхода='{}'",
            len(df9002_16),
            products_reassessment_type,
        )

        return df9002_16

    def _validate_cost_against_osv(
        self,
        df9002: pd.DataFrame,
        osv_df: pd.DataFrame,
        turn_df9002_16: float,
        context,
    ) -> None:
        """Проверяет сходимость себестоимости с общей ОСВ."""
        cost_price_osv_9002 = osv_df.loc[
            osv_df['Счет'].str.startswith('90.02'), 'Дебет_оборот'
        ].sum() / 1000

        cost_price_from_df9002 = df9002['себестоимость_тыс_ед'].sum() + turn_df9002_16

        difference = abs(cost_price_osv_9002 - cost_price_from_df9002)

        if difference > context.tolerance_params['tolerance_reconciliation']:
            raise ConvergenceError(
                f"Себестоимость из отчёта по проводкам ({cost_price_from_df9002:,.2f} тыс.ед.) "
                f"отличается от общей ОСВ ({cost_price_osv_9002:,.2f} тыс.ед.) "
                f"на {difference:,.2f} тыс.ед. (допуск: {context.tolerance_params['tolerance_reconciliation']})",
                problem_data=pd.DataFrame([{
                    'проверка': 'себестоимость_против_ОСВ',
                    'из_проводок, тыс.ед.': round(cost_price_from_df9002, 2),
                    'из_ОСВ, тыс.ед.': round(cost_price_osv_9002, 2),
                    'разница, тыс.ед.': round(difference, 2),
                    'допуск, тыс.ед.': context.tolerance_params['tolerance_reconciliation'],
                }]),
                reference_name='себестоимость_против_ОСВ',
            )

        logger.debug(
            "[OK] Сходимость себестоимости: ОСВ={:,.2f}, отчёт={:,.2f}, разница={:,.2f}",
            cost_price_osv_9002,
            cost_price_from_df9002,
            difference,
        )

    def _distribute_cost_to_buyers(
        self,
        df9001: pd.DataFrame,
        df9002: pd.DataFrame,
    ) -> pd.DataFrame:
        """Распределяет себестоимость на контрагентов пропорционально выручке.

        Каждая единица себестоимости попадает ровно в одну строку результата:
        - кост ключа (Документ, ном_группа) делится между контрагентами этого
          документа пропорционально их доле выручки (без задвоения, когда в
          одном документе несколько контрагентов по одной номенклатуре);
        - кост документов без выручки распределяется по номенклатурной группе
          пропорционально выручке группы;
        - кост групп, у которых нет выручки вовсе, не сжигается, а остаётся
          в отчёте с контрагентом '3 лица'.
        """
        logger.debug("Распределение себестоимости на контрагентов")

        df_alloc = self._prepare_cost_allocation(df9001, df9002)
        df9002_rem, group_pot = self._compute_key_remainder(df9002, df_alloc)
        df_alloc, revenue_group = self._share_group_remainder(df9001, df_alloc, group_pot)

        orphans = self._find_orphan_cost_groups(group_pot, revenue_group)
        df_result = self._finalize_distribution(df_alloc, orphans)

        logger.debug(
            "Себестоимость распределена: {} строк с контрагентами ({} строк сиротских)",
            len(df_result),
            len(orphans),
        )

        return df_result

    def _prepare_cost_allocation(
        self,
        df9001: pd.DataFrame,
        df9002: pd.DataFrame,
    ) -> pd.DataFrame:
        """Собирает общий DataFrame-скелет распределения: выручка по контрагентам
        (df9001) × выручка ключа (Документ, ном_группа) × себестоимость ключа
        (df9002); затем считает долю строки в выручке ключа и прямой кост по строке.
        """
        df_buyers = df9001.copy()

        revenue_key = df9001.groupby(
            ['Документ', 'ном_группа'], as_index=False
        )[['выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']].sum().rename(
            columns={
                'выручка_без_ндс_тыс_ед': 'выручка_ключа_ед',
                'выручка_без_ндс_тыс_руб': 'выручка_ключа_руб',
            }
        )

        df_alloc = df_buyers.merge(revenue_key, on=['Документ', 'ном_группа'], how='left')
        df_alloc = df_alloc.merge(df9002, on=['Документ', 'ном_группа'], how='left')

        rev_key_pos = df_alloc['выручка_ключа_ед'] > 0
        df_alloc['доля_строки'] = np.where(
            rev_key_pos,
            df_alloc['выручка_без_ндс_тыс_ед'] / df_alloc['выручка_ключа_ед'],
            0.0,
        )

        # Прямой кост: кост ключа × доля строки в выручке ключа (без задвоения)
        df_alloc['кост_прямой'] = (
            df_alloc['себестоимость_тыс_ед'].fillna(0) * df_alloc['доля_строки']
        )
        df_alloc['кост_прямой_руб'] = (
            df_alloc['себестоимость_тыс_руб'].fillna(0) * df_alloc['доля_строки']
        )

        return df_alloc

    def _compute_key_remainder(
        self,
        df9002: pd.DataFrame,
        df_alloc: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Считает остаток коста по ключу после прямого распределения (df9002_rem)
        и свод остатков по номенклатурным группам с ненулевым костом (group_pot).
        """

        # Остаток коста после прямого распределения по контрагентам ключа
        allocated_key = df_alloc.groupby(['Документ', 'ном_группа'], as_index=False)[
            ['кост_прямой', 'кост_прямой_руб']
        ].sum().rename(columns={
            'кост_прямой': 'кост_распределён_ед',
            'кост_прямой_руб': 'кост_распределён_руб',
        })
        df9002_rem = df9002.merge(allocated_key, on=['Документ', 'ном_группа'], how='left')
        df9002_rem['кост_остаток'] = (
            df9002_rem['себестоимость_тыс_ед']
            - df9002_rem['кост_распределён_ед'].fillna(0)
        )
        df9002_rem['кост_остаток_руб'] = (
            df9002_rem['себестоимость_тыс_руб']
            - df9002_rem['кост_распределён_руб'].fillna(0)
        )

        # Остатки по номенклатурной группе — распределяются по выручке группы
        group_pot = df9002_rem.groupby('ном_группа')[['кост_остаток', 'кост_остаток_руб']].sum()
        group_pot = group_pot[group_pot['кост_остаток'] != 0]

        return df9002_rem, group_pot

    def _share_group_remainder(
        self,
        df9001: pd.DataFrame,
        df_alloc: pd.DataFrame,
        group_pot: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Распределяет остаток коста по группе между строками пропорционально
        выручке группы и считает итоговую себестоимость по строке.
        """
        revenue_group = df9001.groupby('ном_группа', as_index=False)[
            ['выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']
        ].sum().rename(columns={
            'выручка_без_ндс_тыс_ед': 'выручка_группы_ед',
            'выручка_без_ндс_тыс_руб': 'выручка_группы_руб',
        })
        df_alloc = df_alloc.merge(revenue_group, on='ном_группа', how='left')
        rev_group_pos = df_alloc['выручка_группы_ед'] > 0
        df_alloc['доля_группы'] = np.where(
            rev_group_pos,
            df_alloc['выручка_без_ндс_тыс_ед'] / df_alloc['выручка_группы_ед'],
            0.0,
        )

        grp_cost = df_alloc['ном_группа'].map(group_pot['кост_остаток']).fillna(0)
        grp_cost_rub = df_alloc['ном_группа'].map(group_pot['кост_остаток_руб']).fillna(0)
        df_alloc['кост_группы'] = grp_cost * df_alloc['доля_группы']
        df_alloc['кост_группы_руб'] = grp_cost_rub * df_alloc['доля_группы']

        df_alloc['Итоговая_себестоимость'] = df_alloc['кост_прямой'] + df_alloc['кост_группы']
        df_alloc['Итоговая_себестоимость_руб'] = (
            df_alloc['кост_прямой_руб'] + df_alloc['кост_группы_руб']
        )

        return df_alloc, revenue_group

    def _find_orphan_cost_groups(
        self,
        group_pot: pd.DataFrame,
        revenue_group: pd.DataFrame,
    ) -> pd.DataFrame:
        """Группы, у которых остался кост без выручки, — контрагент '3 лица'."""
        return group_pot[
            ~group_pot.index.isin(revenue_group['ном_группа'])
            | (
                revenue_group.set_index('ном_группа')['выручка_группы_ед']
                .reindex(group_pot.index).fillna(0) == 0
            )
        ]

    def _finalize_distribution(
        self,
        df_alloc: pd.DataFrame,
        orphans: pd.DataFrame,
    ) -> pd.DataFrame:
        """Формирует итог: строки по контрагентам + сироты по '3 лица', сводит по
        (контрагент, ном_группа) и переименовывает колонки себестоимости.
        """
        df_result = df_alloc[['контрагент', 'ном_группа',
                              'выручка_без_ндс_тыс_ед', 'Итоговая_себестоимость',
                              'выручка_без_ндс_тыс_руб', 'Итоговая_себестоимость_руб']]

        if not orphans.empty:
            # Столбец из python-списка получает 'object', а pd.concat(string,
            # object) в pandas 2.x понижает итог до 'object' — валидация выхода
            # шага требует 'string' (регрессия 14.09.2026:
            # journal_df['контрагент'] = object). Типы выравниваем по
            # df_result ДО конкатенации, а не переприводим после.
            df_orphans = align_dtypes_to_reference(
                pd.DataFrame({
                    'контрагент': ['3 лица'] * len(orphans),
                    'ном_группа': orphans.index.to_numpy(),
                    'выручка_без_ндс_тыс_ед': 0.0,
                    'Итоговая_себестоимость': orphans['кост_остаток'].to_numpy(),
                    'выручка_без_ндс_тыс_руб': 0.0,
                    'Итоговая_себестоимость_руб': orphans['кост_остаток_руб'].to_numpy(),
                }),
                df_result,
                columns=['контрагент', 'ном_группа'],
            )
            df_result = pd.concat([df_result, df_orphans], ignore_index=True)

        df_result = df_result.groupby(['контрагент', 'ном_группа'], as_index=False)[
            ['выручка_без_ндс_тыс_ед', 'Итоговая_себестоимость',
             'выручка_без_ндс_тыс_руб', 'Итоговая_себестоимость_руб']
        ].sum()

        return df_result.rename(columns={
            'Итоговая_себестоимость': 'себестоимость_тыс_ед',
            'Итоговая_себестоимость_руб': 'себестоимость_тыс_руб',
        })
