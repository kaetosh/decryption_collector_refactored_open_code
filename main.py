
"""
Главная точка входа приложения Decryption Collector.

Это приложение собирает расшифровку баланса бухгалтерского учета
из оборотно-сальдовых ведомостей, используя модульную архитектуру
на основе паттерна Pipeline.

Использование:
    python3 main.py
"""

import sys
from pathlib import Path
import argparse
import warnings
warnings.filterwarnings('ignore', message='Data Validation extension is not supported')

# Добавляем корневую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from logging_handling.logger_config import setup_logger
from pipeline.base import Pipeline, ProcessingContext
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


def parse_arguments() -> argparse.Namespace:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Decryption Collector - сбор расшифровки баланса из ОСВ",
        add_help=True
    )

    parser.add_argument(
        '-t', '--traceback',
        action='store_true',
        help='Выводить полную трассировку стека при ошибках'
    )

    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Подробный режим логирования (DEBUG уровень)'
    )

    parser.add_argument(
        '--no-interactive',
        action='store_true',
        help='Не задавать интерактивных вопросов (для автоматического режима)'
    )

    # parse_known_args вместо parse_args - не падает, если аргументы не распознаны
    args, unknown = parser.parse_known_args()
    return args


def ask_user_about_traceback() -> bool:
    """
    Интерактивно спрашивает пользователя, нужен ли traceback.

    Используется в IDE (Spyder), когда аргументы не переданы через командную строку.
    """
    try:
        print("\n" + "=" * 80)
        print("[DIAG] Режим диагностики")
        print("=" * 80)
        print("Хотите выводить полную трассировку стека при ошибках?")
        print("  [enter] - нет (по умолчанию, чистый вывод)")
        print("  [y]   - да (полный traceback для отладки)")
        print("=" * 80)

        POSITIVE_RESPONSES = {'y', 'yes', 'д', 'да'}
        response = input("Ваш выбор: ").strip().lower()
        return response in POSITIVE_RESPONSES
    except (EOFError, KeyboardInterrupt):
        # Если stdin недоступен (например, при запуске из cron)
        return False


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
        # ФАЗА 0
        logger.info("ФАЗА 0: Ожидаем общую ОСВ в INPUT DATA")
        pause_for_osv_general_export()
        logger.info("Проверяем Общую ОСВ...")
        context = initialize_context()
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


if __name__ == "__main__":
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

    exit_code = main(show_traceback=show_traceback, verbose=verbose)
    sys.exit(exit_code)
