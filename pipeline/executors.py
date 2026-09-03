# -*- coding: utf-8 -*-
"""
Оркестрация фаз выполнения приложения.

Содержит функции для:
- фазовые паузы (ожидание выгрузки из 1С);
- инициализация контекста и загрузка справочников;
- сохранение результатов.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext, Step
from pipeline.constants import ColumnNames
from pipeline.errors import ReferenceMismatchError
from pipeline.step_config import OpuReportConstants
from config.settings import REFERENCE_CONFIGS
from io_module import DataLoader, DataSaver
from io_module.output_manager import get_run_dir
from utils.currency_utils import (
    needs_conversion,
    get_currency,
    get_rate_for_date_with_info,
    get_last_rate_date,
)

# Формат даты перевода валютных остатков баланса (совпадает с форматом
# колонки «дата» в справочниках Курс_AED / Курс_CNY)
BALANCE_DATE_FORMAT = '%d.%m.%Y'


def pause_for_osv_general_export(interactive: bool = True) -> None:
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
    if interactive:
        try:
            input("\n[PAUSE] Когда файл с Общей ОСВ будет готов, нажмите Enter для продолжения...")
        except EOFError:
            pass
    print("=" * 80 + "\n")


def pause_for_1c_export(context: ProcessingContext, interactive: bool = True) -> None:
    """
    Приостанавливает выполнение и ждет, пока бухгалтер выгрузит файлы из 1С.
    """
    expected_count = len(context.data.get('expected_filenames', []))
    print("\n" + "=" * 80)
    print(f"[LIST] Сформирован список из {expected_count} регистров к выгрузке.")
    print(f"[FOLDER] Список сохранен в папке: {get_run_dir()}")
    print()
    print("[>>] ВАШИ ДЕЙСТВИЯ:")
    print(f"   1. Откройте файл 'Выгрузить_*.xlsx' в папке {get_run_dir()}")
    print("   2. Выгрузите указанные регистры из 1С")
    print("   3. Положите все файлы в папку INPUT_DATA в подпапки, указанные в поле 'куда класть' файла 'Выгрузить_*.xlsx'")
    print()
    print("[i]  Для досрочного выхода из программы нажмите Ctrl+C")
    print("=" * 80)
    if interactive:
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
            ColumnNames.CURRENCY,
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
    "курс_aed": ReferenceSpec(
        sheet_name="Курс_AED",
        strings=(),
        required=False,
    ),
    "курс_cny": ReferenceSpec(
        sheet_name="Курс_CNY",
        strings=(),
        required=False,
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
    # Currency of the company (new reference column). Default = RUB.
    if (
        ColumnNames.CURRENCY in row.index
        and pd.notna(row[ColumnNames.CURRENCY])
        and str(row[ColumnNames.CURRENCY]).strip()
    ):
        context.currency = str(row[ColumnNames.CURRENCY]).strip().upper()
        logger.debug("Company currency: {}", context.currency)
    else:
        context.currency = "RUB"
        logger.debug("Company currency is not set; using RUB.")


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


def ask_balance_date_if_needed(context: ProcessingContext, interactive: bool = True) -> None:
    """
    Запрашивает у пользователя дату перевода валютных остатков в рубли
    для расшифровки баланса (вариант А — один раз в cli.main).

    Дата нужна только компаниям с валютой, отличной от RUB. Если дата
    уже задана заранее (CLI-флаг --balance-date), вопрос не задаётся.

    В неинтерактивном режиме (--no-interactive / недоступный stdin),
    а также при EOF/Ctrl+C используется последняя дата из справочника
    курса соответствующей валюты — с предупреждением в логе.

    Args:
        context: Контекст обработки (валюта должна быть уже заполнена).
        interactive: Разрешено ли задавать вопросы пользователю.
    """
    if not needs_conversion(context):
        logger.debug(
            "Валюта компании {} — перевод остатков не требуется, дата баланса не нужна.",
            get_currency(context),
        )
        return

    if context.balance_date:
        rate, rate_date = get_rate_for_date_with_info(context, context.balance_date)
        if rate_date == context.balance_date:
            logger.info(
                "Остатки будут переведены в рубли по курсу {} на заданную дату {}.",
                currency,
                rate_date,
            )
        else:
            logger.info(
                "Остатки будут переведены в рубли по курсу {} на ближайшую доступную "
                "в справочнике дату {} (задано {}).",
                currency,
                rate_date,
                context.balance_date,
            )
        logger.info("Курс перевода остатков баланса: {}", rate)
        context.balance_date = rate_date
        return

    currency = get_currency(context)

    def _fallback() -> None:
        last_date = get_last_rate_date(context)
        context.balance_date = last_date
        logger.warning(
            "[!] Дата перевода остатков не задана. Используется последняя дата "
            "из справочника курса {}: {}. (Задать явно: --balance-date ДД.ММ.ГГГГ)",
            currency,
            last_date,
        )

    if not interactive:
        _fallback()
        return

    prompt = (
        f"\nОстатки по данной компании в валюте {currency}. "
        f"Введите дату, на которую нужно перевести остатки в рубли "
        f"для расшифровки баланса (ДД.ММ.ГГГГ): "
    )

    while True:
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            _fallback()
            return

        if not raw:
            print("[!] Дата не введена. Повторите ввод (ДД.ММ.ГГГГ) или нажмите Ctrl+C.")
            continue

        try:
            datetime.strptime(raw, BALANCE_DATE_FORMAT)
        except ValueError:
            print(f"[!] Некорректная дата: {raw!r}. Ожидается формат ДД.ММ.ГГГГ, например 31.12.2025.")
            continue

        # Сразу проверяем наличие курса на выбранную дату (ближайшую <=)
        # и показываем его пользователю до запуска конвейера
        try:
            rate, rate_date = get_rate_for_date_with_info(context, raw)
        except ValueError as e:
            print(f"[!] {e}")
            continue

        context.balance_date = rate_date
        if rate_date == raw:
            logger.info(
                "Остатки будут переведены в рубли по курсу {} на дату {}.",
                currency,
                rate_date,
            )
        else:
            logger.info(
                "Остатки будут переведены в рубли по курсу {} на ближайшую доступную "
                "в справочнике дату {} (запрошено {}).",
                currency,
                rate_date,
                raw,
            )
        logger.info("Курс перевода остатков баланса: {}", rate)
        return


def _get_output_filename(company_name: str, period: str, context: ProcessingContext) -> str:
    """
    Получает имя файла из справочника КомпанииГруппы.

    Если компания не найдена или столбец отсутствует — 
    возвращает стандартное имя файла.

    Args:
        company_name: Название компании
        period: Период отчётности

    Returns:
        Имя файла (например, "balance_breakdown_ББЛ_2025.xlsx")
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


