# -*- coding: utf-8 -*-
"""
Смоук: пауза при неактуальном справочнике (PAUSE_ON_REFERENCE_ERROR).

Сценарий «обновил справочник — продолжил с того же шага»: справочная ошибка
шага (ReferenceMismatchError и подвиды, MissingContractorError в строгом
режиме, MissingOSGroupError) не останавливает прогон, а ждёт Enter; после
подтверждения декоратор handle_pipeline_errors откатывает контекст к
состоянию ДО шага, перечитывает Справочники.xlsx (refresh_references) и
повторяет упавший шаг. Данные выгрузок не перечитываются — это весь смысл.

Что фиксирует:
  - пауза и повтор шага при ReferenceMismatchError (wait -> True);
  - откат контекста перед повтором (атрибуты + context.data);
  - счётчик пауз для титульного листа (REFERENCE_PAUSE_COUNT_KEY);
  - повторные паузы при повторных ошибках (несколько обновлений справочника);
  - «стоп» / флаг False / неинтерактив / без TTY — прежнее поведение [STOP]
    (пауза не делается, справочник не перечитывается);
  - ConvergenceError / MissingFilesError паузой НЕ обрабатываются;
  - ошибка при самом перечитывании обновлённого справочника -> честный [STOP]
    с новой причиной в __cause__.

Чистка устаревшей диагностики (проблема оказалась устранена правкой):
  - файл mismatch_* первой попытки удаляется после успешного повтора;
  - чужие и созданные ДО шага файлы папки (reference_scope_*,
    manual_corrections_*, mismatch_* прошлых шагов) не трогаются;
  - «стоп», повторная ошибка и занятый файл (PermissionError) оставляют
    диагностику на месте — получатель должен видеть нерешённую проблему;
  - ERROR «[STOP] Обработка остановлена» не попадает в сводку
    предупреждений, если прогон продолжился, и остаётся при реальном стопе.

Изоляция: реальные _INPUT_DATA / _REFERENCE_DATA не затрагиваются —
refresh_references подменяется, пауза — подменяется; тестируется контракт
декоратора, а не файловый ввод-вывод. Файлы диагностики пишутся в
временную папку вывода (temp_run_dir), реальная _OUTPUT_DATA не трогается.

Запуск: python -u _smoke_reference_pause.py
"""
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import REFERENCE_PAUSE_RESOLVED_KEY
from io_module.output_manager import configure_run, get_run_dir
from pipeline.base import ProcessingContext, Step
from pipeline.errors import (
    ConvergenceError,
    MissingFilesError,
    ProcessingStepError,
    ReferenceMismatchError,
)
from pipeline.executors import (
    REFERENCE_PAUSE_ENABLED_KEY,
    REFERENCE_PAUSE_COUNT_KEY,
    wait_for_reference_update,
)

PASSED = 0
FAILED = 0
_failed_messages: list[str] = []


