# -*- coding: utf-8 -*-
"""
Смоук-тест: подтягивание контрагента в шаге 17 по номеру субконто из ПланСчетовБУ.

Раньше `_extract_contractors` читал только `Субконто Дт_1`/`Субконто Кт_1`,
а из справочника собирался tuple кодов, у которых `субконто_1 == 'Контрагенты'`.
Контрагент не обязан сидеть в первом субконто: на реальных данных СТБ счёт
98.01 (доходы будущих периодов) имеет `субконто_2 = Контрагенты`, и 36
проводок 91.01 молча уходили в 'не_указано'.

Теперь строится карта {код счёта: номер субконто} по всем колонкам
`субконто_1/2/3`, и колонка выгрузки выбирается по этому номеру.

Проверяет (данные синтетические, _INPUT_DATA не используется):

1. Регресс: контрагент в 1-м субконто читается как раньше.
2. Контрагент во 2-м субконто (кейс 98.01) — берётся из `Субконто Дт_2`.
3. Контрагент в 3-м субконто (на будущее, новых компаний).
4. Обе стороны: доходы из `Субконто Дт_N`, расходы из `Субконто Кт_N`.
5. Счёт, у которого контрагента нет в плане, — 'не_указано'.
6. План требует субконто на позиции, которой нет в кадре — WARNING,
   без KeyError, строки остаются с 'не_указано'.
7. Дубль 'Контрагенты' в двух колонках одного счёта — побеждает меньший номер.
8. Имя колонки собирается целочисленно: 'Субконто Дт_2', а не 'Субконто Дт_2.0'
   (регрессия dtype после .map(): без Int64 pandas даёт float).
9. `_load_references`: нет строк по компании / ни одного счёта с контрагентом
   → ReferenceMismatchError с подсказкой и перечнем колонок субконто.

Запуск: conda run -n fl_acc_card python -u _smoke_17_contractor_subconto.py
"""
import sys
from pathlib import Path

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.base import ProcessingContext
from pipeline.errors import ReferenceMismatchError
from pipeline.step_config import PlanAccountsConstants, StepConstants
from pipeline.steps._step17_base import Step17BaseMixin
from pipeline.steps._step17_data import Step17DataMixin

# При перенаправлении вывода в файл консоль Windows пишет в cp1251 —
# фиксируем UTF-8, чтобы кириллица не роняла print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

failures: list = []
PASSED = 0

UNSPECIFIED = StepConstants.UNSPECIFIED
SUBCONTO_COLS = PlanAccountsConstants.SUBCONTO_COLS


class MockStep(Step17BaseMixin, Step17DataMixin):
    """Мок-класс: только миксины шага 17, без конструктора Step."""

    def __init__(self):
        # Не вызываем super().__init__() — нужен только методы миксинов
        pass

    def _process(self, context):
        """Заглушка абстрактного метода Step — в тесте не вызывается."""
        return context

    def _save_reference_mismatch_report(self, error):
        pass


def check(condition: bool, message: str) -> None:
    """Мини-ассерт с накоплением результата."""
    global PASSED
    if condition:
        PASSED += 1
        print(f"[OK] {message}")
    else:
        failures.append(message)
        print(f"[FAIL] {message}")


def make_plan(rows: list) -> pd.DataFrame:
    """Синтетический лист ПланСчетовБУ: rows = (код, субконто_1, ...)."""
    return pd.DataFrame({
        'компания': pd.Series(['Тестовая'] * len(rows), dtype='string'),
        'код': pd.Series([r[0] for r in rows], dtype='string'),
        'наименование': pd.Series(['счёт'] * len(rows), dtype='string'),
        'субконто_1': pd.Series([r[1] for r in rows], dtype='string'),
        'субконто_2': pd.Series(
            [r[2] if len(r) > 2 else None for r in rows], dtype='string'),
        'субконто_3': pd.Series(
            [r[3] if len(r) > 3 else None for r in rows], dtype='string'),
    })


# План: позиции 1, 2 и 3, счёт без контрагента и дубль в двух колонках.
PLAN = make_plan([
    ('60.01', 'Контрагенты', 'Договоры'),
    ('98.01', 'Доходы будущих периодов', 'Контрагенты'),
    ('76.07', 'Договоры', 'Договоры аренды', 'Контрагенты'),
    ('44.01', 'Номенклатура', 'Подразделение'),
    ('45', 'Контрагенты', 'Контрагенты'),
])


