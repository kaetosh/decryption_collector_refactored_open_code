
"""
Главная точка входа приложения Decryption Collector.

Это приложение собирает расшифровку баланса бухгалтерского учета
из оборотно-сальдовых ведомостей, используя модульную архитектуру
на основе паттерна Pipeline.

Использование:
    python3 main.py
"""

import sys
import pandas as pd
from pathlib import Path
import argparse
import warnings
warnings.filterwarnings('ignore', message='Data Validation extension is not supported')

# Добавляем корневую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from logging_handling.logger_config import setup_logger
from pipeline.base import Pipeline, ProcessingContext
from config.settings import REFERENCE_CONFIGS
from pipeline.base import Step
from pipeline.errors import ReferenceMismatchError

from pipeline.steps import (
    Step1aListExpectedRegistersStep,
    Step1bVerifyFilesStep,
    Step2FlatSummaryOSVStep,
    Step3AddAccountColumnStep,
    Step4AddReceivableTypeStep,
    Step5AddReceivableSubtypeStep,
    Step6AddOSGroupColumnStep,
    Step7AddLongShortTermColumnStep,
    Step8AddBioactiveSegmentColumnStep,
    Step9AddRelatedPartyTypeColumnStep,
    Step10ClassifyLeaseSourceStep,
    Step11Split60AccountDebtByOSStatusStep,
    Step11aCheckContractorSimilarityStep,
    Step12Split84AccountBalanceStep,
    Step13BuildBalanceBreakdownStep,
    Step14BuildOpuFoundationStep,
    Step15AddAdminExpensesToOpuStep,
    Step16AddCommExpensesToOpuStep,
    Step17AddOtherIncomeExpensesToOpuStep,
    Step18AddTaskAndOtherMovementsStep,
    Step19BuildOpuStep
)

from io_module import DataLoader, DataSaver

def create_preparation_pipeline() -> Pipeline:
    """
    Первый пайплайн: подготовка к выгрузке из 1С.
    
    Выполняет только Шаг 1а — формирует список регистров,
    которые нужно выгрузить из 1С, и сохраняет его в Excel.
    
    Returns:
        Объект Pipeline с первым шагом
    """
    pipeline = Pipeline(name="Подготовка списка выгрузок")
    pipeline.add_step(Step1aListExpectedRegistersStep())
    return pipeline


def create_main_pipeline() -> Pipeline:
    """
    Второй пайплайн: основная обработка данных.
    
    Выполняет шаги 1б-13 после того, как все выгрузки из 1С 
    уже расположены в папке INPUT_DATA.
    
    Returns:
        Объект Pipeline с шагами 2-13
    """
    pipeline = Pipeline(name="Основной конвейер сборки расшифровки баланса")
    
    # ЭТАП 1: Загрузка и подготовка данных (баланс и опу)
    pipeline.add_step(Step1bVerifyFilesStep())
    pipeline.add_step(Step2FlatSummaryOSVStep())
    
    # ЭТАП 2: Добавление классификационных столбцов баланс
    pipeline.add_step(Step3AddAccountColumnStep())
    pipeline.add_step(Step4AddReceivableTypeStep())
    pipeline.add_step(Step5AddReceivableSubtypeStep())
    pipeline.add_step(Step6AddOSGroupColumnStep())
    pipeline.add_step(Step7AddLongShortTermColumnStep())
    pipeline.add_step(Step8AddBioactiveSegmentColumnStep())
    pipeline.add_step(Step9AddRelatedPartyTypeColumnStep())
    
    # ЭТАП 3: Специальные расчеты и классификации баланс
    pipeline.add_step(Step10ClassifyLeaseSourceStep())
    pipeline.add_step(Step11Split60AccountDebtByOSStatusStep())
    pipeline.add_step(Step11aCheckContractorSimilarityStep())
    pipeline.add_step(Step12Split84AccountBalanceStep())
    
    # ЭТАП 4: Финальная сборка расшифровки баланса
    pipeline.add_step(Step13BuildBalanceBreakdownStep())
    
    # ЭТАП 5: Добавление классификационных столбцов опу
    pipeline.add_step(Step14BuildOpuFoundationStep())
    pipeline.add_step(Step15AddAdminExpensesToOpuStep())
    pipeline.add_step(Step16AddCommExpensesToOpuStep())
    pipeline.add_step(Step17AddOtherIncomeExpensesToOpuStep())
    pipeline.add_step(Step18AddTaskAndOtherMovementsStep())
    
    # ЭТАП 6: Финальная сборка расшифровки опу
    pipeline.add_step(Step19BuildOpuStep())
    
    return pipeline


