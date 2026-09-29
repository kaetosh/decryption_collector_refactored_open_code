# -*- coding: utf-8 -*-
"""
Точка входа командной строки приложения Decryption Collector.

Это приложение собирает расшифровку баланса бухгалтерского учета
из оборотно-сальдовых ведомостей, используя модульную архитектуру
на основе паттерна Pipeline.

Использование:
    python main.py
    python -m cli.main
"""

import sys
from pathlib import Path
from time import perf_counter

# Добавляем корневую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from logging_handling.logger_config import (
    format_warnings_summary,
    get_collected_warnings,
    setup_logger,
)

from config.defaults import TOLERANCE_DESCRIPTIONS, format_tolerance_value
from config.loader import load_params
from pipeline.factories import (
    create_preparation_pipeline,
    create_main_pipeline,
)
from pipeline.executors import (
    pause_for_osv_general_export,
    pause_for_1c_export,
    initialize_context,
    ask_balance_date_if_needed,
    save_results,
)
from pipeline.errors import PipelineError, ProcessingStepError
from cli.arguments import parse_arguments
from cli.splash import show_splash
from config.settings import SHOW_SPLASH, BASE_DIR, to_relative
from io_module.auto_sort import cleanup_old_archive
from io_module.output_manager import cleanup_old_runs, configure_run, get_run_id, get_run_dir
from io_module.run_summary import log_run_summary


def _warn_about_unknown_args(
    unknown_args: list,
    suggestions: dict,
) -> None:
    """
    Сообщает о нераспознанных аргументах командной строки.

    Аргументы разбираются в entry_point() до настройки логирования,
    поэтому предупреждение выводится здесь. Молчаливый прогон с опечаткой
    опаснее явной ошибки: программа остаётся в интерактивном режиме и
    выглядит «зависшей», а на самом деле ждёт ввод, которого не будет.
    """
    if not unknown_args:
        return

    for arg in unknown_args:
        hint = suggestions.get(arg)
        message = f"[!] Неизвестный аргумент: {arg}"
        if hint:
            message += f" (возможно, имелось в виду: {hint})"
        else:
            message += " — аргумент проигнорирован"
        logger.warning(message)

    logger.warning(
        "[!] Список доступных аргументов: python main.py --help"
    )


def _log_warnings_summary() -> None:
    """
    Печатает в конце прогона сводку предупреждений — что программа
    поправила или пропустила молча.

    Отдельной таблицей, а не размазанным по шагам шумом: получатель
    запуска видит полную картину одной таблицей, не листая app.log.
    Каждое предупреждение логируется отдельной записью — иначе
    многострочная сводка превысит лимит обрезки консольного сообщения.
    Формулировки берутся из logging_handling.logger_config — те же
    строки печатаются в консоли и попадают на титульный лист отчёта.
    """
    items = get_collected_warnings()
    if not items:
        logger.info("Предупреждений за прогон нет")
        return

    total = sum(count for _, count in items)
    logger.info("=" * 80)
    logger.info(
        "СВОДКА ПРЕДУПРЕЖДЕНИЙ ЗА ПРОГОН: всего {} (позиций: {})",
        total,
        len(items),
    )
    for line in format_warnings_summary():
        logger.info("  {}", line)
    logger.info("=" * 80)


