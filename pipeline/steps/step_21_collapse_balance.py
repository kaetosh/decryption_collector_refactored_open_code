# -*- coding: utf-8 -*-
"""
Шаг 21: Свёртывание статей расшифровки баланса.

Некоторые статьи баланса показываются свёрнуто — находится разница
между статьёй актива и статьёй пассива одного вида, и в зависимости
от знака результат отражается на активной или пассивной стороне.

Пример: ОНА (1180) и ОНО (1420) сворачиваются в сальдированную статью.

Сворачивание выполняется по справочнику «СтатьиБаланс_свернуто»:
- ключ парирования: номер_группы_сворачивания;
- стороны группы различаются по колонке «Актив/Пассив» данных
  (А = положительная сторона, П = отрицательная);
- правило: если итог > 0 — результат на активной стороне, < 0 —
  на пассивной, ≈ 0 — обе строки удаляются.

В отличие от шага 20 (ОПУ), строки группы НЕ группируются дополнительно
по «3 уровень»/«4 уровень: СВЯЗАННОСТЬ»: у активной и пассивной статей
эти атрибуты могут различаться (например, Собственность vs 3 лица),
и группировка разнесла бы их по разным ключам, сломав неттинг.
"""

from __future__ import annotations

from typing import Final

import pandas as pd
from loguru import logger

from io_module.output_manager import get_output_dir
from pipeline.base import ProcessingContext, Step
from utils import needs_conversion


