"""
Шаг 19: Финальная сборка расшифровки ОПУ.

Содержит только главный метод _process.
Вся логика вынесена в миксины:
- _step19_base — константы и валидация структуры
- _step19_validation — приведение типов, чистка околонулевых строк, сверка ЧП
- _step19_mapping — маппинг ОПУ (простановка счет_фо)
- _step19_report — финальная сборка отчёта
"""
from __future__ import annotations

from loguru import logger

from pipeline.base import ProcessingContext
from pipeline.steps._step19_base import Step19BaseMixin
from pipeline.steps._step19_validation import Step19ValidationMixin
from pipeline.steps._step19_mapping import Step19MappingMixin
from pipeline.steps._step19_report import Step19ReportMixin


class Step19BuildOpuStep(
    Step19BaseMixin,
    Step19ValidationMixin,
    Step19MappingMixin,
    Step19ReportMixin,
):
    """
    Шаг 19: собирает расшифровку ОПУ, контролирует увязку с балансом
    и формирует итоговую таблицу ОПУ.

    Особое внимание уделено типам данных:
    - числовые колонки остаются числовыми;
    - текстовые колонки приводятся к StringDtype;
    - object dtype в создаваемых/обрабатываемых данных не используется.
    """

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Начало финальной сборки расшифровки ОПУ")

        journal_df = context.journal_df.copy()

        self._prepare_amount_column(journal_df)
        journal_df = self._drop_zero_amount_rows(journal_df)
        self._check_profit_vs_balance(context.balance_df, journal_df, context)

        mapping_ref = self._get_reference(context, self.MAPPING_REF)
        journal_df = self._apply_opu_mapping(journal_df, mapping_ref)
        self._ensure_all_rows_mapped(journal_df)

        journal_df = self._ensure_clean_dtypes(journal_df)

        context.journal_df = journal_df

        chart_ref = self._get_reference(context, self.CHART_OF_ACCOUNTS_REF)
        context.pnl_df = self._build_opu_report(journal_df, chart_ref, context)

        logger.info("[OK] Собрана расшифровка ОПУ: {} строк отчетности", len(context.pnl_df))

        return context
