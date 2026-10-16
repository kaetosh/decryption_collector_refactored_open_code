# -*- coding: utf-8 -*-
"""
Смоук-тест: реконциляция шага 1c + чтение формул в выгрузках ОСВ.

Регресс МЭЗ 15.10.2026 (счёт 14):
- В выгрузке `МЭЗ_осв_14_2025_.xlsx` ячейка конечного сальдо была формулой `=N9-N10`.
  Программа читала xlsx через openpyxl без `data_only`, получала строку-формулу,
  `pd.to_numeric(errors='coerce')` молча роняла её в NaN, и реконциляция видела
  −945,52 тыс. вместо −250 655,12 тыс. — расхождение −249 709,60 тыс. при пороге 1 050.
- Кроме того, check-таблица обработчика («Итого» против суммы субконто) отбрасывалась
  при загрузке, и косячная расшифровка всплывала только косвенно — с подсказкой про
  «перезакрытие периода», хотя период закрывался корректно.

Что проверяет (полностью автономно, синтетические данные + при наличии реального
архивного файла МЭЗ — чтение кэша формулы):

  1. `_preprocessor_openpyxl` с `data_only=True` читает кэшированное значение
     формулы из настоящего файла МЭЗ 14 (если есть в архиве запусков), а не
     строку-формулу / NaN.
  2. `load_account_osv()` теперь возвращает (df, check_df); check_df настоящего
     файла МЭЗ 14 не пуст и фиксирует внутреннюю несходимость файла.
  3. `_warn_internal_mismatches`: ловит превышение допуска (1 рубль), игнорирует
     нулевую/малую разницу, пустой/незнакомый check_df.
  4. Интеграция `_process` шага 1c: сходится с общей ОСВ — ок; расхождение с
     общей ОСВ — ConvergenceError с problem_data; при дополнительном внутреннем
     mismatch в problem_data попадают оба типа проверки.

Запуск: conda run -n fl_acc_card python -u _smoke_01c_reconcile.py
"""
import io
import shutil
import sys
from pathlib import Path

import openpyxl
import pandas as pd

from loguru import logger

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import io_module.data_io as dio
from config.defaults import format_amount
from data_processors.file_processor import FileProcessor
from data_processors.file_handler import FileHandler
from io_module import DataLoader
from pipeline.base import ProcessingContext
from pipeline.errors import ConvergenceError
from pipeline.step_config import PlanAccountsConstants, ReconciliationConstants
from pipeline.steps.step_01c_reconcile_totals import Step1cReconcileTotalsStep

FAILED = 0
PASSED = 0


def check(condition: bool, message: str) -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  [ok] {message}")
    else:
        FAILED += 1
        print(f"  [FAIL] {message}")

print("1) Чтение кэша формулы из настоящего выгрузки МЭЗ_осв_14_2025_.xlsx")

real_file = (
    Path("_INPUT_DATA")
    / "_archive"
    / "20261015_195753"
    / "accounts_osv"
    / "МЭЗ_осв_14_2025_.xlsx"
)
if real_file.exists():
    tmp_dir = Path("_tmp_smoke_01c")
    tmp_dir.mkdir(exist_ok=True)
    tmp_file = tmp_dir / "МЭЗ_осв_14_2025_.xlsx"
    shutil.copy(real_file, tmp_file)

    saved_dir = dio.ACCOUNTS_OSV_DIR
    dio.ACCOUNTS_OSV_DIR = tmp_dir
    try:
        df, check_df = DataLoader.load_account_osv()
    finally:
        dio.ACCOUNTS_OSV_DIR = saved_dir

    row = df[df["Субконто"] == "Объекты основных средств"]
    check(not row.empty, "строка 'Объекты основных средств' прочлась из файла")
    if not row.empty:
        credit = pd.to_numeric(row["Кредит_конец"], errors="coerce").iloc[0]
        check(
            abs(credit - 249709601.37) < 0.01,
            f"кэш формулы N11 = {credit:,.2f} (ожидалось 249709601.37), "
            f"а не строка-формула / NaN",
        )

    check(
        not check_df.empty,
        "check_df не пуст — контрольная таблица вернулась из загрузки",
    )
    if not check_df.empty and "Сальдо_конец_разница" in check_df.columns:
        diff = pd.to_numeric(check_df["Сальдо_конец_разница"], errors="coerce").abs()
        # После фикса data_only=True файл МЭЗ 14 читается корректно:
        # субконто (945 517,35 + 249 709 601,37) сходятся с итогом (250 655 118,72),
        # так что внутренняя разница = 0 — это ожидаемо и правильно.
        check(
            diff.max() <= ReconciliationConstants.INTERNAL_CONVERGENCE_TOLERANCE_RUB,
            f"внутренняя несходимость устранена (max разница {diff.max():,.0f} руб.)",
        )

    shutil.rmtree(tmp_dir, ignore_errors=True)
