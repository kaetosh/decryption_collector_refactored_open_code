"""
Шаг 17: Добавление прочих доходов и расходов в расшифровку ОПУ (счета 91.01/91.02)

Обработка:
- Продажа активов (запасы, ОС): корректировка на НДС, подтягивание контрагентов,
  распределение осиротевших расходов
- Кредитные линии: подтягивание контрагентов из справочника КредитОбслуж
- Процентные расходы/доходы: подтягивание контрагентов
- Изменение условий ППА: подтягивание контрагентов из справочника ППА
- Определение группа_ка, сегмент_ка, вид_связи для всех строк

Вся логика вынесена в миксины:
- _step17_base — константы класса
- _step17_data — загрузка данных и фильтрация
- _step17_processing — бизнес-обработка (ППА, продажа активов, кредитные линии)
- _step17_fx — курсовые разницы
- _step17_merge — финальное слияние с main_df
"""
import numpy as np
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.steps._step17_base import Step17BaseMixin
from pipeline.steps._step17_data import Step17DataMixin
from pipeline.steps._step17_processing import Step17ProcessingMixin
from pipeline.steps._step17_fx import Step17FXMixin
from pipeline.steps._step17_merge import Step17MergeMixin


class Step17AddOtherIncomeExpensesToOpuStep(
    Step17BaseMixin,
    Step17DataMixin,
    Step17ProcessingMixin,
    Step17FXMixin,
    Step17MergeMixin,
):
    """
    Шаг 17: Обработка прочих доходов и расходов (счета 91.01/91.02).

    Добавляет в расшифровку ОПУ:
     - Прочие доходы (91.01)
     - Прочие расходы (91.02)

    с детализацией по контрагентам и видам связи.
    """

    def __init__(self):
        super().__init__(
            name="Шаг 17: Прочие доходы и расходы (91.01/91.02)",
            description=(
                "Добавление прочих доходов и расходов "
                "с детализацией по контрагентам и видам связи"
            )
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Начало обработки прочих доходов и расходов")

        name_company = context.company

        osv_df, transactions_all_df = self._load_data_from_context(context)

        refs = self._load_references(name_company, context)

        df_9101, df_9102 = self._filter_91_transactions(
            transactions_all_df,
            refs['mapping_opu']
        )

        segment_company = refs['companies'].loc[
            refs['companies']['сокращенное_наименование_компании'] == name_company,
            'сегмент'
        ].iloc[0]

        df_9101['сегмент'] = segment_company
        df_9102['сегмент'] = segment_company
        df_9101['сегмент'] = df_9101['сегмент'].astype('string')
        df_9102['сегмент'] = df_9102['сегмент'].astype('string')

        logger.debug(
            "Создан столбец 'сегмент': 91.01 unique={}, 91.02 unique={}",
            df_9101['сегмент'].unique().tolist(),
            df_9102['сегмент'].unique().tolist(),
        )

        self._log_rate_anomalies(df_9101, df_9102, context, 'вход шага 17: после фильтрации')

        df_9101 = self._extract_contractors(
            df_9101,
            is_income=True,
            accounts_with_contractors=refs['accounts_with_contractors']
        )
        df_9102 = self._extract_contractors(
            df_9102,
            is_income=False,
            accounts_with_contractors=refs['accounts_with_contractors']
        )

        df_9101, df_9102 = self._process_ppa(
            df_9101, df_9102, refs['ppa'], name_company
        )

        df_9101, df_9102 = self._process_asset_sales(
            df_9101, df_9102, asset_sale_types=refs['asset_sale_types']
        )

        df_9101 = self._process_credit_lines(
            df_9101, refs['credit'], is_income=True, company_name=name_company
        )
        df_9102 = self._process_credit_lines(
            df_9102, refs['credit'], is_income=False, company_name=name_company
        )

        group_unique = refs['group_companies'].drop_duplicates(subset='ВариантыНазвания')

        mapping_group = (
            group_unique
            .set_index('ВариантыНазвания')['ВидСвязиКА']
            .astype('string')
        )
        mapping_segment_ka = (
            group_unique
            .set_index('ВариантыНазвания')['сегмент']
            .astype('string')
        )

        segment_company = refs['companies'].loc[
            refs['companies']['сокращенное_наименование_компании'] == name_company,
            'сегмент'
        ].iloc[0]

        logger.debug(
            "segment_company = '{}', type = {}, isna = {}",
            segment_company,
            type(segment_company),
            pd.isna(segment_company),
        )

        df_9101 = self._enrich_with_connection_info(
            df_9101, mapping_group, mapping_segment_ka, segment_company
        )

        logger.debug(
            "df_9101['сегмент'] unique = {}, NA count = {}",
            df_9101['сегмент'].unique(),
            df_9101['сегмент'].isna().sum(),
        )

        df_9102 = self._enrich_with_connection_info(
            df_9102, mapping_group, mapping_segment_ka, segment_company
        )

        self._log_rate_anomalies(df_9101, df_9102, context, 'выход шага 17: перед агрегацией')

        self._prepare_fx_columns(df_9101, df_9102, context)

        df_9101 = self._add_service_columns(df_9101, self.ACCOUNT_OTHER_INCOME)
        df_9102 = self._add_service_columns(df_9102, self.ACCOUNT_OTHER_EXPENSE)

        df_final = self._merge_with_main_df(
            context.journal_df,
            df_9101,
            df_9102,
            context,
        )

        context.journal_df = df_final

        logger.info(
            "[OK] Прочие доходы и расходы добавлены: 91.01 — {} позиций, 91.02 — {} позиций",
            len(df_9101),
            len(df_9102),
        )

        return context