def _prepare_journal_for_output(
    journal_df: pd.DataFrame,
    context: ProcessingContext,
) -> pd.DataFrame:
    """Готовит journal_df к сохранению на лист «исходники ОПУ».

    Для рублёвых компаний (needs_conversion=False) удаляет служебный
    столбец «оборот, тыс.руб.»: перевод в рубли не выполняется, поэтому
    он всегда равен «оборот, тыс.ед.» и вводит пользователя в заблуждение.
    Для валютных компаний столбец сохраняется — в нём результат
    конвертации по курсу на дату операции (см. add_ruble_amount_column).
    context.journal_df не изменяется — правится только копия для файла.
    """
    if journal_df is None or needs_conversion(context):
        return journal_df

    rub_col = OpuReportConstants.RUB_AMOUNT_COL
    if rub_col in journal_df.columns:
        journal_df = journal_df.drop(columns=[rub_col])
        logger.debug(
            "Лист «исходники ОПУ»: столбец '{}' удалён — валюта RUB, "
            "значения равны 'оборот, тыс.ед.'",
            rub_col,
        )
    return journal_df


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

        # Рублёвым компаниям служебный «оборот, тыс.руб.» в исходниках ОПУ
        # не нужен — он равен «оборот, тыс.ед.» (перевод не выполняется)
        journal_df = _prepare_journal_for_output(journal_df, context)

        if all(df is None for df in [balance_df, summary_osv_df, pnl_df, journal_df]):
            logger.warning("Нет данных для сохранения")
            return

        output_path = DataSaver.save_combined_report(balance_df, summary_osv_df, pnl_df, journal_df, filename)
        logger.info("Комбинированный отчёт сохранён: {}", output_path)

    except Exception as e:
        logger.error("Ошибка при сохранении результатов: {}", e)
        raise

