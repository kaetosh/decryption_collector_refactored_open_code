"""
Mixin с валидацией и управлением типами данных для Шага 19.

Содержит:
- Приведение серий к чистым типам (_clean_series, _normalize_numeric_series, ...)
- Удаление object-колонок (_ensure_clean_dtypes)
- Приведение столбца 'оборот, тыс.ед.' к numeric (_prepare_amount_column)
- Удаление околонулевых строк (_drop_zero_amount_rows)
- Сверку ЧП vs НРП (_check_profit_vs_balance, _get_retained_earnings)
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext
from pipeline.step_config import OpuReportConstants


class Step19ValidationMixin:
    """Миксин валидации и управления типами для Шага 19."""

    def _is_numeric_non_bool(self, series: pd.Series) -> bool:
        """
        True для числовых серий, кроме boolean.
        """
        return pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)

    def _clean_series(self, series: pd.Series) -> pd.Series:
        """
        Приводит серию к аккуратному типу:
        - числовые серии остаются числовыми;
        - остальные серии становятся string.
        """
        if self._is_numeric_non_bool(series):
            return self._normalize_numeric_series(series)

        return self._to_clean_string(series)

    def _normalize_numeric_series(self, series: pd.Series) -> pd.Series:
        """
        Приводит числовую серию к nullable numeric dtype:
        - float с целыми значениями может быть приведен к Int64;
        - обычные float остаются Float64;
        - integer приводится к Int64.
        """
        if pd.api.types.is_float_dtype(series):
            non_null = series.dropna()

            if not non_null.empty:
                try:
                    if (non_null % 1 == 0).all():
                        return self._safe_cast_numeric(series, "Int64")
                except (TypeError, ValueError, OverflowError):
                    pass

            return series.astype("Float64")

        if pd.api.types.is_integer_dtype(series):
            return self._safe_cast_numeric(series, "Int64")

        return series.astype("Float64")

    def _safe_cast_numeric(self, series: pd.Series, dtype: str) -> pd.Series:
        """
        Безопасно приводит числовую серию к целевому numeric dtype.
        При ошибке использует Float64.
        """
        try:
            return series.astype(dtype)
        except (TypeError, ValueError, OverflowError):
            try:
                return series.astype("Float64")
            except (TypeError, ValueError, OverflowError):
                return pd.to_numeric(series, errors="coerce").astype("Float64")

    def _common_numeric_dtype(self, left: pd.Series, right: pd.Series) -> str:
        """
        Возвращает общий числовой dtype для двух числовых серий.
        """
        if pd.api.types.is_float_dtype(left) or pd.api.types.is_float_dtype(right):
            return "Float64"

        return "Int64"

    def _to_clean_string(self, series: pd.Series) -> pd.Series:
        """
        Приводит серию к StringDtype:
        - числовые значения переводятся в строку;
        - целочисленные float переводятся в строку без '.0', если это возможно;
        - пустые строки заменяются на NA;
        - лишние пробелы удаляются.
        """
        if self._is_numeric_non_bool(series):
            if pd.api.types.is_float_dtype(series):
                non_null = series.dropna()

                if not non_null.empty:
                    try:
                        if (non_null % 1 == 0).all():
                            series = self._safe_cast_numeric(series, "Int64")
                    except (TypeError, ValueError, OverflowError):
                        pass

            series = series.astype("string")

        elif isinstance(series.dtype, pd.CategoricalDtype):
            series = series.astype("string")

        else:
            series = series.astype("string")

        series = series.str.strip()
        return series.replace({"": pd.NA})

    def _align_series_dtypes(self, left: pd.Series, right: pd.Series) -> tuple[pd.Series, pd.Series]:
        """
        выравнивает типы двух серий для merge/map/compare:
        - если обе серии числовые, приводим к общему числовому dtype;
        - иначе приводим обе к string.
        """
        left_clean = self._clean_series(left)
        right_clean = self._clean_series(right)

        if self._is_numeric_non_bool(left_clean) and self._is_numeric_non_bool(right_clean):
            common_dtype = self._common_numeric_dtype(left_clean, right_clean)
            left_clean = self._safe_cast_numeric(left_clean, common_dtype)
            right_clean = self._safe_cast_numeric(right_clean, common_dtype)
        else:
            left_clean = self._to_clean_string(left_clean)
            right_clean = self._to_clean_string(right_clean)

        return left_clean, right_clean

    def _ensure_clean_dtypes(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Гарантирует, что в DataFrame нет object-колонок:
        - числовые колонки остаются числовыми;
        - остальные колонки приводятся к string.

        Если нужно сохранить datetime-колонки как datetime,
        этот метод можно адаптировать.
        """
        result = df.copy()

        for col in result.columns:
            if self._is_numeric_non_bool(result[col]):
                continue

            result[col] = self._to_clean_string(result[col]).array

        if not isinstance(result.index, pd.MultiIndex) and pd.api.types.is_object_dtype(result.index.dtype):
            cleaned_index = self._to_clean_string(pd.Series(result.index)).array
            result.index = pd.Index(cleaned_index, name=result.index.name)

        return result

    def _prepare_amount_column(self, df: pd.DataFrame) -> None:
        """
        Приводит столбец с оборотом к числовому виду.
        Пустые/нечисловые значения заменяются на 0.
        """
        if self.AMOUNT_COL not in df.columns:
            raise ValueError(f"В журнале ОПУ отсутствует столбец '{self.AMOUNT_COL}'.")

        converted = pd.to_numeric(df[self.AMOUNT_COL], errors="coerce")
        bad_count = int(converted.isna().sum())

        if bad_count:
            logger.warning(
                "В столбце '{}' обнаружено {} пустых/нечисловых значений. "
                "Они будут заменены на 0.",
                self.AMOUNT_COL,
                bad_count,
            )

        df[self.AMOUNT_COL] = converted.fillna(0.0).astype("float64").array

    def _drop_zero_amount_rows(self, journal_df: pd.DataFrame) -> pd.DataFrame:
        """
        Удаляет околонулевые строки по валютному столбцу 'оборот, тыс.ед.'.

        Строки с abs('оборот, тыс.ед.') < ZERO_ROWS_EPSILON (0 или ~1e-12)
        дают аномальный курс руб/ед в итоговом ОПУ. Фильтр выполняется
        ТОЛЬКО по валютному столбцу: рублёвый столбец 'оборот, тыс.руб.'
        не проверяется, чтобы не ломать сходимость в рублях для строк
        с ед≈0 и руб≠0.
        """
        zero_mask = journal_df[self.AMOUNT_COL].abs() < self.ZERO_ROWS_EPSILON

        if 'доход_расход' in journal_df.columns:
            zero_mask = zero_mask & (
                journal_df['доход_расход'] != OpuReportConstants.FX_DIFFERENCE_LABEL
            )

        zero_count = int(zero_mask.sum())

        if not zero_count:
            logger.debug(
                "Околонулевых строк по столбцу '{}' (порог {:g}) не найдено.",
                self.AMOUNT_COL,
                self.ZERO_ROWS_EPSILON,
            )
            return journal_df

        if self.RUB_AMOUNT_COL in journal_df.columns:
            dropped_rub = pd.to_numeric(
                journal_df.loc[zero_mask, self.RUB_AMOUNT_COL], errors="coerce"
            )
            dropped_rub_sum = float(dropped_rub.fillna(0.0).sum())
            logger.info(
                "[Чистка] Удалено {} околонулевых строк: abs('{}') < {:g}. "
                "Сумма '{}' по удалённым строкам: {:,.4f} (контроль потерь).",
                zero_count,
                self.AMOUNT_COL,
                self.ZERO_ROWS_EPSILON,
                self.RUB_AMOUNT_COL,
                dropped_rub_sum,
            )
        else:
            logger.info(
                "[Чистка] Удалено {} околонулевых строк: abs('{}') < {:g}. "
                "Столбец '{}' отсутствует (не валютная компания) — контроль потерь не требуется.",
                zero_count,
                self.AMOUNT_COL,
                self.ZERO_ROWS_EPSILON,
                self.RUB_AMOUNT_COL,
            )

        return journal_df.loc[~zero_mask].copy()

    def _check_profit_vs_balance(self, balance_df: pd.DataFrame, journal_df: pd.DataFrame,
                                 context: ProcessingContext) -> None:
        """
        Сверяет чистую прибыль по ОПУ и нераспределенную прибыль
        текущего периода по балансу.
        """
        retained_earnings = self._get_retained_earnings(balance_df)
        net_profit = float(journal_df[self.AMOUNT_COL].sum())
        diff = abs(retained_earnings - net_profit)

        if diff > context.tolerance_params['tolerance_pnl_balance']:
            message = (
                "Разница между чистой прибылью в расшифровке ОПУ и НРП текущего периода "
                f"в расшифровке баланса составляет {diff:,.0f} тыс.ед., что превышает "
                f"допустимый порог в {context.tolerance_params['tolerance_pnl_balance']:,.0f} тыс.ед."
            )
            logger.error(message)
            raise ValueError(message)

        logger.info(
            "[OK] Сходимость чистой прибыли с балансом подтверждена: разница {:,.0f} тыс. ед. (порог {:,.0f})",
            diff,
            context.tolerance_params['tolerance_pnl_balance'],
        )

    def _get_retained_earnings(self, balance_df: pd.DataFrame) -> float:
        """
        Возвращает значение нераспределенной прибыли текущего периода
        по коду 240010200 из расшифровки баланса.
        """
        if self.VALUE_COL not in balance_df.columns:
            raise ValueError(f"В расшифровке баланса отсутствует столбец '{self.VALUE_COL}'.")

        try:
            raw_value = balance_df.loc[self.RETAINED_EARNINGS_CODE, self.VALUE_COL]
        except KeyError as exc:
            raise ValueError(
                f"В расшифровке баланса не найдена строка '{self.RETAINED_EARNINGS_CODE}'."
            ) from exc

        if isinstance(raw_value, pd.Series):
            if len(raw_value) != 1:
                raise ValueError(
                    f"По коду '{self.RETAINED_EARNINGS_CODE}' в расшифровке баланса "
                    f"найдено {len(raw_value)} значений, ожидалось одно."
                )
            raw_value = raw_value.iloc[0]

        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Значение НРП по коду '{self.RETAINED_EARNINGS_CODE}' не является числом: {raw_value!r}."
            ) from exc

        if pd.isna(value):
            raise ValueError(
                f"Значение НРП по коду '{self.RETAINED_EARNINGS_CODE}' не может быть NaN."
            )

        return value
