
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
from dataclasses import dataclass, field
from typing import Any
warnings.filterwarnings('ignore', message='Data Validation extension is not supported')

# Добавляем корневую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from logging_handling.logger_config import setup_logger
from pipeline.base import Pipeline, ProcessingContext
from config.settings import REFERENCE_CONFIGS
from config.defaults import TOLERANCE_DESCRIPTIONS
from config.loader import load_params
from pipeline.base import Step
from pipeline.errors import ReferenceMismatchError

from pipeline.steps import (
    Step1aListExpectedRegistersStep,
    Step1bVerifyFilesStep,
    Step1cReconcileTotalsStep,
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

class ColumnNames:
    SHORT_COMPANY_NAME = "сокращенное_наименование_компании"
    SEGMENT = "сегмент"
    PERIOD_TYPE = "тип_периода"
    DECRYPTION_FILENAME = "название_файла_расшифровки"

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
    pipeline = Pipeline(name="Основной конвейер сборки расшифровки ББ и ОПУ")
    
    # ЭТАП 1: Загрузка и подготовка данных (баланс и опу)
    pipeline.add_step(Step1bVerifyFilesStep())
    pipeline.add_step(Step1cReconcileTotalsStep())
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

def pause_for_osv_general_export() -> None:
    """
    Приостанавливает выполнение и ждет, пока бухгалтер выгрузит Общую ОСВ из 1С.
    """
    print("\n" + "=" * 80)
    print()
    print("[>>] ВАШИ ДЕЙСТВИЯ:")
    print("   1. Убедитесь, что файл с актуальной Общей ОСВ расположен в папке INPUT_DATA.")
    print("   2. Убедитесь, что имя файла с Общей ОСВ имеет следующий формат:")
    print("      СокрНаименованиеКомпании_общаяосв_нд_Период_.xlsx, например, РЗК_общаяосв_нд_2025_.xlsx")
    print("   3. Убедитесь, что наименование компании соотвествует данным на листе КомпанииГруппы файла Справочники.xlsx из папки _REFERENCE_DATA")
    print()
    print("[i]  Для досрочного выхода из программы нажмите Ctrl+C")
    print("=" * 80)
    try:
        input("\n[PAUSE] Когда файл с Общей ОСВ будет готов, нажмите Enter для продолжения...")
    except EOFError:
        pass
    print("=" * 80 + "\n")


def pause_for_1c_export(context: ProcessingContext) -> None:
    """
    Приостанавливает выполнение и ждет, пока бухгалтер выгрузит файлы из 1С.
    """
    expected_count = len(context.data.get('expected_filenames', []))
    print("\n" + "=" * 80)
    print(f"[LIST] Сформирован список из {expected_count} регистров к выгрузке.")
    print("[FOLDER] Список сохранен в папке OUTPUT_DATA.")
    print()
    print("[>>] ВАШИ ДЕЙСТВИЯ:")
    print("   1. Откройте файл 'Выгрузить_*.xlsx' в папке OUTPUT_DATA")
    print("   2. Выгрузите указанные регистры из 1С")
    print("   3. Положите все файлы в папку INPUT_DATA в подпапки, указанные в поле 'куда класть' файла 'Выгрузить_*.xlsx'")
    print()
    print("[i]  Для досрочного выхода из программы нажмите Ctrl+C")
    print("=" * 80)
    try:
        input("\n[PAUSE] Когда файлы будут готовы, нажмите Enter для продолжения...")
    except EOFError:
        pass
    print("=" * 80 + "\n")




# ─────────────────────────────────────────────────────────────────────────────
# 1. Единый реестр справочников — единственная точка правды
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ReferenceSpec:
    """Спецификация справочника: что и как загружать."""
    sheet_name: str
    strings: tuple[str, ...] = ()
    extra_kwargs: dict[str, Any] = field(default_factory=dict)
    usecols: tuple[int, ...] | None = None
    required: bool = True


REFERENCE_REGISTRY: dict[str, ReferenceSpec] = {
    "план_счетов_фо": ReferenceSpec(
        sheet_name="ПланСчетов",
        strings=("РСБУ Код отчетности", "Итоговый номер счета"),
    ),
    "меппинг_баланс": ReferenceSpec(
        sheet_name="Меппинг_бб",
        **REFERENCE_CONFIGS["Меппинг_бб"],
    ),
    "меппинг_опу": ReferenceSpec(
        sheet_name="Меппинг_опу",
        **REFERENCE_CONFIGS["Меппинг_опу"],
    ),
    "компании_группы": ReferenceSpec(
        sheet_name="КомпанииГруппы",
        strings=(
            ColumnNames.SHORT_COMPANY_NAME,
            ColumnNames.DECRYPTION_FILENAME,
            ColumnNames.PERIOD_TYPE,
        ),
    ),
    "выгрузки": ReferenceSpec(sheet_name="Выгрузки"),
    "параметры": ReferenceSpec(
        sheet_name="Параметры",
        strings=(
            "параметр",
            "описание",
            "ед. изм.",
            "тип данных значения",
        ),
        required=False,
    ),
    "план_счетов_бу": ReferenceSpec(
        sheet_name="ПланСчетовБУ",
        strings=(
            "компания", "код", "наименование",
            "субконто_1", "субконто_2", "субконто_3",
        ),
    ),
    "справочник_уфр": ReferenceSpec(
        sheet_name="СправочникУФР",
        strings=("строка_уфр", ColumnNames.SEGMENT, ColumnNames.SHORT_COMPANY_NAME, "ном_группа_1с"),
    ),
    "справочник_ппа": ReferenceSpec(
        sheet_name="ППА",
        strings=(
            "группа_ос", "вид_взаиморасчетов", "наименование_компании",
            "рбп", "ос_ппа", "ос_после_перехода_в_собственность",
            "договор_аренды", "контрагент",
        ),
    ),
    "кредит_обслуж": ReferenceSpec(
        sheet_name="КредитОбслуж",
        strings=("компания", "рбп_кредитные_линии", "контрагент"),
    ),
    "вид_связи_ка": ReferenceSpec(
        sheet_name="ВидСвязиКА",
        strings=("ВидСвязиКА", ColumnNames.SEGMENT, "ВариантыНазвания"),
    ),
    "прочие_доходы_ндс": ReferenceSpec(
        sheet_name="ПрочиеДоходыНДС",
        strings=("прочие_доходы_ндс",),
    ),
    "виды_рбп_аренда_лизинг": ReferenceSpec(
        sheet_name="ВидыРБП_АрендаЛизинг",
        strings=("виды_рбп_аренда_лизинг",),
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# 2. Вспомогательные функции — каждая отвечает за одну задачу
# ─────────────────────────────────────────────────────────────────────────────
def _parse_osv_filename(filename: str) -> tuple[str, str]:
    """Извлекает компанию и период из имени файла ОСВ.

    Ожидается формат: CompanyName_Register_Account_Period_.xlsx
    """
    stem = Path(filename).stem
    parts = [p.strip() for p in stem.split("_") if p.strip()]

    if len(parts) < 3:
        raise ValueError(
            f"Некорректное имя файла '{filename}'. "
            "Ожидается формат: CompanyName_Register_Account_Period_.xlsx"
        )

    return parts[0], parts[-1]


def _load_all_references() -> dict[str, pd.DataFrame]:
    """Загружает все справочники согласно REFERENCE_REGISTRY."""
    references: dict[str, pd.DataFrame] = {}

    for key, spec in REFERENCE_REGISTRY.items():
        logger.debug(
            "Загрузка справочника '{}' (лист '{}')",
            key,
            spec.sheet_name,
        )

        loader_kwargs = dict(spec.extra_kwargs)
        loader_kwargs["required"] = spec.required

        if spec.usecols is not None:
            loader_kwargs.setdefault("usecols", list(spec.usecols))

        df = DataLoader.load_reference_data(
            sheet_name=spec.sheet_name,
            strings=list(spec.strings),
            **loader_kwargs,
        )

        if df.empty:
            references[key] = df
        else:
            references[key] = Step.clean_whitespace(df)

    return references


def _get_required_field(row: pd.Series, field_name: str, company: str) -> str:
    """
    Извлекает обязательное поле из строки справочника.
    
    Args:
        row: Строка из справочника (pd.Series)
        field_name: Название поля для извлечения
        company: Название компании (для сообщения об ошибке)
    
    Returns:
        Значение поля в виде строки с удалёнными пробелами
    
    Raises:
        ReferenceMismatchError: Если поле пустое или отсутствует
    """
    value = row[field_name]
    if pd.isna(value) or not str(value).strip():
        raise ReferenceMismatchError(
            message=f"У компании '{company}' не заполнено поле '{field_name}'",
            problem_data=row.to_frame().T,
            reference_name="КомпанииГруппы",
            searched_company=company,
        )
    return str(value).strip()


def _validate_and_enrich_company_info(context: ProcessingContext) -> None:
    """
    Валидирует наличие компании в справочнике и обогащает контекст данными о компании.
    
    Args:
        context: Контекст обработки с загруженными справочниками
    
    Raises:
        ValueError: Если в справочнике отсутствуют обязательные колонки
        ReferenceMismatchError: Если компания не найдена, найдено несколько записей,
                                или не заполнены обязательные поля
    """
    directory = context.references["компании_группы"]
    
    # Проверка наличия обязательных колонок
    required_columns = {ColumnNames.SHORT_COMPANY_NAME, ColumnNames.SEGMENT, ColumnNames.PERIOD_TYPE}
    missing = required_columns - set(directory.columns)
    if missing:
        raise ValueError(
            "В справочнике 'компании_группы' отсутствуют обязательные колонки: "
            f"{sorted(missing)}"
        )
    
    # Нормализация имён компаний для поиска
    normalized_names = (
        directory[ColumnNames.SHORT_COMPANY_NAME]
        .fillna("")
        .astype(str)
        .str.strip()
    )
    
    # Поиск компании в справочнике
    matches = directory[normalized_names == context.company]
    
    if matches.empty:
        problem_data = (
            directory[[ColumnNames.SHORT_COMPANY_NAME]]
            .drop_duplicates()
            .rename(columns={ColumnNames.SHORT_COMPANY_NAME: "компания_в_справочнике"})
        )
        raise ReferenceMismatchError(
            message=f"Компания '{context.company}' не найдена в справочнике",
            problem_data=problem_data,
            reference_name="КомпанииГруппы",
            searched_company=context.company,
        )
    
    if len(matches) > 1:
        raise ReferenceMismatchError(
            message=(
                f"У компании '{context.company}' найдено "
                f"{len(matches)} записей. Ожидается одна."
            ),
            problem_data=matches.copy(),
            reference_name="КомпанииГруппы",
            duplicate_count=len(matches),
        )
    
    # Получаем единственную запись компании
    row = matches.iloc[0]
    context.company = _get_required_field(row, ColumnNames.SHORT_COMPANY_NAME, context.company)
    
    # Извлекаем и валидируем обязательные поля через вспомогательную функцию
    context.segment = _get_required_field(row, ColumnNames.SEGMENT, context.company)
    context.type_period = _get_required_field(row, ColumnNames.PERIOD_TYPE, context.company)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Оркестратор — теперь читается как план действий
# ─────────────────────────────────────────────────────────────────────────────
def initialize_context() -> ProcessingContext:
    logger.debug("Инициализация контекста обработки")

    context = ProcessingContext()
    
    # Шаг 1. Загрузка общей ОСВ
    osv_df, osv_filename = DataLoader.load_general_osv()
    
    if osv_df.empty:
        raise ValueError("Загруженная общая ОСВ пуста")

    context.common_osv_df = osv_df
    context.name_file_general_osv = osv_filename

    # Шаг 2. Извлечение метаданных из имени файла
    context.company, context.period = _parse_osv_filename(osv_filename)

    # Шаг 3. Загрузка всех справочников и параметров
    context.references = _load_all_references()
    

    # Шаг 4. Валидация компании и обогащение контекста
    _validate_and_enrich_company_info(context)

    logger.debug(
        "Контекст инициализирован: компания={}, период={}, строк в ОСВ={}, справочников={}",
        context.company,
        context.period,
        len(osv_df),
        len(context.references)
        
        
    )
    return context

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
        
        if all(df is None for df in [balance_df, summary_osv_df, pnl_df, journal_df]):
            logger.warning("Нет данных для сохранения")
            return
        
        DataSaver.save_combined_report(balance_df, summary_osv_df, pnl_df, journal_df, filename)
        logger.info("Комбинированный отчёт сохранён: {}", filename)

    except Exception as e:
        logger.error("Ошибка при сохранении результатов: {}", e)
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
            companies_df[ColumnNames.SHORT_COMPANY_NAME] == company_name
        ]
        
        if matching.empty:
            logger.warning(
                "Компания '{}' не найдена в справочнике. Используем стандартное имя файла.",
                company_name
            )
            return f"balance_breakdown_{company_name}_{period}.xlsx"
        
        # Получаем шаблон имени файла
        filename_template = matching.iloc[0][ColumnNames.DECRYPTION_FILENAME]
        
        if pd.isna(filename_template) or not filename_template:
            logger.warning(
                "Столбец 'название_файла_расшифровки' пуст для '{}'. Используем стандартное имя файла.",
                company_name
            )
            return f"balance_breakdown_{company_name}_{period}.xlsx"
        
        # Подставляем период, если в шаблоне есть плейсхолдер
        filename = filename_template
        filename = filename.replace('{period}', str(period)).replace('{период}', str(period))
        
        # Добавляем расширение, если его нет
        if not filename.endswith('.xlsx'):
            filename = f"{filename}.xlsx"
        
        logger.debug("Имя файла из справочника: {}", filename)
        return filename
        
    except (KeyError, IndexError, AttributeError) as e:
        logger.warning(
            "Не удалось получить имя файла из справочника: {}. Используем стандартное имя файла.", e
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
        # Корректное завершение по Ctrl+C — без traceback
        logger.info("")
        logger.info("=" * 80)
        logger.warning("⚠ Получен сигнал прерывания (Ctrl+C). Завершаем работу.")
        logger.info("   Несохранённые промежуточные результаты могли быть потеряны.")
        logger.info("=" * 80)
        return 130  # стандартный код выхода для SIGINT

    except FileNotFoundError as e:
        logger.error("✗ Ошибка: не найдены файлы выгрузок")
        logger.error("  {}", e)
        if show_traceback:
            logger.exception("Трассировка стека:")
        return 1

    except Exception as e:
        logger.critical("✗ Неожиданная ошибка: {}", e)
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
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