def make_df(rows: list) -> pd.DataFrame:
    """rows = (Корр.счет, значение 1-го субконто, 2-го, 3-го)."""
    records = []
    for code, *subs in rows:
        rec = {'Корр.счет': code}
        for position in (1, 2, 3):
            value = subs[position - 1] if len(subs) >= position else None
            rec[f'Субконто Дт_{position}'] = value
            rec[f'Субконто Кт_{position}'] = value
        records.append(rec)
    return pd.DataFrame(records).astype('string')


# --------------------------------------------------------------------------
# 1. Построение карты позиций
# --------------------------------------------------------------------------
def test_build_contractor_positions() -> None:
    print("\n=== 1. _build_contractor_positions ===")
    step = MockStep()
    positions = step._build_contractor_positions(PLAN)

    check(positions.get('60.01') == 1,
          "контрагент в 1-м субконто → позиция 1")
    check(positions.get('98.01') == 2,
          "контрагент во 2-м субконто → позиция 2 (кейс 98.01)")
    check(positions.get('76.07') == 3,
          "контрагент в 3-м субконто → позиция 3")
    check('44.01' not in positions,
          "счёт без Контрагенты в карту не попадает")
    check(positions.get('45') == 1,
          "дубль Контрагенты в 1-м и 2-м → побеждает меньший номер")
    check(all(isinstance(v, int) for v in positions.values()),
          "значения карты — целые числа")

    # Колонки субконто в плане могут отсутствовать — не должно падать.
    positions_partial = step._build_contractor_positions(
        PLAN.drop(columns=['субконто_3'])
    )
    check(positions_partial.get('76.07') is None,
          "отсутствующая колонка субконто_3 не роняет построение карты")
    check(positions_partial.get('60.01') == 1,
          "остальные позиции при этом корректны")


# --------------------------------------------------------------------------
# 2. Извлечение контрагента
# --------------------------------------------------------------------------
def test_extract_positions() -> None:
    print("\n=== 2. _extract_contractors: позиции 1/2/3 ===")
    step = MockStep()
    positions = step._build_contractor_positions(PLAN)

    df = make_df([
        ('60.01', 'ООО Ромашка', 'Договор №1'),
        ('98.01', 'Доходы БП', 'Павлов Сергей Игоревич'),
        ('76.07', 'Договоры', 'Договор аренды', 'ЛИМАН АО'),
        ('44.01', 'Товары', 'Цех №2'),
    ])
    result = step._extract_contractors(df, is_income=True,
                                       contractor_positions=positions)
    values = result['контрагент'].tolist()

    check(values[0] == 'ООО Ромашка',
          f"позиция 1 читается как раньше (получено '{values[0]}')")
    check(values[1] == 'Павлов Сергей Игоревич',
          f"позиция 2 читается из Субконто Дт_2 (получено '{values[1]}')")
    check(values[2] == 'ЛИМАН АО',
          f"позиция 3 читается из Субконто Дт_3 (получено '{values[2]}')")
    check(values[3] == UNSPECIFIED,
          f"счёт без контрагента → '{UNSPECIFIED}' (получено '{values[3]}')")
    check(str(result['контрагент'].dtype) == 'string',
          "dtype колонки 'контрагент' — string, не object")


def test_extract_income_vs_expense() -> None:
    print("\n=== 3. Разные стороны: доходы Дт / расходы Кт ===")
    step = MockStep()
    positions = step._build_contractor_positions(PLAN)

    # Доходы: контрагент лежит на стороне Дт.
    df_income = make_df([('98.01', 'Доходы БП', 'Иванов Иван Иванович')])
    income = step._extract_contractors(df_income, is_income=True,
                                       contractor_positions=positions)
    check(income['контрагент'].iloc[0] == 'Иванов Иван Иванович',
          "доходы берут контрагента из Субконто Дт_2")

    # Расходы: контрагент на стороне Кт, колонок Дт в кадре нет вовсе.
    df_expense = pd.DataFrame({
        'Корр.счет': pd.Series(['98.01'], dtype='string'),
        'Субконто Кт_2': pd.Series(['Поставщик ООО'], dtype='string'),
    })
    expense = step._extract_contractors(df_expense, is_income=False,
                                        contractor_positions=positions)
    check(expense['контрагент'].iloc[0] == 'Поставщик ООО',
          "расходы берут контрагента из Субконто Кт_2")
    check('Субконто Дт_1' not in df_expense.columns,
          "отсутствие колонок Дт не мешает разбору расходов")


