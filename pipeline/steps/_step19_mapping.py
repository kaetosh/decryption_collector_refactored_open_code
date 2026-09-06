"""
Mixin с маппингом ОПУ для Шага 19.

Содержит:
- _apply_opu_mapping: простановка счет_фо по меппингу
- _prepare_aligned_key_frames: выравнивание ключей журнала и маппинга
- _prepare_mapping_frame: подготовка справочника к join
- _get_old_target_values: получение старых значений счет_фо
- _ensure_all_rows_mapped: контроль полноты маппинга
- _get_problem_columns: столбцы для выгрузки проблемных данных
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

from pipeline.errors import MissingMappingError


class Step19MappingMixin:
    """Миксин маппинга ОПУ для Шага 19."""

    def _apply_opu_mapping(self, journal_df: pd.DataFrame, mapping_ref: pd.DataFrame) -> pd.DataFrame:
        """
        Проставляет счета финансового/управленческого учета по маппингу ОПУ.

        Логика:
        1. Берем ключи из журнала.
        2. Выравниваем типы ключей журнала и маппинга.
        3. Джуиним справочник маппинга.
        4. Если маппинг дал значение — используем его.
        5. Если маппинг пустой — оставляем старое значение счет_фо, если оно было.
        """
        if journal_df.empty:
            if self.TARGET_ACCOUNT_COL not in journal_df.columns:
                journal_df[self.TARGET_ACCOUNT_COL] = pd.Series(
                    pd.NA,
                    index=journal_df.index,
                    dtype="string",
                )
            return journal_df

        key_cols = list(self.MAPPING_KEY_COLS)

        self._validate_columns(journal_df, key_cols, "журнале ОПУ")
        self._validate_columns(
            mapping_ref,
            (*key_cols, self.TARGET_ACCOUNT_COL),
            "справочнике маппинга ОПУ",
        )

        journal_keys, mapping_keys = self._prepare_aligned_key_frames(journal_df, mapping_ref)
        mapping_df = self._prepare_mapping_frame(mapping_ref, mapping_keys)

        source_df = journal_keys.assign(**{self.ROW_ID_COL: range(len(journal_df))})

        mapped_df = (
            source_df.merge(
                mapping_df,
                on=key_cols,
                how="left",
                validate="m:1",
            )
            .sort_values(self.ROW_ID_COL)
        )

        mapped_series = mapped_df["_mapped_account"].reset_index(drop=True)
        old_series = self._get_old_target_values(journal_df).reset_index(drop=True)

        mapped_series, old_series = self._align_series_dtypes(mapped_series, old_series)

        new_values = mapped_series.where(mapped_series.notna(), old_series)
        journal_df[self.TARGET_ACCOUNT_COL] = new_values.array

        return journal_df

    def _prepare_aligned_key_frames(
        self,
        journal_df: pd.DataFrame,
        mapping_ref: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """
        Готовит ключевые столбцы журнала и маппинга с согласованными типами.
        """
        key_cols = list(self.MAPPING_KEY_COLS)

        journal_keys = journal_df.loc[:, key_cols].copy()
        mapping_keys = mapping_ref.loc[:, key_cols].copy()

        for col in key_cols:
            left_aligned, right_aligned = self._align_series_dtypes(
                journal_keys[col],
                mapping_keys[col],
            )

            journal_keys[col] = left_aligned.array
            mapping_keys[col] = right_aligned.array

        return journal_keys, mapping_keys

    def _prepare_mapping_frame(self, mapping_ref: pd.DataFrame, mapping_keys: pd.DataFrame) -> pd.DataFrame:
        """
        Готовит справочник маппинга к join:
        - использует уже выровненные ключи;
        - приводит целевой счет к корректному типу;
        - удаляет дубли ключей;
        - при наличии дублей отдает приоритет строке с заполненным счетом.
        """
        key_cols = list(self.MAPPING_KEY_COLS)

        mapping_df = mapping_keys.copy()
        mapping_df[self.TARGET_ACCOUNT_COL] = self._clean_series(mapping_ref[self.TARGET_ACCOUNT_COL]).array

        mapping_df = mapping_df.assign(__has_target__=mapping_df[self.TARGET_ACCOUNT_COL].notna())

        mapping_df = (
            mapping_df.sort_values("__has_target__", ascending=False)
            .drop_duplicates(subset=key_cols, keep="first")
            .rename(columns={self.TARGET_ACCOUNT_COL: "_mapped_account"})
        )

        return mapping_df.loc[:, [*key_cols, "_mapped_account"]]

    def _get_old_target_values(self, df: pd.DataFrame):
        """
        Возвращает старые значения счет_фо в корректном типе.
        """
        if self.TARGET_ACCOUNT_COL not in df.columns:
            return pd.Series(pd.NA, index=df.index, dtype="string")

        return self._clean_series(df[self.TARGET_ACCOUNT_COL])

    def _ensure_all_rows_mapped(self, journal_df: pd.DataFrame) -> None:
        """
        Проверяет, что для всех строк ОПУ определен счет_фо.

        Если есть незамапленные строки:
        - формирует problem_data;
        - считает уникальные незамапленные комбинации;
        - выбрасывает MissingMappingError.
        """
        if journal_df.empty:
            logger.info("Журнал ОПУ пуст, проверка маппинга пропущена.")
            return

        if self.TARGET_ACCOUNT_COL not in journal_df.columns:
            journal_df[self.TARGET_ACCOUNT_COL] = pd.Series(
                pd.NA,
                index=journal_df.index,
                dtype="string",
            )
        else:
            journal_df[self.TARGET_ACCOUNT_COL] = self._clean_series(journal_df[self.TARGET_ACCOUNT_COL]).array

        unmatched_mask = journal_df[self.TARGET_ACCOUNT_COL].isna().to_numpy()
        unmatched_count = int(unmatched_mask.sum())
        total_count = len(journal_df)

        logger.info(
            "Найдено совпадений: {} из {}",
            total_count - unmatched_count,
            total_count,
        )
        logger.info(
            "Не найдено (NaN): {} ({:.1%})",
            unmatched_count,
            unmatched_count / total_count,
        )

        if unmatched_count == 0:
            return

        journal_df[self.TARGET_ACCOUNT_COL] = self._to_clean_string(journal_df[self.TARGET_ACCOUNT_COL]).array
        journal_df[self.TARGET_ACCOUNT_COL] = journal_df[self.TARGET_ACCOUNT_COL].fillna(self.UNMAPPED_ACCOUNT)

        problem_columns = self._get_problem_columns(journal_df)
        problem_data = journal_df.loc[unmatched_mask, problem_columns].copy()
        problem_data = self._ensure_clean_dtypes(problem_data)

        detail_columns = [col for col in self.PROBLEM_DETAIL_COLS if col in problem_data.columns]
        if not detail_columns:
            detail_columns = list(self.MAPPING_KEY_COLS)

        unmapped_unique = problem_data[detail_columns].drop_duplicates()

        raise MissingMappingError(
            message=(
                "НЕ ВСЕ позиции соответствуют Меппингу опу. "
                f"Найдено {len(unmapped_unique)} уникальных незамапленных комбинаций"
            ),
            problem_data=problem_data,
            reference_name="Меппинг ОПУ",
            unique_combinations_count=len(unmapped_unique),
            total_unmapped_rows=len(problem_data),
        )

    def _get_problem_columns(self, df: pd.DataFrame) -> list[str]:
        """
        Возвращает доступные столбцы для выгрузки проблемных данных.
        """
        ordered_columns = [*self.PROBLEM_DETAIL_COLS, self.TARGET_ACCOUNT_COL, self.AMOUNT_COL]
        return list(dict.fromkeys(col for col in ordered_columns if col in df.columns))
