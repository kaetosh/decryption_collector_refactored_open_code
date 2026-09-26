# -*- coding: utf-8 -*-
"""
Общая база для шагов сворачивания (20 — ОПУ, 21 — баланс).

Сворачивание пар статей/доходов-расходов по справочнику «*_свернуто»
делит между шагами: константы колонок результата, приведение типов
(запрет object) и сохранение отчёта в mismatches/. Сама логика
парирования сторон и матчинга групп остаётся в конкретных шагах.
"""

from __future__ import annotations

from typing import Final

import pandas as pd
from loguru import logger

from io_module import DataSaver
from pipeline.base import Step
from pipeline.step_config import ReportLayoutConstants


class CollapseStepBase(Step):
    """Общие константы и хелперы шагов сворачивания (20 и 21)."""

    # Имена колонок отчётных листов объявлены в step_config
    # (ReportLayoutConstants) — одна точка правды для шагов и для
    # представления результата (io_module).
    LEVEL1_COL: Final = ReportLayoutConstants.LEVEL1_COL
    LEVEL2_COL: Final = ReportLayoutConstants.LEVEL2_COL
    LEVEL3_COL: Final = ReportLayoutConstants.LEVEL3_COL
    LEVEL4_COL: Final = ReportLayoutConstants.LEVEL4_COL
    VALUE_COL: Final = ReportLayoutConstants.VALUE_COL
    RUB_VALUE_COL: Final = ReportLayoutConstants.RUB_VALUE_COL
    RSBU_CODE_COL: Final = ReportLayoutConstants.RSBU_CODE_COL
    ACCOUNT_COL: Final = ReportLayoutConstants.ACCOUNT_COL
    REPORT_TYPE_COL: Final = ReportLayoutConstants.REPORT_TYPE_COL
    ARTICLE_COL: Final = ReportLayoutConstants.ARTICLE_COL
    ASSET_LIABILITY_COL: Final = ReportLayoutConstants.ASSET_LIABILITY_COL

    GROUP_COL: Final = "номер_группы_сворачивания"

    ZERO_EPSILON: Final = 1e-6

    def _clean_collapse_dtypes(
        self,
        df: pd.DataFrame,
        numeric_codes: bool = False,
    ) -> pd.DataFrame:
        """
        Приводит DataFrame к чистым типам (string для текста, float/Int64
        для чисел), запрещая object dtype (иначе поймаем TypeError
        при сохранении в Excel).

        :param numeric_codes: True — числовые служебные коды (РСБУ Код
            отчетности и т.п.) приводятся к nullable Int64/Float64
            (шаг 21); False — числовые колонки остаются как есть (шаг 20).
        """
        result = df.copy()
        for col in result.columns:
            if col in (self.VALUE_COL, self.RUB_VALUE_COL):
                if not pd.api.types.is_numeric_dtype(result[col]):
                    result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0.0)
                continue
            if pd.api.types.is_numeric_dtype(result[col]) and not pd.api.types.is_bool_dtype(result[col]):
                if not numeric_codes:
                    continue
                non_null = result[col].dropna()
                if not non_null.empty and (non_null % 1 == 0).all():
                    try:
                        result[col] = result[col].astype("Int64")
                        continue
                    except (TypeError, ValueError, OverflowError):
                        pass
                result[col] = result[col].astype("Float64")
                continue
            result[col] = result[col].astype("string").str.strip().replace({"": pd.NA})
        return result

    def _save_collapse_report(
        self,
        collapsed_rows: list[dict],
        company: str,
        period: str,
        report_prefix: str,
    ) -> None:
        """
        Сохраняет отчёт о сворачивании в mismatches/.
        Сбой сохранения (файл открыт в Excel и т.п.) не останавливает шаг.
        """
        if not collapsed_rows:
            return

        report_df = pd.DataFrame(collapsed_rows)
        filename = f"{report_prefix}_{company}_{period}.xlsx"
        try:
            output_path = DataSaver.save_to_excel(
                report_df, filename, subfolder="mismatches"
            )
            logger.info("[FOLDER] Отчёт о сворачивании сохранён: {}", output_path)
        except PermissionError:
            logger.warning(
                "[!] Не удалось сохранить отчёт о сворачивании: "
                "файл открыт в другой программе"
            )
        except Exception as exc:
            logger.warning("[!] Ошибка сохранения отчёта о сворачивании: {}", exc)