# --------------------------------------------------------------------------
# 3. Критерий имени колонки (регрессия dtype)
# --------------------------------------------------------------------------
def test_column_name_is_integer() -> None:
    print("\n=== 4. Имя колонки — целое число, без '.0' ===")
    step = MockStep()
    positions = step._build_contractor_positions(PLAN)

    # Позиция 2 обязана дать имя 'Субконто Дт_2': после .map() серия с NaN
    # получает float64, и f'{position}' дал бы '2.0' — такой колонки в кадре
    # нет, метод молча оставил бы 'не_указано'.
    df = make_df([('98.01', 'Доходы БП', 'ООО Второй субконто')])
    result, seen = capture_warnings(
        step._extract_contractors, df, is_income=True,
        contractor_positions=positions,
    )

    check(result['контрагент'].iloc[0] == 'ООО Второй субконто',
          "колонка найдена по целочисленному имени")
    check(not any('Субконто Дт_2.0' in s for s in seen),
          f"имя 'Субконто Дт_2.0' не фигурировало в предупреждениях: {seen}")


# --------------------------------------------------------------------------
# 4. Отсутствующая в кадре колонка — WARNING, без KeyError
# --------------------------------------------------------------------------
def test_missing_column_warns_not_raises() -> None:
    print("\n=== 5. План требует позицию, колонки в кадре нет ===")
    step = MockStep()

    # Счёт контрагента указывает на 3-е субконто, но в кадре только _1.
    plan = make_plan([('76.07', 'Договоры', 'Договоры аренды', 'Контрагенты')])
    positions = step._build_contractor_positions(plan)
    check(positions.get('76.07') == 3, "карта даёт позицию 3")

    df = pd.DataFrame({
        'Корр.счет': pd.Series(['76.07'], dtype='string'),
        'Субконто Кт_1': pd.Series(['Договор аренды'], dtype='string'),
    })

    try:
        result, seen = capture_warnings(
            step._extract_contractors, df, is_income=False,
            contractor_positions=positions,
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"не должно падать, получено {type(exc).__name__}: {exc}")
        return

    check(len(seen) == 1 and 'Субконто Кт_3' in seen[0],
          f"выдан WARNING с именем недостающей колонки (получено: {seen})")
    check(result['контрагент'].iloc[0] == UNSPECIFIED,
          f"строка остаётся с '{UNSPECIFIED}'")
    check('1' in seen[0],
          f"в предупреждении указано число затронутых строк (получено: {seen})")




# --------------------------------------------------------------------------
# 5. Пустые ветки _load_references
# --------------------------------------------------------------------------
def make_context(plan: pd.DataFrame) -> ProcessingContext:
    """Минимальный контекст для _load_references.

    Колонки обязательны: `справочник_ппа` фильтруется по
    `наименование_компании`, `кредит_обслуж` — по `компания`.
    """

    def empty(*cols: str) -> pd.DataFrame:
        return pd.DataFrame({c: pd.Series([], dtype='string') for c in cols})

    context = ProcessingContext.__new__(ProcessingContext)
    context.references = {
        'меппинг_опу': empty('счет', 'доход_расход'),
        'справочник_ппа': empty('наименование_компании'),
        'вид_связи_ка': empty('ВидСвязиКА'),
        'компании_группы': empty('сокращенное_наименование_компании'),
        'кредит_обслуж': empty('компания', 'рбп_кредитные_линии', 'контрагент'),
        'план_счетов_бу': plan,
        'прочие_доходы_ндс': pd.DataFrame(
            {'прочие_доходы_ндс': pd.Series(['Прочие доходы'], dtype='string')}),
        'виды_рбп_аренда_лизинг': empty('виды_рбп_аренда_лизинг'),
    }
    return context


def capture_warnings(func, *args, **kwargs):
    """Вызывает func, собирая предупреждения loguru в список отформатированных строк."""
    seen: list = []
    original_warning = logger.warning

    def _sink(message, *fmt_args, **fmt_kwargs):
        # Мок перехватывает вызов ДО форматирования loguru, поэтому
        # подставляем аргументы руками — иначе в списке остаются '{}'.
        text = str(message)
        if fmt_args:
            try:
                text = text.format(*fmt_args, **fmt_kwargs)
            except (IndexError, KeyError):
                pass
        seen.append(text)

    logger.warning = _sink
    try:
        result = func(*args, **kwargs)
    finally:
        logger.warning = original_warning
    return result, seen


