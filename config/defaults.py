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
}

# Схема валидации: имя -> (тип, min, max, nullable)
SCHEMA = {
    "tolerance_balance":   (float, 0.0, 10000.0, False),
    "tolerance_reconciliation": (float, 0.0, 10000.0, False),
    "tolerance_leased_os": (float, 0.0, 10000.0, False),
    "tolerance_pnl_balance": (float, 0.0, 10000.0, False),
    "tolerance_rate_deviation": (float, 0.0, 10.0, False),
}

TOLERANCE_DESCRIPTIONS: dict[str, str] = {
    "tolerance_balance": "Сходимость баланса (Актив = Пассив)",
    "tolerance_reconciliation": "Расхождение регистров с Общей ОСВ (перезакрытие баз 1С)",
    "tolerance_leased_os": "Расхождение по арендованным ОС (ОСВ 01.03/02.03 = Ведомость аморизации)",
    "tolerance_pnl_balance": "Взаимоувязка ОПУ и Баланса (Чистая прибыль = НРП периода)",
    "tolerance_rate_deviation": "Отклонение курса от медианы листа Курс_<валюта> при конвертации проводок ОПУ (доля; 0.3 = 30%)",
}

