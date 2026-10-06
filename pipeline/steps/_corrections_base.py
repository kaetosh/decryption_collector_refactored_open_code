# -*- coding: utf-8 -*-
"""Общая часть шагов ручных корректировок признаков (12а — баланс, 18а — ОПУ).

Зачем
----
Выгрузки из 1С содержат ошибки. Например, на договоре по счёту 60 признак
«инвест_договор» помечен «нет», а по факту «да». Ошибочный признак уезжает в
сырые выгрузки, а оттуда — в ключ меппинга, и строка получает не тот счёт_фо.
Справочник при этом исправен (комбинация с признаком «нет» в нём есть), поэтому
шаг 13 отрабатывает штатно: конвейер собирает «зелёный» отчёт, а ошибка остаётся
невидимой. Править итоговый отчёт бессмысленно — он уже собран из неверных данных.

Как
---
Шаги 12а и 18а применяют правила из файла `_INPUT_DATA/_corrections/Правки.xlsx`
ДО маппинга: 12а — до шага 13 (баланс), 18а — до шага 19 (ОПУ). Признак меняется,
ключ меняется, счёт_фо тянется из `Меппинг_бб`/`Меппинг_опу` обычным порядком.
Справочник остаётся единственной точкой правды для «куда попасть».

Принципы
--------
* **Ничего не применяется молча.** Каждое правило либо попадает в аудит
  «Применённые правки» с суммой и числом строк, либо — в «Не применились» с
  причиной. Правило, которое «просто ничего не сделало», — худший исход:
  бухгалтер увидит зелёный отчёт и решит, что договор поправлен.
* **Правка не стоп, но громкая.** Неприменённое правило — WARNING, а не
  исключение: возможно, бухгалтер уже исправил данные в 1С. Исключение
  бросается только когда правила нельзя понять в принципе (неизвестный признак)
  или файл сломан — тогда это ошибка входных данных (см. io_module/corrections.py).
* **Приоритет при конфликте — последняя строка файла.** Файл правил читает
  человек, порядок строк в нём осмыслен; но конфликт обязательно логируется
  WARNING, а не разрешается тихо.
* **Суммы не меняются.** Правится только то, какой счёт ФО получает сальдо.
  Сальдо по ОСВ, обороты и сверки остаются прежними. Если стороны «актив/пассив»
  у старого и нового счёта ФО разные — шаг 13 честно напишет ERROR о расхождении
  баланса: это правильный сигнал (признак был поставлен неверно), а не регресс.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import to_relative
from io_module.corrections import get_corrections
from io_module.data_io import DataSaver
from io_module.output_manager import get_output_dir, get_run_id
from pipeline.constants import ColumnNames
from pipeline.errors import ReferenceMismatchError
from pipeline.step_config import ManualCorrectionsConstants as MC


def _text(series: pd.Series, index: pd.Index) -> pd.Series:
    """Строковое представление серии для сравнения: без регистра и краевых пробелов."""
    return series.astype("string").str.strip().str.casefold().reindex(index)


def _missing_like(index: pd.Index) -> pd.Series:
    """Серия из пустых значений той же длины, что и кадр."""
    return pd.Series(pd.NA, index=index, dtype="string")


class ManualCorrectionsMixin:
    """Применение правил файла Правки.xlsx к одному кадру (ОСВ или журнал)."""

    # ─── Что именно правит шаг (задаётся наследником) ───
    CORRECTION_AREA: str = ""          # MC.AREA_BALANCE / MC.AREA_OPU
    CORRECTION_AREA_LABEL: str = ""    # человекочитаемое имя области в логе и отчёте
    CORRECTION_TARGET_ATTR: str = ""   # атрибут контекста: summary_osv_df / journal_df
    CORRECTION_TARGET_LABEL: str = ""  # что за кадр: «сводная ОСВ» / «журнал ОПУ»
    CORRECTION_AMOUNT_COLS: tuple[str, ...] = ()  # числовые колонки для суммы в аудите
    CORRECTION_ALLOWED_FEATURES: tuple[str, ...] = ()  # какие признаки вообще меняем
    # Колонки «кто именно задет» — попадают в лист «Затронутые строки» и служат
    # предметом проверки на слишком широкое правило (см. _broad_rule_note).
    CORRECTION_DETAIL_COLUMNS: tuple[str, ...] = ()

    # =========================================================================
    # ОТБОР ПРАВИЛ ДЛЯ ЭТОГО ШАГА
    # =========================================================================

    def _select_rules(self, corrections: pd.DataFrame, context) -> pd.DataFrame:
        """Оставляет правила этой области, компании и периода.

        Чужие компании и периоды — не проблема: один файл правил обслуживает все
        прогоны, и молчаливый пропуск здесь штатен. WARNING поднимается только
        для значений, которые не совпадают ни с текущей компанией, ни с любой
        компанией справочника: скорее всего, это опечатка в имени.
        """
        if corrections.empty:
            return corrections

        index = corrections.index
        area = (
            _text(corrections[MC.COL_AREA], index)
            if MC.COL_AREA in corrections
            else _missing_like(index)
        )
        mask = area.eq(self.CORRECTION_AREA)

        mask &= self._scope_mask(corrections, MC.COL_COMPANY, context)
        mask &= self._scope_mask(corrections, MC.COL_PERIOD, context, current=str(context.period or ""))

        return corrections.loc[mask].reset_index(drop=True)

    def _scope_mask(
        self,
        corrections: pd.DataFrame,
        column: str,
        context,
        current: str = "",
    ) -> pd.Series:
        """Маска правил, относящихся к текущей компании/периоду (пусто = любая).

        Важно: в маску попадают только пустые значения и значения, совпадающие
        с ТЕКУЩИМ прогоном. Полный список компаний справочника нужен для
        другого — чтобы отличить «правило для другой компании» (молчаливый
        пропуск, это штатно) от «опечатка в имени» (WARNING). Если отбирать по
        всему справочнику, чужие правила попадут в прогон.
        """
        index = corrections.index
        if column not in corrections.columns:
            return pd.Series(True, index=index)

        values = _text(corrections[column], index)
        target = (current or (context.company or "")).strip().casefold()

        mask = values.isna() | values.eq(target)

        known = {target}
        if column == MC.COL_COMPANY:
            companies = (context.references or {}).get("компании_группы")
            if isinstance(companies, pd.DataFrame) and ColumnNames.SHORT_COMPANY_NAME in companies:
                known |= set(_text(companies[ColumnNames.SHORT_COMPANY_NAME], companies.index))

        unknown = values.notna() & ~values.isin(known)
        if unknown.any():
            logger.warning(
                "[!] Правки: строки файла с неизвестным значением в колонке «{}» — "
                "{} — пропущены. Проверьте написание: текущая компания «{}».",
                column,
                sorted(values[unknown].unique().tolist()),
                context.company,
            )
        return mask

    def _validate_features(self, rules: pd.DataFrame) -> None:
        """Проверяет, что правила меняют признаки, которые участвуют в маппинге.

        Без этой проверки опечатка в «признаке» переписывала бы несуществующий
        столбец (тихо, в никуда), а опечатка в имени существующего дала бы правило,
        которое шаг 13 в ключ маппинга не возьмёт, — и ошибка уехала бы дальше
        незамеченной.

        Raises:
            ReferenceMismatchError: в правилах есть признак вне ключа маппинга.
        """
        if not self.CORRECTION_ALLOWED_FEATURES:
            return

        bad = sorted(set(rules[MC.COL_FEATURE]) - set(self.CORRECTION_ALLOWED_FEATURES))
        if bad:
            problem = rules.loc[rules[MC.COL_FEATURE].isin(bad)].reset_index(drop=True)
            raise ReferenceMismatchError(
                f"В файле ручных корректировок указаны признаки, которые не "
                f"участвуют в ключе маппинга {self.CORRECTION_AREA_LABEL}: {bad}. "
                f"Допустимые признаки: {list(self.CORRECTION_ALLOWED_FEATURES)}",
                problem_data=problem,
                reference_name="Правки (ручные корректировки)",
            )

    # =========================================================================
    # ПРИМЕНЕНИЕ ПРАВИЛ
    # =========================================================================

    @staticmethod
    def _selectors_of(rule: pd.Series) -> dict[str, str]:
        """Непустые селекторы правила: все колонки, кроме служебных."""
        return {
            col: value
            for col, value in rule.items()
            if col not in MC.RESERVED_COLUMNS and pd.notna(value)
        }

    @staticmethod
    def _describe_selectors(selectors: dict[str, str]) -> str:
        """Человекочитаемый вид селектора для лога и отчёта."""
        return "; ".join(f"{col}={value}" for col, value in selectors.items()) or "—"

    @staticmethod
    def _selector_mask(df: pd.DataFrame, selectors: dict[str, str]) -> pd.Series:
        """Маска строк кадра, подходящих под селектор правила.

        Один векторизованный .eq на селектор: журнал ОПУ — сотни тысяч строк,
        построчная проверка дала бы минуты вместо секунд.
        """
        mask = pd.Series(True, index=df.index)
        for col, value in selectors.items():
            mask &= _text(df[col], df.index).eq(str(value).strip().casefold())
        return mask

    def _amount_sums(self, df: pd.DataFrame, mask: pd.Series) -> dict[str, Any]:
        """Суммы по затронутым строкам — чтобы бухгалтер оценил существенность."""
        sums: dict[str, Any] = {}
        for col in self.CORRECTION_AMOUNT_COLS:
            if col in df.columns:
                sums[col] = float(pd.to_numeric(df.loc[mask, col], errors="coerce").sum())
        return sums

    def _detail_columns(self, df: pd.DataFrame, feature: str) -> list[str]:
        """Колонки листа «Затронутые строки»: кто задет + суммы + сам признак."""
        cols = [col for col in self.CORRECTION_DETAIL_COLUMNS if col in df.columns]
        cols += [col for col in self.CORRECTION_AMOUNT_COLS if col in df.columns]
        if feature in df.columns and feature not in cols:
            cols.append(feature)
        return cols

    def _broad_rule_note(self, df: pd.DataFrame, mask: pd.Series, selectors: dict[str, str]) -> str:
        """Насколько правило шире задуманного: чем подробнее описание, тем честнее отчёт.

        Правило «счёт 60.01 + допсубконто = Анисимов» может зацепить не одну
        строку, а все строки этого контрагента по всем видам расчётов — бухгалтер
        об этом узнает только из лога. Здесь считаем, сколько разных значений
        лежит в колонках, которые правило НЕ перечислило, и говорим об этом
        прямо: «задело 7 строк, у них 3 разных субконто».

        Returns:
            Текст заметки (пустой, если правило узкое) — попадает в лог и в
            лист «Применённые правки».
        """
        notes: list[str] = []
        for col in self.CORRECTION_DETAIL_COLUMNS:
            if col in selectors or col not in df.columns:
                continue
            values = sorted({str(v) for v in df.loc[mask, col].dropna().tolist()})
            if len(values) <= 1:
                continue
            preview = MC.BROAD_RULE_PREVIEW
            shown = "; ".join(values[:preview])
            more = f" и ещё {len(values) - preview}" if len(values) > preview else ""
            notes.append(f"{col}: {len(values)} значений ({shown}{more})")

        if not notes:
            return ""
        note = "; ".join(notes)
        logger.warning(
            "[!] Правки: правило задело {} строк с разными значениями неуказанных "
            "селекторов — {}. Если нужна не вся выборка, добавьте их в правило.",
            int(mask.sum()), note,
        )
        return note

    def _affected_frame(
        self,
        df: pd.DataFrame,
        mask: pd.Series,
        feature: str,
        before: str,
        new_value: str,
        limit: int,
    ) -> Optional[pd.DataFrame]:
        """Срез задетых строк для листа «Затронутые строки».

        limit — сколько строк ещё можно показать. None, если нечего показывать
        (лимит исчерпан): возврат None тише пустого листа, но и не теряет данные
        молча — о срезе пишет вызывающий.
        """
        if limit <= 0:
            return None

        affected = df.loc[mask].head(limit)
        if affected.empty:
            return None
        return affected.loc[:, self._detail_columns(df, feature)].assign(
            **{
                "признак": feature,
                "было": before,
                "стало": new_value,
            }
        )

    def _apply_rules(
        self,
        frame: pd.DataFrame,
        rules: pd.DataFrame,
        context,
    ) -> tuple[pd.DataFrame, list[dict], list[dict], Optional[pd.DataFrame], int]:
        """Применяет правила к кадру по порядку строк файла.

        Returns:
            (кадр с изменениями, записи применённых правил, записи неприменённых,
             срез задетых строк для аудита, сколько строк в срезе усечено)
        """
        df = frame.copy()
        if MC.MARKER_COL not in df.columns:
            df[MC.MARKER_COL] = pd.Series(pd.NA, index=df.index, dtype="string")

        applied: list[dict] = []
        skipped: list[dict] = []
        details: list[pd.DataFrame] = []
        detail_total = 0
        detail_limit = MC.AUDIT_DETAIL_ROW_LIMIT
        touched: dict[str, set] = {}  # признак -> индексы строк, уже изменённые им

        for _, rule in rules.iterrows():
            record = self._rule_record(rule)
            selectors = self._selectors_of(rule)
            feature = str(rule[MC.COL_FEATURE])
            new_value = str(rule[MC.COL_NEW_VALUE])
            old_value = rule.get(MC.COL_OLD_VALUE)
            record["селектор"] = self._describe_selectors(selectors)

            reason = self._check_rule(df, selectors, feature)
            if reason:
                skipped.append({**record, "причина": reason})
                self._log_skipped(record, reason)
                continue

            mask = self._selector_mask(df, selectors)

            if pd.notna(old_value):
                current = _text(df[feature], df.index)
                matches_old = current.eq(str(old_value).strip().casefold())
                if not (mask & matches_old).any():
                    reason = (
                        f"«старое_значение» = «{old_value}» не совпало ни с одной строкой: "
                        f"по этому селектору признак «{feature}» уже другой"
                    )
                    skipped.append({**record, "причина": reason})
                    self._log_skipped(record, reason)
                    continue
                stale = int((mask & ~matches_old).sum())
                mask &= matches_old
                if stale:
                    logger.warning(
                        "[!] Правки: {} строк по правилу «{}: {} → {}» не изменены — "
                        "их признак уже не равен «{}».",
                        stale, feature, old_value, new_value, old_value,
                    )

            conflict = touched.get(feature, set()) & set(df.index[mask])
            if conflict:
                logger.warning(
                    "[!] Правки: {} строк меняются признаком «{}» повторно "
                    "(два правила в файле) — применено последнее правило.",
                    len(conflict), feature,
                )

            before = self._current_values(df.loc[mask, feature])
            note = self._broad_rule_note(df, mask, selectors)
            detail_total += int(mask.sum())
            affected = self._affected_frame(
                df, mask, feature, before, new_value,
                detail_limit - sum(len(part) for part in details),
            )
            if affected is not None:
                details.append(affected)

            df.loc[mask, feature] = new_value
            self._mark_rows(df, mask, f"{feature}: {before} → {new_value}")
            touched.setdefault(feature, set()).update(df.index[mask])

            record.update({
                "было": before,
                "стало": new_value,
                "строк": int(mask.sum()),
                "также задело": note,
                **self._amount_sums(df, mask),
            })
            applied.append(record)
            self._log_applied(record)

        # Признаки возвращаем в 'string': присваивание в столбец типа object
        # оставило бы его object, а базовый шаг (_validate_output) запрещает
        # object в кадре — прогон упал бы уже на валидации выхода.
        for feature in {item["признак"] for item in applied}:
            if feature in df.columns and df[feature].dtype == "object":
                df[feature] = df[feature].astype("string")

        detail_frame = pd.concat(details, ignore_index=True) if details else None
        truncated = detail_total - (len(detail_frame) if detail_frame is not None else 0)
        if truncated > 0:
            logger.warning(
                "[!] Правки: лист «{}» покажет {} из {} задетых строк (лимит {}) — "
                "полный перечень по этой области смотрите на листе «исходники» "
                "по колонке «{}».",
                MC.AUDIT_SHEET_DETAIL,
                detail_total - truncated,
                detail_total,
                MC.AUDIT_DETAIL_ROW_LIMIT,
                MC.MARKER_COL,
            )
        return df, applied, skipped, detail_frame, truncated

    def _check_rule(self, df: pd.DataFrame, selectors: dict[str, str], feature: str) -> str:
        """Причины, по которым правило применить нельзя (пустая строка — можно)."""
        if not selectors:
            return (
                "не заполнен ни один селектор — правило затронуло бы весь кадр; "
                "минимум — колонка «счет»"
            )
        unknown_cols = [col for col in selectors if col not in df.columns]
        if unknown_cols:
            return f"в кадре нет столбцов: {unknown_cols}"
        if feature not in df.columns:
            return f"в кадре нет столбца признака «{feature}»"
        if not self._selector_mask(df, selectors).any():
            return "не найдено строк по селектору"
        return ""

    @staticmethod
    def _current_values(series: pd.Series) -> str:
        """Текущие значения признака в изменяемых строках: «да» либо «да и нет (5)»."""
        values = sorted({str(v) for v in series.tolist()})
        if len(values) == 1:
            return values[0]
        if len(values) > 3:
            return "/".join(values[:3]) + f" и ещё {len(values) - 3}"
        return " / ".join(values)

    @staticmethod
    def _mark_rows(df: pd.DataFrame, mask: pd.Series, label: str) -> None:
        """Ставит колонку-маркер: видно в «исходниках», что строку правили.

        Несколько правок одной строки накапливаются в одном тексте
        («инвест_договор: нет → да; вид_связи: 3 лица → Связанная»), а не
        затирают друг друга.
        """
        prior = df.loc[mask, MC.MARKER_COL]
        has_prior = prior.notna()
        merged = np.where(
            has_prior,
            prior.fillna("").astype(str) + "; " + label,
            label,
        )
        df.loc[mask, MC.MARKER_COL] = pd.Series(merged, index=prior.index, dtype="string")

    @staticmethod
    def _rule_record(rule: pd.Series) -> dict[str, Any]:
        """Общая часть записи аудита для применённого и неприменённого правила."""
        return {
            MC.SUMMARY_ENTRY_AREA: rule.get(MC.COL_AREA),
            "признак": rule.get(MC.COL_FEATURE),
            "новое_значение": rule.get(MC.COL_NEW_VALUE),
            "комментарий": rule.get(MC.COL_COMMENT),
        }

    # =========================================================================
    # ЛОГИРОВАНИЕ ПРАВИЛ
    # =========================================================================

    # Служебные колонки записи аудита — их значения не считаются суммами
    _RECORD_META = frozenset({
        MC.SUMMARY_ENTRY_AREA, "признак", "новое_значение", "комментарий",
        "селектор", "было", "стало", "строк", "также задело",
    })

    @classmethod
    def _log_applied(cls, record: dict[str, Any]) -> None:
        amounts = ", ".join(
            f"{col} = {value:,.2f}"
            for col, value in record.items()
            if col not in cls._RECORD_META
        )
        tail = f" ({amounts})" if amounts else ""
        logger.info(
            "Правка применена: «{}» — {} → {} по правилу [{}], затронуто строк: {}{}",
            record.get("признак"),
            record.get("было"),
            record.get("стало"),
            record.get("селектор"),
            record.get("строк"),
            tail,
        )

    @staticmethod
    def _log_skipped(record: dict[str, Any], reason: str) -> None:
        logger.warning(
            "[!] Правка не применена: «{}» = «{}» по правилу [{}] — {}. "
            "Проверьте правило в файле Правки.xlsx.",
            record.get("признак"),
            record.get("новое_значение"),
            record.get("селектор"),
            reason,
        )

    # =========================================================================
    # АУДИТ
    # =========================================================================

    def _save_audit(
        self,
        applied: list[dict],
        skipped: list[dict],
        detail: Optional[pd.DataFrame],
        context,
    ) -> None:
        """Пишет отчёт о правках в mismatches/ тремя листами.

        «Применённые правки» отвечает на вопрос «что изменилось и на какую сумму»,
        «Не применились» — на «почему мой договор всё ещё попал не туда» (без него
        правило, которое не сработало, выглядит как «сработало»), «Затронутые строки»
        — на «а какие именно строки попали под правило». Последний лист отвечает
        на вопрос, который агрегат не отвечает: правило «счёт + контрагент» могло
        зацепить не одну строку, а все строки этого контрагента по видам расчётов.
        """
        if not applied and not skipped:
            return

        # Имя файла и сам путь считаются внутри try: непригодный аудит не должен
        # ронять прогон — данные уже поправлены, терять отчёт из-за подписи
        # к диагностике незачем.
        try:
            filename = (
                f"{MC.AUDIT_PREFIX}{self.CORRECTION_AREA}_{context.company}_{get_run_id()}.xlsx"
            )
            output_path = get_output_dir("mismatches") / filename
            with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
                self._to_sheet(pd.DataFrame(applied), writer, MC.AUDIT_SHEET_APPLIED)
                self._to_sheet(pd.DataFrame(skipped), writer, MC.AUDIT_SHEET_SKIPPED)
                self._to_sheet(
                    detail if detail is not None else pd.DataFrame(),
                    writer,
                    MC.AUDIT_SHEET_DETAIL,
                )
            logger.info("[FOLDER] Отчёт о ручных правках сохранён: {}", to_relative(output_path))
        except PermissionError:
            logger.warning(
                "[!] Не удалось сохранить отчёт о ручных правках: файл открыт "
                "в другой программе"
            )
        except Exception as exc:  # noqa: BLE001 — аудит не должен ронять прогон
            logger.warning("[!] Ошибка сохранения отчёта о ручных правках: {}", exc)

    @staticmethod
    def _to_sheet(rows: pd.DataFrame, writer: pd.ExcelWriter, sheet_name: str) -> None:
        """Пишет лист аудита с общим форматированием (шапка, автофильтр)."""
        frame = rows if not rows.empty else pd.DataFrame({"правил": []})
        frame.to_excel(writer, sheet_name=sheet_name, index=False)
        DataSaver._apply_excel_formatting(writer.sheets[sheet_name])

    def _update_summary(
        self,
        applied: list[dict],
        skipped: list[dict],
        context,
    ) -> None:
        """Дополняет сводку правок в context.data — её читает титульный лист."""
        if not applied and not skipped:
            return

        summary = context.data.get(MC.SUMMARY_KEY) or {
            MC.SUMMARY_FILE: "Правки.xlsx",
            MC.SUMMARY_APPLIED_RULES: 0,
            MC.SUMMARY_APPLIED_ROWS: 0,
            MC.SUMMARY_SKIPPED_RULES: 0,
            MC.SUMMARY_BY_AREA: [],
            MC.SUMMARY_HAS_PROBLEMS: False,
        }

        rows = sum(int(item.get("строк") or 0) for item in applied)
        summary[MC.SUMMARY_APPLIED_RULES] += len(applied)
        summary[MC.SUMMARY_APPLIED_ROWS] += rows
        summary[MC.SUMMARY_SKIPPED_RULES] += len(skipped)
        summary.setdefault(MC.SUMMARY_BY_AREA, []).append({
            MC.SUMMARY_ENTRY_AREA: self.CORRECTION_AREA_LABEL,
            MC.SUMMARY_ENTRY_RULES: len(applied),
            MC.SUMMARY_ENTRY_ROWS: rows,
        })
        if skipped:
            summary[MC.SUMMARY_HAS_PROBLEMS] = True
        context.data[MC.SUMMARY_KEY] = summary

    # =========================================================================
    # ОСНОВНОЙ ХОД ШАГА
    # =========================================================================

    def _process(self, context):
        """Правит признаки в кадре этого шага (реализация _process шага)."""
        corrections = get_corrections(context)
        if corrections is None or corrections.empty:
            logger.debug(
                "Ручные корректировки: файла нет или он пуст — {} без правок",
                self.CORRECTION_TARGET_LABEL,
            )
            return context

        rules = self._select_rules(corrections, context)
        if rules.empty:
            logger.debug(
                "Ручные корректировки: правил для области «{}» (компания {}, период {}) нет",
                self.CORRECTION_AREA_LABEL, context.company, context.period,
            )
            return context

        self._validate_features(rules)

        frame = getattr(context, self.CORRECTION_TARGET_ATTR, None)
        if frame is None or frame.empty:
            logger.warning(
                "[!] Ручные правки: {} пуст — правила области «{}» не применены",
                self.CORRECTION_TARGET_LABEL, self.CORRECTION_AREA_LABEL,
            )
            return context

        df, applied, skipped, detail, _ = self._apply_rules(frame, rules, context)
        setattr(context, self.CORRECTION_TARGET_ATTR, df)

        self._save_audit(applied, skipped, detail, context)
        self._update_summary(applied, skipped, context)

        touched_rows = sum(int(item.get("строк") or 0) for item in applied)
        logger.info(
            "[OK] Ручные правки ({}): применено {} правил на {} строк",
            self.CORRECTION_AREA_LABEL, len(applied), touched_rows,
        )
        return context