def test_load_references_errors() -> None:
    print("\n=== 6. _load_references: ветки ошибок ===")
    step = MockStep()

    # 6.1. Нет строк по компании.
    try:
        step._load_references('Нет Такой Компании', make_context(PLAN))
        check(False, "ожидалась ReferenceMismatchError для неизвестной компании")
    except ReferenceMismatchError as exc:
        check('ПланСчетовБУ' in str(exc), "текст называет лист ПланСчетовБУ")
        check(exc.problem_data is not None and not exc.problem_data.empty,
              "problem_data содержит список компаний справочника")
    except Exception as exc:  # noqa: BLE001
        check(False, f"другое исключение: {type(exc).__name__}: {exc}")

    # 6.2. Строки по компании есть, но ни одного счёта с контрагентом.
    plan_no_contractor = make_plan([('44.01', 'Номенклатура', 'Подразделение')])
    try:
        step._load_references('Тестовая', make_context(plan_no_contractor))
        check(False, "ожидалась ReferenceMismatchError при отсутствии контрагентов")
    except ReferenceMismatchError as exc:
        text = str(exc)
        check('Контрагенты' in text, "текст называет искомое значение")
        check(all(col in text for col in SUBCONTO_COLS),
              f"текст перечисляет все колонки субконто ({', '.join(SUBCONTO_COLS)})")
        problem_cols = list(exc.problem_data.columns)
        check(all(col in problem_cols for col in SUBCONTO_COLS),
              f"problem_data содержит все колонки субконто: {problem_cols}")
    except Exception as exc:  # noqa: BLE001
        check(False, f"другое исключение: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# 6. Счастливый путь через _load_references
# --------------------------------------------------------------------------
def test_load_references_ok() -> None:
    print("\n=== 7. _load_references: карта доходит до вызывающего кода ===")
    step = MockStep()

    try:
        refs = step._load_references('Тестовая', make_context(PLAN))
    except Exception as exc:  # noqa: BLE001
        check(False, f"не должно падать: {type(exc).__name__}: {exc}")
        return

    positions = refs.get('contractor_positions')
    check(isinstance(positions, dict), "ключ 'contractor_positions' присутствует")
    check('accounts_with_contractors' not in refs,
          "старый ключ 'accounts_with_contractors' больше не возвращается")
    if isinstance(positions, dict):
        check(positions.get('98.01') == 2, "позиция 2 по счёту 98.01 дошла")
        check(positions.get('60.01') == 1, "позиция 1 по счёту 60.01 дошла")
        check('44.01' not in positions, "счёт без контрагента отсутствует")
        check('ppa' in refs and 'mapping_opu' in refs,
              "прочие ключи справочников не сломаны")


if __name__ == '__main__':
    print("=" * 60)
    print("Смоук-тест: контрагент по номеру субконто (шаг 17, ПланСчетовБУ)")
    print("=" * 60)

    test_build_contractor_positions()
    test_extract_positions()
    test_extract_income_vs_expense()
    test_column_name_is_integer()
    test_missing_column_warns_not_raises()
    test_load_references_errors()
    test_load_references_ok()

    total = PASSED + len(failures)
    label = "подтягивание контрагента по позиции субконто (шаг 17)"
    if failures:
        print(f"\nSMOKE_FAIL ({len(failures)}/{total}) — {label}")
        for item in failures:
            print(f"  [FAIL] {item}")
        sys.exit(1)
    print(f"\nSMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


def test_extract_income_vs_expense() -> None:
    print("\n=== 3. Разные стороны: доходы Дт / расходы Кт ===")
    step = MockStep()
    positions = step._build_contractor_positions(PLAN)

    # Доходы: контрагент лежит на стороне Дт.
    df_income = make_df([('98.01', 'Доходы БП', 'Иванов Иван Иванович')])
    income = step._extract_contractors(df_income, is_income=True,
                                       contractor_positions=positions)
    check(income['контрагент'].iloc[0] == 'Иванов Иван Иванович',
          "доходы берут контрагента из Субконто Дт_2")

    # Расходы: контрагент на стороне Кт, колонок Дт в кадре нет вовсе.
    df_expense = pd.DataFrame({
        'Корр.счет': pd.Series(['98.01'], dtype='string'),
        'Субконто Кт_2': pd.Series(['Поставщик ООО'], dtype='string'),
    })
    expense = step._extract_contractors(df_expense, is_income=False,
                                        contractor_positions=positions)
    check(expense['контрагент'].iloc[0] == 'Поставщик ООО',
          "расходы берут контрагента из Субконто Кт_2")
    check('Субконто Дт_1' not in df_expense.columns,
          "отсутствие колонок Дт не мешает разбору расходов")