class Step21CollapseBalanceArticlesStep(Step):
    """
    Шаг 21: Свёртывание статей расшифровки баланса.

    Работает с финальной расшифровкой баланса (balance_df), собранной
    на шаге 13. Пары «актив + пассив» из справочника
    «СтатьиБаланс_свернуто» сворачиваются в одну сальдированную строку.
    """

    REF_NAME: Final = "статьи_баланс_свернуто"

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
    ASSET_SIDE: Final = "А"
    PASSIVE_SIDE: Final = "П"

    ZERO_EPSILON: Final = 1e-6

    def __init__(self) -> None:
        super().__init__(
            name="Шаг 21: Свёртывание статей расшифровки баланса",
            description="Свёртывание пар статей актива/пассива по справочнику "
                        "'СтатьиБаланс_свернуто'",
        )
        self._skip_balance_validation = True

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        balance_df = context.balance_df

        if balance_df is None or balance_df.empty:
            logger.info("Шаг 21 пропущен: balance_df пуст")
            return context

        required_cols = [
            self.LEVEL1_COL, self.LEVEL2_COL,
            self.ASSET_LIABILITY_COL, self.VALUE_COL,
        ]
        missing = [c for c in required_cols if c not in balance_df.columns]
        if missing:
            logger.warning(
                "В balance_df отсутствуют колонки {}, сворачивание пропущено",
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
                "Справочник '{}' не содержит валидных групп для сворачивания, "
                "шаг пропущен",
                self.REF_NAME,
            )
            return context

        has_rub = (
            needs_conversion(context)
            and self.RUB_VALUE_COL in balance_df.columns
        )

        balance_before = len(balance_df)
        value_sum_before = float(pd.to_numeric(balance_df[self.VALUE_COL]).sum())

        balance_df, collapsed_report_rows = self._collapse(balance_df, groups, has_rub)
        balance_after = len(balance_df)
        value_sum_after = float(pd.to_numeric(balance_df[self.VALUE_COL]).sum())

        self._save_collapse_report(
            collapsed_report_rows,
            context.company,
            context.period,
        )

        logger.info(
            "[OK] Свёрнуто {} групп статей баланса: {} строк -> {} строк",
            len(collapsed_report_rows),
            balance_before,
            balance_after,
        )
        if abs(value_sum_before - value_sum_after) > self.ZERO_EPSILON:
            logger.warning(
                "[!] Сумма 'Значение' баланса изменилась после сворачивания: "
                "{:.2f} -> {:.2f} — проверьте справочник '{}'",
                value_sum_before,
                value_sum_after,
                self.REF_NAME,
            )

        context.balance_df = balance_df
        return context

    # ------------------------------------------------------------------
    # Построение справочника групп
    # ------------------------------------------------------------------

    def _build_groups(
        self,
        ref_df: pd.DataFrame,
    ) -> dict[str, list[tuple[str, str]]]:
        """
        Строит группы для сворачивания: {group_id: [(1 уровень, 2 уровень), ...]}.

        В отличие от шага 20 стороны пары не захардкожены по названиям
        «1 уровень» — активная/пассивная сторона определяется в данных
        по колонке «Актив/Пассив». Это обобщается на любые будущие
        группы справочника без правок кода.
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
        ref[self.LEVEL1_COL] = (
            ref[self.LEVEL1_COL].astype("string").str.strip()
        )
        ref[self.LEVEL2_COL] = (
            ref[self.LEVEL2_COL].astype("string").str.strip()
        )
        ref = ref[ref[self.LEVEL1_COL] != ""]
        ref = ref[ref[self.LEVEL2_COL] != ""]

        # Порядок строк справочника внутри группы сохраняется
        groups: dict[str, list[tuple[str, str]]] = {}
        for _, row in ref.iterrows():
            group_id = str(row[self.GROUP_COL]).strip()
            key = (row[self.LEVEL1_COL], row[self.LEVEL2_COL])
            if key not in groups.setdefault(group_id, []):
                groups[group_id].append(key)

        logger.debug("Построено {} групп для сворачивания", len(groups))
        return groups

    # ------------------------------------------------------------------
    # Сворачивание
    # ------------------------------------------------------------------

    def _collapse(
        self,
        balance_df: pd.DataFrame,
        groups: dict[str, list[tuple[str, str]]],
        has_rub: bool,
    ) -> tuple[pd.DataFrame, list[dict]]:
        """
        Сворачивает группы статей актива/пассива в balance_df.

        Возвращает:
            (свёрнутый balance_df, список строк для отчёта)
            Отчётный список включает и группы с итогом ≈ 0
            (обе стороны удалены из баланса, строка-результат не создаётся).
        """
        inserted_rows: list[dict] = []
        report_rows: list[dict] = []
        used_indices: set[int] = set()

        output_cols = [
            self.RSBU_CODE_COL, self.LEVEL1_COL, self.LEVEL2_COL,
            self.LEVEL3_COL, self.LEVEL4_COL, self.ASSET_LIABILITY_COL,
            self.VALUE_COL,
        ]
        if has_rub:
            output_cols.append(self.RUB_VALUE_COL)
        output_cols.extend([self.REPORT_TYPE_COL, self.ARTICLE_COL])

        level1_series = balance_df[self.LEVEL1_COL].astype("string").str.strip()
        level2_series = balance_df[self.LEVEL2_COL].astype("string").str.strip()

        for group_id, ref_keys in groups.items():
            asset_rows_list: list[pd.DataFrame] = []
            passive_rows_list: list[pd.DataFrame] = []

            for l1, l2 in ref_keys:
                mask = (level1_series == l1) & (level2_series == l2)
                rows = balance_df[mask]
                if rows.empty:
                    continue

                sides = rows[self.ASSET_LIABILITY_COL].astype("string").str.strip()
                asset_part = rows[sides == self.ASSET_SIDE]
                passive_part = rows[sides == self.PASSIVE_SIDE]
                other_part = rows[
                    ~sides.isin([self.ASSET_SIDE, self.PASSIVE_SIDE])
                ]
                if not other_part.empty:
                    logger.warning(
                        "Группа {}: строки ({!r}, {!r}) со значением "
                        "«Актив/Пассив» вне '{}'/'{}' ({}) — из сворачивания "
                        "исключены",
                        group_id, l1, l2,
                        self.ASSET_SIDE, self.PASSIVE_SIDE,
                        sorted(set(other_part[self.ASSET_LIABILITY_COL].astype(str))),
                    )

                if not asset_part.empty:
                    asset_rows_list.append(asset_part)
                if not passive_part.empty:
                    passive_rows_list.append(passive_part)

            asset_rows = (
                pd.concat(asset_rows_list) if asset_rows_list
                else pd.DataFrame()
            )
            passive_rows = (
                pd.concat(passive_rows_list) if passive_rows_list
                else pd.DataFrame()
            )

            if asset_rows.empty and passive_rows.empty:
                continue

            if asset_rows.empty or passive_rows.empty:
                logger.debug(
                    "Группа {}: в балансе только одна сторона "
                    "(актив: {}, пассив: {}) — сворачивать нечего, строки не изменены",
                    group_id,
                    len(asset_rows),
                    len(passive_rows),
                )
                continue

            used_indices.update(asset_rows.index.tolist())
            used_indices.update(passive_rows.index.tolist())

            asset_sum = float(asset_rows[self.VALUE_COL].sum())
            passive_sum = float(passive_rows[self.VALUE_COL].sum())
            total = asset_sum + passive_sum

            meta = {
                "группа": group_id,
                "сумма_актив": asset_sum,
                "сумма_пассив": passive_sum,
                "итог": total,
            }

            # Итог ≈ 0: обе стороны удаляются, строка-результат не создаётся
            # (аналог удаления нулевых строк в шаге 13 и ZERO_EPSILON в шаге 20)
            if abs(total) < self.ZERO_EPSILON:
                logger.info(
                    "Группа {}: итог сворачивания ≈ 0 — обе стороны удалены из баланса",
                    group_id,
                )
                report_rows.append({**meta, "направление": "≈0"})
                continue

            # Активная сторона — «позитивная», пассивная — «негативная».
            # Победившая сторона определяет атрибуты строки-результата.
            if total >= 0:
                result_rows = asset_rows
                direction = self.ASSET_SIDE
            else:
                result_rows = passive_rows
                direction = self.PASSIVE_SIDE

            source_row = result_rows.iloc[0]
            row_dict: dict = {
                self.ACCOUNT_COL: source_row.name,
                self.RSBU_CODE_COL: source_row.get(self.RSBU_CODE_COL, pd.NA),
                self.LEVEL1_COL: source_row.get(self.LEVEL1_COL, pd.NA),
                self.LEVEL2_COL: source_row.get(self.LEVEL2_COL, pd.NA),
                self.LEVEL3_COL: source_row.get(self.LEVEL3_COL, pd.NA),
                self.LEVEL4_COL: source_row.get(self.LEVEL4_COL, pd.NA),
                self.ASSET_LIABILITY_COL: direction,
                self.VALUE_COL: total,
            }
            if has_rub:
                rub_total = float(
                    asset_rows[self.RUB_VALUE_COL].sum()
                    + passive_rows[self.RUB_VALUE_COL].sum()
                )
                row_dict[self.RUB_VALUE_COL] = round(rub_total, 2)

            inserted_rows.append(row_dict)
            report_rows.append({**meta, "направление": direction, **row_dict})

        if not used_indices:
            return balance_df, report_rows

        result_df = balance_df.drop(index=list(used_indices)).copy()

        if inserted_rows:
            if self.ACCOUNT_COL not in result_df.columns:
                result_df = result_df.reset_index()
            new_rows = pd.DataFrame(inserted_rows)
            for col in output_cols:
                if col not in new_rows.columns:
                    new_rows[col] = pd.NA
            new_rows = new_rows[output_cols + [self.ACCOUNT_COL]]
            result_df = pd.concat([result_df, new_rows], ignore_index=True)
            result_df = result_df.set_index(self.ACCOUNT_COL)

        result_df = self._clean_balance_dtypes(result_df)
        result_df = result_df.sort_index(na_position="last")

        return result_df, report_rows

    def _clean_balance_dtypes(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Приводит DataFrame к чистым типам (string для текста, float/Int64
        для чисел), запрещая object dtype (иначе поймаем TypeError
        при сохранении в Excel).
        """
        result = df.copy()
        for col in result.columns:
            if col in (self.VALUE_COL, self.RUB_VALUE_COL):
                if not pd.api.types.is_numeric_dtype(result[col]):
                    result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0.0)
                continue
            if pd.api.types.is_numeric_dtype(result[col]) and not pd.api.types.is_bool_dtype(result[col]):
                # Числовые служебные коды (РСБУ Код отчетности и т.п.)
                # приводим к nullable Int64 — как в исходном балансе
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
                / f"step21_collapse_balance_{company}_{period}.xlsx"
            )
            report_df.to_excel(output_path, index=False)
            logger.info(
                "[FOLDER] Отчёт о свёрнутых статьях баланса сохранён: {}",
                output_path,
            )
        except PermissionError:
            logger.warning(
                "[!] Не удалось сохранить отчёт о свертке: файл открыт в другой программе"
            )
        except Exception as exc:
            logger.warning("[!] Ошибка сохранения отчёта о свертке: {}", exc)
