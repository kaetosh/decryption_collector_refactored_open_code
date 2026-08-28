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

# Добавляем корневую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from logging_handling.logger_config import setup_logger
from config.defaults import TOLERANCE_DESCRIPTIONS
from config.loader import load_params
from pipeline.factories import (
    create_preparation_pipeline,
    create_main_pipeline,
)
from pipeline.executors import (
    pause_for_osv_general_export,
    pause_for_1c_export,
    initialize_context,
    save_results,
)
from cli.arguments import parse_arguments, ask_user_about_traceback
from io_module.output_manager import cleanup_old_runs, configure_run, get_run_id, get_run_dir


def main(show_traceback: bool = False, verbose: bool = False) -> int:
    """Главная функция приложения."""
    # Настраиваем логирование
    if verbose:
        setup_logger(console_level='DEBUG')
    else:
        setup_logger()

    logger.info("=" * 80)
    logger.info("Запуск приложения --СОБИРАТЕЛЬ РАСШИФРОВОК--")
    if show_traceback:
        logger.info("Режим: с полной трассировкой стека")
    logger.info("=" * 80)

    try:
        # Инициализация вывода: все результаты запуска пишутся
        # в отдельную папку _OUTPUT_DATA/run_<run_id>; папки прошлых
        # запусков сверх KEEP_LAST_RUNS удаляются
        configure_run()
        cleanup_old_runs()
        logger.info("[FOLDER] Результаты запуска сохраняются в: {}", get_run_dir())

        # ФАЗА 0
        logger.info("ФАЗА 0: Ожидаем общую ОСВ в INPUT DATA")
        pause_for_osv_general_export()
        logger.info("Проверяем Общую ОСВ...")
        context = initialize_context()
        context.run_id = get_run_id()
        context.tolerance_params = load_params(context)

        # ФАЗА 1
        logger.info("ФАЗА 1: Формирование списка выгрузок из 1С")
        preparation_pipeline = create_preparation_pipeline()
        context = preparation_pipeline.run(context)

        # ПАУЗА
        pause_for_1c_export(context)

        # ФАЗА 2
        logger.info("ФАЗА 2: Основная обработка данных")

        lines = ["Используемые допуски сходимости, не более, в тыс.:"]

        for key, value in context.tolerance_params.items():
            description = TOLERANCE_DESCRIPTIONS.get(key, key)
            formatted_value = f"{value:,.0f}".replace(",", " ")
            lines.append(f"  • {description}: {formatted_value}")

        logger.info("\n".join(lines))
        main_pipeline = create_main_pipeline()
        context = main_pipeline.run(context)

        save_results(context)

        logger.info("=" * 80)
        logger.info("Приложение успешно завершено")
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
        logger.error("[!!] Ошибка: не найден файл (общая ОСВ, справочник или выгрузка). Подробнее: {}", cause)
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

    # Определяем режим работы
    # Если передан --no-interactive или любые другие аргументы - не спрашиваем
    if args.no_interactive or len(sys.argv) > 1:
        show_traceback = args.traceback
        verbose = args.verbose
    else:
        # Запуск без аргументов (например, через F5 в IDE) - спрашиваем
        show_traceback = ask_user_about_traceback()
        verbose = False

    return main(show_traceback=show_traceback, verbose=verbose)


if __name__ == "__main__":
    sys.exit(entry_point())