def pause_for_1c_export(context: ProcessingContext) -> None:
    """
    Приостанавливает выполнение и ждет, пока бухгалтер выгрузит файлы из 1С.
    
    Эта функция вынесена отдельно, чтобы:
    - Легко читалась в main.py
    - Могла быть заменена на GUI-диалог или автоматический режим
    - Не засорять бизнес-логику шагов
    """
    expected_count = len(context.data.get('expected_filenames', []))
    
    print("\n" + "=" * 80)
    print(f"[LIST] Сформирован список из {expected_count} регистров к выгрузке.")
    print("[FOLDER] Список сохранен в папке OUTPUT_DATA.")
    print()
    print("[>>] ВАШИ ДЕЙСТВИЯ:")
    print("   1. Откройте файл 'Выгрузить_*.xlsx' в папке OUTPUT_DATA")
    print("   2. Выгрузите указанные регистры из 1С")
    print("   3. Положите все файлы в папку INPUT_DATA")
    print("=" * 80)
    
    try:
        input("\n[PAUSE] Когда файлы будут готовы, нажмите Enter для продолжения...")
    except EOFError:
        pass
    print("=" * 80 + "\n")


def initialize_context() -> ProcessingContext:
    logger.debug("Инициализация контекста обработки")

    try:
        context = ProcessingContext()

        osv_df, osv_filename = DataLoader.load_general_osv()

        if osv_df.empty:
            raise ValueError("Загруженная общая ОСВ пуста")

        context.common_osv_df = osv_df
        context.name_file_general_osv = osv_filename

        stem = Path(osv_filename).stem
        parts = [part.strip() for part in stem.split("_") if part.strip()]

        if len(parts) < 3:
            raise ValueError(
                f"Некорректное имя файла '{osv_filename}'. "
                "Ожидается формат: CompanyName_Register_Account_Period_.xlsx"
            )

        context.company = parts[0].strip()
        context.period = parts[-1].strip()

        context.references["план_счетов_фо"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="ПланСчетов",
                strings=["РСБУ Код отчетности", "Итоговый номер счета"],
            )
        )

        context.references["меппинг_баланс"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="Меппинг",
                **REFERENCE_CONFIGS["Меппинг"],
            )
        )

        context.references["меппинг_опу"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="Меппинг_опу",
                **REFERENCE_CONFIGS["Меппинг_опу"],
            )
        )

        context.references["компании_группы"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="КомпанииГруппы",
                strings=[
                    "сокращенное_наименование_компании",
                    "название_файла_расшифровки",
                ],
            )
        )

        context.references["выгрузки"] = Step.clean_whitespace(
            DataLoader.load_reference_data(sheet_name="Выгрузки")
        )

        context.references["план_счетов_бу"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="ПланСчетовБУ",
                strings=[
                    "компания",
                    "код",
                    "наименование",
                    "субконто_1",
                    "субконто_2",
                    "субконто_3",
                ],
            )
        )

        context.references["справочник_уфр"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="СправочникУФР",
                strings=[
                    "строка_уфр",
                    "сегмент",
                    "сокращенное_наименование_компании",
                    "ном_группа_1с",
                ],
            )
        )

        context.references["справочник_ппа"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="ППА",
                strings=[
                    "группа_ос",
                    "вид_взаиморасчетов",
                    "наименование_компании",
                    "рбп",
                    "ос_ппа",
                    "ос_после_перехода_в_собственность",
                    "договор_аренды",
                    "контрагент",
                ],
            )
        )

        context.references["кредит_обслуж"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="КредитОбслуж",
                strings=["компания", "рбп_кредитные_линии", "контрагент"],
            )
        )

        context.references["вид_связи_ка"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="ВидСвязиКА",
                strings=["ВидСвязиКА", "сегмент", "ВариантыНазвания"],
            )
        )

        context.references["прочие_доходы_ндс"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="ПрочиеДоходыНДС",
                strings=["прочие_доходы_ндс"],
            )
        )

        context.references["виды_рбп_аренда_лизинг"] = Step.clean_whitespace(
            DataLoader.load_reference_data(
                sheet_name="ВидыРБП_АрендаЛизинг",
                strings=["виды_рбп_аренда_лизинг"],
            )
        )

        company_directory_df = context.references["компании_группы"]

        required_columns = {"сокращенное_наименование_компании", "сегмент"}
        missing_columns = required_columns - set(company_directory_df.columns)

        if missing_columns:
            raise ValueError(
                "В справочнике 'компании_группы' отсутствуют обязательные колонки: "
                f"{sorted(missing_columns)}"
            )

        company_values = (
            company_directory_df["сокращенное_наименование_компании"]
            .fillna("")
            .astype(str)
            .str.strip()
        )

        matching_rows = company_directory_df[company_values == context.company]

        if matching_rows.empty:
            problem_data = (
                company_directory_df[["сокращенное_наименование_компании"]]
                .drop_duplicates()
                .rename(
                    columns={
                        "сокращенное_наименование_компании": "компания_в_справочнике"
                    }
                )
            )

            raise ReferenceMismatchError(
                message=f"Компания '{context.company}' не найдена в справочнике",
                problem_data=problem_data,
                reference_name="КомпанииГруппы",
                searched_company=context.company,
            )

        if len(matching_rows) > 1:
            raise ReferenceMismatchError(
                message=(
                    f"У компании '{context.company}' найдено "
                    f"{len(matching_rows)} записей. Ожидается одна."
                ),
                problem_data=matching_rows.copy(),
                reference_name="КомпанииГруппы",
                duplicate_count=len(matching_rows),
            )

        row = matching_rows.iloc[0]
        segment = row["сегмент"]

        if pd.isna(segment) or not str(segment).strip():
            raise ReferenceMismatchError(
                message=f"У компании '{context.company}' не заполнен сегмент",
                problem_data=matching_rows.copy(),
                reference_name="КомпанииГруппы",
                searched_company=context.company,
            )

        context.company = str(row["сокращенное_наименование_компании"]).strip()
        context.segment = str(segment).strip()

        logger.debug(
            "Контекст инициализирован: компания=%s, период=%s, строк в ОСВ=%s, справочников=%s",
            context.company,
            context.period,
            len(osv_df),
            len(context.references),
        )

        return context

    except Exception:
        logger.exception("Ошибка при инициализации контекста")
        raise

