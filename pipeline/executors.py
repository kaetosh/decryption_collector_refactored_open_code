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
import sys
from typing import Any, Optional

import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext, Step
from pipeline.constants import ColumnNames
from pipeline.errors import ReferenceMismatchError, PeriodMismatchError, TooManyFilesError
from pipeline.step_config import (
    BalanceReportConstants,
    ManualCorrectionsConstants,
    OpuReportConstants,
    PlanAccountsConstants,
)
from config.loader import load_params
from config.settings import (
    ACCOUNT_CARDS_DIR,
    ACCOUNTS_OSV_DIR,
    CORRECTIONS_FILE,
    CORRECTIONS_FILE_NAME,
    ACCOUNTS_OSV_LEASE_DIR,
    AUTO_SORT_ENABLED,
    INBOX_DIR,
    OSV_GENERAL_DIR,
    REFERENCE_CONFIGS,
    REFERENCE_DATA_FILE,
    REFERENCE_DIR,
    REFERENCE_PAUSE_ENABLED_KEY,
    REFERENCE_PAUSE_COUNT_KEY,
    SPECIAL_REPORTS_DIR,
    to_relative,
)
from io_module import (
    DataLoader,
    DataSaver,
    prepare_general_osv_from_inbox,
    safe_build_cover_rows,
    sort_inbox_by_expected_list,
)
from io_module.output_manager import get_output_dir, get_run_dir, get_run_id
from logging_handling.logger_config import format_warnings_summary
from utils.reference_scope import ScopeDiagnostics, resolve_company_view
from utils.currency_utils import (
    needs_conversion,
    get_currency,
    get_last_rate_date,
    format_rate_date,
    record_balance_rate,
    resolve_rate,
    warn_stale_rate,
)

# Формат даты перевода валютных остатков баланса (совпадает с форматом
# колонки «дата» в справочниках Курс_AED / Курс_CNY)
BALANCE_DATE_FORMAT = '%d.%m.%Y'


def pause_for_osv_general_export(interactive: bool = True) -> None:
    """
    Приостанавливает выполнение и ждет, пока бухгалтер выгрузит файлы из 1С.

    После нажатия Enter (волна 1 автосортировки, io_module/auto_sort.py)
    файлы общей ОСВ из 00_inbox переносятся в general_osv; остальные
    файлы inbox остаются ждать волну 2 (pause_for_1c_export).
    """
    # Сканируем целевые папки на наличие файлов перед показом инструкции
    folders_to_check = {
        "general_osv": OSV_GENERAL_DIR,
        "accounts_osv": ACCOUNTS_OSV_DIR,
        "special_reports": SPECIAL_REPORTS_DIR,
        "accounts_osv_lease": ACCOUNTS_OSV_LEASE_DIR,
        "transaction_report": ACCOUNT_CARDS_DIR,
    }

    found_files: dict[str, list[str]] = {}
    for label, path in folders_to_check.items():
        if path.exists():
            files = [f.name for f in sorted(path.glob("*.xlsx")) if not f.name.startswith("~$")]
            if files:
                found_files[label] = files

    has_files = bool(found_files)

    print("\n" + "=" * 80)
    print()
    print("[>>] ВАШИ ДЕЙСТВИЯ:")
    print(f"   1. Выгрузите из 1С ВСЕ нужные файлы и положите их в папку {to_relative(INBOX_DIR)}")
    print("      (одним движением, без подпапок: Общая ОСВ, ОСВ по счетам, спецотчёты,")
    print("      отчеты по проводкам — скрипт сам разложит их по папкам)")
    print("   2. Убедитесь, что имя файла с Общей ОСВ имеет следующий формат:")
    print("      СокрНаименованиеКомпании_общаяосв_нд_Период_.xlsx, например, РЗК_общаяосв_нд_2025_.xlsx")
    print(f"   3. Убедитесь, что наименование компании соответствует данным на листе КомпанииГруппы файла Справочники.xlsx из папки {to_relative(REFERENCE_DIR)}")
    print()

    if has_files:
        print("[!] ВНИМАНИЕ: В целевых папках уже есть файлы:")
        for label, files in found_files.items():
            folder_path = folders_to_check[label]
            print(f"      {to_relative(folder_path)}:")
            for f in files:
                print(f"        - {f}")
        print()
        print("      При нажатии Enter:")
        print("      • Файлы Общей ОСВ из 00_inbox перенесутся в general_osv")
        print("      • Обнаруженные в других папках Общие ОСВ заархивируются")
        print("      • Остальные файлы в целевых папках — заархивируются как хвосты прошлых сессий")
        print("      (ничего не удаляется безвозвратно — всё в _INPUT_DATA/_archive/<run_id>/)")
        print()
    else:
        print("[i]  Целевые папки пусты — чистый старт.")

    print("[i]  После нажатия Enter скрипт перенесет Общую ОСВ из 00_inbox в general_osv;")
    print("[i]  остальные файлы останутся в 00_inbox до следующей паузы")
    print("[i]  Для досрочного выхода из программы нажмите Ctrl+C")
    print("=" * 80)
    if interactive:
        try:
            input("\n[PAUSE] Когда файлы будут выгружены, нажмите Enter для продолжения...")
        except EOFError:
            pass
    print("=" * 80 + "\n")
    if AUTO_SORT_ENABLED:
        prepare_general_osv_from_inbox()