else:
    print("  [skip] реальный архивный файл не найден — тест 1 пропущен")

print()

step = Step1cReconcileTotalsStep()

def make_check_df(saldo_conec_diff: float, saldo_nachalo_diff: float = 0.0,
                  oborot_diff: float = 0.0,
                  filename: str = "Тест_осв_14_2025_.xlsx") -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Субконто": "14.06",
            "Дебет_начало": 0.0, "Кредит_начало": 0.0,
            "Дебет_оборот": 0.0, "Кредит_оборот": 0.0,
            "Дебет_конец": 0.0, "Кредит_конец": 0.0,
            "Сальдо_начало_разница": saldo_nachalo_diff,
            "Оборот_разница": oborot_diff,
            "Сальдо_конец_разница": saldo_conec_diff,
            "Исх.файл": filename,
        }
    ])

res = step._warn_internal_mismatches(make_check_df(150.0))
check(len(res) == 1 and res["Тип_проверки"].iloc[0] == "Внутренняя сверка файла (Итого vs субконто)",
      f"разница 150 руб. вернула 1 строку (факт {len(res)})")

res = step._warn_internal_mismatches(make_check_df(1.0))
check(len(res) == 0, "разница ровно 1 рубль — не превышает допуск")

res = step._warn_internal_mismatches(make_check_df(0.0))
check(len(res) == 0, "нулевая разница — пусто")

res = step._warn_internal_mismatches(pd.DataFrame())
check(len(res) == 0, "пустой check_df — пусто")

res = step._warn_internal_mismatches(None)
check(len(res) == 0, "check_df None — пусто, не падает")

res = step._warn_internal_mismatches(pd.DataFrame({"Субконто": ["14.06"]}))
check(len(res) == 0, "незнакомый формат check_df — пусто, не падает")

print()

COMMON_OSV = pd.DataFrame([
    {"Счет": "14.06", "Дебет_конец": 0.0, "Кредит_конец": 250655118.72},
])

PLAN = pd.DataFrame([
    {"компания": "МЭЗ", "код": "14", "наименование": "Материалы",
     "субконто_1": "Номенклатура", "субконто_2": None, "субконто_3": None},
])

TOL = {"tolerance_reconciliation": 1050.0}


def build_context(osv_all_df: pd.DataFrame, check_df: pd.DataFrame) -> ProcessingContext:
    ctx = ProcessingContext(
        company="МЭЗ",
        segment="",
        period="2025",
        type_period="",
        run_id="20260101_000000",
        common_osv_df=COMMON_OSV,
        references={"план_счетов_бу": PLAN},
        tolerance_params=TOL,
    )
    dio.DataLoader.load_account_osv = staticmethod(lambda: (osv_all_df, check_df))
    return ctx


def restore_load():
    dio.DataLoader.load_account_osv = DataLoader.load_account_osv