def save_results(context: ProcessingContext) -> None:
    """
    Сохранить результаты обработки в один комбинированный отчёт.
    
    Файл содержит два листа:
    - "Расшифровка_ББЛ" — финальный отчёт (balance breakdown)
    - "исходники" — обработанный main_df
    
    Имя файла берётся из справочника КомпанииГруппы (столбец название_файла_расшифровки).
    Если компания не найдена — используется стандартное имя.
    """
    logger.info("Сохранение результатов")
    
    try:
        company_name = context.company
        period = context.period
        
        # 1. Получаем имя файла из справочника
        filename = _get_output_filename(company_name, period, context)
        
        # 2. Проверяем наличие данных
        balance_df = context.balance_df
        summary_osv_df = context.summary_osv_df
        
        pnl_df = context.pnl_df
        journal_df = context.journal_df
        
        if(balance_df is None
           and summary_osv_df is None
           and pnl_df is None
           and journal_df is None):
            logger.warning("Нет данных для сохранения")
            return
        
        # 3. Если есть оба DataFrame — сохраняем комбинированный отчёт
        else:
            DataSaver.save_combined_report(
                balance_df,
                summary_osv_df,
                pnl_df,
                journal_df,
                filename
            )
            logger.info(f"Комбинированный отчёт сохранён: {filename}")
        # # 4. Если есть только финальный отчёт
        # elif final_report is not None:
        #     DataSaver.save_to_excel(final_report, filename)
        #     logger.info(f"Сохранён только финальный отчёт: {filename}")
        # # 5. Если есть только main_df
        # elif main_df is not None:
        #     DataSaver.save_to_excel(main_df, filename, subfolder="intermediate")
        #     logger.info(f"Сохранён только основной DataFrame: {filename}")
            
    except Exception as e:
        logger.error(f"Ошибка при сохранении результатов: {e}")
        raise

