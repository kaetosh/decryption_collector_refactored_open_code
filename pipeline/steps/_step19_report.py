"""
Mixin с финальной сборкой отчёта ОПУ для Шага 19.

Содержит:
- _finalize_columns: упорядочивание столбцов (удаление служебных, перенос в конец)
- _build_opu_report: формирование итоговой расшифровки ОПУ
- _empty_opu_report: пустой отчёт с корректными типами
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext
from utils import needs_conversion


class Step19ReportMixin:
    """Миксин сборки итогового отчёта ОПУ для Шага 19."""

    def _finalize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Удаляет DROP_COLUMNS и переносит TAIL_COLUMNS в конец после 'Значение'.

        Устойчиво к отсутствию столбцов: удаляются/переносятся
        только фактически присутствующие столбцы.

        Внимание: swap 'Итоговый номер счета' / 'РСБУ Код отчетности' здесь
        не выполняется, потому что 'Итоговый номер счета' уходит в индекс
        при set_index() в _build_opu_report. Swap делается в
        data_io._prepare_pnl_df() после reset_index().
        """
        df = df.drop(columns=[c for c in self.DROP_COLUMNS if c in df.columns])

        tail = [c for c in self.TAIL_COLUMNS if c in df.columns]
        if not tail:
            return df

        rest = [c for c in df.columns if c not in tail]
        value_cols = [c for c in (self.VALUE_COL, self.RUB_VALUE_COL) if c in rest]
        for c in value_cols:
            rest.remove(c)
        rest.extend(value_cols)

        return df[rest + tail]

    def _build_opu_report(self, journal_df: pd.DataFrame, chart_of_accounts_df: pd.DataFrame, context: ProcessingContext) -> pd.DataFrame:
        """
        Формирует итоговую расшифровку ОПУ:
        - берет из плана счетов строки с отчетностью 'ОПУ';
        - агрегирует обороты по счет_фо;
        - разносит суммы по итоговым номерам счетов;
        - удаляет нулевые строки.
        """
        self._validate_columns(
            chart_of_accounts_df,
            (self.REPORT_TYPE_COL, self.ACCOUNT_COL),
            "плане счетов ФО",
        )

        chart_df = chart_of_accounts_df.copy()

        report_type = self._to_clean_string(chart_df[self.REPORT_TYPE_COL])
        opu_mask = report_type == self.OPU_REPORT

        opu_template = chart_df.loc[opu_mask].copy()

        if opu_template.empty:
            logger.warning(
                "В плане счетов не найдены строки с отчетностью '{}'.",
                self.OPU_REPORT,
            )
            return self._empty_opu_report()

        journal_target = journal_df[self.TARGET_ACCOUNT_COL].copy()
        template_account = opu_template[self.ACCOUNT_COL].copy()

        journal_target, template_account = self._align_series_dtypes(journal_target, template_account)

        grouped = journal_df.assign(**{self.GROUP_ACCOUNT_COL: journal_target.array})
        sums_by_account = grouped.groupby(self.GROUP_ACCOUNT_COL, dropna=False)[self.AMOUNT_COL].sum()

        if needs_conversion(context) and self.RUB_AMOUNT_COL in journal_df.columns:
            sums_by_account_rub = grouped.groupby(self.GROUP_ACCOUNT_COL, dropna=False)[self.RUB_AMOUNT_COL].sum()
        else:
            sums_by_account_rub = None

        opu_template[self.ACCOUNT_COL] = template_account.array

        duplicated_accounts = int(opu_template[self.ACCOUNT_COL].duplicated().sum())
        if duplicated_accounts:
            raise ValueError(
                f"В плане счетов найдено дублей по столбцу '{self.ACCOUNT_COL}': {duplicated_accounts}."
            )

        opu_template = opu_template.set_index(self.ACCOUNT_COL)

        mapped_values = pd.Series(
            opu_template.index.map(sums_by_account),
            index=opu_template.index,
        )

        opu_template[self.VALUE_COL] = mapped_values.fillna(0.0).astype("float64").array

        if sums_by_account_rub is not None:
            mapped_values_rub = pd.Series(
                opu_template.index.map(sums_by_account_rub),
                index=opu_template.index,
            )
            opu_template[self.RUB_VALUE_COL] = mapped_values_rub.fillna(0.0).astype("float64").array
        opu_template = opu_template[opu_template[self.VALUE_COL] != 0]

        opu_template = self._ensure_clean_dtypes(opu_template)
        opu_template = self._finalize_columns(opu_template)

        return opu_template

    def _empty_opu_report(self) -> pd.DataFrame:
        """
        Возвращает пустой ОПУ с корректными типами:
        - индекс: string;
        - значение: float64.
        """
        empty = pd.DataFrame({self.VALUE_COL: pd.Series(dtype="float64")})
        empty.index = pd.Index(pd.array([], dtype="string"), name=self.ACCOUNT_COL)
        return empty
