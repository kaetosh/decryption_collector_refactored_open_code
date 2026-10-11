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

from config.settings import (
    PAUSE_ON_REFERENCE_ERROR,
    REFERENCE_PAUSE_COUNT_KEY,
    STRICT_CONTRACTOR_CHECK,
)
from pipeline.errors import (
    ReferenceMismatchError,
    ConvergenceError,
    MissingFilesError,
    MissingContractorError,
    MissingOSGroupError,
    TooManyFilesError,
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


def _snapshot_context(target) -> dict:
    """
    Сохраняет состояние контекста ДО вызова шага: ссылки на все атрибуты
    и копию словаря context.data.

    Используется паузой на неактуальном справочнике: шаг мог частично
    изменить таблицы/context.data до того, как упал на ReferenceMismatchError.
    Перед повторным выполнением шага контекст откатывается к этому состоянию,
    чтобы повтор начался «с чистого листа» (как если бы шаг ещё не выполнялся).

    Сохраняются ссылки, а не глубокие копии — O(1) по объёму данных.
    In-place мутации DataFrame (изменение объекта по той же ссылке) этим
    снапшотом не откатываются, но шаги пайплайна работают с локальными
    .copy() и присваивают результат в контекст только при успехе (аудит 10/2026).
    """
    names = getattr(target, "__slots__", None) or list(getattr(target, "__dict__", {}).keys())
    fields = [name for name in names if not str(name).startswith("__")]
    snapshot = {name: getattr(target, name, None) for name in fields}
    data = getattr(target, "data", None)
    if isinstance(data, dict):
        snapshot["data"] = dict(data)
    return snapshot


def _restore_context(target, snapshot: dict) -> None:
    """Возвращает атрибуты контекста к снапшоту (тем объектам, что были до шага)."""
    for name, old in snapshot.items():
        if getattr(target, name, None) is not old:
            try:
                setattr(target, name, old)
            except Exception:  # noqa: BLE001 — откат не должен ломать паузу
                continue


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

        # Состояние контекста до шага: если шаг упадёт на справочнике и встанет
        # пауза, откатим атрибуты и context.data к этому моменту, чтобы повтор
        # шага начался с чистого состояния (шаг мог частично изменить таблицы).
        snapshot = _snapshot_context(context)
        # Снимок папки mismatches/ на тот же случай: после успешного повтора
        # диагностика первой попытки описывает уже исправленную проблему.
        diagnostics_before = self._snapshot_diagnostics()

        def _pause_and_retry(reason: str) -> bool:
            """
            Пауза на неактуальном справочнике: ждём Enter, перечитываем
            Справочники.xlsx, откатываем контекст к состоянию до шага.

            Args:
                reason: Почему шаг упал — печатается в блоке паузы. ERROR
                    «[STOP] Обработка остановлена» логируется вызывающим
                    кодом только если паузы не будет: при успешном
                    возобновлении он попал бы в сводку предупреждений
                    титульного листа и пугал получателя у зелёного отчёта.

            Returns:
                True — шаг надо повторить (пользователь подтвердил);
                False — прервать прогон как раньше ([STOP]).
            """
            if not PAUSE_ON_REFERENCE_ERROR:
                return False
            from pipeline.executors import refresh_references, wait_for_reference_update

            if not wait_for_reference_update(context, reason):
                return False
            # Шаг мог частично изменить таблицы/context.data до ошибки —
            # откатываем к состоянию до вызова шага, чтобы повтор был чистым.
            _restore_context(context, snapshot)
            # Счётчик для титульного листа: получатель видит, что отчёт собран
            # после паузы на обновлении справочника (после отката context.data,
            # иначе счётчик затерялся бы вместе с мутациями шага).
            context.data[REFERENCE_PAUSE_COUNT_KEY] = (
                int(context.data.get(REFERENCE_PAUSE_COUNT_KEY, 0)) + 1
            )
            try:
                refresh_references(context)
            except ReferenceMismatchError as refresh_error:
                # Обновлённый справочник сам невалиден (например, изменён период
                # в КомпанииГруппы) — честный [STOP] с новой причиной.
                _record(context, "error", str(refresh_error))
                logger.error(
                    "[STOP] Обработка остановлена: при перечитывании обновлённого "
                    "справочника на этапе '{}' возникла ошибка: {}",
                    step_name, refresh_error,
                )
                raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from refresh_error
            logger.info(
                "[RESUME] Справочник обновлён — повторяем шаг '{}' "
                "(выгрузки не перечитываются).",
                step_name,
            )
            return True

        def _retry_step():
            """
            Повтор шага после обновления справочника + чистка устаревшей
            диагностики первой попытки.

            Чистка выполняется именно здесь: если повтор снова упадёт,
            исключение уйдёт наружу, файлы диагностики останутся, и
            получатель увидит реальную проблему, а не устранённую.
            """
            result = wrapper(self, context)
            # Duck-typing как в ветке успеха: счётчик для титульного листа
            # пишем в тот же объект, куда ушла метрика повторного шага.
            target = result if hasattr(result, "data") else context
            self._drop_resolved_diagnostics(target, diagnostics_before)
            return result

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
                # Строгий режим: падаем (или ждём обновления справочника)
                _record(context, "error", str(e))
                reason = f"неизвестные контрагенты на этапе '{step_name}': {e}"
                if _pause_and_retry(reason):
                    return _retry_step()
                logger.error("[STOP] Обработка остановлена: {}", reason)
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

        except MissingOSGroupError as e:
            # Ошибка несоответствия справочнику ППА по группам ОС.
            # Мягкий режим обрабатывается внутри шага (step_06): при
            # STRICT_OS_GROUP_CHECK=False исключение не выбрасывается,
            # поэтому сюда попадаем только в строгом режиме.
            _record(context, "error", str(e))
            e.step_name = step_name
            self._save_reference_mismatch_report(e)
            reason = f"не найдены группы ОС на этапе '{step_name}': {e}"
            if _pause_and_retry(reason):
                return _retry_step()
            logger.error("[STOP] Обработка остановлена: {}", reason)
            raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e

        except ConvergenceError as e:
            # Сходимость проводок с ОСВ сверх допуска. Диагностика
            # (значение из проводок, из ОСВ, разница, допуск) уходит в
            # mismatches/. Остановка жёсткая в обоих режимах
            # EXPORT_REPORT_ON_MISMATCH: этот флаг управляет только
            # мягкой веткой увязки ЧП = НРП в шаге 19, а проверки
            # выручки/себестоимости/расходов против ОСВ (шаги 14-16)
            # всегда останавливали конвейер — просто раньше из-за
            # ValueError это выглядело как CRITICAL [!!].
            _record(context, "error", str(e))
            e.step_name = step_name
            self._save_reference_mismatch_report(e)
            logger.error(
                "[STOP] Обработка остановлена: расхождение при сверке на этапе '{}': {}",
                step_name, e,
            )
            raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e

        except ReferenceMismatchError as e:
            _record(context, "error", str(e))
            e.step_name = step_name
            self._save_reference_mismatch_report(e)
            reason = f"несоответствие данных справочникам на этапе '{step_name}': {e}"
            if _pause_and_retry(reason):
                return _retry_step()
            logger.error("[STOP] Обработка остановлена: {}", reason)
            raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e

        except MissingFilesError as e:
            _record(context, "error", str(e))
            e.step_name = step_name
            self._save_missing_files_report(e)
            logger.error(
                "[STOP] Обработка остановлена: отсутствуют файлы выгрузок на этапе '{}': {}",
                step_name, e,
            )
            raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e

        except TooManyFilesError as e:
            _record(context, "error", str(e))
            e.step_name = step_name
            # Логируем список найденных файлов для диагностики
            logger.error(
                "[STOP] Обработка остановлена: найдено несколько файлов вместо одного на этапе '{}': {}",
                step_name, e,
            )
            if e.found_files:
                logger.error("[STOP] Найденные файлы: {}", ", ".join(e.found_files))
            raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e

        except Exception as e:
            _record(context, "error", str(e))
            logger.error("[ERR] Ошибка на этапе '{}': {}", step_name, e)
            raise ProcessingStepError(f"Сбой на этапе '{step_name}'") from e

    return wrapper
