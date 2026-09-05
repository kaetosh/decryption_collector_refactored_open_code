"""
Шаг 20: Свёртывание прочих доходов и расходов в ОПУ.

Некоторые прочие доходы и расходы в ОПУ показываются свёрнуто —
находится разница между доходом и расходом одного вида, и в зависимости
от знака результат отражается как доход или расход.

Сворачивание выполняется по справочнику «Прочие_дох_рас_свернуто»:
- ключ группировки: 1 уровень + 2 уровень (точка плана) + 3 уровень
  + 4 уровень: СВЯЗАННОСТЬ
- правило: если сумма < 0 — результат доход, > 0 — расход
"""

from __future__ import annotations

from typing import Final

import pandas as pd
from loguru import logger

from io_module.output_manager import get_output_dir
from pipeline.base import ProcessingContext, Step
from utils import needs_conversion


class Step20CollapseOtherIncomeExpensesStep(Step):
    """
    Шаг 20: Свёртывание прочих доходов и расходов в ОПУ.

    Работает с финальной расшифровкой ОПУ (pnl_df), собранной на шаге 19.
    Пары «доход + расход» из справочника «Прочие_дох_рас_свернуто»
    сворачиваются в одну строку с финансовым результатом.
    """

    REF_NAME: Final = "прочие_дох_рас_свернуто"

    LEVEL1_COL: Final = "1 уровень"
    LEVEL2_COL: Final = "2 уровень (точка плана)"
    LEVEL3_COL: Final = "3 уровень"
    LEVEL4_COL: Final = "4 уровень: СВЯЗАННОСТЬ"
    VALUE_COL: Final = "Значение"
    RUB_VALUE_COL: Final = "Значение_руб"
    RSBU_CODE_COL: Final = "РСБУ Код отчетности"
    ACCOUNT_COL: Final = "Итоговый номер счета"
    REPORT_TYPE_COL: Final = "Отчетность"
    ARTICLE_COL: Final = "Статья отчетности"
    ASSET_LIABILITY_COL: Final = "Актив/Пассив"

    GROUP_COL: Final = "номер_группы_сворачивания"
    INCOME_LEVEL1: Final = "Прочие доходы"
    EXPENSE_LEVEL1: Final = "Прочие расходы"

    ZERO_EPSILON: Final = 1e-6

    def __init__(self) -> None:
        super().__init__(
            name="Шаг 20: Свёртывание прочих доходов и расходов в ОПУ",
            description="Свёртывание пар прочих доходов/расходов по справочнику "
                        "'Прочие_дох_рас_свернуто'",
        )
        self._skip_balance_validation = True

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        pnl_df = context.pnl_df

        if pnl_df is None or pnl_df.empty:
            logger.info("Шаг 20 пропущен: pnl_df пуст")
            return context

        required_cols = [
            self.LEVEL1_COL, self.LEVEL2_COL, self.LEVEL3_COL,
            self.LEVEL4_COL, self.VALUE_COL,
        ]
        missing = [c for c in required_cols if c not in pnl_df.columns]
        if missing:
            logger.warning(
                "В pnl_df отсутствуют колонки {}, сворачивание пропущено",
                missing,
            )
            return context

        ref_df = context.references.get(self.REF_NAME)
        if ref_df is None or ref_df.empty:
            logger.info(
                "Справочник '{}' не найден или пуст, сворачивание пропущено",
                self.REF_NAME,
            )
            return context

        groups = self._build_groups(ref_df)
        if not groups:
            logger.info(
                "Справочник '{}' не содержит валидных пар для сворачивания, "
                "шаг пропущен",
                self.REF_NAME,
            )
            return context

        has_rub = (
            needs_conversion(context)
            and self.RUB_VALUE_COL in pnl_df.columns
        )

        pnl_before = len(pnl_df)
        pnl_df, collapsed_report_rows = self._collapse(pnl_df, groups, has_rub)
        pnl_after = len(pnl_df)

        self._save_collapse_report(
            collapsed_report_rows,
            context.company,
            context.period,
        )

        logger.info(
            "[OK] Свёрнуто {} пар прочих доходов/расходов: "
            "{} строк -> {} строк",
            len(collapsed_report_rows),
            pnl_before,
            pnl_after,
        )

        context.pnl_df = pnl_df
        return context

    # ------------------------------------------------------------------
    # Построение справочника групп
    # ------------------------------------------------------------------

    def _build_groups(
        self,
        ref_df: pd.DataFrame,
    ) -> dict[str, tuple[str, str]]:
        """
        Строит маппинг нормализованный_вид -> (1_уровень_дохода, 1_уровень_расхода).

        Параметры:
            ref_df — DataFrame из листа «Прочие_дох_рас_свернуто»

        Возвращает:
            dict[нормализованный_вид_дохода_расхода, (level1_income, level1_expense)]
        """
        required = {self.GROUP_COL, self.LEVEL1_COL, self.LEVEL2_COL}
        missing = [c for c in required if c not in ref_df.columns]
        if missing:
            logger.warning(
                "В справочнике '{}' отсутствуют колонки {}, "
                "группировка невозможна",
                self.REF_NAME,
                missing,
            )
            return {}

        ref = ref_df.copy()
        ref = ref.dropna(subset=[self.GROUP_COL, self.LEVEL1_COL, self.LEVEL2_COL])
        ref[self.LEVEL2_COL] = (
            ref[self.LEVEL2_COL]
            .astype("string")
            .str.strip()
        )
        ref = ref[ref[self.LEVEL2_COL] != ""]

        groups: dict[str, tuple[str, str]] = {}

        for _, row in ref.iterrows():
            name = str(row[self.LEVEL2_COL]).strip()
            level1 = str(row[self.LEVEL1_COL]).strip()

            if name in groups:
                continue

            income_row = ref[
                (ref[self.LEVEL2_COL] == name)
                & (ref[self.LEVEL1_COL].str.strip() == self.INCOME_LEVEL1)
            ]
            expense_row = ref[
                (ref[self.LEVEL2_COL] == name)
                & (ref[self.LEVEL1_COL].str.strip() == self.EXPENSE_LEVEL1)
            ]

            if income_row.empty or expense_row.empty:
                group_id = row.get(self.GROUP_COL, pd.NA)
                logger.debug(
                    "Группа {} ('{}'): отсутствует пара доход/расход — пропущена",
                    group_id,
                    name,
                )
                continue

            groups[name] = (self.INCOME_LEVEL1, self.EXPENSE_LEVEL1)

        logger.debug("Построено {} групп для сворачивания", len(groups))
        return groups

    # ------------------------------------------------------------------
    # Сворачивание
    # ------------------------------------------------------------------

    def _collapse(
        self,
        pnl_df: pd.DataFrame,
        groups: dict[str, tuple[str, str]],
        has_rub: bool,
    ) -> tuple[pd.DataFrame, list[dict]]:
        """
        Сворачивает пары доходов и расходов в pnl_df.

        Возвращает:
            (свёрнутый pnl_df, список свёрнутых строк для отчёта)
        """
        collapsed_rows: list[dict] = []

        base_cols = [
            self.RSBU_CODE_COL, self.ACCOUNT_COL, self.LEVEL1_COL,
            self.LEVEL2_COL, self.LEVEL3_COL, self.LEVEL4_COL,
            self.ASSET_LIABILITY_COL, self.VALUE_COL,
            self.REPORT_TYPE_COL, self.ARTICLE_COL,
        ]
        if has_rub:
            base_cols.append(self.RUB_VALUE_COL)
        output_cols = [
            self.RSBU_CODE_COL, self.ACCOUNT_COL, self.LEVEL1_COL,
            self.LEVEL2_COL, self.LEVEL3_COL, self.LEVEL4_COL,
            self.ASSET_LIABILITY_COL, self.VALUE_COL,
        ]
        if has_rub:
            output_cols.append(self.RUB_VALUE_COL)
        output_cols.extend([self.REPORT_TYPE_COL, self.ARTICLE_COL])

        used_indices: set[int] = set()

        for name, (income_level1, expense_level1) in groups.items():
            income_mask = (
                (pnl_df[self.LEVEL1_COL].str.strip() == income_level1)
                & (pnl_df[self.LEVEL2_COL].str.strip() == name)
            )
            expense_mask = (
                (pnl_df[self.LEVEL1_COL].str.strip() == expense_level1)
                & (pnl_df[self.LEVEL2_COL].str.strip() == name)
            )

            income_rows = pnl_df[income_mask]
            expense_rows = pnl_df[expense_mask]

            if income_rows.empty and expense_rows.empty:
                continue

            used_indices.update(income_rows.index.tolist())
            used_indices.update(expense_rows.index.tolist())

            key_cols = [self.LEVEL3_COL, self.LEVEL4_COL]

            grouped_income = (
                income_rows.groupby(key_cols, dropna=False)[self.VALUE_COL]
                .sum()
            )
            grouped_expense = (
                expense_rows.groupby(key_cols, dropna=False)[self.VALUE_COL]
                .sum()
            )

            if has_rub:
                rub_income = income_rows.groupby(key_cols, dropna=False)[
                    self.RUB_VALUE_COL
                ].sum()
                rub_expense = expense_rows.groupby(key_cols, dropna=False)[
                    self.RUB_VALUE_COL
                ].sum()

            all_keys = grouped_income.index.union(grouped_expense.index)

            for key in all_keys:
                income_sum = grouped_income.get(key, 0.0)
                expense_sum = grouped_expense.get(key, 0.0)
                total = income_sum + expense_sum

                if abs(total) < self.ZERO_EPSILON:
                    continue

                level3 = key[0] if len(key) > 0 else pd.NA
                level4 = key[1] if len(key) > 1 else pd.NA

                if total < 0:
                    result_level1 = income_level1
                    source_row = income_rows.iloc[0] if not income_rows.empty else expense_rows.iloc[0]
                else:
                    result_level1 = expense_level1
                    source_row = (
                        expense_rows.iloc[0]
                        if not expense_rows.empty
                        else income_rows.iloc[0]
                    )

                row_dict: dict = {
                    self.RSBU_CODE_COL: source_row.get(self.RSBU_CODE_COL, pd.NA),
                    self.ACCOUNT_COL: source_row.get(self.ACCOUNT_COL, pd.NA),
                    self.LEVEL1_COL: result_level1,
                    self.LEVEL2_COL: name,
                    self.LEVEL3_COL: level3,
                    self.LEVEL4_COL: level4,
                    self.ASSET_LIABILITY_COL: source_row.get(
                        self.ASSET_LIABILITY_COL, pd.NA
                    ),
                    self.VALUE_COL: total,
                    self.REPORT_TYPE_COL: source_row.get(
                        self.REPORT_TYPE_COL, pd.NA
                    ),
                    self.ARTICLE_COL: source_row.get(self.ARTICLE_COL, pd.NA),
                }
                if has_rub:
                    rub_inc = rub_income.get(key, 0.0)
                    rub_exp = rub_expense.get(key, 0.0)
                    row_dict[self.RUB_VALUE_COL] = rub_inc + rub_exp

                collapsed_rows.append(row_dict)

        if not used_indices:
            return pnl_df, collapsed_rows

        result_df = pnl_df.drop(index=list(used_indices)).copy()

        if collapsed_rows:
            new_rows = pd.DataFrame(collapsed_rows)
            for col in output_cols:
                if col not in new_rows.columns:
                    new_rows[col] = pd.NA
            new_rows = new_rows[output_cols]
            result_df = pd.concat([result_df, new_rows], ignore_index=True)

        result_df = self._clean_pnl_dtypes(result_df)
        result_df = result_df.sort_values(
            self.ACCOUNT_COL, na_position="last"
        ).reset_index(drop=True)

        return result_df, collapsed_rows

    def _clean_pnl_dtypes(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Приводит DataFrame к чистым типам (string для текста, float для чисел),
        запрещая object dtype (иначе поймаем TypeError при сохранении в Excel).
        """
        result = df.copy()
        for col in result.columns:
            if col in (self.VALUE_COL, self.RUB_VALUE_COL):
                if not pd.api.types.is_numeric_dtype(result[col]):
                    result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0.0)
                continue
            if pd.api.types.is_numeric_dtype(result[col]) and not pd.api.types.is_bool_dtype(result[col]):
                continue
            result[col] = result[col].astype("string").str.strip().replace({"": pd.NA})
        return result

    # ------------------------------------------------------------------
    # Сохранение отчёта
    # ------------------------------------------------------------------

    def _save_collapse_report(
        self,
        collapsed_rows: list[dict],
        company: str,
        period: str,
    ) -> None:
        """
        Сохраняет отчёт о сворачивании в mismatches/.
        """
        if not collapsed_rows:
            return

        report_df = pd.DataFrame(collapsed_rows)

        try:
            output_path = (
                get_output_dir("mismatches")
                / f"step20_collapse_opu_{company}_{period}.xlsx"
            )
            report_df.to_excel(output_path, index=False)
            logger.info(
                "[FOLDER] Отчёт о свёрнутых строках ОПУ сохранён: {}",
                output_path,
            )
        except PermissionError:
            logger.warning(
                "[!] Не удалось сохранить отчёт о свертке: файл открыт в другой программе"
            )
        except Exception as exc:
            logger.warning("[!] Ошибка сохранения отчёта о свертке: {}", exc)
