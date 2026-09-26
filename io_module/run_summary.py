# -*- coding: utf-8 -*-
"""
Краткая сводка ключевых цифр прогона для консоли.

Зачем она нужна: после длительного прогона в консоли остаётся хвост из
последних шагов и сообщение «Приложение успешно завершено». Чтобы понять,
что получилось, пользователь вынужден открывать Excel и листать до листа
«Расшифровка_ББЛ». Сводка закрывает этот вопрос на месте: актив, пассив,
сходимость баланса, выручка, объём расшифровок и статус увязки ОПУ.

Что модуль НЕ делает:
- не меняет бизнес-логику и не проверяет данные повторно: все числа
  берутся из уже собранных context.balance_df / context.pnl_df, то есть
  ровно из тех строк, что попали в книгу;
- не импортирует pipeline.base/pipeline.steps — работает по атрибутам
  контекста, как и io_module/report_cover.py;
- не может сорвать прогон: любая ошибка даёт WARNING и пустую сводку.

Цифры (актив, пассив, расхождение, выручка) не считаются здесь: они
взяты из report_cover.collect_figures — того же источника, что и
«КЛЮЧЕВЫЕ ЦИФРЫ ОТЧЁТА» на титульном листе, поэтому консоль и файл
не могут разойтись в числах. Модуль добавляет только то, чего на
титульном листе нет и что нужно именно в консоли: счётчики строк
и статус увязки.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from io_module.report_cover import collect_figures, pnl_balance_status_text

# Ширина колонки подписей в блоке сводки
LABEL_WIDTH = 34

# Рамки блока
HEADER = "ИТОГ ПРОГОНА"
SEPARATOR = "=" * 78


def collect_key_figures(context: Any) -> list[tuple[str, str]]:
    """
    Всё, что печатается в блоке: цифры отчёта и статус увязки.
    Числа приходят готовыми из report_cover.collect_figures.
    """
    figures = list(collect_figures(context))
    figures.append(("Увязка ОПУ и баланса (ЧП = НРП)", pnl_balance_status_text(context)))
    return figures



def format_run_summary(context: Any) -> list[str]:
    """
    Готовый блок для консоли: рамка, подписи в одну колонку, значения.

    Возвращается списком строк, а не одной строкой намеренно: консольный
    формат логгера печатает short_message с обрезкой до 500 символов
    (см. _patch_record в logging_handling/logger_config.py), и блок
    целиком в одну запись обрезался бы. По строкам — как в сводке
    предупреждений в cli/main.py.
    """
    lines = [SEPARATOR, f"{HEADER} (тыс.ед., как в отчёте)", SEPARATOR]
    for label, value in collect_key_figures(context):
        lines.append(f"  {label:<{LABEL_WIDTH}} {value}")
    lines.append(SEPARATOR)
    return lines


def log_run_summary(context: Any) -> None:
    """
    Печатает сводку в консоль. Ошибки не поднимаются: файл отчёта уже
    сохранён, и отсутствие cosmetic-блока не должно менять код выхода.
    """
    try:
        lines = format_run_summary(context)
    except Exception as exc:  # noqa: BLE001 - сводка не должна влиять на прогон
        logger.warning("[!] Не удалось сформировать сводку ключевых цифр: {}", exc)
        return
    for line in lines:
        logger.info(line)


def _row_count(df: Any) -> str:
    return str(len(df)) if df is not None else "—"
