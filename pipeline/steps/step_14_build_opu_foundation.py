"""
Шаг 14: Формирование основы расшифровки ОПУ — выручка и себестоимость (90.01/90.02)

Создаёт первичную таблицу расшифровки ОПУ, которая будет
дополняться данными в последующих шагах:
- Шаг 15: коммерческие расходы (90.07)
- Шаг 16: управленческие расходы (90.08)
- Шаг 17: прочие доходы/расходы (91.01/91.02)
- Шаг 18: налог на прибыль (99)
"""
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.steps._step14_base import Step14BaseMixin
from pipeline.steps._step14_data import Step14DataMixin
from pipeline.steps._step14_accounts import Step14AccountsMixin
from pipeline.steps._step14_transform import Step14TransformMixin
from utils import add_ruble_amount_column


class Step14BuildOpuFoundationStep(
    Step14BaseMixin,
    Step14DataMixin,
    Step14AccountsMixin,
    Step14TransformMixin,
    Step,
):
    """
    Шаг 14: Формирование основы расшифровки ОПУ —
    сбор выручки и себестоимости из проводок 90.01/90.02.
    """

    def __init__(self):
        super().__init__(
            name="Шаг 14: Формирование основы расшифровки ОПУ — выручка и себестоимость (90.01/90.02)",
            description="Создание первичной таблицы ОПУ из отчёта по проводкам 90 счета"
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки."""
        logger.debug("Начало формирования основы расшифровки ОПУ")

        name_company = context.company

        # 1. Загрузка и подготовка данных
        transactions_all_df = context.journal_df.copy()
        osv_df = context.common_osv_df

        transactions_all_df = add_ruble_amount_column(transactions_all_df, context)

        # 2. Загрузка справочников
        mapping_opu_df, directory_ufr_df, group_companies_df, company_directory_df = self._load_reference_data(name_company, context)

        context.data['transactions_all_df'] = transactions_all_df

        # 3. Обработка выручки (90.01)
        df9001 = self._process_revenue_9001(transactions_all_df, osv_df, context)

        # 4. Обработка себестоимости (90.02) с выделением переоценки
        df9002, df9002_16 = self._process_cost_9002(
            transactions_all_df, osv_df, name_company, company_directory_df, context
        )

        # 5. Распределение себестоимости на контрагентов
        df_result = self._distribute_cost_to_buyers(df9001, df9002)

        # 6. Преобразование в длинный формат (melt)
        df_result = self._reshape_to_long_format(df_result)

        # 7. Обогащение данными из справочников
        df_result = self._enrich_with_mappings(
            df_result, mapping_opu_df, directory_ufr_df, group_companies_df
        )

        # 8. Объединение с переоценкой (df9002_16)
        df_final = self._merge_with_reassessment(df_result, df9002_16)

        context.journal_df = df_final

        account_counts = df_final['счет'].value_counts().to_dict()
        logger.info(
            "[OK] Основа ОПУ сформирована: {} строк ({})",
            len(df_final),
            ', '.join(f'{k}: {v}' for k, v in sorted(account_counts.items(), key=lambda x: -x[1]))
        )

        return context
