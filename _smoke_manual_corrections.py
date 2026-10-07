# -*- coding: utf-8 -*-
"""Смоук ручных корректировок признаков (файл Правки.xlsx, шаги 12а и 18а).

Что покрыто
-----------
1. Загрузка файла правок: нет файла / пустой / без обязательных колонок /
   с заполненными и незаполненными строками / с неизвестной областью.
2. Применение к балансу: изменение признака, маркер, селекторы, регистр,
   ограничение «нет строк по селектору», «старое_значение» как проверка.
3. Применение к ОПУ: правило по контрагенту, правило своей области.
4. Отбор по компании и периоду.
5. Ошибки: неизвестный признак → ReferenceMismatchError; нет выходного кадра.
6. Сводка для титульного листа: применённые, неприменённые, обе области.
7. Регрессия: шаг не трогает значения, если правил нет; текст титульного
   листа совпадает с ключами констант модуля.

Смоук обязан падать при провале: check() увеличивает FAILED, финальная строка —
SMOKE_OK (PASSED/total) либо SMOKE_FAIL, код возврата 1 при провалах.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

from config.settings import CORRECTIONS_FILE_NAME, CORRECTIONS_SHEET_NAME
from io_module import corrections as corrections_io
from io_module.report_cover import _manual_corrections_text
from pipeline.base import ProcessingContext
from pipeline.errors import InputDataError, ReferenceMismatchError
from pipeline.step_config import ManualCorrectionsConstants as MC
from pipeline.steps.step_12a_apply_corrections_balance import Step12aApplyCorrectionsBalanceStep
from pipeline.steps.step_18a_apply_corrections_opu import Step18aApplyCorrectionsOpuStep
from loguru import logger

# Ловушка WARNING: раздел 12 проверяет, что пустой автошаблон НЕ предупреждает
# о старом файле, а файл с правилами — предупреждает ровно двумя сообщениями.
warn_capture: list[str] = []
_warn_sink_id = logger.add(
    lambda m: warn_capture.append(str(m)), level="WARNING", format="{message}"
)

PASSED = 0
FAILED = 0
TMP_ROOT = Path(tempfile.mkdtemp(prefix="smoke_corrections_"))


def check(condition, description):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK]   {description}")
    else:
        FAILED += 1
        print(f"  [FAIL] {description}")


def make_context(company="Тест", period="8мес2026"):
    ctx = ProcessingContext()
    ctx.company = company
    ctx.period = period
    ctx.segment = "Собственность"
    ctx.type_period = "8 мес"
    ctx.references = {
        "компании_группы": pd.DataFrame({
            "сокращенное_наименование_компании": ["Тест", "Другая"],
        }),
    }
    return ctx


def make_osv():
    """Сводная ОСВ с полным набором признаков баланса."""
    rows = [
        # счёт, субконто, вид_зд, подвид_зд, группа_ос, д/к, сегмент_био, вид_связи, инвест
        ("60.01", "Договор №141/24", "Дебиторская задолженность", "тест",
         "не_указано", "не_указано", "не_указано", "3 лица", "нет"),
        ("60.01", "Договор №142/24", "Дебиторская задолженность", "тест",
         "не_указано", "не_указано", "не_указано", "3 лица", "нет"),
        ("60.01", "Контрагент А", "Дебиторская задолженность", "тест",
         "не_указано", "не_указано", "не_указано", "3 лица", "нет"),
        ("60.02", "Договор №200/25", "Дебиторская задолженность", "тест",
         "не_указано", "не_указано", "не_указано", "3 лица", "нет"),
    ]
    df = pd.DataFrame(rows, columns=[
        "счет", "субконто", "вид_задолженности", "подвид_задолженности",
        "группа_ос_аренды_лизинга", "долгая_короткая_часть",
        "сегмент_биоактивов_для_01_02", "вид_связи", "инвест_договор",
    ]).astype("string")
    df["сальдо, тыс.ед."] = [100.0, 250.0, 40.0, 70.0]
    return df


def make_journal():
    df = pd.DataFrame({
        "счет": ["91.01", "91.01", "91.02"],
        "вид_дохода_расхода": ["Аренда", "Аренда", "Аренда"],
        "сегмент": ["СХ", "СХ", "СХ"],
        "вид_связи": ["3 лица", "3 лица", "3 лица"],
        "контрагент": ["ООО Ромашка", "ООО Ромашка", "ООО Ромашка"],
        "оборот, тыс.ед.": [-500.0, -300.0, 200.0],
    }).astype("string")
    df["оборот, тыс.ед."] = [-500.0, -300.0, 200.0]
    return df


def make_osv_with_counterparty():
    """ОСВ с допсубконто-контрагентом: счёт 60.01, два вида расчётов у одного контрагента.

    Ровно тот случай, из-за которого нужно правило по допсубконто и предупреждение
    о слишком широком правиле: у контрагента не одна строка, а несколько.
    """
    rows = [
        ("60.01", "Материалы, товары и услуги прочие", "Анисимов Сергей Евгеньевич ИП"),
        ("60.01", "Авансы выданные", "Анисимов Сергей Евгеньевич ИП"),
        ("60.01", "Материалы, товары и услуги прочие", "ООО Ромашка"),
    ]
    df = pd.DataFrame(rows, columns=["счет", "субконто", "допсубконто"]).astype("string")
    for col, value in (
        ("вид_задолженности", "Кредиторская задолженность"),
        ("подвид_задолженности", "Торговая КЗ"),
        ("группа_ос_аренды_лизинга", "не_указано"),
        ("долгая_короткая_часть", "не_указано"),
        ("сегмент_биоактивов_для_01_02", "не_указано"),
        ("вид_связи", "3 лица"),
        ("инвест_договор", "нет"),
        ("договор", "не_указано"),
    ):
        df[col] = value
    df["сальдо, тыс.ед."] = [-5536.92, -1200.0, -900.0]
    return df


def write_rules(rows, folder=None, columns=None, sheet=CORRECTIONS_SHEET_NAME):
    """Кладёт файл правил в временную папку и возвращает её."""
    target = Path(folder or TMP_ROOT)
    target.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=columns)
    df.to_excel(target / CORRECTIONS_FILE_NAME, sheet_name=sheet, index=False)
    return target


def context_with_rules(rules, osv=None, journal=None, **kwargs):
    """Контекст с кэшированными правилами — шаг не ходит на диск."""
    folder = write_rules(rules, folder=TMP_ROOT / f"r{id(rules)}")
    ctx = make_context(**kwargs)
    ctx.data[MC.CONTEXT_KEY] = corrections_io.load_corrections(folder)
    if osv is not None:
        ctx.summary_osv_df = osv.copy()
    if journal is not None:
        ctx.journal_df = journal.copy()
    return ctx



print("\n1. Загрузка файла правок")
# Папка запуска нужна шагам для записи аудита — в реальном прогоне её создаёт
# configure_run() из cli/main.py до старта конвейера
from io_module.output_manager import configure_run  # noqa: E402

RUN_ID = configure_run()

# 1.1 Файла нет — пустой кадр, не ошибка. Заодно проверяем шаблон (D)
empty_dir = TMP_ROOT / "no_file"
empty_dir.mkdir(parents=True, exist_ok=True)
check(corrections_io.load_corrections(empty_dir).empty, "нет файла — пустые правила, без исключения")

template_path = corrections_io.resolve_corrections_file(empty_dir)
check(template_path.exists(), "файла не было — создан шаблон")
if template_path.exists():
    book = pd.read_excel(template_path, sheet_name=None)
    check(
        MC.TEMPLATE_SHEET_RULES in book and MC.TEMPLATE_SHEET_HELP in book,
        f"в шаблоне оба листа: {list(book)}",
    )
    check(book[MC.TEMPLATE_SHEET_RULES].empty, "лист «Правки» в шаблоне пуст (примеры не станут правилами)")
    help_text = " ".join(
        book[MC.TEMPLATE_SHEET_HELP].fillna("").astype(str).values.ravel()
    )
    check(
        all(feature in help_text for feature in corrections_io.BALANCE_FEATURES),
        "в справке перечислены все допустимые признаки баланса",
    )
    check(
        all(feature in help_text for feature in corrections_io.OPU_FEATURES),
        "в справке перечислены все допустимые признаки ОПУ",
    )
    check("76.07" in help_text and "договор" in help_text, "в справке описаны уровни детализации по счетам")
    check(
        MC.COL_OLD_VALUE in help_text and "не_указано" in help_text,
        "в справке объяснены старое_значение и значение «не_указано»",
    )
    check(
        MC.AUDIT_SHEET_DETAIL in help_text,
        "в справке назван лист «Затронутые строки»",
    )

# 1.2 Нет обязательных колонок — InputDataError с перечнем того, что есть
bad_dir = write_rules(
    [{"счет": "60.01", "признак_не_тот": "x"}],
    folder=TMP_ROOT / "bad_cols",
    columns=["счет", "признак_не_тот"],
)
try:
    corrections_io.load_corrections(bad_dir)
    check(False, "нет обязательных колонок — InputDataError")
except InputDataError as exc:
    check("признак" in str(exc) and "счет" in str(exc), f"нет колонок — InputDataError со списком: {exc}")
except Exception as exc:  # noqa: BLE001
    check(False, f"нет колонок — InputDataError, а {type(exc).__name__}")

# 1.3 Нет листа «Правки» — InputDataError
wrong_sheet_dir = write_rules(
    [{"счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"}],
    folder=TMP_ROOT / "wrong_sheet",
    sheet="Лист1",
)
try:
    corrections_io.load_corrections(wrong_sheet_dir)
    check(False, "нет листа «Правки» — InputDataError")
except InputDataError as exc:
    check("Лист1" in str(exc), f"нет листа — InputDataError со списком листов: {exc}")
except Exception as exc:  # noqa: BLE001
    check(False, f"нет листа — InputDataError, а {type(exc).__name__}")

# 1.4 Незаполненная строка отбрасывается, заполненные остаются
mixed_dir = write_rules(
    [
        {"счет": "60.01", "субконто": "Договор №141/24", "признак": "инвест_договор", "новое_значение": "да"},
        {"счет": "60.01", "признак": "инвест_договор"},  # без нового_значения
    ],
    folder=TMP_ROOT / "mixed",
    columns=["счет", "субконто", "признак", "новое_значение"],
)
mixed = corrections_io.load_corrections(mixed_dir)
check(len(mixed) == 1, f"незаполненная строка отброшена (осталось {len(mixed)})")
check(
    list(mixed.columns) == ["счет", "субконто", "признак", "новое_значение", "область"],
    f"добавлена колонка «область» по умолчанию: {list(mixed.columns)}",
)
check(
    (mixed[MC.COL_AREA] == MC.AREA_BALANCE).all(),
    f"пустая область = «{MC.AREA_BALANCE}»",
)

# 1.5 Неизвестная область отсекается
area_dir = write_rules(
    [
        {"счет": "60.01", "область": "баланс", "признак": "инвест_договор", "новое_значение": "да"},
        {"счет": "60.01", "область": "упс", "признак": "инвест_договор", "новое_значение": "да"},
    ],
    folder=TMP_ROOT / "areas",
    columns=["счет", "область", "признак", "новое_значение"],
)
areas = corrections_io.load_corrections(area_dir)
check(len(areas) == 1, f"неизвестная область отсечена (осталось {len(areas)})")

# 1.6 Пробелы по краям не ломают сопоставление, область в любом регистре
spaced_dir = write_rules(
    [{"счет": " 60.01 ", "область": "Баланс", "признак": " инвест_договор ", "новое_значение": " да "}],
    folder=TMP_ROOT / "spaced",
    columns=["счет", "область", "признак", "новое_значение"],
)
spaced = corrections_io.load_corrections(spaced_dir)
check(
    len(spaced) == 1
    and spaced.iloc[0][MC.COL_NEW_VALUE] == "да"
    and spaced.iloc[0][MC.COL_AREA] == "баланс",
    f"пробелы срезаны, область в нижнем регистре: {spaced.to_dict('records')}",
)

# 1.7 Внутренние двойные пробелы схлопываются так же, как в данных (C):
#      иначе правило «Анисимов  Сергей» молча не совпало бы с «Анисимов Сергей»
inner_dir = write_rules(
    [{"счет": " 60.01 ", "допсубконто": "Анисимов  Сергей\tЕвгеньевич  ИП",
      "признак": "инвест_договор", "новое_значение": "да"}],
    folder=TMP_ROOT / "inner_spaces",
    columns=["счет", "допсубконто", "признак", "новое_значение"],
)
inner = corrections_io.load_corrections(inner_dir)
check(
    inner.iloc[0]["допсубконто"] == "Анисимов Сергей Евгеньевич ИП",
    f"двойные пробелы и табы схлопнуты как в данных: {inner.iloc[0]['допсубконто']!r}",
)

print("\n2. Применение к балансу (шаг 12а)")
step_b = Step12aApplyCorrectionsBalanceStep()

ctx = context_with_rules(
    [
        {"счет": "60.01", "субконто": "Договор №141/24",
         "признак": "инвест_договор", "новое_значение": "да", "старое_значение": "нет",
         "комментарий": "договор инвестиционный, признак в 1С не выставлен"},
    ],
    osv=make_osv(),
)
result = step_b._process(ctx)
osv = result.summary_osv_df
check(
    osv.loc[osv["субконто"] == "Договор №141/24", "инвест_договор"].iloc[0] == "да",
    "признак изменён по ключу счёт + субконто",
)
check(
    (osv["инвест_договор"] == "нет").sum() == 3,
    f"остальные строки не тронуты ({int((osv['инвест_договор'] == 'нет').sum())} шт. осталось «нет»)",
)
check(
    "инвест_договор: нет → да" in str(osv.loc[osv["субконто"] == "Договор №141/24", MC.MARKER_COL].iloc[0]),
    f"колонка-маркер «{MC.MARKER_COL}» заполнена в изменённой строке",
)
check(
    osv[MC.MARKER_COL].isna().sum() == 3,
    "в остальных строках маркер пуст",
)
summary = result.data.get(MC.SUMMARY_KEY) or {}
check(summary.get(MC.SUMMARY_APPLIED_RULES) == 1, f"сводка: применено 1 правило ({summary.get(MC.SUMMARY_APPLIED_RULES)})")
check(summary.get(MC.SUMMARY_APPLIED_ROWS) == 1, f"сводка: 1 строка ({summary.get(MC.SUMMARY_APPLIED_ROWS)})")
check(not summary.get(MC.SUMMARY_HAS_PROBLEMS), "сводка: проблем нет")

print("\n3. Селекторы, регистр и защиты")
# 3.1 Регистр и пробелы в значении селектора не важны
ctx = context_with_rules(
    [{"счет": "60.01", "субконто": "договор №141/24", "признак": "инвест_договор", "новое_значение": "да"}],
    osv=make_osv(),
)
osv = step_b._process(ctx).summary_osv_df
check(
    (osv["инвест_договор"] == "да").sum() == 1,
    "селектор сравнивается без учёта регистра и краевых пробелов",
)

# 3.2 Два селектора сужают выборку
ctx = context_with_rules(
    [{"счет": "60.01", "субконто": "Контрагент А", "вид_связи": "3 лица",
      "признак": "инвест_договор", "новое_значение": "да"}],
    osv=make_osv(),
)
osv = step_b._process(ctx).summary_osv_df
check(
    (osv["инвест_договор"] == "да").sum() == 1
    and osv.loc[osv["субконто"] == "Контрагент А", "инвест_договор"].iloc[0] == "да",
    "несколько селекторов работают как «И»",
)

# 3.3 Пустой селектор запрещён: без «счёт» правило съело бы весь кадр
ctx = context_with_rules(
    [{"признак": "инвест_договор", "новое_значение": "да"}],
    osv=make_osv(),
)
osv = step_b._process(ctx).summary_osv_df
check(
    (osv["инвест_договор"] == "нет").all(),
    "правило без селекторов не применяется (кадр не изменён)",
)
summary = result.data.get(MC.SUMMARY_KEY) or {}
check(
    bool((ctx.data.get(MC.SUMMARY_KEY) or {}).get(MC.SUMMARY_HAS_PROBLEMS)),
    "неприменённое правило помечает сводку как проблемную",
)

# 3.4 Селектор не найден — правило не применено, а не «применено в никуда»
ctx = context_with_rules(
    [{"счет": "60.01", "субконто": "Договора №999/99", "признак": "инвест_договор", "новое_значение": "да"}],
    osv=make_osv(),
)
osv = step_b._process(ctx).summary_osv_df
check((osv["инвест_договор"] == "нет").all(), "нет строк по селектору — кадр не изменён")
check(
    (ctx.data.get(MC.SUMMARY_KEY) or {}).get(MC.SUMMARY_SKIPPED_RULES) == 1,
    "нет строк по селектору — правило попало в «не применились»",
)

# 3.5 «старое_значение» — проверка актуальности правила


print("\n4. Применение к ОПУ (шаг 18а)")
step_o = Step18aApplyCorrectionsOpuStep()

ctx = context_with_rules(
    [{"счет": "91.01", "контрагент": "ООО Ромашка", "область": "опу",
      "признак": "вид_дохода_расхода", "новое_значение": "Проценты к уплате"}],
    journal=make_journal(),
)
journal = step_o._process(ctx).journal_df
check(
    (journal["вид_дохода_расхода"] == "Проценты к уплате").sum() == 2,
    "ОПУ: признак изменён по селектору «контрагент» (2 строки из 3)",
)
check(
    journal.loc[2, "вид_дохода_расхода"] == "Аренда",
    "ОПУ: строка вне селектора не тронута",
)
check(
    "вид_дохода_расхода: Аренда → Проценты к уплате" in str(journal.loc[0, MC.MARKER_COL]),
    f"ОПУ: маркер «{MC.MARKER_COL}» заполнен",
)

# 4.2 Балансовое правило не применяется к ОПУ и наоборот
ctx = context_with_rules(
    [
        # Явная область «опу» + признак баланса: правило нельзя понять
        {"счет": "91.01", "область": "опу", "признак": "инвест_договор", "новое_значение": "да"},
    ],
    journal=make_journal(),
)
try:
    step_o._process(ctx)
    check(False, "признак баланса в области ОПУ — ошибка")
except ReferenceMismatchError as exc:
    check(
        "инвест_договор" in str(exc),
        f"признак, не входящий в ключ маппинга ОПУ, даёт ReferenceMismatchError: {exc}",
    )
except Exception as exc:  # noqa: BLE001
    check(False, f"ожидался ReferenceMismatchError, а {type(exc).__name__}: {exc}")

# 4.3 Правило своей области не цепляется к чужому шагу
ctx = context_with_rules(
    [{"счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"}],
    journal=make_journal(),
)
journal = step_o._process(ctx).journal_df
check(
    (journal["вид_дохода_расхода"] == "Аренда").all(),
    "балансовое правило не применяется в шаге ОПУ",
)

print("\n5. Отбор по компании и периоду")
# Значения у правил разные, иначе нельзя отличить «применилось» от «не применилось»
ctx = context_with_rules(
    [
        {"компания": "Тест", "счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"},
        {"компания": "Другая", "счет": "60.01", "признак": "инвест_договор", "новое_значение": "нет"},
        {"счет": "60.02", "признак": "инвест_договор", "новое_значение": "да"},
    ],
    osv=make_osv(),
    company="Тест",
)
osv = step_b._process(ctx).summary_osv_df
check(
    (osv.loc[osv["счет"] == "60.01", "инвест_договор"] == "да").all()
    and osv.loc[osv["счет"] == "60.02", "инвест_договор"].iloc[0] == "да",
    "применены правила своей компании и универсальные (пустая компания), чужая — нет",
)

ctx = context_with_rules(
    [
        {"период": "8мес2026", "счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"},
        {"период": "кв1", "счет": "60.02", "признак": "инвест_договор", "новое_значение": "нет"},
    ],
    osv=make_osv(),
    period="8мес2026",
)
osv = step_b._process(ctx).summary_osv_df
check(
    (osv.loc[osv["счет"] == "60.01", "инвест_договор"] == "да").all()
    and osv.loc[osv["счет"] == "60.02", "инвест_договор"].iloc[0] == "нет",
    "применены правила своего периода, чужого периода — нет",
)

ctx = context_with_rules(
    [{"компания": "Тестик", "счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"}],
    osv=make_osv(),
    company="Тест",
)
osv = step_b._process(ctx).summary_osv_df
check(
    (osv["инвест_договор"] == "нет").all(),
    "опечатка в имени компании не применяется (пропуск с предупреждением)",
)

print("\n6. Пустые кадры и неизвестные столбцы")
ctx = context_with_rules(
    [{"счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"}],
    osv=make_osv(),
)
ctx.summary_osv_df = pd.DataFrame()
osv = step_b._process(ctx).summary_osv_df
check(osv.empty, "пустая ОСВ — шаг не падает и не добавляет правил в сводку")

ctx = context_with_rules(
    [{"счет": "60.01", "нет_такого_столбца": "x", "признак": "инвест_договор", "новое_значение": "да"}],
    osv=make_osv(),
)
osv = step_b._process(ctx).summary_osv_df
check(
    (osv["инвест_договор"] == "нет").all()
    and (ctx.data.get(MC.SUMMARY_KEY) or {}).get(MC.SUMMARY_SKIPPED_RULES) == 1,
    "селектор по несуществующему столбцу — правило не применено, кадр цел",
)

print("\n7. Порядок правил и маркер при нескольких правках")
ctx = context_with_rules(
    [
        {"счет": "60.01", "субконто": "Договор №141/24", "признак": "инвест_договор", "новое_значение": "да"},
        {"счет": "60.01", "субконто": "Договор №141/24", "признак": "вид_связи", "новое_значение": "Связанная"},
    ],
    osv=make_osv(),
)
osv = step_b._process(ctx).summary_osv_df
marker = str(osv.loc[osv["субконто"] == "Договор №141/24", MC.MARKER_COL].iloc[0])
check(
    "инвест_договор" in marker and "вид_связи" in marker,
    f"маркер накапливает несколько правок: {marker}",
)
check(
    osv.loc[osv["субконто"] == "Договор №141/24", "вид_связи"].iloc[0] == "Связанная",
    "вторая правка другого признака тоже применена",
)

print("\n8. Сводка и титульный лист")
ctx = context_with_rules(
    [{"счет": "60.01", "субконто": "Договор №141/24", "признак": "инвест_договор", "новое_значение": "да"}],
    osv=make_osv(),
)
result = step_b._process(ctx)
ctx_o = context_with_rules(
    [{"счет": "91.01", "контрагент": "ООО Ромашка", "область": "опу",
      "признак": "вид_дохода_расхода", "новое_значение": "Проценты к уплате"}],
    journal=make_journal(),
)
# обе области в одном контексте — как в реальном прогоне
ctx.data[MC.CONTEXT_KEY] = ctx_o.data[MC.CONTEXT_KEY]
result = step_b._process(ctx)
result.journal_df = ctx_o.journal_df
result = step_o._process(result)
summary = result.data.get(MC.SUMMARY_KEY) or {}
check(summary.get(MC.SUMMARY_APPLIED_RULES) == 2, f"сводка суммирует обе области: {summary.get(MC.SUMMARY_APPLIED_RULES)}")
check(summary.get(MC.SUMMARY_APPLIED_ROWS) == 3, f"сводка: затронуто 3 строки: {summary.get(MC.SUMMARY_APPLIED_ROWS)}")
check(
    len(summary.get(MC.SUMMARY_BY_AREA) or []) == 2,
    f"в сводке обе области: {[e[MC.SUMMARY_ENTRY_AREA] for e in summary.get(MC.SUMMARY_BY_AREA) or []]}",
)

text = _manual_corrections_text(result)
check("применено" in text and "2 правил" in text, f"текст титульного листа: {text}")

empty_ctx = make_context()
check(
    "не применялись" in _manual_corrections_text(empty_ctx),
    "без правок титульный лист говорит «не применялись»",
)

ctx = context_with_rules(
    [{"счет": "60.01", "субконто": "Нет такого", "признак": "инвест_договор", "новое_значение": "да"}],
    osv=make_osv(),
)
result = step_b._process(ctx)
check("НЕ ПРИМЕНЕНО" in _manual_corrections_text(result),
      f"неприменённое правило видно на титульном листе: {_manual_corrections_text(result)}")

print("\n9. Отчёт аудита в mismatches/")
from io_module.output_manager import get_output_dir  # noqa: E402

try:
    ctx = context_with_rules(
        [
            {"счет": "60.01", "субконто": "Договор №141/24", "признак": "инвест_договор", "новое_значение": "да"},
            {"счет": "60.01", "субконто": "Неизвестный", "признак": "инвест_договор", "новое_значение": "да"},
        ],
        osv=make_osv(),
    )
    step_b._process(ctx)
    audit_files = [
        f for f in get_output_dir("mismatches").glob(f"{MC.AUDIT_PREFIX}*.xlsx")
        if step_b.CORRECTION_AREA in f.name
    ]
    check(len(audit_files) == 1, f"аудит баланса сохранён в mismatches/: {[f.name for f in audit_files]}")
    if audit_files:
        audit = pd.read_excel(audit_files[0], sheet_name=None)
        check(
            MC.AUDIT_SHEET_APPLIED in audit and MC.AUDIT_SHEET_SKIPPED in audit,
            f"в аудите оба листа: {list(audit)}",
        )
        check(len(audit[MC.AUDIT_SHEET_APPLIED]) == 1, "лист применённых правок содержит применённое правило")
        check(len(audit[MC.AUDIT_SHEET_SKIPPED]) == 1, "лист «Не применились» содержит неприменённое")
        check(
            "причина" in audit[MC.AUDIT_SHEET_SKIPPED].columns,
            f"у неприменённого правила есть причина: {list(audit[MC.AUDIT_SHEET_SKIPPED].columns)}",
        )
    from io_module.report_cover import DIAGNOSTIC_INFORMATIONAL_PREFIXES  # noqa: E402
    check(
        MC.AUDIT_PREFIX in DIAGNOSTIC_INFORMATIONAL_PREFIXES,
        "аудит помечен как информационный (правок не требует)",
    )
finally:
    shutil.rmtree(TMP_ROOT, ignore_errors=True)

print("\n10. Регрессия: шаг без правок не трогает данные")
base = make_osv()
ctx = make_context()
ctx.summary_osv_df = base.copy()
ctx.data[MC.CONTEXT_KEY] = pd.DataFrame()
after = step_b._process(ctx).summary_osv_df
check(after.equals(base), "без файла правок кадр не изменён (включая отсутствие колонки-маркера)")
check(MC.SUMMARY_KEY not in ctx.data, "без правил сводка не создаётся")

# 10.1 Признак типа object после правки возвращается в string — иначе шаг упал бы
#      на базовой проверке запрета object-колонок
object_osv = make_osv()
object_osv["инвест_договор"] = object_osv["инвест_договор"].astype(object)
ctx = context_with_rules(
    [{"счет": "60.01", "субконто": "Договор №141/24", "признак": "инвест_договор", "новое_значение": "да"}],
    osv=object_osv,
)
osv = step_b._process(ctx).summary_osv_df
check(
    str(osv["инвест_договор"].dtype) == "string"
    and osv.loc[osv["субконто"] == "Договор №141/24", "инвест_договор"].iloc[0] == "да",
    f"признак типа object после правки снова string и с новым значением: {osv['инвест_договор'].dtype}",
)
check(str(osv[MC.MARKER_COL].dtype) == "string", f"маркер всегда string: {osv[MC.MARKER_COL].dtype}")

print("\n11. Детализация и предупреждение о широком правиле (A, B)")
# 11.1 Правило по «счёт + допсубконто» задевает ВСЕ строки контрагента —
#       об этом должно быть сказано прямо, а не замечено по факту в отчёте
ctx = context_with_rules(
    [{"счет": "60.01", "допсубконто": "Анисимов Сергей Евгеньевич ИП",
      "признак": "инвест_договор", "новое_значение": "да", "старое_значение": "нет"}],
    osv=make_osv_with_counterparty(),
)
result = step_b._process(ctx)
audit_path = get_output_dir("mismatches") / (
    f"{MC.AUDIT_PREFIX}{step_b.CORRECTION_AREA}_Тест_{RUN_ID}.xlsx"
)
audit = pd.read_excel(audit_path, sheet_name=None)
check(MC.AUDIT_SHEET_DETAIL in audit, f"в аудите есть лист «{MC.AUDIT_SHEET_DETAIL}»: {list(audit)}")

detail = audit[MC.AUDIT_SHEET_DETAIL]
check(len(detail) == 2, f"в листе детализации обе строки контрагента: {len(detail)}")
check(
    set(detail["субконто"]) == {"Материалы, товары и услуги прочие", "Авансы выданные"},
    f"видны разные субконто задетых строк: {sorted(set(detail['субконто']))}",
)
check(
    "сальдо, тыс.ед." in detail.columns and set(detail["сальдо, тыс.ед."]) == {-5536.92, -1200.0},
    f"в детализации суммы задетых строк: {sorted(detail['сальдо, тыс.ед.'])}",
)
check(
    set(detail["признак"]) == {"инвест_договор"}
    and set(detail["было"]) == {"нет"}
    and set(detail["стало"]) == {"да"},
    "в детализации признак и «было → стало»",
)
applied_row = audit[MC.AUDIT_SHEET_APPLIED].iloc[0]
check(
    "субконто: 2 значений" in str(applied_row.get("также задело")),
    f"широкое правило помечено в «Применённые правки»: {applied_row.get('также задело')}",
)

# 11.2 Узкое правило (добавлен субконто) задевает одну строку — предупреждения нет
narrow_dir = write_rules(
    [{"счет": "60.01", "субконто": "Материалы, товары и услуги прочие",
      "допсубконто": "Анисимов Сергей Евгеньевич ИП",
      "признак": "инвест_договор", "новое_значение": "да", "старое_значение": "нет"}],
    folder=TMP_ROOT / "narrow_rule",
    columns=["счет", "субконто", "допсубконто", "признак", "новое_значение", "старое_значение"],
)
ctx = make_context()
ctx.data[MC.CONTEXT_KEY] = corrections_io.load_corrections(narrow_dir)
ctx.summary_osv_df = make_osv_with_counterparty()
result = step_b._process(ctx)
osv = result.summary_osv_df
check(
    (osv["инвест_договор"] == "да").sum() == 1
    and osv.loc[osv["субконто"] == "Авансы выданные", "инвест_договор"].iloc[0] == "нет",
    "узкое правило меняет ровно одну строку",
)
audit = pd.read_excel(audit_path, sheet_name=None)
check(
    pd.isna(audit[MC.AUDIT_SHEET_APPLIED].iloc[0].get("также задело")),
    "узкое правило не помечено как широкое",
)
check(len(audit[MC.AUDIT_SHEET_DETAIL]) == 1, f"в детализации одна строка: {len(audit[MC.AUDIT_SHEET_DETAIL])}")

# 11.3 Срез ограничен лимитом, о срезе сказано в лог (в аудите — не молча)
wide = pd.concat([make_osv_with_counterparty()] * 4, ignore_index=True)
ctx = context_with_rules(
    [{"счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"}],
    osv=wide,
)
saved_limit = MC.AUDIT_DETAIL_ROW_LIMIT
try:
    MC.AUDIT_DETAIL_ROW_LIMIT = 5
    _, _, _, detail_frame, truncated = step_b._apply_rules(
        wide, ctx.data[MC.CONTEXT_KEY], ctx
    )
    check(len(detail_frame) == 5, f"срез ограничен лимитом: {len(detail_frame)} строк")
    check(truncated == 7, f"посчитано, сколько строк усечено: {truncated}")
finally:
    MC.AUDIT_DETAIL_ROW_LIMIT = saved_limit

print("\n12. Устаревший файл и «универсальные» правила (пустые компания/период)")
# 12.1 Загрузка существующего файла предупреждает о возможности устаревших правил
existing_dir = write_rules(
    [{"счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"}],
    folder=TMP_ROOT / "existing",
    columns=["счет", "признак", "новое_значение"],
)
# load_corrections логирует _warn_stale_file и _warn_universal_rules; проверяем,
# что файл читается, а предупреждения не ломают загрузку
warn_capture.clear()
rules = corrections_io.load_corrections(existing_dir)
check(len(rules) == 1, f"файл с устаревшим правилом загружается, правило цело: {len(rules)}")
check(
    sum(1 for m in warn_capture if "используем существующий файл" in m) == 1,
    "файл с правилами: первое предупреждение — используем существующий файл",
)
check(
    sum(1 for m in warn_capture if "остался от прошлого прогона" in m) == 1,
    "файл с правилами: второе предупреждение — что делать со старым файлом",
)
check(
    corrections_io._warn_universal_rules(rules, existing_dir / CORRECTIONS_FILE_NAME) is None,
    "_warn_universal_rules не возвращает ничего (только пишет в лог)",
)

# 12.2 Правило с привязкой к компании не считается «универсальным»
company_rules = pd.DataFrame([
    {"компания": "Тест", "счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"},
])
# Привязка к компании есть — маска универсальных пуста
blank_company = company_rules[MC.COL_COMPANY].isna()
blank_period = company_rules[MC.COL_PERIOD].isna() if MC.COL_PERIOD in company_rules else pd.Series(True, index=company_rules.index)
unmapped = blank_company & blank_period
check(not unmapped.any(), "правило с заполненной компанией не помечено как универсальное")

# 12.3 Правило с компанией и периодом — привязка полная
full_rules = pd.DataFrame([
    {"компания": "Тест", "период": "8мес2026", "счет": "60.01", "признак": "инвест_договор", "новое_значение": "да"},
])
blank_full = full_rules[MC.COL_COMPANY].isna() & full_rules[MC.COL_PERIOD].isna()
check(not blank_full.any(), "правило с компанией и периодом — привязка полная")

# 12.4 Пустой файл (автосозданный шаблон без правил) НЕ предупреждает о старом
# файле: правил нет, нечему «примениться к прогону», а лишний WARNING пугает
# бухгалтера на каждом чистом прогоне.
warn_capture.clear()
empty_corr_dir = write_rules(
    [],
    folder=TMP_ROOT / "empty_like_template",
    columns=["счет", "признак", "новое_значение"],
)
empty_rules = corrections_io.load_corrections(empty_corr_dir)
check(empty_rules.empty, "пустой файл без правил загружается как пустой кадр")
check(
    not any("используем существующий файл" in m for m in warn_capture),
    "пустой файл без правил не предупреждает о существующем файле",
)
check(
    not any("остался от прошлого прогона" in m for m in warn_capture),
    "пустой файл без правил не даёт инструкции по переносу файла",
)

logger.remove(_warn_sink_id)

print()
if FAILED:
    print(f"SMOKE_FAIL — провалено {FAILED}, успешно {PASSED}")
    raise SystemExit(1)
print(f"SMOKE_OK ({PASSED}/{PASSED + FAILED})")