def main(
    show_traceback: bool = False,
    verbose: bool = False,
    balance_date: str | None = None,
    no_interactive: bool = False,
    no_splash: bool = False,
    unknown_args: list | None = None,
    suggestions: dict | None = None,
) -> int:
    """Главная функция приложения."""
    start_time = perf_counter()
    # Настраиваем логирование
    if verbose:
        setup_logger(console_level='DEBUG')
    else:
        setup_logger()

    # Аргументы разбираются до логирования (entry_point), поэтому
    # предупреждение об опечатках печатаем здесь, когда логирование готово.
    _warn_about_unknown_args(unknown_args, suggestions)

    # Заставка — до loguru-шапки: блок идёт в stdout, записи логгера в
    # stderr/app.log, порядок в консоли не зависит от их слияния.
    # show_splash не бросает исключений — декоративный блок не остановит
    # конвейер даже при сбое (логируется в DEBUG).
    if SHOW_SPLASH and not no_splash:
        show_splash(show_traceback=show_traceback, verbose=verbose)

    logger.info("[FOLDER] Базовая папка проекта: {}", BASE_DIR)

    try:
        # Инициализация вывода: все результаты запуска пишутся
        # в отдельную папку _OUTPUT_DATA/run_<run_id>; папки прошлых
        # запусков сверх KEEP_LAST_RUNS удаляются
        configure_run()
        cleanup_old_runs()
        # Архив _INPUT_DATA/_archive тоже растёт на каждом прогоне
        # (старые выгрузки, перезаписанные версии) — очищаем его той же
        # ручкой KEEP_LAST_RUNS. Архив текущего запуска на этом шаге ещё
        # не создан (архивация идёт в паузах), поэтому он не пострадает.
        cleanup_old_archive()
        logger.info("[FOLDER] Результаты запуска сохраняются в: {}", to_relative(get_run_dir()))

        # ФАЗА 0
        logger.info("ФАЗА 0: Ожидаем общую ОСВ в INPUT DATA")
        pause_for_osv_general_export(interactive=not no_interactive)
        logger.info("Проверяем Общую ОСВ...")
        context = initialize_context()
        context.run_id = get_run_id()
        context.tolerance_params = load_params(context)

        # Дата перевода валютных остатков (для компаний с валютой != RUB):
        # приоритет — CLI-флаг --balance-date, затем интерактивный вопрос;
        # без TTY (--no-interactive) — последняя дата из справочника курса
        if balance_date and not context.balance_date:
            context.balance_date = balance_date
        ask_balance_date_if_needed(context, interactive=not no_interactive)

        # ФАЗА 1
        logger.info("ФАЗА 1: Формирование списка выгрузок из 1С")
        preparation_pipeline = create_preparation_pipeline()
        context = preparation_pipeline.run(context)

        # ПАУЗА
        pause_for_1c_export(context, interactive=not no_interactive)

        # ФАЗА 2
        logger.info("ФАЗА 2: Основная обработка данных")

        logger.info("Применённые допуски сходимости:")
        for key, value in context.tolerance_params.items():
            description = TOLERANCE_DESCRIPTIONS.get(key, key)
            logger.info("  {}: {}", description, format_tolerance_value(key, value))
        main_pipeline = create_main_pipeline()
        context = main_pipeline.run(context)

        save_results(context)

        # Сводка цифр — до сводки предупреждений: сначала результат,
        # затем его качество. Обе печатаются после save_results, где
        # известны и собранные таблицы, и полный список предупреждений.
        log_run_summary(context)

        _log_warnings_summary()

        elapsed = perf_counter() - start_time
        if elapsed >= 60:
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            duration_str = f"{mins} мин {secs} сек"
        else:
            duration_str = f"{int(elapsed)} сек"

        logger.info("=" * 80)
        logger.info("Приложение успешно завершено за {}", duration_str)
        logger.info("=" * 80)
        return 0

    except KeyboardInterrupt:
        # Корректное завершение по Ctrl+C - без traceback
        logger.info("")
        logger.info("=" * 80)
        logger.warning("[!] Получен сигнал прерывания (Ctrl+C). Завершаем работу.")
        logger.info("   Несохранённые промежуточные результаты могли быть потеряны.")
        logger.info("=" * 80)
        return 130  # стандартный код выхода для SIGINT

    except FileNotFoundError as e:
        cause = e.__cause__ if e.__cause__ is not None else e
        logger.error(
            "[STOP] Обработка остановлена: не найден файл (общая ОСВ, справочник или выгрузка). Подробнее: {}",
            cause,
        )
        if show_traceback:
            logger.exception("Трассировка стека:")
        return 1

    except PipelineError as e:
        # Ожидаемые остановки конвейера: предусмотренные ошибки пайплайна
        # (несоответствие справочникам, отсутствующие файлы — включая «сырые»
        # PipelineError вне шагов, например PeriodMismatchError) и обёртки
        # ProcessingStepError — тоже подкласс PipelineError, декоратор шагов
        # создаёт их с сохранением первопричины в __cause__. Непредвиденные
        # сбои внутри шагов (первопричина — не PipelineError)
        # классифицируются как неожиданные.
        cause = e.__cause__ if e.__cause__ is not None else e
        if isinstance(e, ProcessingStepError) and not isinstance(cause, PipelineError):
            logger.critical("[!!] Неожиданная ошибка: {}", cause)
            if show_traceback:
                logger.exception("Трассировка стека:")
            return 1
        logger.error("[STOP] Обработка остановлена: {}", cause)
        logger.error(
            "[STOP] Дальнейшая обработка невозможна, пока не актуализированы "
            "справочники/входные данные."
        )
        logger.error(
            "[STOP] Список проблемных значений — в mismatches/ папки запуска, "
            "детали — в логе выше."
        )
        if show_traceback:
            logger.exception("Трассировка стека:")
        return 1

    except Exception as e:
        cause = e.__cause__ if e.__cause__ is not None else e
        logger.critical("[!!] Неожиданная ошибка: {}", cause)
        if show_traceback:
            logger.exception("Трассировка стека:")
        return 1


def entry_point() -> int:
    """Разбирает аргументы командной строки и запускает приложение."""
    args = parse_arguments()

    return main(
        show_traceback=args.traceback,
        verbose=args.verbose,
        balance_date=args.balance_date,
        no_interactive=args.no_interactive,
        no_splash=args.no_splash,
        unknown_args=getattr(args, 'unknown_args', None),
        suggestions=getattr(args, 'suggestions', None),
    )


if __name__ == "__main__":
    sys.exit(entry_point())