def _get_output_filename(company_name: str, period: str, context: ProcessingContext) -> str:
    """
    Получает имя файла из справочника КомпанииГруппы.
    
    Если компания не найдена или столбец отсутствует — 
    возвращает стандартное имя файла.
    
    Args:
        company_name: Название компании
        period: Период отчётности
        
    Returns:
        Имя файла (например, "Расшифровка_ББЛ_ББ_2025.xlsx")
    """
    try:
        companies_df = context.references['компании_группы']
        
        # Ищем компанию
        matching = companies_df[
            companies_df['сокращенное_наименование_компании'] == company_name
        ]
        
        if matching.empty:
            logger.warning(
                f"Компания '{company_name}' не найдена в справочнике. "
                f"Используем стандартное имя файла."
            )
            return f"balance_breakdown_{company_name}_{period}.xlsx"
        
        # Получаем шаблон имени файла
        filename_template = matching.iloc[0]['название_файла_расшифровки']
        
        if pd.isna(filename_template) or not filename_template:
            logger.warning(
                f"Столбец 'название_файла_расшифровки' пуст для '{company_name}'. "
                f"Используем стандартное имя файла."
            )
            return f"balance_breakdown_{company_name}_{period}.xlsx"
        
        # Подставляем период, если в шаблоне есть плейсхолдер
        filename = filename_template
        if '{period}' in filename:
            filename = filename.replace('{period}', str(period))
        elif '{период}' in filename:
            filename = filename.replace('{период}', str(period))
        
        # Добавляем расширение, если его нет
        if not filename.endswith('.xlsx'):
            filename = f"{filename}.xlsx"
        
        logger.debug(f"Имя файла из справочника: {filename}")
        return filename
        
    except Exception as e:
        logger.warning(
            f"Не удалось получить имя файла из справочника: {e}. "
            f"Используем стандартное имя файла."
        )
        return f"balance_breakdown_{company_name}_{period}.xlsx"

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
        
        response = input("Ваш выбор: ").strip().lower()
        return response in ('y', 'yes', 'д', 'да')
    except (EOFError, KeyboardInterrupt):
        # Если stdin недоступен (например, при запуске из cron)
        return False

def main(show_traceback: bool = False, verbose: bool = False) -> int:
    
    """
    Главная функция приложения.
    
    Выполняет следующие действия:
    1. Настраивает логирование
    2. Инициализирует контекст
    3. Запускает первый пайплайн (подготовка списка выгрузок)
    4. Делает паузу для выгрузки данных из 1С
    5. Запускает второй пайплайн (основная обработка)
    6. Сохраняет результаты
    """
    # Настраиваем логирование
    if verbose:
        setup_logger(console_level='DEBUG')
    else:
        setup_logger()
    
    logger.info("=" * 80)
    logger.info("Запуск приложения Decryption Collector v2.0")
    if show_traceback:
        logger.info("Режим: с полной трассировкой стека")
    logger.info("=" * 80)
    
    try:
        context = initialize_context()
        
        # ФАЗА 1
        logger.info("ФАЗА 1: Формирование списка выгрузок из 1С")
        preparation_pipeline = create_preparation_pipeline()
        context = preparation_pipeline.run(context)
        
        # ПАУЗА
        pause_for_1c_export(context)
        
        # ФАЗА 2
        logger.info("ФАЗА 2: Основная обработка данных")
        main_pipeline = create_main_pipeline()
        context = main_pipeline.run(context)
        
        save_results(context)
        
        logger.info("=" * 80)
        logger.info("✓ Приложение успешно завершено")
        logger.info("=" * 80)
        
        return 0
        
    except FileNotFoundError as e:
        logger.error("✗ Ошибка: не найдены файлы выгрузок")
        logger.error(f"  {e}")
        if show_traceback:
            logger.exception("Трассировка стека:")
        return 1
        
    except Exception as e:
        logger.critical(f"✗ Неожиданная ошибка: {e}")
        if show_traceback:
            logger.exception("Трассировка стека:")
        return 1


if __name__ == "__main__":
    args = parse_arguments()
    
    # Определяем, нужно ли спрашивать пользователя
    # Если аргументы переданы явно (через командную строку), используем их
    # Если нет (запуск через F5 в Spyder) - спрашиваем
    was_args_passed = len(sys.argv) > 1
    
    if was_args_passed:
        # Аргументы переданы - используем их
        show_traceback = args.traceback
        verbose = args.verbose
    else:
        # Аргументы не переданы - спрашиваем пользователя
        show_traceback = ask_user_about_traceback()
        verbose = False  # По умолчанию не verbose
    
    exit_code = main(show_traceback=show_traceback, verbose=verbose)
    sys.exit(exit_code)
