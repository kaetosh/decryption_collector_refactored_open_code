# -*- coding: utf-8 -*-
"""
Декораторы для шагов конвейера.

Содержит декоратор `handle_pipeline_errors`, который унифицирует обработку
исключений в методах `execute` шагов: сохранение проблемных данных,
мягкий/строгий режим для неизвестных контрагентов, логирование
и запись метрик выполнения (context.step_metrics).
"""

from functools import wraps
from time import perf_counter

import pandas as pd
from loguru import logger

from config.settings import STRICT_CONTRACTOR_CHECK
from pipeline.errors import (
    ReferenceMismatchError,
    MissingFilesError,
    MissingContractorError,
)
from pipeline.errors import ProcessingStepError


def _collect_rows(target) -> dict:
    """
    Собирает количество строк основных таблиц контекста.

    Используется для отладки: видно, как меняется объём данных
    от шага к шагу. Возвращает словарь {имя_таблицы: число_строк};
    ключи с префиксом 'data.' — таблицы из context.data.
    """
    rows = {}
    for attr in ("common_osv_df", "summary_osv_df", "journal_df", "balance_df", "pnl_df"):
        df = getattr(target, attr, None)
        if df is not None:
            rows[attr] = len(df)
    data = getattr(target, "data", None)
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, pd.DataFrame):
                rows[f"data.{key}"] = len(value)
    return rows


def handle_pipeline_errors(func):
    """
    Декоратор для унифицированной обработки ошибок шага конвейера.

    Обернутая функция должна быть методом класса `Step` (дочернего),
    чтобы использовать его состояние (self.name) и вспомогательные методы
    (self._save_reference_mismatch_report, self._save_missing_files_report,
     self._apply_soft_contractor_handling).

    Обрабатывает:
        - MissingContractorError: мягкий/строгий режим для неизвестных контрагентов
        - ReferenceMismatchError: сохранение problem_data и поднятие ProcessingStepError
        - MissingFilesError: сохранение отчёта об отсутствующих файлах
        - Exception: общий перехват

    После успешного выполнения шага (включая мягкий режим) в
    context.step_metrics записывается метрика: статус, длительность
    и число строк таблиц контекста.

    Args:
        func: Метод `execute` шага.

    Returns:
        Обёрнутый метод с единой обработкой ошибок.
    """

    @wraps(func)
    def wrapper(self, context):
        step_name = self.name
        started = perf_counter()

        def _record(target, status, error=None, rows=None):
            """
            Записывает метрику выполнения шага в target.step_metrics.

            Duck-typing (getattr) вместо импорта ProcessingContext —
            иначе возникнет циклический импорт base <-> decorators.
            """
            metrics = getattr(target, "step_metrics", None)
            if metrics is not None:
                metrics.append({
                    "step": step_name,
                    "status": status,
                    "duration_sec": round(perf_counter() - started, 3),
                    "error": error,
                    "rows": rows,
                })

        logger.debug("--- Начало этапа: {} ---", step_name)

        try:
            result = func(self, context)
            # Шаг теоретически может вернуть новый контекст — пишем метрику в него
            target = result if hasattr(result, "step_metrics") else context
            rows = _collect_rows(target)
            _record(target, "ok", rows=rows)
            if rows:
                logger.debug("Этап '{}': таблицы после шага (строк): {}", step_name, rows)
            return result

        except MissingContractorError as e:
            # Специальная обработка для неизвестных контрагентов
            e.step_name = step_name
            self._save_reference_mismatch_report(e)

            if STRICT_CONTRACTOR_CHECK:
                # Строгий режим: падаем
                _record(context, "error", str(e))
                logger.error(
                    "[ERR] Критическая ошибка: неизвестные контрагенты на этапе '{}': {}",
                    step_name, e,
                )
                raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e
            else:
                # Мягкий режим: заменяем на '3 лица' и продолжаем
                logger.warning(
                    "[!] Мягкий режим: неизвестные контрагенты заменены на '{}'",
                    e.replacement_value,
                )
                result = self._apply_soft_contractor_handling(context, e)
                target = result if hasattr(result, "step_metrics") else context
                rows = _collect_rows(target)
                _record(target, "soft", rows=rows)
                if rows:
                    logger.debug("Этап '{}': таблицы после шага (строк): {}", step_name, rows)
                return result

        except ReferenceMismatchError as e:
            _record(context, "error", str(e))
            e.step_name = step_name
            self._save_reference_mismatch_report(e)
            logger.error(
                "[ERR] Ошибка несоответствия данных на этапе '{}': {}",
                step_name, e,
            )
            raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e

        except MissingFilesError as e:
            _record(context, "error", str(e))
            e.step_name = step_name
            self._save_missing_files_report(e)
            logger.error(
                "[ERR] Ошибка: отсутствуют файлы выгрузок на этапе '{}': {}",
                step_name, e,
            )
            raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e

        except Exception as e:
            _record(context, "error", str(e))
            logger.error("[ERR] Ошибка на этапе '{}': {}", step_name, e)
            raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e

    return wrapper
