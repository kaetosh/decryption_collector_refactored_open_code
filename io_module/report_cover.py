# -*- coding: utf-8 -*-
"""
Титульный лист итогового отчёта (лист «О отчёте»).

Зачем он нужен: файл с расшифровкой уходит получателю по почте, и вместе
с ним уходят все данные о запуске — какая компания, какой период, какие
допуски применялись, свёден ли отчёт и в каком режиме шла сборка. Без
титульного листа получатель видит четыре листа с колонками и вынужден
догадываться, какой из них отчётный, а какой служебный.

Лист собирается из уже готовых данных контекста (pipeline/base.py,
ProcessingContext): параметры компании, ключевые цифры отчёта
(collect_figures), применённые допуски, сводку предупреждений прогона,
диагностику увязки ЧП = НРП и результат сверки ОСВ с выгрузками.
Ничего нового конвейер не вычисляет — существующие значения просто
переносятся в файл.

Ключевые цифры считает collect_figures(context) — единственный источник
для титульного листа и для консольной сводки (io_module/run_summary.py):
файл и консоль не могут разойтись в числах. Прибыль не показывается
(итог листа ОПУ не равен прибыли периода) — вместо неё выводится
статус увязки ЧП = НРП.

Правила, которые модуль соблюдает:
- не импортирует pipeline.steps/base — работает по атрибутам контекста
  (io_module не должен знать о шагах);
- любая ошибка при сборе строк не должна ломать сохранение отчёта:
  весь публичный вызов обёрнут в try/except и при неудаче даёт пустой
  лист-заглушку;
- названия листов отчёта объявлены здесь и импортируются в
  io_module/data_io.py — одна точка правды для имён и для легенды.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from loguru import logger
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from config.defaults import (
    DEFAULTS,
    TOLERANCE_DESCRIPTIONS,
    format_amount,
    format_tolerance_value,
)
from config.settings import (
    EXPORT_REPORT_ON_MISMATCH,
    SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR,
    STRICT_ASSET_SALE_DISTRIBUTION_CHECK,
    STRICT_CONTRACTOR_CHECK,
    STRICT_CREDIT_CONTRACTOR_CHECK,
    STRICT_OS_GROUP_CHECK,
    STRICT_PPA_MAPPING_CHECK,
)
from io_module.auto_sort import EXCEL_TEMP_PREFIX
from io_module.output_manager import get_run_dir
from pipeline.step_config import OpuReportConstants, ReconciliationConstants, ReportLayoutConstants
from utils.currency_utils import needs_conversion

# ─────────────────────────────────────────────────────────────────────────────
# Имена листов и колонок титульного листа
# ─────────────────────────────────────────────────────────────────────────────
COVER_SHEET = "О отчёте"
COVER_LABEL_HEADER = "Показатель"
COVER_VALUE_HEADER = "Значение"

# Листы итогового отчёта. Импортируются в io_module/data_io.py при
# сохранении, чтобы имя листа и его описание в легенде не расходились.
SHEET_BALANCE = "Расшифровка_ББЛ"
SHEET_BALANCE_SOURCE = "исходники ББЛ"
SHEET_PNL = "Расшифровка_ОПУ"
SHEET_PNL_SOURCE = "исходники ОПУ"

# Цвета ярлычков листов в Excel: отчётные — зелёный, служебные — серый,
# титульный — синий. Нужен, чтобы в списке листов было видно, что сдавать.
TAB_COLOR_REPORT = "1E7B34"
TAB_COLOR_SOURCE = "808080"
TAB_COLOR_COVER = "1F4E79"

# Ширины колонок титульного листа (символы Excel)
COVER_LABEL_WIDTH = 46
COVER_VALUE_WIDTH = 100

# Сколько предупреждений перечислять на титульном листе. Больше — шум:
# получатель файла не будет читать сотню строк, а полный список есть в логе.
COVER_WARNINGS_LIMIT = 12

# Подпапки папки запуска с диагностикой: имя -> что внутри
DIAGNOSTIC_SUBFOLDERS: tuple[tuple[str, str], ...] = (
    ("mismatches", "проблемные данные — требуют разбора"),
    ("warnings", "данные, восстановленные программой автоматически"),
)

# Диагностика, которая пишется при ШТАТНОЙ работе шагов, а не из-за ошибки.
# Один префикс на папку `mismatches` вводил в заблуждение: подпись «требуют
# правки» стояла и у файлов, которые пишутся при каждом прогоне по умолчанию
# и ничего не требуют. Подпись выбирается по имени файла (см. _diagnostic_hint).
#   reference_scope_*        — аудит применённых индивидуальных правок
#                              меппинга (pipeline/executors.py);
#   step20/step21_collapse_* — отчёты свёртки статей ОПУ и баланса.
DIAGNOSTIC_INFORMATIONAL_PREFIXES: tuple[str, ...] = (
    "reference_scope_",
    "step20_collapse_opu_",
    "step21_collapse_balance_",
)
INFORMATIONAL_HINT = "сводная информация о работе программы, правок не требует"

# Служебные файлы Excel (~$...) не показываем в легенде диагностики
_SORT_REPORT_NAME = "sort_report.xlsx"

# Строка ОПУ верхнего уровня, которую показываем как выручку. Значение
# колонки «1 уровень» приходит из справочника мэппинга ОПУ, а не из кода,
# поэтому ищется по названию; нет такой строки — не показываем ноль.
REVENUE_LEVEL = "Выручка"


def is_section_header(value: str) -> bool:
    """Заголовок раздела — строка без значения во второй колонке."""
    return value == ""


# ─────────────────────────────────────────────────────────────────────────────
# Сборка содержимого титульного листа
# ─────────────────────────────────────────────────────────────────────────────
def build_cover_rows(
    context: Any,
    warnings: Optional[list[str]] = None,
) -> list[tuple[str, str]]:
    """
    Собирает строки титульного листа как пары (показатель, значение).

    Строка-заголовок раздела — пара с пустым значением (см. is_section_header).

    Args:
        context: ProcessingContext текущего запуска.
        warnings: Сводка предупреждений прогона — готовые строки из
            logging_handling.logger_config.format_warnings_summary()
            (текст и количество повторов в одной строке). None — сводка
            недоступна, раздел не создаётся. Показываются первые
            COVER_WARNINGS_LIMIT строк, остальное — одной строкой со
            ссылкой на лог.

    Returns:
        Список пар (показатель, значение).
    """
    rows: list[tuple[str, str]] = []

    rows.append(("ЗАКАЗ", ""))
    rows.extend(_order_rows(context))
    rows.append(("", ""))

    rows.append(("СОСТОЯНИЕ ОТЧЁТА", ""))
    rows.extend(_status_rows(context, warnings))
    rows.append(("", ""))

    # Цифры — сразу после статуса: получатель файла видит результат,
    # не открывая «Расшифровку_ББЛ». Те же пары печатаются в консоли
    # (io_module/run_summary.py), поэтому файл и консоль совпадают.
    rows.append(("КЛЮЧЕВЫЕ ЦИФРЫ ОТЧЁТА", ""))
    rows.extend(collect_figures(context))

    if warnings is not None:
        rows.append(("", ""))
        rows.extend(_warning_rows(warnings))
    rows.append(("", ""))

    rows.append(("РЕЖИМЫ СБОРКИ", ""))
    rows.append((
        "Режимы, влияющие на цифры отчёта",
        "сменить — config/settings.py, значения действуют до следующего запуска",
    ))
    rows.extend(_mode_rows())
    rows.append(("", ""))

    rows.append(("ПРИМЕНЁННЫЕ ДОПУСКИ СХОДИМОСТИ", ""))
    rows.extend(_tolerance_rows(context))
    rows.append(("", ""))

    rows.append(("СОСТАВ ФАЙЛА", ""))
    rows.extend(_legend_rows())
    rows.append(("", ""))

    diagnostics = _diagnostic_rows()
    if diagnostics:
        rows.append(("ФАЙЛЫ ДИАГНОСТИКИ ЭТОГО ПРОГОНА", ""))
        rows.extend(diagnostics)

    return rows


def _order_rows(context: Any) -> list[tuple[str, str]]:
    """Кто, за какой период и из какого файла собран отчёт."""
    period = context.period or "—"
    if context.type_period:
        period = f"{period} (тип периода: {context.type_period})"

    return [
        ("Компания", context.company or "—"),
        ("Период отчётности", period),
        ("Сегмент", context.segment or "—"),
        ("Валюта остатков", _currency_text(context)),
        ("Файл общей ОСВ", context.name_file_general_osv or "—"),
        ("Идентификатор запуска", context.run_id or "—"),
        ("Дата формирования отчёта", datetime.now().strftime("%d.%m.%Y %H:%M:%S")),
    ]


def _currency_text(context: Any) -> str:
    """Валюта и дата перевода остатков (для валютных компаний)."""
    currency = (context.currency or "RUB").upper()
    if currency == "RUB":
        return "RUB — перевод остатков в рубли не требуется"
    if context.balance_date:
        return f"{currency} — остатки переведены в рубли на {context.balance_date}"
    return f"{currency} — дата перевода остатков не задана"


def _reference_scope_text(context: Any) -> str:
    """
    Индивидуальный меппинг компании: сколько строк справочника (Меппинг_опу /
    Меппинг_бб) правилось именно для этой компании.

    Сводку заполняет pipeline.executors._apply_company_reference_scope
    (ключ и имена полей — OpuReportConstants.REFERENCE_SCOPE_*) до старта
    конвейера.
    """
    summary = (
        (context.data or {}).get(OpuReportConstants.REFERENCE_SCOPE_SUMMARY_KEY) or {}
    )
    entries = summary.get("entries") or []

    if entries:
        parts = []
        for entry in entries:
            sheet = entry.get(OpuReportConstants.REFERENCE_SCOPE_ENTRY_SHEET, '—')
            individual = entry.get(
                OpuReportConstants.REFERENCE_SCOPE_ENTRY_INDIVIDUAL, 0)
            overridden = entry.get(
                OpuReportConstants.REFERENCE_SCOPE_ENTRY_OVERRIDDEN, 0)
            text = f"{sheet}: {individual} строк (перекрыто универсальных — {overridden})"
            # Строки, отброшенные как чужие, — часть результата, а не пустота:
            # без них получатель видит «правок нет» и не понимает, почему в
            # справочнике статья даёт два типа, а в отчёте — один.
            dropped = entry.get(
                OpuReportConstants.REFERENCE_SCOPE_ENTRY_DROPPED, 0)
            if dropped:
                text += f", отброшено строк других компаний — {dropped}"
            parts.append(text)
        return f"применён — {'; '.join(parts)}"

    if summary.get(OpuReportConstants.REFERENCE_SCOPE_UNKNOWN_VALUES):
        return (
            "не применён — в колонке «компания» есть неизвестные значения "
            "(см. предупреждения и mismatches/)"
        )

    return "не применялся (правок для компании нет)"


def _status_rows(context: Any, warnings: Optional[list[str]]) -> list[tuple[str, str]]:
    """Сводится ли отчёт, прошла ли сверка выгрузок, сколько было предупреждений."""
    rows = [("Увязка ОПУ и баланса (ЧП = НРП)", pnl_balance_status_text(context))]

    reconciliation = (context.data or {}).get(ReconciliationConstants.SUMMARY_KEY)
    if reconciliation:
        tolerance = reconciliation.get("tolerance")
        if tolerance is not None:
            rows.append((
                "Сверка выгрузок с Общей ОСВ",
                f"ПРОЙДЕНА — расхождения в пределах "
                f"{format_tolerance_value('tolerance_reconciliation', tolerance)}",
            ))

    if warnings is None:
        warnings_text = "сводка недоступна (см. лог прогона)"
    elif warnings:
        warnings_text = f"{len(warnings)} — см. раздел ниже"
    else:
        warnings_text = "нет"
    rows.append(("Предупреждений за прогон", warnings_text))
    rows.append(("Индивидуальный меппинг компании", _reference_scope_text(context)))

    for label, df_name in (
        ("Строк в расшифровке баланса", "balance_df"),
        ("Строк в расшифровке ОПУ", "pnl_df"),
    ):
        df = getattr(context, df_name, None)
        rows.append((label, f"{len(df)}" if df is not None else "—"))

    return rows


def pnl_balance_status_text(context: Any) -> str:
    """
    Текст статуса увязки ОПУ и баланса.

    Диагностику пишет шаг 19 при EXPORT_REPORT_ON_MISMATCH=True: отчёт
    выгружается несведённым, для анализа. Титульный лист фиксирует это
    явно — иначе получатель файла примет его за отчётность.

    Функция публичная: тот же статус печатается в консоли в конце
    прогона (io_module/run_summary.py), чтобы вывод совпадал с файлом.
    """
    diagnostics = (context.data or {}).get(OpuReportConstants.MISMATCH_DIAGNOSTICS_KEY)
    if not diagnostics:
        return "СВЕДЕНО — расхождение в пределах допуска"

    diff = diagnostics.get("diff")
    tolerance = diagnostics.get("tolerance")
    if diff is None:
        return "НЕ СВЕДЕНО — см. лог прогона"
    if tolerance is None:
        return (
            f"НЕ СВЕДЕНО — расхождение {format_amount(diff)} тыс.ед. "
            f"Отчёт выгружен для анализа расхождения и не является отчётностью"
        )
    return (
        f"НЕ СВЕДЕНО — расхождение {format_amount(diff)} тыс.ед. при допуске "
        f"{format_amount(tolerance)} тыс.ед. Отчёт выгружен для анализа расхождения "
        f"и не является отчётностью"
    )


def collect_figures(context: Any) -> list[tuple[str, str]]:
    """
    Ключевые цифры отчёта: актив, пассив, их расхождение и чистая прибыль.

    Единственный источник цифр для двух представлений сразу: титульного
    листа (раздел «КЛЮЧЕВЫЕ ЦИФРЫ ОТЧЁТА») и блока в консоли
    (io_module/run_summary.py). Оба читают эти же пары, поэтому файл и
    консоль физически не могут разойтись в числах.

    Ничего не перепроверяется: суммы берутся из уже собранных
    context.balance_df / context.pnl_df — ровно тех строк, что попали
    в книгу. Чего в списке нет и почему:
    - счётчики строк: на титульном листе они уже есть в «СОСТОЯНИИ ОТЧЁТА».
    """
    figures: list[tuple[str, str]] = []

    active, passive = _balance_sides(getattr(context, "balance_df", None), context)
    if active is None or passive is None:
        figures.append(("Баланс", "расшифровка не собрана — см. лог прогона"))
    else:
        figures.extend([
            ("Актив (итог баланса)", f"{_amount_text(active)} тыс.ед."),
            (
                "Пассив (итог баланса)",
                f"{_amount_text(passive)} тыс.ед. (со знаком минус)",
            ),
            (
                "Расхождение актив - пассив",
                f"{_amount_text(active + passive)} тыс.ед. — "
                f"{_balance_verdict(context, active + passive)}",
            ),
        ])

    net_profit = _net_profit(getattr(context, "pnl_df", None), context)
    if net_profit is not None:
        figures.append(("Чистая прибыль (убыток)", f"{_amount_text(net_profit)} тыс.ед."))

    return figures


def _net_profit(pnl_df: Any, context: Any) -> float | None:
    """
    Чистая прибыль (убыток) из финальной расшифровки ОПУ.

    Сумма по столбцу 'Значение' (или 'Значение_руб' для валютных компаний).
    В отчёте прибыль имеет отрицательный знак, убыток — положительный.
    Для отображения переворачиваем знак: прибыль = положительное, убыток = отрицательное.
    """
    if not _has_columns(pnl_df, ReportLayoutConstants.VALUE_COL):
        return None

    value_col = (
        ReportLayoutConstants.RUB_VALUE_COL
        if needs_conversion(context) and ReportLayoutConstants.RUB_VALUE_COL in pnl_df.columns
        else ReportLayoutConstants.VALUE_COL
    )

    values = pd.to_numeric(pnl_df[value_col], errors="coerce")
    total = float(values.sum())

    if pd.isna(total):
        return None

    # Flip sign: in report profit is negative, loss is positive
    # For display: profit = positive, loss = negative
    return -total


def _balance_sides(balance_df: Any, context: Any) -> tuple[float | None, float | None]:
    """
    Итоги актива и пассива по колонке «Актив/Пассив» расшифровки.

    Пассив в отчёте со знаком «-», поэтому стороны складываются как есть,
    а расхождение — это их сумма (в сведённом отчёте это 0).

    Для валютных компаний использует колонку 'Значение_руб' вместо 'Значение'.
    """
    needed = (ReportLayoutConstants.VALUE_COL, ReportLayoutConstants.ASSET_LIABILITY_COL)
    if not _has_columns(balance_df, *needed):
        return None, None

    value_col = (
        ReportLayoutConstants.RUB_VALUE_COL
        if needs_conversion(context) and ReportLayoutConstants.RUB_VALUE_COL in balance_df.columns
        else ReportLayoutConstants.VALUE_COL
    )

    values = pd.to_numeric(balance_df[value_col], errors="coerce")
    sides = balance_df[ReportLayoutConstants.ASSET_LIABILITY_COL].astype("string").str.strip()
    active = float(values[sides == ReportLayoutConstants.ASSET_SIDE].sum())
    passive = float(values[sides == ReportLayoutConstants.PASSIVE_SIDE].sum())
    return active, passive


def _balance_verdict(context: Any, diff: float) -> str:
    """Сошёлся ли баланс по тому же допуску, что проверяет шаг 13."""
    tolerance = (getattr(context, "tolerance_params", None) or {}).get(
        "tolerance_balance",
        DEFAULTS["tolerance_balance"],
    )
    limit = format_tolerance_value("tolerance_balance", tolerance)
    # Написание «СВЕДЕНО» без ё — как в pnl_balance_status_text, чтобы
    # рядом стоящие строки титульного листа читались одинаково.
    if abs(diff) <= tolerance:
        return f"СВЕДЕНО (допуск {limit})"
    return f"НЕ СВЕДЕНО (допуск {limit})"


def _revenue(pnl_df: Any) -> float | None:
    """
    Выручка из расшифровки ОПУ: модуль суммы строк с 1 уровнем «Выручка».

    В ОПУ доходы имеют знак «-», поэтому показывается модуль суммы.
    pd.to_numeric(errors='coerce') — страховка на случай нечисловых
    значений: колонка «Значение» приходит float64 (её тип проверяет
    _validate_output в pipeline/base.py), но титульный лист не должен
    падать из-за неподготовленных данных.
    """
    if not _has_columns(pnl_df, ReportLayoutConstants.VALUE_COL, ReportLayoutConstants.LEVEL1_COL):
        return None
    mask = pnl_df[ReportLayoutConstants.LEVEL1_COL].astype("string").str.strip() == REVENUE_LEVEL
    if not mask.any():
        return None
    values = pd.to_numeric(pnl_df[ReportLayoutConstants.VALUE_COL], errors="coerce")
    return abs(float(values[mask].sum()))


def _has_columns(df: Any, *columns: str) -> bool:
    return df is not None and all(column in df.columns for column in columns)


def _amount_text(value: float) -> str:
    """
    Сумма с разделителями разрядов и без «-0».

    format_amount округляет до целых, и копеечное расхождение баланса
    (например -0.01) печаталось бы как «-0» — выглядит как ошибка знака.
    """
    if abs(value) < 0.5:
        value = 0.0
    return format_amount(value)


def _warning_rows(warnings: list[str]) -> list[tuple[str, str]]:
    """
    Перечень предупреждений прогона: текст — сколько раз встретился.

    Строки приходят готовыми из logging_handling.logger_config
    (format_warnings_summary) вместе с количеством повторов. Здесь
    они обрезаются до COVER_WARNINGS_LIMIT строк: полный список
    остаётся в логе прогона.
    """
    rows: list[tuple[str, str]] = [("ПРЕДУПРЕЖДЕНИЯ ЗА ПРОГОН", "")]
    if not warnings:
        return rows + [("Предупреждений не было", "прогон прошёл без замечаний")]

    shown = warnings[:COVER_WARNINGS_LIMIT]
    for text in shown:
        if " (×" in text:
            label, _, count = text.rpartition(" (×")
            rows.append((label, f"повторов: {count.rstrip(')')}"))
        else:
            rows.append((text, "1 раз"))

    hidden = len(warnings) - len(shown)
    if hidden > 0:
        rows.append((
            f"...и ещё {hidden} предупреждений",
            "полный список — в логе прогона (app.log)",
        ))
    return rows


def _mode_rows() -> list[tuple[str, str]]:
    """Текущие значения флагов режимов, меняющих результат сборки."""
    modes = (
        (
            "Неизвестные контрагенты",
            STRICT_CONTRACTOR_CHECK,
            "строгий режим: сборка останавливается, строки в mismatches/",
            "мягкий режим: контрагент подставляется «3 лица»",
        ),
        (
            "Группы ОС аренды и лизинга",
            STRICT_OS_GROUP_CHECK,
            "строгий режим: остановка, если договора или РБП нет в справочнике ППА",
            "мягкий режим: группа ОС помечается «не_указано», сборка продолжается",
        ),
        (
            "РБП кредитных линий",
            STRICT_CREDIT_CONTRACTOR_CHECK,
            "строгий режим: остановка, если РБП нет в справочнике КредитОбслуж",
            "мягкий режим: РБП попадает в отчёт, сборка продолжается",
        ),
        (
            "Объекты ОС в справочнике ППА",
            STRICT_PPA_MAPPING_CHECK,
            "строгий режим: остановка, если объект ОС не заведён в ППА",
            "мягкий режим: контрагент заменяется на «3 лица», сборка продолжается",
        ),
        (
            "Распределение расходов 91.02",
            STRICT_ASSET_SALE_DISTRIBUTION_CHECK,
            "строгий режим: нераспределённый остаток останавливает сборку",
            "мягкий режим: остаток сохранён отдельной строкой в отчёте",
        ),
        (
            "Отчёт при несходимости ЧП = НРП",
            EXPORT_REPORT_ON_MISMATCH,
            "отчёт выгружается даже несведённым, статус отмечен выше",
            "сборка останавливается, отчёт не выгружается",
        ),
        (
            "Необязательные спецотчёты",
            SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR,
            "ошибка в необязательном файле останавливает сборку",
            "при ошибке файла сборка продолжается без этой детализации",
        ),
    )
    return [
        (label, strict_text if flag else soft_text)
        for label, flag, strict_text, soft_text in modes
    ]


def _tolerance_rows(context: Any) -> list[tuple[str, str]]:
    """Допуски сходимости, с которыми считался этот отчёт."""
    rows = [
        (
            TOLERANCE_DESCRIPTIONS.get(key, key),
            format_tolerance_value(key, value),
        )
        for key, value in (context.tolerance_params or {}).items()
    ]
    return rows or [("Допуски", "не загружены — взяты значения по умолчанию")]


def _legend_rows() -> list[tuple[str, str]]:
    """Что за лист и передаётся ли он дальше."""
    return [
        (SHEET_BALANCE, "расшифровка баланса — отчётный лист, передаётся"),
        (SHEET_BALANCE_SOURCE, "данные, из которых собран баланс — служебный лист, не передаётся"),
        (SHEET_PNL, "расшифровка ОПУ — отчётный лист, передаётся"),
        (SHEET_PNL_SOURCE, "данные, из которых собран ОПУ — служебный лист, не передаётся"),
        (COVER_SHEET, "этот лист: заказ, режимы, допуски и состав файла"),
    ]


def _diagnostic_rows() -> list[tuple[str, str]]:
    """Файлы диагностики текущего прогона: имя — что внутри."""
    try:
        run_dir = get_run_dir()
    except RuntimeError:
        return []

    rows: list[tuple[str, str]] = []
    for subfolder, hint in DIAGNOSTIC_SUBFOLDERS:
        rows.extend(_files_in_folder(run_dir / subfolder, f"{subfolder}/", hint))

    rows.extend(_files_in_folder(run_dir, "", "что и куда программа разложила файлы из 00_inbox", only=_SORT_REPORT_NAME))
    return rows


def _is_informational(filename: str) -> bool:
    """Файл диагностики создан при штатной работе шага, а не из-за ошибки."""
    return filename.startswith(DIAGNOSTIC_INFORMATIONAL_PREFIXES)


def _diagnostic_hint(filename: str, default_hint: str) -> str:
    """
    Подпись файла диагностики: у штатной информации своя.

    Папка `mismatches` на самом деле неоднородна: туда пишутся и настоящие
    проблемные данные (mismatch_*, missing_files_*), и файлы, которые создаются
    при штатной работе шагов (аудит меппинга, отчёты свёртки). Одна подпись на
    всю папку заставляла получателя искать ошибки там, где их нет.
    """
    if _is_informational(filename):
        return INFORMATIONAL_HINT
    return default_hint


def _files_in_folder(
    folder: Path,
    prefix: str,
    hint: str,
    only: Optional[str] = None,
) -> list[tuple[str, str]]:
    """
    Список файлов папки (без служебных ~$) в виде строк легенды.

    Подпись выбирается по имени файла, а не по папке (см. _diagnostic_hint).
    Порядок: сначала то, что требует разбора, затем штатная информация —
    на титульном листе порядок читается как приоритет. Внутри групп файлы
    остаются отсортированы по имени (сортировка устойчива).
    """
    if not folder.is_dir():
        return []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    files = [
        entry
        for entry in entries
        if entry.is_file()
        and not entry.name.startswith(EXCEL_TEMP_PREFIX)
        and (only is None or entry.name == only)
    ]
    files.sort(key=lambda entry: _is_informational(entry.name))
    return [
        (f"{prefix}{entry.name}", _diagnostic_hint(entry.name, hint))
        for entry in files
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Запись и оформление листа
# ─────────────────────────────────────────────────────────────────────────────
def write_cover_sheet(writer: Any, rows: list[tuple[str, str]]) -> None:
    """
    Записывает титульный лист первым листом книги.

    Вызывается из DataSaver.save_combined_report до записи данных, чтобы
    получатель файла открывал его сразу.

    Args:
        writer: Экземпляр pd.ExcelWriter (engine='openpyxl').
        rows: Строки от build_cover_rows.
    """
    if not rows:
        return

    cover_df = pd.DataFrame(rows, columns=[COVER_LABEL_HEADER, COVER_VALUE_HEADER])
    cover_df.to_excel(writer, sheet_name=COVER_SHEET, index=False)
    format_cover_sheet(writer.sheets[COVER_SHEET])


def format_cover_sheet(worksheet: Any) -> None:
    """
    Оформляет титульный лист: шапка, ширины колонок, перенос текста,
    выделение заголовков разделов.

    Числовой формат не применяется — все значения записаны строками
    (единицы измерения и статусы важнее выравнивания по разрядам).
    """
    header_font = Font(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    section_font = Font(bold=True, size=11)
    section_fill = PatternFill(start_color="DCE6F1", end_color="DCE6F1", fill_type="solid")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for cell in worksheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row_idx in range(2, worksheet.max_row + 1):
        label_cell = worksheet.cell(row=row_idx, column=1)
        value_cell = worksheet.cell(row=row_idx, column=2)
        label_cell.alignment = Alignment(vertical="top", wrap_text=True)
        value_cell.alignment = Alignment(vertical="top", wrap_text=True)

        if is_section_header(value_cell.value or ""):
            label_cell.font = section_font
            label_cell.fill = section_fill
            value_cell.fill = section_fill
        elif label_cell.value:
            label_cell.font = Font(bold=True, size=10)

    for col_idx, width in ((1, COVER_LABEL_WIDTH), (2, COVER_VALUE_WIDTH)):
        worksheet.column_dimensions[worksheet.cell(row=1, column=col_idx).column_letter].width = width

    # Синий ярлычок: лист открывается первым и виден в списке листов книги
    worksheet.sheet_properties.tabColor = TAB_COLOR_COVER


def safe_build_cover_rows(
    context: Any,
    warnings: Optional[list[str]] = None,
) -> list[tuple[str, str]]:
    """
    Обёртка build_cover_rows, которая не может сорвать сохранение отчёта.

    Титульный лист — справочная информация, а не результат расчёта:
    любая ошибка при его сборке (неожиданный тип значения, недоступная
    папка запуска) не должна превращать готовый отчёт в упавший прогон.
    """
    try:
        return build_cover_rows(context, warnings)
    except Exception as exc:
        logger.warning(
            "[!] Не удалось сформировать титульный лист «{}»: {}. "
            "Отчёт сохранён без него.",
            COVER_SHEET,
            exc,
        )
        return []
