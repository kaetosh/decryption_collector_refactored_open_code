# -*- coding: utf-8 -*-
"""
Смоук актуальности справочника курса валюты (utils/currency_utils.py).

Зачем: лист Курс_AED заканчивается 31.08.2026, а компания отчитывается
за сентябрь — весь баланс и все проводки ОПУ молча переводятся по курсу
месячной давности, а лог об этом сообщал на уровне INFO и потому не попадал
ни в сводку предупреждений, ни на титульный лист. Проверять надо две разные
вещи, которые раньше были неразличимы:

1. resolve_rate() — сам факт промаха и признак covered;
2. уровень сообщения: календарный промах (выходной) -> INFO, просрочка
   справочника -> [!] WARNING.

Разделы:
  1. resolve_rate: точная дата, выходной внутри листа, просрочка.
  2. Уровни логов get_rate_for_date_with_info и get_rate_for_date (молчит).
  3. add_ruble_amount_column: агрегированный WARNING по датам после конца
     листа + зеркальная проверка на даты раньше начала листа (регресс).
  4. ask_balance_date_if_needed: WARNING на флаге --balance-date,
     интерактивный переспрос (y / n / Enter / EOF).
  5. Титульный лист: строка «Актуальность курса валюты».
  6. Регресс значений курса.

Запуск: python -u _smoke_currency_rate_freshness.py
"""
import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

import pipeline.executors as executors
from io_module import report_cover as rc
from pipeline.step_config import CurrencyConstants
from utils import currency_utils as cu

PASSED = 0
FAILED = 0
_failed_messages: list[str] = []

# ── Ловушка на предупреждения/инфо ────────────────────────────────────────────
# format='{message}' — иначе sink отдаёт строку вместе со временем и уровнем,
# и проверка префикса «[!]» (он же ключ группировки в сводке предупреждений)
# проверяла бы не сообщение, а оформление логгера.
warnings_text: list[str] = []
infos_text: list[str] = []
logger.add(lambda m: warnings_text.append(str(m)), level="WARNING", format="{message}")
logger.add(lambda m: infos_text.append(str(m)), level="INFO", format="{message}")


def reset_logs() -> None:
    warnings_text.clear()
    infos_text.clear()


def check(condition: bool, message: str) -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK] {message}")
    else:
        FAILED += 1
        _failed_messages.append(message)
        print(f"  [FAIL] {message}")


# ── Фикстуры ─────────────────────────────────────────────────────────────────
# Лист курса AED: 01.08.2026 — 31.08.2026 (последняя дата = 31.08),
# плюс одна «дыра» внутри: 15.08 отсутствует (имитация пропуска в справочнике).
RATE_ROWS = [
    ("01.08.2026", 23.0000),
    ("02.08.2026", 23.0500),
    ("14.08.2026", 23.1000),
    ("16.08.2026", 23.2000),   # 15.08 — дыра внутри диапазона
    ("20.08.2026", 23.3086),
    ("31.08.2026", 23.3086),   # последняя дата листа
]


def make_rates_df() -> pd.DataFrame:
    return pd.DataFrame(RATE_ROWS, columns=["дата", "курс"])


def make_context(currency: str = "AED", rates: pd.DataFrame | None = None, **extra) -> SimpleNamespace:
    base = dict(
        currency=currency,
        balance_date=None,
        data={},
        tolerance_params={"tolerance_rate_deviation": 0.35},
        references={"курс_aed": make_rates_df() if rates is None else rates},
    )
    base.update(extra)
    return SimpleNamespace(**base)


def find_row(rows: list, needle: str) -> str:
    for label, value in rows:
        if needle in (label or ""):
            return value
    return ""


def patch_input(answers: list[str]):
    """Подменяет input() на очередь ответов; по исчерпании — EOF."""
    queue = list(answers)
    original = builtins.input

    def _fake_input(prompt: str = "") -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    builtins.input = _fake_input
    return original