def check(condition: bool, message: str) -> None:
    """Одна проверка с прогрессом (зелёная — счётчик, красная — счётчик)."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[OK] {message}")
    else:
        FAILED += 1
        _failed_messages.append(message)
        print(f"[FAIL] {message}")


def make_context(**kwargs) -> ProcessingContext:
    """Контекст с включённой паузой (как выставляет cli/main в интерактиве)."""
    context = ProcessingContext(
        company="Тест",
        segment="Собственность",
        period="202608",
        type_period="8 мес",
    )
    context.data[REFERENCE_PAUSE_ENABLED_KEY] = True
    context.tolerance_params = {"tolerance_balance": 5000}
    for key, value in kwargs.items():
        setattr(context, key, value)
    return context


# ─────────────────────────────────────────────────────────────────────────────
# Шаги-заглушки
# ─────────────────────────────────────────────────────────────────────────────

class _FailsNSteps(Step):
    """Бросает ReferenceMismatchError первые N вызовов, затем успех."""

    def __init__(self, fails: int = 1):
        super().__init__("step_fails_n")
        self.fails = fails
        self.calls = 0

    def _process(self, context):
        self.calls += 1
        if self.calls <= self.fails:
            raise ReferenceMismatchError(
                "нет записи", reference_name="Меппинг_бб",
            )
        return context


class _MutatingFailingStep(Step):
    """Мутирует таблицы и context.data, затем падает на справочнике."""

    def __init__(self):
        super().__init__("step_mutate")
        self.calls = 0
        self.seen_columns = None

    def _process(self, context):
        self.calls += 1
        if self.calls == 1:
            df = context.summary_osv_df.copy()
            df["мусорная_колонка"] = "x"
            context.summary_osv_df = df
            context.data["утечка"] = 1
            raise ReferenceMismatchError("test", reference_name="Меппинг_бб")
        self.seen_columns = list(context.summary_osv_df.columns)
        return context


class _OtherErrorStep(Step):
    """Бросает заданную ошибку (не справочную) — пауза не применяется."""

    def __init__(self, error: Exception):
        super().__init__("step_other_error")
        self._error = error

    def _process(self, context):
        raise self._error


class _FailsWithData(Step):
    """Падает на справочнике С problem_data (пишет файл в mismatches/) N раз."""

    def __init__(self, fails: int = 1):
        super().__init__("step_fails_data")
        self.fails = fails
        self.calls = 0

    def _process(self, context):
        self.calls += 1
        if self.calls <= self.fails:
            raise ReferenceMismatchError(
                "нет записи",
                problem_data=pd.DataFrame({
                    "счёт": ["60.01"],
                    "причина": ["нет строки в Меппинг_бб"],
                }),
                reference_name="Меппинг_бб",
            )
        return context


@contextmanager
def temp_run_dir():
    """Папка вывода прогона во временном каталоге (реальные файлы не трогаем)."""
    with tempfile.TemporaryDirectory(prefix="smoke_pause_out_") as tmp:
        with patch("io_module.output_manager.OUTPUT_DATA_DIR", Path(tmp)):
            configure_run()
            yield Path(get_run_dir())


def diagnostic_files(run_dir: Path) -> list[str]:
    """Имена диагностических файлов папки запуска (mismatches/)."""
    return sorted(path.name for path in (run_dir / "mismatches").glob("*.xlsx"))


def captured_messages(level: str = "WARNING") -> tuple[list, int]:
    """Sink того же вида, что в logger_config: собирает тексты указанного уровня.

    Нужен, чтобы проверить, что ERROR «[STOP] Обработка остановлена» не
    попадает в сводку предупреждений титульного листа у зелёного прогона.
    """
    messages: list[str] = []
    sink_id = logger.add(
        lambda m: messages.append(str(m)),
        level=level,
        format="{message}",
    )
    return messages, sink_id

# ─────────────────────────────────────────────────────────────────────────────
# Тесты
# ─────────────────────────────────────────────────────────────────────────────

def test_pause_and_retry_step() -> None:
    """ReferenceMismatchError + Enter -> шаг повторяется, справочник перечитан."""
    step = _FailsNSteps(fails=1)
    context = make_context()

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=True) as mock_wait, \
            patch("pipeline.executors.refresh_references") as mock_refresh:
        step.execute(context)

    check(step.calls == 2, f"шаг повторён после паузы (вызовов: {step.calls})")
    check(mock_wait.call_count == 1, "пауза запрошена ровно один раз")
    check(mock_refresh.call_count == 1, "справочники перечитаны после Enter")
    check(
        context.data.get(REFERENCE_PAUSE_COUNT_KEY) == 1,
        f"счётчик пауз на титульный лист = 1 (факт: {context.data.get(REFERENCE_PAUSE_COUNT_KEY)})",
    )
    statuses = [m["status"] for m in context.step_metrics]
    check(
        statuses == ["error", "ok"],
        f"метрики: сначала error (первый провал), потом ok (повтор): {statuses}",
    )


def test_context_rollback_before_retry() -> None:
    """Перед повтором контекст откатывается к состоянию до шага."""
    step = _MutatingFailingStep()
    original = pd.DataFrame({
        "счет": pd.Series(["60.01"], dtype="string"),
        "сальдо, тыс.ед.": [1.0],
    })
    context = make_context(summary_osv_df=original)

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=True), \
            patch("pipeline.executors.refresh_references"):
        step.execute(context)

    check(
        step.seen_columns == list(original.columns),
        f"повтор видит исходные колонки без мутаций шага (факт: {step.seen_columns})",
    )
    check(
        "утечка" not in context.data,
        f"context.data откатан: ключа «утечка» нет (факт: {sorted(context.data.keys())})",
    )
    # После успешного шага _clean_whitespace возвращает очищенную копию —
    # сравниваем содержимое, а не идентичность объекта.
    check(
        "мусорная_колонка" not in context.summary_osv_df.columns
        and context.summary_osv_df["счет"].tolist() == ["60.01"],
        f"context.summary_osv_df без мутаций шага (колонки: {list(context.summary_osv_df.columns)})",
    )


def test_two_pauses_multiple_updates() -> None:
    """Ошибка повторилась после первого обновления -> вторая пауза."""
    step = _FailsNSteps(fails=2)
    context = make_context()

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=True) as mock_wait, \
            patch("pipeline.executors.refresh_references") as mock_refresh:
        step.execute(context)

    check(step.calls == 3, f"шаг повторён после двух пауз (вызовов: {step.calls})")
    check(mock_wait.call_count == 2, f"пауз было две (факт: {mock_wait.call_count})")
    check(mock_refresh.call_count == 2, f"справочник перечитан дважды (факт: {mock_refresh.call_count})")
    check(
        context.data.get(REFERENCE_PAUSE_COUNT_KEY) == 2,
        f"счётчик пауз = 2 (факт: {context.data.get(REFERENCE_PAUSE_COUNT_KEY)})",
    )

def test_stop_aborts_like_before() -> None:
    """«стоп» в паузе — прежнее поведение: [STOP], справочник не перечитывается."""
    step = _FailsNSteps(fails=1)
    context = make_context()

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=False), \
            patch("pipeline.executors.refresh_references") as mock_refresh:
        try:
            step.execute(context)
        except ProcessingStepError as exc:
            check(
                isinstance(exc.__cause__, ReferenceMismatchError),
                f"прерывание даёт ProcessingStepError с исходной причиной (факт: {exc.__cause__!r})",
            )
        else:
            raise AssertionError("Ожидался ProcessingStepError при «стоп»")
    check(mock_refresh.call_count == 0, "справочник не перечитан после «стоп»")
    check(
        context.data.get(REFERENCE_PAUSE_COUNT_KEY, 0) == 0,
        "счётчик пауз не увеличился после «стоп»",
    )


def test_no_pause_when_flag_disabled() -> None:
    """PAUSE_ON_REFERENCE_ERROR=False — прежнее поведение без паузы."""
    step = _FailsNSteps(fails=1)
    context = make_context()

    with patch("pipeline.decorators.PAUSE_ON_REFERENCE_ERROR", False), \
            patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update") as mock_wait, \
            patch("pipeline.executors.refresh_references") as mock_refresh:
        try:
            step.execute(context)
        except ProcessingStepError:
            pass
        else:
            raise AssertionError("Ожидался ProcessingStepError при выключенной паузе")
    check(mock_wait.call_count == 0, "пауза не запрашивалась при выключенном флаге")
    check(mock_refresh.call_count == 0, "справочник не перечитывался при выключенном флаге")
    check(step.calls == 1, "шаг не повторялся при выключенном флаге")


def test_no_pause_in_non_interactive() -> None:
    """--no-interactive (флаг в context.data False) — прежнее поведение."""
    step = _FailsNSteps(fails=1)
    context = make_context()
    context.data[REFERENCE_PAUSE_ENABLED_KEY] = False

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.refresh_references") as mock_refresh:
        try:
            step.execute(context)
        except ProcessingStepError:
            pass
        else:
            raise AssertionError("Ожидался ProcessingStepError в неинтерактиве")
    check(mock_refresh.call_count == 0, "справочник не перечитывался в неинтерактиве")
    check(step.calls == 1, "шаг не повторялся в неинтерактиве")


def test_no_pause_without_tty() -> None:
    """Без TTY пауза не делается (фоновый запуск, редирект в файл)."""
    step = _FailsNSteps(fails=1)
    context = make_context()

    with patch("pipeline.executors._stdin_is_tty", return_value=False), \
            patch("pipeline.executors.refresh_references") as mock_refresh:
        try:
            step.execute(context)
        except ProcessingStepError:
            pass
        else:
            raise AssertionError("Ожидался ProcessingStepError без TTY")
    check(mock_refresh.call_count == 0, "справочник не перечитывался без TTY")
    check(step.calls == 1, "шаг не повторялся без TTY")


def test_convergence_error_not_paused() -> None:
    """ConvergenceError — расхождение данных, а не справочник: паузы нет."""
    step = _OtherErrorStep(
        ConvergenceError("расхождение с ОСВ", reference_name="выручка_против_ОСВ")
    )
    context = make_context()

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update") as mock_wait, \
            patch("pipeline.executors.refresh_references") as mock_refresh:
        try:
            step.execute(context)
        except ProcessingStepError:
            pass
        else:
            raise AssertionError("Ожидался ProcessingStepError для ConvergenceError")
    check(mock_wait.call_count == 0, "ConvergenceError не запрашивает паузу")
    check(mock_refresh.call_count == 0, "ConvergenceError не перечитывает справочник")


def test_missing_files_error_not_paused() -> None:
    """MissingFilesError — файлы выгрузок, а не справочник: паузы нет."""
    step = _OtherErrorStep(MissingFilesError("нет файла", missing_files=["a.xlsx"]))
    context = make_context()

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update") as mock_wait, \
            patch("pipeline.executors.refresh_references") as mock_refresh:
        try:
            step.execute(context)
        except ProcessingStepError:
            pass
        else:
            raise AssertionError("Ожидался ProcessingStepError для MissingFilesError")
    check(mock_wait.call_count == 0, "MissingFilesError не запрашивает паузу")
    check(mock_refresh.call_count == 0, "MissingFilesError не перечитывает справочник")


def test_refresh_failure_becomes_stop() -> None:
    """Обновлённый справочник сам невалиден -> честный [STOP] с новой причиной."""
    step = _FailsNSteps(fails=1)
    context = make_context()
    refresh_error = ReferenceMismatchError(
        "период в КомпанииГруппы не совпадает", reference_name="КомпанииГруппы",
    )

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=True), \
            patch("pipeline.executors.refresh_references", side_effect=refresh_error):
        try:
            step.execute(context)
        except ProcessingStepError as exc:
            check(
                exc.__cause__ is refresh_error,
                f"причина [STOP] — ошибка перечитывания обновлённого справочника (факт: {exc.__cause__!r})",
            )
        else:
            raise AssertionError("Ожидался ProcessingStepError при битом обновлённом справочнике")
    check(step.calls == 1, "шаг не повторялся: перечитывание упало раньше")


def test_wait_for_reference_update_answers() -> None:
    """Примитивы самой паузы: Enter — True, «стоп»/EOF — False."""
    context = make_context()

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("builtins.input", return_value=""):
        check(
            wait_for_reference_update(context) is True,
            "пустой Enter — продолжить (True)",
        )

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("builtins.input", return_value="стоп"):
        check(
            wait_for_reference_update(context) is False,
            "«стоп» — прервать (False)",
        )

    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("builtins.input", side_effect=EOFError):
        check(
            wait_for_reference_update(context) is False,
            "EOF (закрытый stdin) — прервать (False)",
        )

    # Мусорный ввод -> переспрос, затем Enter -> True
    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("builtins.input", side_effect=["qwe", ""]):
        check(
            wait_for_reference_update(context) is True,
            "мусорный ввод переспрашивается, Enter продолжает (True)",
        )

    # Флаг выключен -> False даже при TTY
    context.data[REFERENCE_PAUSE_ENABLED_KEY] = False
    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("builtins.input", return_value=""):
        check(
            wait_for_reference_update(context) is False,
            "флаг неинтерактива в context.data отключает паузу (False)",
        )


def test_stale_diagnostics_removed_after_resume() -> None:
    """Шаг прошёл после правки справочника — файл первой попытки удалён."""
    step = _FailsWithData(fails=1)
    context = make_context()

    with temp_run_dir() as run_dir, \
            patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=True), \
            patch("pipeline.executors.refresh_references"):
        step.execute(context)
        left = diagnostic_files(run_dir)

    check(step.calls == 2, f"шаг повторён после паузы (вызовов: {step.calls})")
    check(
        left == [],
        f"диагностика устранённой проблемы удалена из mismatches/ (осталось: {left})",
    )
    check(
        context.data.get(REFERENCE_PAUSE_RESOLVED_KEY) == 1,
        f"счётчик удалённой диагностики на титульный лист = 1 "
        f"(факт: {context.data.get(REFERENCE_PAUSE_RESOLVED_KEY)})",
    )


def test_foreign_diagnostics_survive_resume() -> None:
    """Чистка трогает только файлы этого шага: чужие и ранние остаются."""
    step = _FailsWithData(fails=1)
    context = make_context()
    kept = (
        "reference_scope_Тест_000000_000000.xlsx",
        "manual_corrections_баланс_Тест_000000_000000.xlsx",
        "missing_files_step_1b_000000_000000.xlsx",
        "mismatch_step_01_прочее_000000_000000.xlsx",
    )

    with temp_run_dir() as run_dir, \
            patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=True), \
            patch("pipeline.executors.refresh_references"):
        folder = run_dir / "mismatches"
        folder.mkdir(parents=True, exist_ok=True)
        for name in kept:
            (folder / name).write_text("заглушка", encoding="utf-8")
        step.execute(context)
        left = diagnostic_files(run_dir)

    check(
        left == sorted(kept),
        f"остались только чужие и созданные до шага файлы (факт: {left})",
    )


def test_diagnostics_kept_on_stop() -> None:
    """«стоп» в паузе: проблема не решена — диагностика остаётся."""
    step = _FailsWithData(fails=1)
    context = make_context()

    with temp_run_dir() as run_dir, \
            patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=False), \
            patch("pipeline.executors.refresh_references"):
        try:
            step.execute(context)
        except ProcessingStepError:
            pass
        else:
            raise AssertionError("Ожидался ProcessingStepError при «стоп»")
        left = diagnostic_files(run_dir)

    check(len(left) == 1, f"диагностика сохранена после «стоп» (факт: {left})")
    check(
        REFERENCE_PAUSE_RESOLVED_KEY not in context.data,
        "счётчик удалённой диагностики не вырос при «стоп»",
    )


def test_diagnostics_kept_when_retry_fails_again() -> None:
    """Справочник обновили, но шаг упал снова и пользователь ответил «стоп»."""
    step = _FailsWithData(fails=2)
    context = make_context()

    with temp_run_dir() as run_dir, \
            patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch(
                "pipeline.executors.wait_for_reference_update",
                side_effect=[True, False],
            ) as mock_wait, \
            patch("pipeline.executors.refresh_references"):
        try:
            step.execute(context)
        except ProcessingStepError:
            pass
        else:
            raise AssertionError("Ожидался ProcessingStepError после второго провала")
        left = diagnostic_files(run_dir)

    check(mock_wait.call_count == 2, f"было две паузы (факт: {mock_wait.call_count})")
    check(
        len(left) == 1,
        f"диагностика нерешённой проблемы осталась (факт: {left})",
    )


def test_locked_diagnostics_file_survives() -> None:
    """Файл открыт в Excel: удаление не удалось — прогон продолжается."""
    step = _FailsWithData(fails=1)
    context = make_context()

    with temp_run_dir() as run_dir, \
            patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=True), \
            patch("pipeline.executors.refresh_references"), \
            patch.object(Path, "unlink", side_effect=PermissionError("файл занят")):
        step.execute(context)
        left = diagnostic_files(run_dir)

    check(step.calls == 2, "занятый файл не сорвал повторный проход шага")
    check(
        len(left) == 1,
        f"занятый файл оставлен на месте (факт: {left})",
    )
    check(
        REFERENCE_PAUSE_RESOLVED_KEY not in context.data,
        "занятый файл не попал в счётчик удалённой диагностики",
    )


def test_stop_error_logged_only_on_real_stop() -> None:
    """[STOP] в сводке предупреждений — только когда прогон реально остановился."""
    stop_marker = "[STOP] Обработка остановлена"

    # Успешное возобновление: сводка предупреждений остаётся чистой.
    resumed_messages, sink_id = captured_messages()
    try:
        step = _FailsWithData(fails=1)
        context = make_context()
        with temp_run_dir(), \
                patch("pipeline.executors._stdin_is_tty", return_value=True), \
                patch("pipeline.executors.wait_for_reference_update", return_value=True) as mock_wait, \
                patch("pipeline.executors.refresh_references"):
            step.execute(context)
            reason = mock_wait.call_args.args[1]
    finally:
        logger.remove(sink_id)

    stop_lines = [line for line in resumed_messages if stop_marker in line]
    check(
        not stop_lines,
        f"после возобновления в сводке нет строки «{stop_marker}» (факт: {stop_lines})",
    )
    check(
        "step_fails_data" in reason and "Меппинг_бб" in reason,
        f"причина передана в блок паузы (факт: {reason!r})",
    )

    # Реальная остановка: прежнее поведение — ERROR в сводке остаётся.
    stopped_messages, sink_id = captured_messages()
    try:
        step = _FailsWithData(fails=1)
        context = make_context()
        with temp_run_dir(), \
                patch("pipeline.executors._stdin_is_tty", return_value=True), \
                patch("pipeline.executors.wait_for_reference_update", return_value=False), \
                patch("pipeline.executors.refresh_references"):
            try:
                step.execute(context)
            except ProcessingStepError:
                pass
            else:
                raise AssertionError("Ожидался ProcessingStepError при «стоп»")
    finally:
        logger.remove(sink_id)

    stop_lines = [line for line in stopped_messages if stop_marker in line]
    check(
        len(stop_lines) == 1,
        f"при реальной остановке строка «{stop_marker}» в сводке одна (факт: {stop_lines})",
    )


def main() -> None:
    test_pause_and_retry_step()
    test_context_rollback_before_retry()
    test_two_pauses_multiple_updates()
    test_stop_aborts_like_before()
    test_no_pause_when_flag_disabled()
    test_no_pause_in_non_interactive()
    test_no_pause_without_tty()
    test_convergence_error_not_paused()
    test_missing_files_error_not_paused()
    test_refresh_failure_becomes_stop()
    test_wait_for_reference_update_answers()
    test_stale_diagnostics_removed_after_resume()
    test_foreign_diagnostics_survive_resume()
    test_diagnostics_kept_on_stop()
    test_diagnostics_kept_when_retry_fails_again()
    test_locked_diagnostics_file_survives()
    test_stop_error_logged_only_on_real_stop()

    total = PASSED + FAILED
    label = "пауза при неактуальном справочнике (PAUSE_ON_REFERENCE_ERROR)"
    if FAILED:
        print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
        for line in _failed_messages:
            print(f"  [FAIL] {line}")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


if __name__ == "__main__":
    main()


