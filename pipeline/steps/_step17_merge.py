"""
Mixin с финальной сборкой для Шага 17.

Методы:
    _add_service_columns: добавление служебных столбцов, удаление технических
    _merge_with_main_df: объединение 91.01/91.02 с main_df
"""
import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext


class Step17MergeMixin:
    """Миксин слияния с main_df для Шага 17."""

    def _add_service_columns(self, df: pd.DataFrame, account: str) -> pd.DataFrame:
        """Добавляет служебные столбцы для соответствия структуре main_df."""
        if 'счет' not in df.columns:
            df['счет'] = account
            df['счет'] = df['счет'].astype('string')
        else:
            df['счет'] = df['счет'].astype('string')

        cols_to_drop = [
            'Имя_файла',
            'Дата',
            'Сумма',
            'Содержание_1',
            'Содержание_2',
            'Субконто Дт_1',
            'Субконто Дт_2',
            'Субконто Дт_3',
            'Субконто Кт_1',
            'Субконто Кт_2',
            'Субконто Кт_3',
            'Дт',
            'Кт',
            'Корр.счет',
        ]

        df = df.drop(
            columns=[c for c in cols_to_drop if c in df.columns],
            errors='ignore'
        )

        return df

    def _merge_with_main_df(
        self,
        main_df: pd.DataFrame,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        context: ProcessingContext,
    ) -> pd.DataFrame:
        """
        Объединяет 91.01 и 91.02 с основной расшифровкой ОПУ.

        При наличии служебных столбцов _руб_баз / _fx (см. _prepare_fx_columns)
        выполняет выделение курсовых разниц в отдельные строки «Курсовые разницы».
        """
        logger.debug("Объединение с основной расшифровкой ОПУ")

        df_combined = pd.concat([df_9101, df_9102], ignore_index=True)

        group_cols = list(self.GROUP_COLS)
        existing_group_cols = [
            col for col in group_cols if col in df_combined.columns
        ]

        for col in existing_group_cols:
            df_combined[col] = df_combined[col].fillna('не_указано').astype('string')

        sum_cols = ['оборот, тыс.ед.', 'оборот, тыс.руб.']
        has_fx = (
            self.RUB_BASE_COL in df_combined.columns
            and self.FX_COL in df_combined.columns
        )
        if has_fx:
            sum_cols.extend([self.RUB_BASE_COL, self.FX_COL])

        df_combined = df_combined.groupby(
            existing_group_cols,
            as_index=False
        )[sum_cols].sum()

        logger.debug(
            "Группировка: {} → {} строк",
            len(df_9101) + len(df_9102),
            len(df_combined),
        )

        if has_fx:
            df_combined = self._split_fx_difference_rows(df_combined, context)

        if has_fx:
            df_combined = df_combined.drop(
                columns=[self.RUB_BASE_COL, self.FX_COL],
                errors='ignore',
            )

        df_final = pd.concat([main_df, df_combined], ignore_index=True)

        text_cols = [
            'счет',
            'контрагент',
            'ном_группа',
            'доход_расход',
            'вид_дохода_расхода',
            'сегмент',
            'группа_ка',
            'сегмент_ка',
            'вид_связи',
            'объект для изм ппа',
            'рбп_кредитные_линии',
            'рбп_проценты'
        ]

        for col in text_cols:
            if col in df_final.columns:
                df_final[col] = df_final[col].astype('string')

        logger.debug(
            "Объединение завершено: {} + {} = {} строк",
            len(main_df),
            len(df_combined),
            len(df_final),
        )

        return df_final
