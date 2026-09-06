"""
Mixin с загрузкой данных для Шага 14.

Методы:
    _load_transactions: загрузка отчёта по проводкам из файла
    _get_osv_from_context: получение общей ОСВ из контекста
    _load_reference_data: загрузка справочников (УФР, МеппингОПУ, ВидСвязиКА, КомпанииГруппы)
"""
from typing import Tuple

import pandas as pd
from loguru import logger

from io_module import DataLoader


class Step14DataMixin:
    """Миксин загрузки данных для Шага 14."""

    def _load_transactions(self) -> pd.DataFrame:
        """Загружает общий отчёт по проводкам."""
        logger.debug("Загрузка отчёта по проводкам")
        transactions_all_df = DataLoader.load_transaction_report()
        transactions_all_df = self.clean_whitespace(transactions_all_df)

        logger.debug(
            "Загружено {} проводок, {} уникальных Кт счетов",
            len(transactions_all_df),
            transactions_all_df['Кт'].nunique(),
        )

        return transactions_all_df

    def _get_osv_from_context(self, context) -> pd.DataFrame:
        """Получает общую ОСВ из context."""
        osv_df = self.get_df_from_context(
            context,
            'osv',
            hint="Убедитесь, что предыдущие шаги (1-13) выполнены успешно.",
        )

        return osv_df.copy()

    def _load_reference_data(
        self,
        name_company: str,
        context,
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Загружает все необходимые справочники.

        Returns:
            Tuple[mapping_opu_df, directory_ufr_df, group_companies_df, company_directory_df]
        """
        logger.debug("Загрузка справочников")

        directory_ufr_df = context.references['справочник_уфр']

        directory_ufr_df = directory_ufr_df.loc[
            directory_ufr_df["сокращенное_наименование_компании"] == name_company
        ]

        mapping_opu_df = context.references['меппинг_опу']

        group_companies_df = context.references['вид_связи_ка']

        company_directory_df = context.references['компании_группы']

        logger.debug(
            "Загружено: УФР={}, МеппингОПУ={}, ВидСвязиКА={}, КомпанииГруппы={}",
            len(directory_ufr_df),
            len(mapping_opu_df),
            len(group_companies_df),
            len(company_directory_df),
        )

        return mapping_opu_df, directory_ufr_df, group_companies_df, company_directory_df