osv_ok = pd.DataFrame([
    {"Субконто": "14.06", "Дебет_конец": 0.0, "Кредит_конец": 250655118.72,
     "Исх.файл": "Тест_осв_14_2025_.xlsx", "Level_0": "14"},
])
check_ok = pd.DataFrame([
    {"Субконто": "14.06", "Сальдо_начало_разница": 0.0, "Оборот_разница": 0.0,
     "Сальдо_конец_разница": 0.0, "Исх.файл": "Тест_осв_14_2025_.xlsx"},
])

ctx = build_context(osv_ok, check_ok)
try:
    result = step._process(ctx)
    check(result is ctx, "шаг вернул контекст")
    check(
        result.data.get(ReconciliationConstants.SUMMARY_KEY, {}).get("tolerance") == 1050.0,
        "SUMMARY_KEY с допуском записан в контекст",
    )
finally:
    restore_load()

osv_bad = pd.DataFrame([
    {"Субконто": "14.06", "Дебет_конец": 0.0, "Кредит_конец": 945517.35,
     "Исх.файл": "Тест_осв_14_2025_.xlsx", "Level_0": "14"},
])
ctx = build_context(osv_bad, check_ok)
try:
    step._process(ctx)
    check(False, "ожидался ConvergenceError при расхождении с общей ОСВ")
except ConvergenceError as e:
    check(e.problem_data is not None and len(e.problem_data) > 0,
          f"problem_data не пуст ({len(e.problem_data)} строк)")
    check(
        (e.problem_data["Тип_проверки"] == "Сальдо (ОСВ vs ОСВ по счетам)").any(),
        "в problem_data есть строка сверки сальдо с общей ОСВ",
    )
    diff_col = e.problem_data.loc[
        e.problem_data["Тип_проверки"] == "Сальдо (ОСВ vs ОСВ по счетам)",
        "сальдо_свернуто, тыс. ед._diff",
    ].iloc[0]
    check(
        abs(float(diff_col) - (-249709.60)) < 0.05,
        f"разница сальдо ≈ −249 709,60 тыс.ед. (факт {float(diff_col):,.2f})",
    )
except Exception as e:  # noqa: BLE001
    check(False, f"ожидался ConvergenceError, пришёл {type(e).__name__}: {e}")
finally:
    restore_load()

check_bad = make_check_df(150.0)
ctx = build_context(osv_bad, check_bad)
try:
    step._process(ctx)
    check(False, "ожидался ConvergenceError")
except ConvergenceError as e:
    types = set(e.problem_data["Тип_проверки"].unique())
    check("Сальдо (ОСВ vs ОСВ по счетам)" in types, "тип 'Сальдо (ОСВ vs ОСВ по счетам)' в problem_data")
    check("Внутренняя сверка файла (Итого vs субконто)" in types,
          f"тип 'Внутренняя сверка файла' в problem_data (типы: {types})")
    check(
        e.problem_data[e.problem_data["Тип_проверки"]
                      == "Внутренняя сверка файла (Итого vs субконто)"]["Исх.файл"].iloc[0]
        == "Тест_осв_14_2025_.xlsx",
        "внутренний mismatch подписан именем файла",
    )
except Exception as e:  # noqa: BLE001
    check(False, f"ожидался ConvergenceError, пришёл {type(e).__name__}: {e}")
finally:
    restore_load()

caught_warnings: list[str] = []

def sink(message):
    caught_warnings.append(message.message)

handler_id = logger.add(sink, level="WARNING", format="{message}")
try:
    ctx = build_context(osv_ok, make_check_df(150.0))
    step._process(ctx)
    check(
        any("«Итого» не сходится с суммой субконто" in w for w in caught_warnings),
        "WARNING про внутреннюю несходимость файла записан в лог",
    )
finally:
    logger.remove(handler_id)
    restore_load()

print()
print(f"SMOKE_OK ({PASSED}/{PASSED + FAILED})" if FAILED == 0
      else f"SMOKE_FAIL ({PASSED}/{PASSED + FAILED})")
sys.exit(1 if FAILED else 0)
