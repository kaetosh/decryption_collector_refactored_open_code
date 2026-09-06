"""
Mixin с обработкой счетов 90.01/90.02 для Шага 14.

Методы:
    _process_revenue_9001: обработка выручки (90.01)
    _validate_revenue_against_osv: сверка выручки с ОСВ
    _process_cost_9002: обработка себестоимости (90.02)
    _process_reassessment: обработка переоценки (Дт 90.02 Кт 16)
    _validate_cost_against_osv: сверка себестоимости с ОСВ
    _distribute_cost_to_buyers: распределение себестоимости пропорционально выручке
"""
from typing import Tuple

import numpy as np
import pandas as pd
from loguru import logger

from pipeline.errors import MissingMappingError, ReferenceMismatchError


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

        df9001['ндс_ставка'] = (
            df9001['Субконто Кт_2']
            .astype(str)
            .str.replace('%', '', regex=False)
            .str.strip()
            .replace('', pd.NA)
        )
        df9001['ндс_ставка'] = pd.to_numeric(df9001['ндс_ставка'], errors='coerce') / 100

        df9001['выручка_без_ндс_тыс_ед'] = (df9001['Сумма'] / 1000) / (1 + df9001['ндс_ставка'])
        df9001['выручка_без_ндс_тыс_руб'] = (df9001['Сумма_руб'] / 1000) / (1 + df9001['ндс_ставка'])

        df9001 = df9001.loc[:, ['Документ', 'контрагент', 'ном_группа', 'выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']]
        df9001 = df9001.groupby(
            ['Документ', 'контрагент', 'ном_группа'],
            as_index=False
        )[['выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']].sum()

        self._validate_revenue_against_osv(df9001, osv_df, context)

        logger.debug("Выручка обработана: {} строк", len(df9001))

        return df9001

    def _validate_revenue_against_osv(
        self,
        df9001: pd.DataFrame,
        osv_df: pd.DataFrame,
        context,
    ) -> None:
        """Проверяет сходимость выручки с общей ОСВ."""
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
            raise ValueError(
                f"Выручка из отчёта по проводкам ({revenue_from_df9001:,.2f} тыс.ед.) "
                f"отличается от общей ОСВ ({revenue_without_vat:,.2f} тыс.ед.) "
                f"на {difference:,.2f} тыс.ед. (допуск: {context.tolerance_params['tolerance_reconciliation']})"
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
            raise ValueError(
                f"Себестоимость из отчёта по проводкам ({cost_price_from_df9002:,.2f} тыс.ед.) "
                f"отличается от общей ОСВ ({cost_price_osv_9002:,.2f} тыс.ед.) "
                f"на {difference:,.2f} тыс.ед. (допуск: {context.tolerance_params['tolerance_reconciliation']})"
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
        """Распределяет себестоимость на контрагентов пропорционально выручке."""
        logger.debug("Распределение себестоимости на контрагентов")

        df_merged = df9001.merge(df9002, on=['Документ', 'ном_группа'], how='outer')

        mask_empty = df_merged['контрагент'].isna() | (df_merged['контрагент'] == '')

        df_buyers = df_merged[~mask_empty].copy()

        revenue_sum = df_buyers.groupby('ном_группа')['выручка_без_ндс_тыс_ед'].transform('sum')
        revenue_sum_rub = df_buyers.groupby('ном_группа')['выручка_без_ндс_тыс_руб'].transform('sum')

        cost_to_distribute = df_merged[mask_empty].groupby('ном_группа')['себестоимость_тыс_ед'].sum()
        cost_to_distribute_rub = df_merged[mask_empty].groupby('ном_группа')['себестоимость_тыс_руб'].sum()

        df_buyers['затраты_группы'] = df_buyers['ном_группа'].map(cost_to_distribute).fillna(0)
        df_buyers['затраты_группы_руб'] = df_buyers['ном_группа'].map(cost_to_distribute_rub).fillna(0)

        ratio = np.where(revenue_sum > 0, df_buyers['затраты_группы'] / revenue_sum, 0)
        ratio_rub = np.where(revenue_sum_rub > 0, df_buyers['затраты_группы_руб'] / revenue_sum_rub, 0)

        df_buyers['Итоговая_себестоимость'] = (
            df_buyers['себестоимость_тыс_ед'].fillna(0) +
            df_buyers['выручка_без_ндс_тыс_ед'] * ratio
        )
        df_buyers['Итоговая_себестоимость_руб'] = (
            df_buyers['себестоимость_тыс_руб'].fillna(0) +
            df_buyers['выручка_без_ндс_тыс_руб'] * ratio_rub
        )

        df_result = df_buyers.drop(columns=['затраты_группы', 'затраты_группы_руб']).reset_index(drop=True)

        df_result = df_result.groupby(
            ['контрагент', 'ном_группа']
        )[['выручка_без_ндс_тыс_ед', 'Итоговая_себестоимость', 'выручка_без_ндс_тыс_руб', 'Итоговая_себестоимость_руб']].sum().reset_index()

        df_result = df_result.rename(columns={
            'Итоговая_себестоимость': 'себестоимость_тыс_ед',
            'Итоговая_себестоимость_руб': 'себестоимость_тыс_руб',
        })

        distributed_count = mask_empty.sum()
        logger.debug(
            "Распределено {} строк себестоимости без покупателей на {} строк с покупателями",
            distributed_count,
            len(df_result),
        )

        return df_result
