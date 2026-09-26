# -*- coding: utf-8 -*-
"""
Created on Thu Aug 27 11:42:20 2026

@author: a.karabedyan
"""

# config/defaults.py
# Эти значения используются, если лист «Параметры» в Справочники.xlsx отсутствует
# или параметр не найден / невалиден.

DEFAULTS = {
    "tolerance_balance": 5000.0,
    "tolerance_reconciliation": 1050.0,
    "tolerance_leased_os": 3000.0,
    "tolerance_pnl_balance": 1050.0,
    "tolerance_rate_deviation": 0.3,
    # Ставка НДС для проводок с пропущенным значением субконто ставки
    # (доля; 0.22 = 22%). Fallback — если параметра нет в листе «Параметры».
    # Стандартная ставка РФ — 0.20; конкретная книга задаёт своё значение.
    "nds_missing_values": 0.20,
    # Допуск на потерю суммы при распределении расходов 91.02 без парной
    # выручки 91.01 (шаг 17): за превышение расход дописывается отдельной
    # строкой-остатком вместо отката всего распределения.
    "tolerance_orphan_distribution": 0.01,
}

# Схема валидации: имя -> (тип, min, max, nullable)
SCHEMA = {
    "tolerance_balance":   (float, 0.0, 10000.0, False),
    "tolerance_reconciliation": (float, 0.0, 10000.0, False),
    "tolerance_leased_os": (float, 0.0, 10000.0, False),
    "tolerance_pnl_balance": (float, 0.0, 10000.0, False),
    "tolerance_rate_deviation": (float, 0.0, 10.0, False),
    "nds_missing_values": (float, 0.0, 1.0, False),
    "tolerance_orphan_distribution": (float, 0.0, 10000.0, False),
}

TOLERANCE_DESCRIPTIONS: dict[str, str] = {
    "tolerance_balance": "Сходимость баланса (Актив = Пассив)",
    "tolerance_reconciliation": "Расхождение регистров с Общей ОСВ (перезакрытие баз 1С)",
    "tolerance_leased_os": "Расхождение по арендованным ОС (ОСВ 01.03/02.03 = Ведомость аморизации)",
    "tolerance_pnl_balance": "Взаимоувязка ОПУ и Баланса (Чистая прибыль = НРП периода)",
    "tolerance_rate_deviation": "Отклонение курса от медианы листа Курс_<валюта> при конвертации проводок ОПУ (доля; 0.35 = 35%)",
    "nds_missing_values": "Ставка НДС для проводок с пропущенным субконто ставки (доля; 0.22 = 22%). Используется в шаге 14 (выручка 90.01)",
    "tolerance_orphan_distribution": "Допуск на потерю суммы при распределении расходов 91.02 по выручке 91.01 (шаг 17). Превышение = нераспределённый остаток, сохраняется отдельной строкой",
}

# Параметры, заданные долей (0.22 = 22%): показываются в процентах.
# Остальные параметры — суммы в тысячах единиц.
FRACTION_PARAMS = frozenset({
    "tolerance_rate_deviation",
    "nds_missing_values",
    "tolerance_orphan_distribution",
})


def format_amount(value: float) -> str:
    """
    Сумма с разделителями разрядов пробелом: 4321.4 -> «4 321».

    Формат файла отличается от формата сообщений лога (там запятая) —
    в документе пробел читается однозначнее.
    """
    return f"{value:,.0f}".replace(",", " ")


def format_tolerance_value(key: str, value: float) -> str:
    """
    Единое представление значения допуска с единицей измерения.

    Используется в двух местах — в консоли перед запуском Фазы 2
    (cli/main.py) и на титульном листе отчёта
    (io_module/report_cover.py), — чтобы печать и файл не расходились.

    Args:
        key: Ключ параметра из SCHEMA.
        value: Значение параметра.

    Returns:
        Например «3 000 тыс.ед.» или «22%».
    """
    if key in FRACTION_PARAMS:
        return f"{value * 100:.0f}%"
    return format_amount(value) + " тыс.ед."