def main() -> int:
    print("\n1. resolve_rate: природа промаха")
    ctx = make_context()

    exact = cu.resolve_rate(ctx, "20.08.2026")
    check(exact.gap_days == 0 and exact.covered, "точная дата: gap 0, covered")
    check(exact.rate == 23.3086, f"точная дата: курс из справочника ({exact.rate})")
    check(exact.requested_date.isoformat() == "2026-08-20", "requested_date = запрошенная")

    weekend = cu.resolve_rate(ctx, "18.08.2026")
    check(weekend.gap_days == 2 and weekend.covered,
          f"выходной внутри листа: covered=True, gap {weekend.gap_days}")
    check(weekend.rate == 23.2000 and weekend.rate_date.isoformat() == "2026-08-16",
          f"выходной внутри листа: взят курс предыдущей даты ({weekend.rate})")

    hole = cu.resolve_rate(ctx, "15.08.2026")
    check(hole.covered and hole.gap_days == 1,
          "дыра внутри листа: covered=True (лист дотягивается), не просрочка")

    stale = cu.resolve_rate(ctx, "30.09.2026")
    check(not stale.covered, "дата после конца листа: covered=False")
    check(stale.gap_days == 30, f"дата после конца листа: отставание {stale.gap_days} дн.")
    check(stale.rate == 23.3086 and stale.rate_date.isoformat() == "2026-08-31",
          "просрочка: взят последний курс листа")

    check(cu.resolve_rate(ctx, "01.08.2026").gap_days == 0, "первая дата листа: точный курс")

    try:
        cu.resolve_rate(ctx, "01.07.2026")
        check(False, "дата раньше листа: ожидался ValueError")
    except ValueError:
        check(True, "дата раньше листа: ValueError (как раньше)")

    print("\n2. Уровни логов")
    reset_logs()
    cu.get_rate_for_date_with_info(ctx, "20.08.2026")
    check(not warnings_text and not infos_text, "точная дата: тишина")

    reset_logs()
    cu.get_rate_for_date_with_info(ctx, "18.08.2026")
    check(not warnings_text, "выходной внутри листа: без WARNING")
    check(any("nearest PREVIOUS" in m for m in infos_text),
          "выходной внутри листа: остаётся INFO")

    reset_logs()
    rate, rate_date = cu.get_rate_for_date_with_info(ctx, "30.09.2026")
    joined = "\n".join(warnings_text)
    check(len(warnings_text) == 1, f"просрочка: ровно один WARNING (было {len(warnings_text)})")
    check(warnings_text and warnings_text[0].startswith("[!]"),
          "просрочка: WARNING с префиксом [!] (попадёт в сводку и на титул)")
    check("31.08.2026" in joined and "30.09.2026" in joined,
          "просрочка: в тексте обе даты")
    check("30 дн." in joined, "просрочка: указано отставание в днях")
    check("Курс_AED" in joined, "просрочка: подсказка, какой лист дополнять")
    check(rate == 23.3086 and rate_date == "31.08.2026",
          "просрочка: курс и дата на месте (регресс значений)")

    reset_logs()
    cu.get_rate_for_date(ctx, "30.09.2026")
    check(not warnings_text and not infos_text,
          "get_rate_for_date молчит (иначе шум на каждую дату проводки)")

    print("\n3. add_ruble_amount_column: даты после конца листа")
    ctx3 = make_context()
    df = pd.DataFrame({
        "Дата": ["10.08.2026", "20.08.2026", "01.09.2026", "30.09.2026"],
        "Сумма": [100.0, 200.0, 300.0, 400.0],
    })
    reset_logs()
    out = cu.add_ruble_amount_column(df.copy(), ctx3)
    joined = "\n".join(warnings_text)
    stale_warnings = [m for m in warnings_text if "заканчивается датой" in m]
    check(len(stale_warnings) == 1,
          f"агрегированный WARNING по просрочке (было {len(stale_warnings)})")
    check("2 дат операций" in joined, "в тексте — число дат операций")
    check("01.09.2026" in joined and "30.09.2026" in joined, "в тексте — диапазон дат")
    check("23.3086" in joined, "в тексте — применённый курс")

    # Ожидание берём из resolve_rate — того же поиска курса, которым пользуется
    # шаг 14. Свой набор значений здесь означал бы вторую правду о курсах.
    expected = df["Сумма"] * df["Дата"].map(lambda dt: cu.resolve_rate(ctx3, dt).rate)
    check((out["Сумма_руб"] - expected).abs().max() < 1e-9,
          "суммы переведены по фактическим курсам (регресс)")
    check(abs(out.loc[0, "Сумма_руб"] - 100.0 * 23.05) < 1e-9,
          "дата внутри листа без точного курса — предыдущая дата (23.05 от 02.08)")

    opu = (ctx3.data.get(CurrencyConstants.SUMMARY_KEY) or {}).get(
        CurrencyConstants.SUMMARY_SECTION_OPU) or {}
    check(opu.get(CurrencyConstants.SUMMARY_TX_DATES_COUNT) == 2,
          "сводка ОПУ: 2 даты")
    check(opu.get(CurrencyConstants.SUMMARY_TX_FIRST_DATE) == "01.09.2026"
          and opu.get(CurrencyConstants.SUMMARY_TX_LAST_DATE) == "30.09.2026",
          "сводка ОПУ: диапазон дат")
    check(opu.get(CurrencyConstants.SUMMARY_RATE_DATE) == "31.08.2026"
          and opu.get(CurrencyConstants.SUMMARY_GAP_DAYS) == 30,
          "сводка ОПУ: дата курса и отставание")
    check(opu.get(CurrencyConstants.SUMMARY_COVERED) is False,
          "сводка ОПУ: covered=False")

    # Регресс: даты в пределах листа просрочку не создают.
    ctx3b = make_context()
    df_ok = pd.DataFrame({"Дата": ["10.08.2026", "20.08.2026"], "Сумма": [100.0, 200.0]})
    reset_logs()
    cu.add_ruble_amount_column(df_ok.copy(), ctx3b)
    check(not any("заканчивается датой" in m for m in warnings_text),
          "даты в пределах листа: WARNING о просрочке нет")
    check(not (ctx3b.data.get(CurrencyConstants.SUMMARY_KEY) or {}),
          "даты в пределах листа: сводка ОПУ не пишется")

    # Регресс: зеркальная проверка «раньше начала листа» не сломана.
    ctx3c = make_context()
    df_old = pd.DataFrame({"Дата": ["01.07.2026", "10.08.2026"], "Сумма": [100.0, 200.0]})
    reset_logs()
    out_old = cu.add_ruble_amount_column(df_old.copy(), ctx3c)
    check(any("нет курсов на даты раньше" in m for m in warnings_text),
          "дата раньше листа: прежний WARNING на месте (регресс)")
    check(abs(out_old.loc[0, "Сумма_руб"] - 100.0 * 23.0000) < 1e-9,
          "дата раньше листа: взят самый ранний курс")

    # Регресс: рублёвая компания — без курсовых сообщений и сводок.
    ctx3d = make_context(currency="RUB", references={})
    df_rub = pd.DataFrame({"Дата": ["01.07.2026"], "Сумма": [100.0]})
    reset_logs()
    out_rub = cu.add_ruble_amount_column(df_rub.copy(), ctx3d)
    check(not warnings_text and not infos_text, "рублёвая компания: тишина")
    check(out_rub.loc[0, "Сумма_руб"] == 100.0, "рублёвая компания: курс 1")
    check(not ctx3d.data.get(CurrencyConstants.SUMMARY_KEY),
          "рублёвая компания: сводка не пишется")

    print("\n4. ask_balance_date_if_needed")
    # 4a. Дата задана флагом, справочник просрочен.
    ctx4 = make_context(balance_date="30.09.2026")
    reset_logs()
    executors.ask_balance_date_if_needed(ctx4, interactive=False)
    joined = "\n".join(warnings_text)
    check(any("НЕ АКТУАЛЕН" in m or "заканчивается датой" in m for m in warnings_text),
          "флаг --balance-date на непокрытую дату: WARNING")
    check(ctx4.balance_date == "31.08.2026", "дата перевода нормализована на дату курса")
    bal = (ctx4.data.get(CurrencyConstants.SUMMARY_KEY) or {}).get(
        CurrencyConstants.SUMMARY_SECTION_BALANCE) or {}
    check(bal.get(CurrencyConstants.SUMMARY_REQUESTED_DATE) == "30.09.2026",
          "сводка баланса: запрошенная дата сохранена (иначе титул её не покажет)")
    check(bal.get(CurrencyConstants.SUMMARY_RATE_DATE) == "31.08.2026",
          "сводка баланса: дата применённого курса")
    check(bal.get(CurrencyConstants.SUMMARY_COVERED) is False, "сводка баланса: covered=False")

    # 4b. Тот же флаг, но дата покрыта — тишина.
    ctx4b = make_context(balance_date="20.08.2026")
    reset_logs()
    executors.ask_balance_date_if_needed(ctx4b, interactive=False)
    check(not warnings_text, "покрытая дата через флаг: без WARNING")
    check(ctx4b.balance_date == "20.08.2026", "покрытая дата через флаг: дата не меняется")

    # 4c. Неинтерактивный режим без заданной даты — берётся последняя из листа.
    ctx4c = make_context()
    reset_logs()
    executors.ask_balance_date_if_needed(ctx4c, interactive=False)
    check(ctx4c.balance_date == "31.08.2026", "неинтерактив: последняя дата листа")
    check(any(w.startswith("[!]") for w in warnings_text),
          "неинтерактив: остаётся предупреждение о незаданной дате")

    # 4d. Интерактив: ввод непокрытой даты и подтверждение «y».
    ctx4d = make_context()
    reset_logs()
    original = patch_input(["30.09.2026", "y"])
    try:
        executors.ask_balance_date_if_needed(ctx4d, interactive=True)
    finally:
        builtins.input = original
    check(ctx4d.balance_date == "31.08.2026", "интерактив + y: курс применён")
    check(any("заканчивается датой" in m for m in warnings_text),
          "интерактив + y: WARNING о неактуальном справочнике")

    # 4e. Интерактив: отказ «n» -> возврат к вводу даты.
    ctx4e = make_context()
    reset_logs()
    original = patch_input(["30.09.2026", "n", "20.08.2026"])
    try:
        executors.ask_balance_date_if_needed(ctx4e, interactive=True)
    finally:
        builtins.input = original
    check(ctx4e.balance_date == "20.08.2026",
          "интерактив + n: возврат к вводу даты, применён введённая")

    # 4f. Интерактив: пустой ответ = отказ (не продолжать молча).
    ctx4f = make_context()
    reset_logs()
    original = patch_input(["30.09.2026", "", "18.08.2026"])
    try:
        executors.ask_balance_date_if_needed(ctx4f, interactive=True)
    finally:
        builtins.input = original
    check(ctx4f.balance_date == "16.08.2026",
          f"интерактив + Enter: отказ по умолчанию ({ctx4f.balance_date})")

    # 4g. Интерактив: выходной внутри листа не спрашивает подтверждения.
    ctx4g = make_context()
    reset_logs()
    original = patch_input(["18.08.2026"])
    try:
        executors.ask_balance_date_if_needed(ctx4g, interactive=True)
    finally:
        builtins.input = original
    check(ctx4g.balance_date == "16.08.2026", "интерактив: выходной принят без вопроса")
    check(not any("заканчивается датой" in m for m in warnings_text),
          "интерактив: выходной не даёт WARNING о просрочке")

    print("\n5. Титульный лист: «Актуальность курса валюты»")
    cover_base = dict(
        company="РЗК", period="9мес2026", type_period="9 месяцев", segment="Прочие ГАП",
        currency="AED", balance_date="31.08.2026",
        name_file_general_osv="РЗК_общаяосв_нд_9мес2026_.xlsx", run_id="20261006_221851",
        balance_df=pd.DataFrame({"Значение": [1.0]}),
        pnl_df=pd.DataFrame({"Значение": [1.0]}),
        tolerance_params={},
    )
    rows = rc.build_cover_rows(SimpleNamespace(**cover_base, data={}), warnings=[])
    check(find_row(rows, "Актуальность курса валюты") == "не проверялся — сведения о курсе не собраны (см. лог прогона)",
          "без сводки — честное «не проверялся», а не «актуален»")

    stale_summary = {CurrencyConstants.SUMMARY_KEY: {
        CurrencyConstants.SUMMARY_SECTION_BALANCE: {
            CurrencyConstants.SUMMARY_COVERED: False,
            CurrencyConstants.SUMMARY_CURRENCY: "AED",
            CurrencyConstants.SUMMARY_REQUESTED_DATE: "30.09.2026",
            CurrencyConstants.SUMMARY_RATE_DATE: "31.08.2026",
            CurrencyConstants.SUMMARY_RATE: 23.3086,
            CurrencyConstants.SUMMARY_GAP_DAYS: 30,
        },
        CurrencyConstants.SUMMARY_SECTION_OPU: {
            CurrencyConstants.SUMMARY_COVERED: False,
            CurrencyConstants.SUMMARY_CURRENCY: "AED",
            CurrencyConstants.SUMMARY_TX_DATES_COUNT: 22,
            CurrencyConstants.SUMMARY_TX_FIRST_DATE: "01.09.2026",
            CurrencyConstants.SUMMARY_TX_LAST_DATE: "30.09.2026",
            CurrencyConstants.SUMMARY_RATE_DATE: "31.08.2026",
            CurrencyConstants.SUMMARY_GAP_DAYS: 30,
        },
    }}
    rows = rc.build_cover_rows(SimpleNamespace(**cover_base, data=stale_summary), warnings=[])
    text = find_row(rows, "Актуальность курса валюты")
    check("НЕ АКТУАЛЕН" in text, "просрочка помечена явно на титуле")
    check("30.09.2026" in text and "31.08.2026" in text, "титул: обе даты")
    check("22 дат операций" in text, "титул: сколько дат ОПУ посчитано по старому курсу")
    check("ОПУ" in text, "титул: отдельно сказано про ОПУ")

    fresh_summary = {CurrencyConstants.SUMMARY_KEY: {
        CurrencyConstants.SUMMARY_SECTION_BALANCE: {
            CurrencyConstants.SUMMARY_COVERED: True,
            CurrencyConstants.SUMMARY_CURRENCY: "AED",
            CurrencyConstants.SUMMARY_REQUESTED_DATE: "18.08.2026",
            CurrencyConstants.SUMMARY_RATE_DATE: "14.08.2026",
            CurrencyConstants.SUMMARY_RATE: 23.1,
            CurrencyConstants.SUMMARY_GAP_DAYS: 4,
        },
    }}
    rows = rc.build_cover_rows(SimpleNamespace(**cover_base, data=fresh_summary), warnings=[])
    text = find_row(rows, "Актуальность курса валюты")
    check(text.startswith("актуален"), "актуальный лист: без НЕ АКТУАЛЕН")
    check("выходной" in text, "актуальный лист: выходной назван прямо, а не спрятан")

    rows = rc.build_cover_rows(
        SimpleNamespace(**{**cover_base, "currency": "RUB"}, data={}), warnings=[])
    check(find_row(rows, "Актуальность курса валюты") == "не требуется — валюта RUB",
          "рублёвая компания: строка есть и не лезет в курсы")

    print("\n6. Регресс значений курса")
    check(cu.get_rate_for_date(ctx, "20.08.2026") == 23.3086, "get_rate_for_date: точная дата")
    check(cu.get_rate_for_date(ctx, "18.08.2026") == 23.2000, "get_rate_for_date: выходной")
    check(cu.get_rate_for_date(ctx, "30.09.2026") == 23.3086, "get_rate_for_date: просрочка")
    check(cu.get_rate_median(ctx) == 23.15, "медиана курсов листа не изменилась")
    check(cu.get_rate_deviation_limit(ctx) == 0.35, "порог отклонения курса не изменился")
    check(cu.get_earliest_rate(ctx) == 23.0000, "самый ранний курс не изменился")
    check(cu.get_last_rate_date(ctx) == "31.08.2026", "последняя дата листа не изменилась")

    print()
    if FAILED:
        print(f"SMOKE_FAIL: {PASSED}/{PASSED + FAILED}")
        for message in _failed_messages:
            print(f"  [FAIL] {message}")
        return 1
    print(f"SMOKE_OK ({PASSED}/{PASSED + FAILED})")
    return 0


if __name__ == "__main__":
    sys.exit(main())