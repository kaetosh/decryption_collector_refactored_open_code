"""
Шаг 19: Финальная сборка расшифровки ОПУ.
"""

from __future__ import annotations

from typing import Final, Sequence

import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext, Step
from pipeline.errors import MissingMappingError


class Step19BuildOpuStep(Step):
    """
    Шаг 19: собирает расшифровку ОПУ, контролирует увязку с балансом
    и формирует итоговую таблицу ОПУ.

    Особое внимание уделено типам данных:
    - числовые колонки остаются числовыми;
    - текстовые колонки приводятся к StringDtype;
    - object dtype в создаваемых/обрабатываемых данных не используется.
    """

    OPU_REPORT: Final = "ОПУ"
    AMOUNT_COL: Final = "оборот, тыс.ед."
    VALUE_COL: Final = "Значение"
    REPORT_TYPE_COL: Final = "Отчетность"
    ACCOUNT_COL: Final = "Итоговый номер счета"
    TARGET_ACCOUNT_COL: Final = "счет_фо"
    UNMAPPED_ACCOUNT: Final = "не_указано"

    RETAINED_EARNINGS_CODE: Final = "240010200"
    MAX_PROFIT_DIFF: Final = 1000

    MAPPING_REF: Final = "меппинг_опу"
    CHART_OF_ACCOUNTS_REF: Final = "план_счетов_фо"

    # Алиасы для обратной совместимости со старыми именами
    BALANCE_COL: Final = AMOUNT_COL
    max_clean_profit_vs_retained_earnings_diff: Final = MAX_PROFIT_DIFF

    MAPPING_KEY_COLS: Final = (
        "счет",
        "вид_дохода_расхода",
        "сегмент",
        "вид_связи",
    )

    PROBLEM_DETAIL_COLS: Final = (
        "контрагент",
        "ном_группа",
        "счет",
        "доход_расход",
        "вид_дохода_расхода",
        "сегмент",
        "группа_ка",
        "сегмент_ка",
        "вид_связи",
        "объект для изм ппа",
        "рбп_кредитные_линии",
    )

    ROW_ID_COL: Final = "__row_id__"
    GROUP_ACCOUNT_COL: Final = "__group_account__"

    def __init__(self) -> None:
        super().__init__(
            name="Шаг 19: Финальная сборка расшифровки ОПУ",
            description="Финальная сборка расшифровки ОПУ",
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Начало финальной сборки расшифровки ОПУ")

        journal_df = context.journal_df.copy()

        self._prepare_amount_column(journal_df)
        self._check_profit_vs_balance(context.balance_df, journal_df)

        mapping_ref = self._get_reference(context, self.MAPPING_REF)
        journal_df = self._apply_opu_mapping(journal_df, mapping_ref)
        self._ensure_all_rows_mapped(journal_df)

        # Гарантируем, что в журнале не остается object-колонок.
        journal_df = self._ensure_clean_dtypes(journal_df)

        context.journal_df = journal_df

        chart_ref = self._get_reference(context, self.CHART_OF_ACCOUNTS_REF)
        context.pnl_df = self._build_opu_report(journal_df, chart_ref)

        return context

    # ------------------------------------------------------------------
    # Вспомогательные методы: справочники и валидация структуры
    # ------------------------------------------------------------------

    def _get_reference(self, context: ProcessingContext, name: str) -> pd.DataFrame:
        """
        Возвращает справочник из context.references с понятной ошибкой,
        если справочник отсутствует.
        """
        try:
            return context.references[name]
        except KeyError as exc:
            raise ValueError(f"В context.references отсутствует справочник '{name}'.") from exc

    @staticmethod
    def _validate_columns(df: pd.DataFrame, columns: Sequence[str], entity_name: str) -> None:
        """
        Проверяет наличие обязательных столбцов в DataFrame.
        """
        missing_columns = [col for col in columns if col not in df.columns]
        if missing_columns:
            raise ValueError(
                f"В {entity_name} отсутствуют обязательные столбцы: {', '.join(missing_columns)}."
            )

    # ------------------------------------------------------------------
    # Управление типами данных
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Подготовка сумм
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Сверка ОПУ и баланса
    # ------------------------------------------------------------------

    def _check_profit_vs_balance(self, balance_df: pd.DataFrame, journal_df: pd.DataFrame) -> None:
        """
        Сверяет чистую прибыль по ОПУ и нераспределенную прибыль
        текущего периода по балансу.
        """
        retained_earnings = self._get_retained_earnings(balance_df)
        net_profit = float(journal_df[self.AMOUNT_COL].sum())
        diff = abs(retained_earnings - net_profit)

        if diff > self.MAX_PROFIT_DIFF:
            message = (
                "Разница между чистой прибылью в расшифровке ОПУ и НРП текущего периода "
                f"в расшифровке баланса составляет {diff:,.0f} тыс.ед., что превышает "
                f"допустимый порог в {self.MAX_PROFIT_DIFF:,.0f} тыс.ед."
            )
            logger.error(message)
            raise ValueError(message)

        logger.info(
            "Разница между чистой прибылью в расшифровке ОПУ и НРП текущего периода "
            "в расшифровке баланса составляет {:,.0f} тыс.ед., что НЕ превышает "
            "допустимый порог в {:,.0f} тыс.ед.",
            diff,
            self.MAX_PROFIT_DIFF,
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

    # ------------------------------------------------------------------
    # Маппинг ОПУ
    # ------------------------------------------------------------------

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

        # duplicates_count = int(mapping_df.duplicated(subset=key_cols).sum())
        # if duplicates_count:
        #     logger.warning(
        #         f"В справочнике маппинга ОПУ найдено дублей по ключу: {duplicates_count}. "
        #         "Приоритет будет отдан записи с заполненным счетом ФО, если такая есть."
        #     )

        mapping_df = mapping_df.assign(__has_target__=mapping_df[self.TARGET_ACCOUNT_COL].notna())

        mapping_df = (
            mapping_df.sort_values("__has_target__", ascending=False)
            .drop_duplicates(subset=key_cols, keep="first")
            .rename(columns={self.TARGET_ACCOUNT_COL: "_mapped_account"})
        )

        return mapping_df.loc[:, [*key_cols, "_mapped_account"]]

    def _get_old_target_values(self, df: pd.DataFrame) -> pd.Series:
        """
        Возвращает старые значения счет_фо в корректном типе.
        """
        if self.TARGET_ACCOUNT_COL not in df.columns:
            return pd.Series(pd.NA, index=df.index, dtype="string")

        return self._clean_series(df[self.TARGET_ACCOUNT_COL])

    # ------------------------------------------------------------------
    # Контроль полноты маппинга
    # ------------------------------------------------------------------

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

        # Если есть незамапленные строки, счет_фо должен стать строковым,
        # чтобы значение 'не_указано' не смешивалось с числовым типом.
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

    # ------------------------------------------------------------------
    # Сборка ОПУ
    # ------------------------------------------------------------------

    def _build_opu_report(self, journal_df: pd.DataFrame, chart_of_accounts_df: pd.DataFrame) -> pd.DataFrame:
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
        opu_template = opu_template[opu_template[self.VALUE_COL] != 0]

        opu_template = self._ensure_clean_dtypes(opu_template)

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


# """
# Шаг 19: Финальная сборка расшифровки ОПУ.
# """
# import pandas as pd
# from loguru import logger
# from pipeline.base import Step, ProcessingContext
# from pipeline.errors import MissingMappingError

# class Step19BuildOpuStep(Step):
#     """
#     Шаг 19: Финальная сборка расшифровки ОПУ.
#     """
    
#     OPU_REPORT = 'ОПУ'
#     BALANCE_COL = 'оборот, тыс.ед.'
    
#     # максимально допустимая разница между чистой прибылью в ОПУ и нераспределенной прибылью текущего периода в балансе
#     max_clean_profit_vs_retained_earnings_diff = 1000
    
#     def __init__(self):
#         super().__init__(
#             name="Шаг 19: Финальная сборка расшифровки ОПУ",
#             description="Финальная сборка расшифровки ОПУ"
#         )

#     def _process(self, context: ProcessingContext) -> ProcessingContext:
#         logger.debug("Начало финальной сборки расшифровки ОПУ")
        
#         # текущая таблица с ОПУ
#         df_final = context.journal_df.copy()
        
#         # сверим чистую прибыль по расшифровке ОПУ
#         # и нераспределенную прибыль текущего периода по расшифровке баланса
#         # это код "240010200"

#         try:
#             current_period_NRP = context.balance_df.loc['240010200', 'Значение'].item()
#         except ValueError as e:
#             # Логируем или обрабатываем ситуацию
#             logger.warning(f"Не удалось извлечь НРП текущего периода из расшифровки баланса: {e}")
#             raise ValueError(f"Не удалось извлечь НРП текущего периода из расшифровки баланса: {e}")
        
#         net_profit = df_final['оборот, тыс.ед.'].sum()
        
#         clean_profit_vs_retained_earnings_diff = abs(current_period_NRP - net_profit)
        
#         if clean_profit_vs_retained_earnings_diff>self.max_clean_profit_vs_retained_earnings_diff:
#             logger.error(f'Разница между чистой прибылью в расшифровке ОПУ и НРП текущего периода в расшифровке балансе составляет {clean_profit_vs_retained_earnings_diff} тыс.ед, что превышает допустимый порог в {self.max_clean_profit_vs_retained_earnings_diff} тыс. ед.')
#             raise ValueError(f'Разница между чистой прибылью в расшифровке ОПУ и НРП текущего периода в расшифровке балансе составляет {clean_profit_vs_retained_earnings_diff} тыс.ед, что превышает допустимый порог в {self.max_clean_profit_vs_retained_earnings_diff} тыс. ед.')
#         else:
#             logger.info(f'Разница между чистой прибылью в расшифровке ОПУ и НРП текущего периода в расшифровке балансе составляет {clean_profit_vs_retained_earnings_diff} тыс.ед, что НЕ превышает допустимый порог в {self.max_clean_profit_vs_retained_earnings_diff} тыс. ед.')
        
#         key_cols = ['счет', 'вид_дохода_расхода', 'сегмент', 'вид_связи']
        
#         ref_keyed = (
#             context.references["меппинг_опу"]
#             .drop_duplicates(subset=key_cols)
#             .set_index(key_cols)['счет_фо']
#         )
        

#         mapped = df_final.set_index(key_cols).index.map(ref_keyed).values
#         # mapped теперь содержит новые значения и NaN там, где ключ не найден
        
#         # Если старое значение уже есть, оставляем его. Иначе берем mapped.
#         # Удобно сделать через pd.Series, чтобы align по индексу
#         mapped_series = pd.Series(mapped, index=df_final.index)
#         df_final['счет_фо'] = mapped_series.fillna(df_final['счет_фо'])
                
#         unmatched = df_final['счет_фо'].isna().sum()
#         total = len(df_final)
#         logger.info(f"Найдено совпадений: {total - unmatched} из {total}")
#         logger.info(f"Не найдено (NaN): {unmatched} ({unmatched/total*100:.1f}%)")
        
#         if unmatched > 0:

#             #Пустые значения заполним значения "не_указано"
#             df_final['счет_фо'] = df_final['счет_фо'].fillna('не_указано')
            
#             unmapped_mask = df_final['счет_фо'] == 'не_указано'
            
#             # Формируем problem_data — все незамапленные строки ОПУ
#             # (не только уникальные комбинации, а все строки для полного анализа)
#             str_cols = ['контрагент', 'ном_группа', 'счет', 'доход_расход', 'вид_дохода_расхода',
#                         'сегмент', 'группа_ка', 'сегмент_ка', 'вид_связи', 'объект для изм ппа', 'рбп_кредитные_линии']
            
#             problem_data = df_final.loc[
#                 unmapped_mask, 
#                 str_cols + ['счет_фо', 'оборот, тыс.ед.']
#             ].copy()
            
#             # Уникальные комбинации для метаданных
#             unmapped_unique = problem_data[str_cols].drop_duplicates()
            
#             # ★ Выбрасываем MissingMappingError
#             # Базовый класс сам сохранит в Excel и залогорирует
#             raise MissingMappingError(
#                 message=(
#                     f"НЕ ВСЕ позиции соответствуют Меппингу опу. "
#                     f"Найдено {len(unmapped_unique)} уникальных незамапленных комбинаций"
#                 ),
#                 problem_data=problem_data,
#                 reference_name="Меппинг ОПУ",
#                 unique_combinations_count=len(unmapped_unique),
#                 total_unmapped_rows=len(problem_data),
#             )
            
#         context.journal_df = df_final
        
        
#         chart_accounts_df = context.references['план_счетов_фо']
        
#         # Фильтрация только статей баланса
#         opu_transcripts = chart_accounts_df[
#             chart_accounts_df['Отчетность'] == self.OPU_REPORT
#         ].copy()
        
#         # Валидация структуры
#         if 'Итоговый номер счета' not in opu_transcripts.columns:
#             raise ValueError("В ПланСчетов отсутствует столбец 'Итоговый номер счета'")
        
#         # Устанавливаем индекс
#         opu_transcripts = opu_transcripts.set_index('Итоговый номер счета')
        
#         # Агрегация сальдо по счёт_фо
#         sum_by_account = df_final.groupby('счет_фо')[self.BALANCE_COL].sum()
        
#         # Маппинг сальдо в баланс
#         opu_transcripts['Значение'] = (
#             opu_transcripts.index
#             .map(sum_by_account)
#             .fillna(0)
#         )
        
#         # Удаление нулевых строк
#         opu_transcripts = opu_transcripts[opu_transcripts['Значение'] != 0]
        
#         context.pnl_df = opu_transcripts
        
#         return context