def pause_for_1c_export(context: ProcessingContext, interactive: bool = True) -> None:
    """
    Приостанавливает выполнение и ждет, пока бухгалтер выгрузит файлы из 1С.

    После нажатия Enter (волна 2 автосортировки, io_module/auto_sort.py)
    файлы из 00_inbox раскладываются по целевым папкам согласно списку
    выгрузок «Выгрузить_<компания>_<период>.xlsx».
    """
    expected_count = len(context.data.get('expected_filenames', []))
    print("\n" + "=" * 80)
    print(f"[LIST] Сформирован список из {expected_count} регистров к выгрузке.")
    print(f"[FOLDER] Список сохранен в папке: {to_relative(get_run_dir())}")
    print()
    print("[>>] ВАШИ ДЕЙСТВИЯ:")
    print(f"   1. Откройте файл 'Выгрузить_*.xlsx' в папке {to_relative(get_run_dir())}")
    print("   2. Выгрузите указанные регистры из 1С")
    print(f"   3. Положите все файлы в папку {to_relative(INBOX_DIR)} (одним движением, без подпапок);")
    print("      после нажатия Enter скрипт сам разложит их по папкам согласно списку")
    print()
    print("[i]  Для досрочного выхода из программы нажмите Ctrl+C")
    print("=" * 80)
    if interactive:
        try:
            input("\n[PAUSE] Когда файлы будут готовы, нажмите Enter для продолжения...")
        except EOFError:
            pass
    print("=" * 80 + "\n")
    if AUTO_SORT_ENABLED:
        sort_inbox_by_expected_list(context)


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
            ColumnNames.PERIOD_REPORT,
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
            PlanAccountsConstants.COMPANY_COL,
            PlanAccountsConstants.CODE_COL,
            PlanAccountsConstants.NAME_COL,
            *PlanAccountsConstants.SUBCONTO_COLS,
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
    "прочие_дох_рас_свернуто": ReferenceSpec(
        sheet_name="Прочие_дох_рас_свернуто",
        strings=("номер_группы_сворачивания", "1 уровень", "2 уровень (точка плана)"),
    ),
    "статьи_баланс_свернуто": ReferenceSpec(
        sheet_name="СтатьиБаланс_свернуто",
        strings=("номер_группы_сворачивания", "1 уровень", "2 уровень (точка плана)"),
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
        PeriodMismatchError: Если период в имени файла общей ОСВ не совпадает
                             с периодом отчётности в справочнике КомпанииГруппы
    """
    directory = context.references["компании_группы"]

    # Проверка наличия обязательных колонок
    required_columns = {
        ColumnNames.SHORT_COMPANY_NAME,
        ColumnNames.SEGMENT,
        ColumnNames.PERIOD_TYPE,
        ColumnNames.PERIOD_REPORT,
    }
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

    # Проверка соответствия периода: из имени файла общей ОСВ vs период отчётности
    # в справочнике «КомпанииГруппы» по строке с именем компании.
    period_in_directory = _get_required_field(
        row, ColumnNames.PERIOD_REPORT, context.company
    )
    if context.period != period_in_directory:
        raise PeriodMismatchError(
            message=(
                f"Период в имени файла общей ОСВ ('{context.period}') не совпадает "
                f"с периодом отчётности компании '{context.company}' в справочнике "
                f"КомпанииГруппы ('{period_in_directory}'). Исправьте период в имени "
                f"файла общей ОСВ или в Справочники.xlsx (поле "
                f"'{ColumnNames.PERIOD_REPORT}')."
            ),
            problem_data=matches.copy(),
            reference_name="КомпанииГруппы",
            searched_company=context.company,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2а. Индивидуальный меппинг компании (колонка «компания» в листах меппинга)
# ─────────────────────────────────────────────────────────────────────────────
def _apply_company_reference_scope(context: ProcessingContext) -> None:
    """
    Приводит меппинги к «взгляду компании»: строки с её именем в колонке
    «компания» (config.settings.REFERENCE_SCOPE_COL) перекрывают универсальные
    («все»), строки других компаний отбрасываются.

    Вызывается после валидации компании (известны company и список компаний
    «КомпанииГруппы») и до старта конвейера — поэтому шаги баланса
    (4/5/7/8/9/13) и ОПУ (14/17/19) читают уже готовый взгляд, а не собирают
    его каждый сам. Компании без индивидуальных строк получают справочник
    без изменений (см. utils/reference_scope.resolve_company_view).
    """
    companies_df = context.references.get('компании_группы')
    known_companies: tuple[str, ...] = ()
    if companies_df is not None and ColumnNames.SHORT_COMPANY_NAME in companies_df.columns:
        known_companies = tuple(
            companies_df[ColumnNames.SHORT_COMPANY_NAME].dropna().astype(str)
        )

    targets = (
        ('меппинг_опу', 'Меппинг_опу', OpuReportConstants.REFERENCE_ROW_KEY),
        ('меппинг_баланс', 'Меппинг_бб', BalanceReportConstants.MAPPING_KEYS),
    )

    entries: list[dict] = []
    diagnostics: list = []

    for reference_key, sheet_name, key_cols in targets:
        reference = context.references.get(reference_key)
        if reference is None or reference.empty:
            continue

        view, diag = resolve_company_view(
            reference,
            context.company,
            key_cols,
            reference_name=sheet_name,
            known_companies=known_companies,
        )
        if view is not None:
            context.references[reference_key] = view

        diagnostics.append(diag)
        if diag.applied or diag.dropped_foreign_rows:
            entries.append({
                OpuReportConstants.REFERENCE_SCOPE_ENTRY_SHEET: sheet_name,
                OpuReportConstants.REFERENCE_SCOPE_ENTRY_INDIVIDUAL: diag.individual_rows,
                OpuReportConstants.REFERENCE_SCOPE_ENTRY_OVERRIDDEN: diag.overridden_rows,
                OpuReportConstants.REFERENCE_SCOPE_ENTRY_DROPPED: diag.dropped_foreign_rows,
            })

    # Сводка для титульного листа отчёта (io_module/report_cover.py)
    context.data[OpuReportConstants.REFERENCE_SCOPE_SUMMARY_KEY] = {
        'company': context.company,
        'entries': entries,
        OpuReportConstants.REFERENCE_SCOPE_UNKNOWN_VALUES: sorted({
            value for diag in diagnostics for value in diag.unknown_scope_values
        }),
    }

    _save_reference_scope_diagnostics(diagnostics, context)


# ─────────────────────────────────────────────────────────────────────────────
# 2б. Пауза при неактуальном справочнике (PAUSE_ON_REFERENCE_ERROR)
# ─────────────────────────────────────────────────────────────────────────────
# Ключ в context.data: разрешена ли пауза в этом запуске. Выставляется в
# cli/main.py сразу после initialize_context (False при --no-interactive),
# чтобы декоратор handle_pipeline_errors не знал о CLI-флаге напрямую.
# Сами ключи объявлены в config/settings.py (REFERENCE_PAUSE_ENABLED_KEY /
# REFERENCE_PAUSE_COUNT_KEY / REFERENCE_PAUSE_RESOLVED_KEY): их читает и
# io_module/report_cover — титульный лист, который не должен импортировать
# pipeline-модули.

# Сколько символов причины печатать в блоке паузы. Текст ошибки может быть
# списком из сотен позиций (счета, контрагенты), а блок паузы печатается
# в консоль целиком, минуя ограничение консольного формата loguru.
_PAUSE_REASON_MAX_CHARS = 300


def _stdin_is_tty() -> bool:
    """Признак «stdin — терминал». Без TTY ждать ввода нельзя (CI, редирект в файл)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def wait_for_reference_update(
    context: ProcessingContext,
    reason: Optional[str] = None,
    reference_name: Optional[str] = None,
) -> bool:
    """Пауза: ждём, пока пользователь обновит справочник.

    Возвращает True — продолжить (пользователь нажал Enter);
    False — прервать прогон как раньше ([STOP] и код выхода 1).

    Пауза делается только если она разрешена для запуска
    (context.data[REFERENCE_PAUSE_ENABLED_KEY], выставляется в cli/main.py)
    И stdin — терминал. В --no-interactive / без TTY паузы нет — поведение
    остаётся прежним (ошибка сразу уходит в [STOP]). Это тот же принцип,
    что у hold_splash: фоновым прогонам лишние паузы не нужны.

    Пользователь может несколько раз править справочник: после Enter шаг
    повторяется, и если ошибка осталась — пауза возникнет снова. «стоп» —
    прервать (штатная остановка [STOP], код 1, с подсказками про mismatches/);
    Ctrl+C прерывает на уровне системы (KeyboardInterrupt, код 130) — общий
    механизм всех пауз, в инструкции пользователю не показывается.

    Args:
        context: Контекст прогона.
        reason: Причина остановки шага (для блока паузы). Нужна, потому что
            ERROR «[STOP] Обработка остановлена» логируется только когда
            паузы нет: при успешном возобновлении он попал бы в сводку
            предупреждений титульного листа зелёного отчёта. Текст
            обрезается — блок паузы печатается в консоль целиком.
        reference_name: Имя справочника из ошибки (reference_name). Шаги
            корректировок правок бросают ReferenceMismatchError про
            Правки.xlsx, а не про Справочники.xlsx: инструкция «внесите правки
            в Справочники.xlsx» уводила бы в другой файл. Здесь выбирается
            тот файл, который на самом деле надо править.
    """
    if not context.data.get(REFERENCE_PAUSE_ENABLED_KEY, True):
        return False
    if not _stdin_is_tty():
        return False

    # Шаги ручных корректировок бросают ReferenceMismatchError с reference_name
    # из ManualCorrectionsConstants: править надо Правки.xlsx, а не
    # Справочники.xlsx. Сравнение идёт по константе того же источника, а не
    # по подстроке в имени файла — иначе переименование файла разъехало бы
    # инструкцию с источником ошибки.
    is_corrections = reference_name == ManualCorrectionsConstants.REFERENCE_NAME
    target_file = CORRECTIONS_FILE_NAME if is_corrections else REFERENCE_DATA_FILE.name
    title = (
        "неактуальный файл ручных корректировок"
        if is_corrections
        else "неактуальный справочник"
    )
    step_label = "правила корректировок исправлены" if is_corrections else "справочник обновлён"

    print()
    print("=" * 80)
    print(f"[>>] ПРОГОН ПРИОСТАНОВЛЕН: {title}.")
    if reason:
        print(f"     Причина: {reason[:_PAUSE_REASON_MAX_CHARS]}"
              f"{'...' if len(reason) > _PAUSE_REASON_MAX_CHARS else ''}")
    print("     Проблемные значения — в файлах mismatches/ папки запуска, детали — в логе выше.")
    print(f"     Файл: {to_relative(CORRECTIONS_FILE if is_corrections else REFERENCE_DATA_FILE)}")
    print()
    print(f"     1. Внесите правки в файл {target_file} (закройте его в Excel).")
    print("     2. Нажмите Enter — обработка продолжится С ТЕКУЩЕГО ШАГА;")
    print("        выгрузки перечитываться не будут.")
    print("     Чтобы остановить скрипт, введите: стоп")
    print("=" * 80)
    while True:
        try:
            answer = input(f"\n[PAUSE] {step_label.capitalize()}? Нажмите Enter для продолжения... ")
        except EOFError:
            print()
            return False
        cleaned = answer.strip().lower()
        if cleaned in ("стоп", "stop", "s", "q", "нет", "n"):
            return False
        if cleaned == "":
            print()
            print(f"[i] Проверяю обновлённый файл и продолжаю с этого же шага...")
            print("     Файлы mismatches/ от прежней попытки будут удалены, если")
            print("     после ваших правок шаг пройдёт — разбирать их будет нечего.")
            return True
        print('[i] Пустой Enter — продолжить, «стоп» — прервать прогон.')


def refresh_references(context: ProcessingContext) -> None:
    """Перечитывает Справочники.xlsx после паузы — без перечитывания выгрузок.

    Обновляет context.references, допуски сходимости (лист «Параметры»),
    повторно валидирует компанию и применяет индивидуальный меппинг
    (_apply_company_reference_scope), сбрасывает кэш ручных правок
    (Правки.xlsx — get_corrections читает его один раз на прогон).

    Данные выгрузок (common_osv_df, summary_osv_df, journal_df, balance_df,
    pnl_df) НЕ трогаются — это и есть выигрыш сценария: дорогая обработка
    выгрузок повторно не выполняется.

    Raises:
        ReferenceMismatchError: обновлённый справочник сам невалиден
            (например, период в КомпанииГруппы изменён — PeriodMismatchError).
            Вызывающий декоратор превращает это в честный [STOP].
    """
    logger.info("[RESUME] Перечитываем Справочники.xlsx (данные выгрузок не затрагиваются)")

    # Кэш ручных правок загружается один раз на прогон (get_corrections):
    # при паузе его надо сбросить, иначе шаги 12а/18а увидят старые правила.
    context.data.pop(ManualCorrectionsConstants.CONTEXT_KEY, None)

    context.references = _load_all_references()

    # Допуски сходимости — тоже лист справочника («Параметры»): если их
    # правили вместе с меппингом, повторный прогон должен видеть новые.
    context.tolerance_params = load_params(context)

    # Компания могла быть переименована/дополнена — повторяем валидацию и
    # обогащение (безопасно: присваивает те же поля, если ничего не менялось).
    _validate_and_enrich_company_info(context)

    # Индивидуальный меппинг компании (колонка «компания» в Меппинг_опу/Меппинг_бб).
    _apply_company_reference_scope(context)

    logger.info(
        "[RESUME] Справочники перечитаны: листов {}, допусков {}",
        len(context.references),
        len(context.tolerance_params),
    )


# Имена листов Excel-диагностики индивидуального меппинга. Первые два — обе
# половины одной подмены: что было до правки справочника и что стало после.
SCOPE_SHEET_APPLIED = 'Применённые строки'
SCOPE_SHEET_OVERRIDDEN = 'Перекрытые строки'
SCOPE_SHEET_INDIVIDUAL_ONLY = 'Индивидуальные без пары'
SCOPE_SHEET_UNKNOWN = 'Неизвестные компании'

# Служебные колонки диагностики индивидуального меппинга:
#   SCOPE_SHEET_COL  — из какого листа справочника строка (кадры Меппинг_опу и
#                      Меппинг_бб склеиваются через pd.concat, а колонки у них
#                      разные — без этой метки строки не различить);
#   SCOPE_OVERRIDER_COL — кем вытеснена строка. В самих перекрытых строках
#                      колонка «компания» всегда «все» и не отвечает на вопрос,
#                      кто именно перекрыл универсальную статью.
SCOPE_SHEET_COL = 'лист'
SCOPE_OVERRIDER_COL = 'перекрыто_компанией'


def _tag_scope_frame(frame: pd.DataFrame, diag: ScopeDiagnostics) -> pd.DataFrame:
    """Добавляет к кадру служебные колонки «лист» и «перекрыто_компанией»."""
    tagged = frame.copy()
    tagged[SCOPE_SHEET_COL] = diag.reference_name
    tagged[SCOPE_OVERRIDER_COL] = diag.company
    return tagged


def _save_reference_scope_diagnostics(diagnostics: list, context: ProcessingContext) -> None:
    """
    Пишет диагностику индивидуального меппинга в mismatches/ — если есть что
    показать:
      * «Применённые строки»    — индивидуальные строки, которые реально
        действуют для компании (т.е. чем именно заменены универсальные);
      * «Перекрытые строки»     — универсальные строки, вытесненные ими;
      * «Индивидуальные без пары» — индивидуальные строки без пары в
        универсальном блоке (возможна опечатка в ключевых колонках);
      * «Неизвестные компании»  — неизвестные значения колонки «компания»
        (опечатка в имени компании).

    Первые два листа — обе половины одной правки: по «Перекрытым» видно, что
    было до неё, по «Применённым» — что стало. Раньше в файле была только
    первая половина, и проверить подмену можно было лишь вручную открыв
    Справочники.xlsx.

    Конвейер ничего не останавливает: отчёт нужен для аудита правок справочника,
    поэтому ошибки сохранения логируются (как в Step._save_reference_mismatch_report).
    """
    applied = [
        _tag_scope_frame(diag.individual_frame, diag)
        for diag in diagnostics
        if diag.individual_frame is not None and not diag.individual_frame.empty
    ]
    overridden = [
        _tag_scope_frame(diag.overridden_frame, diag)
        for diag in diagnostics
        if diag.overridden_frame is not None and not diag.overridden_frame.empty
    ]
    individual_only = [
        _tag_scope_frame(diag.individual_only_frame, diag)
        for diag in diagnostics
        if diag.individual_only_frame is not None and not diag.individual_only_frame.empty
    ]
    unknown = [
        pd.DataFrame({
            SCOPE_SHEET_COL: diag.reference_name,
            'значение_в_колонке_компания': diag.unknown_scope_values,
        })
        for diag in diagnostics if diag.unknown_scope_values
    ]

    if not (applied or overridden or individual_only or unknown):
        return

    try:
        filename = f"reference_scope_{Step._slugify(context.company)}_{get_run_id()}.xlsx"
        output_path = get_output_dir('mismatches') / filename

        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            sheets = (
                (applied, SCOPE_SHEET_APPLIED),
                (overridden, SCOPE_SHEET_OVERRIDDEN),
                (individual_only, SCOPE_SHEET_INDIVIDUAL_ONLY),
                (unknown, SCOPE_SHEET_UNKNOWN),
            )
            for frames, sheet_name in sheets:
                if not frames:
                    continue
                pd.concat(frames, ignore_index=True).to_excel(
                    writer, sheet_name=sheet_name, index=False,
                )
                # Как на остальных листах книги: жирная шапка, закрепление,
                # ширины и автофильтр. В «Применённых строках» лежат тысячи
                # строк справочника — без фильтра искать свою статью нечем.
                DataSaver._apply_excel_formatting(writer.sheets[sheet_name])

        logger.info(
            "[FOLDER] Диагностика индивидуального меппинга сохранена в: {}",
            to_relative(output_path),
        )
    except PermissionError:
        logger.error(
            "[!] НЕ УДАЛОСЬ сохранить диагностику индивидуального меппинга: "
            "файл открыт в другой программе (Excel?) или нет прав на запись.",
        )
    except Exception as save_error:
        logger.warning(
            "[!] Не удалось сохранить диагностику индивидуального меппинга: {}",
            save_error,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Оркестратор — теперь читается как план действий
# ─────────────────────────────────────────────────────────────────────────────
def initialize_context() -> ProcessingContext:
    logger.debug("Инициализация контекста обработки")

    context = ProcessingContext()

    # Шаг 1. Загрузка общей ОСВ
    try:
        osv_df, osv_filename = DataLoader.load_general_osv()
    except TooManyFilesError as e:
        logger.error("[ERR] {}", e)
        if e.found_files:
            logger.error("[ERR] Найденные файлы: {}", ", ".join(e.found_files))
            logger.error(
                "[ERR] Оставьте только один файл общей ОСВ в папке {}",
                to_relative(e.expected_dir),
            )
        raise

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

    # Шаг 5. Индивидуальный меппинг компании: строки с её именем в колонке
    # «компания» перекрывают универсальные («все») — один раз на прогон, до
    # старта конвейера, чтобы все шаги читали один и тот же взгляд
    _apply_company_reference_scope(context)

    logger.debug(
        "Контекст инициализирован: компания={}, период={}, строк в ОСВ={}, справочников={}",
        context.company,
        context.period,
        len(osv_df),
        len(context.references)
    )
    return context


def _log_balance_rate(context: ProcessingContext, coverage, source_note: str = "") -> None:
    """
    Сообщает, каким курсом переводятся остатки, и сохраняет сводку для титула.

    Молчание о неактуальном справочнике здесь опаснее всего: остатки баланса —
    основа всего отчёта, а запрошенную дату получатель файла уже не видит
    (context.balance_date хранит фактическую дату курса). Поэтому просрочка
    уходит и в лог как [!] WARNING, и в context.data -> титульный лист.

    Args:
        context: Контекст обработки; в нём обновляется balance_date.
        coverage: RateCoverage из resolve_rate.
        source_note: Откуда взялась дата — «задано» флагом или введена.
    """
    currency = coverage.currency
    if coverage.gap_days == 0:
        logger.info(
            "Остатки будут переведены в рубли по курсу {} на дату {}{}.",
            currency, format_rate_date(coverage.rate_date),
            f" ({source_note})" if source_note else "",
        )
    elif coverage.covered:
        # Дата внутри диапазона листа, но на неё выпал выходной/праздник —
        # нормальная календарная поправка, а не просрочка справочника.
        logger.info(
            "Остатки будут переведены в рубли по курсу {} на ближайшую доступную "
            "в справочнике дату {} (запрошено {}){}.",
            currency, format_rate_date(coverage.rate_date),
            format_rate_date(coverage.requested_date),
            f" ({source_note})" if source_note else "",
        )
    else:
        warn_stale_rate(coverage, "Перевод остатков баланса")

    logger.info("Курс перевода остатков баланса: {}", coverage.rate)
    context.balance_date = format_rate_date(coverage.rate_date)
    record_balance_rate(context, coverage, date_source=source_note)


def _ask_continue_stale(context: ProcessingContext, coverage) -> bool:
    """
    Переспрашивает при вводе даты, которую справочник не покрывает.

    Единственное место, где пользователь может отменить выбор: молча взять
    курс месячной давности для целого баланса нельзя, но и останавливать
    прогон из-за просрочки листа курса тоже нельзя — вопрос снимает
    противоречие. Пустой ответ и «n» означают отказ (вернуться к вводу даты),
    чтобы по умолчанию continuation с устаревшим курсом не происходила.
    """
    print(
        f"\n[!] ВНИМАНИЕ: лист курса {coverage.currency} заканчивается датой "
        f"{format_rate_date(coverage.rate_date)}, а вы ввели "
        f"{format_rate_date(coverage.requested_date)} — отставание "
        f"{coverage.gap_days} дн."
    )
    print(
        f"    Применится курс {coverage.rate} от {format_rate_date(coverage.rate_date)}: "
        f"все остатки баланса будут переведены по нему."
    )
    print(
        "    Если это не то, что нужно, закройте программу, дополните лист "
        f"Курс_{coverage.currency} и запустите заново."
    )
    while True:
        try:
            answer = input("    Перевести остатки по этому курсу? (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in ("y", "д", "да"):
            return True
        if answer in ("", "n", "н", "нет"):
            return False
        print("[!] Ожидается y или n.")


def ask_balance_date_if_needed(context: ProcessingContext, interactive: bool = True) -> None:
    """
    Запрашивает у пользователя дату перевода валютных остатков в рубли
    для расшифровки баланса (вариант А — один раз в cli.main).

    Дата нужна только компаниям с валютой, отличной от RUB. Если дата
    уже задана заранее (CLI-флаг --balance-date), вопрос не задаётся.

    Актуальность справочника курса проверяется на каждом пути (см.
    _log_balance_rate): если лист заканчивается раньше запрошенной даты,
    это WARNING и — в интерактивном режиме — дополнительный вопрос
    с подтверждением (_ask_continue_stale).

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

    currency = get_currency(context)

    if context.balance_date:
        coverage = resolve_rate(context, context.balance_date)
        _log_balance_rate(context, coverage, source_note="задано")
        return

    def _fallback() -> None:
        """
        Неинтерактивный выбор: последняя дата листа курса.

        Сводка для титульного листа больше НЕ утверждает «актуален», когда
        актуальность проверить не на чем: дата перевода не задана и взята из
        самого справочника, поэтому промаха не будет по построению. Раньше
        сводка считалась отдельным вызовом resolve_rate на последний день
        листа и получала gap_days = 0, covered = True — то есть титул писал
        «актуален» даже при отчётности за месяц после конца справочника.

        Что можно сказать честно: дата перевода выбрана автоматически и равна
        последней дате листа. Фактическую просрочку покажет секция ОПУ титула
        (record_opu_rate) — она считается по датам проводок и не зависит от
        выбора даты перевода.
        """
        last_date = get_last_rate_date(context)
        coverage = resolve_rate(context, last_date)
        context.balance_date = format_rate_date(coverage.rate_date)
        logger.warning(
            "[!] Дата перевода остатков не задана. Используется последняя дата "
            "из справочника курса {}: {} (задать явно: --balance-date ДД.ММ.ГГГГ). "
            "Остатки переведены по курсу этой даты — за более позднюю дату "
            "справочник не содержит.",
            currency,
            coverage.rate_date,
        )
        record_balance_rate(context, coverage, date_source="автовыбор по листу курса")

    if not interactive:
        _fallback()
        return

    prompt = (
        f"\nОстатки по данной компании в валюте {currency}. "
        f"Введите дату, на которую нужно перевести остатки в рубли "
        f"для расшифровки баланса (ДД.ММ.ГГГ): "
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
            coverage = resolve_rate(context, raw)
        except ValueError as e:
            print(f"[!] {e}")
            continue

        if not coverage.covered and not _ask_continue_stale(context, coverage):
            continue

        _log_balance_rate(context, coverage, source_note="введено вручную")
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


def _warn_if_pnl_balance_mismatch(context: ProcessingContext) -> None:
    """
    Предупреждает, если отчёт выгружается без увязки ЧП = НРП.

    Диагностику пишет шаг 19 (_check_profit_vs_balance) при
    EXPORT_REPORT_ON_MISMATCH=True (мягкий режим): при несходимости
    конвейер продолжается и отчёт выгружается «как есть» для анализа
    расхождения. Здесь напоминаем об этом в момент сохранения, чтобы
    финальное «Приложение успешно завершено» не вводило в заблуждение.
    """
    diagnostics = context.data.get(OpuReportConstants.MISMATCH_DIAGNOSTICS_KEY)
    if not diagnostics:
        return

    diff = float(diagnostics.get("diff", 0.0))
    tolerance = float(diagnostics.get("tolerance", 0.0))
    logger.warning(
        "[!] Отчет выгружен БЕЗ увязки ЧП = НРП: разница {:,.0f} тыс.ед. "
        "(допустимый порог {:,.0f}). Файл предназначен только для анализа "
        "расхождения и не является отчетностью.",
        diff,
        tolerance,
    )


def save_results(context: ProcessingContext) -> None:
    """
    Сохранить результаты обработки в один комбинированный отчёт.

    Файл содержит два листа:
    - "Расшифровка_ББЛ" — финальный отчёт (balance breakdown)
    - "исходники" — обработанный main_df

    Имя файла берётся из справочника КомпанииГруппы (столбец название_файла_расшифровки).
    Если компания не найдена — используется стандартное имя.

    Если шаг 19 зафиксировал несходимость ЧП = НРП (EXPORT_REPORT_ON_MISMATCH=True),
    перед сохранением выводится предупреждение: отчёт выгружен не сведённым.
    """
    logger.info("Сохранение результатов")
    _warn_if_pnl_balance_mismatch(context)

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

        # Титульный лист собирается последним, перед сохранением: к этому
        # моменту известны и результат конвейера, и все диагностические файлы.
        # Сводка предупреждений — из коллектора logger_config: она наполняется
        # по ходу прогона, и в момент сохранения уже полна.
        cover_rows = safe_build_cover_rows(
            context,
            warnings=format_warnings_summary(),
        )

        output_path = DataSaver.save_combined_report(
            balance_df,
            summary_osv_df,
            pnl_df,
            journal_df,
            filename,
            cover_rows=cover_rows,
        )
        logger.info("Комбинированный отчёт сохранён: {}", to_relative(output_path))

    except Exception as e:
        logger.error("Ошибка при сохранении результатов: {}", e)
        raise

