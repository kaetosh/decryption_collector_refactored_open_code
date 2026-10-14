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

Актуальность обновлённого справочника (регресс ТокПФ 14.10.2026):
  - повтор шага после паузы видит правку бухгалнера (шаг 5 читал меппинг из
    context.references, а шаги 4/11 — из снимка context.data['mapping'],
    сделанного шагом 1а до паузы, и падали с теми же позициями заново);
  - сопоставление счетов ОСВ со справочником пересобирается при паузе;
  - неоднозначность в обновлённом справочнике — честная ошибка, а не тихая;
  - пустой кадр частичных совпадений — штатное состояние, а не ошибка;
  - инвариант по исходникам: шаги не берут справочники из context.data.

Изоляция: реальные _INPUT_DATA / _REFERENCE_DATA не затрагиваются —
refresh_references подменяется, пауза — подменяется; тестируется контракт
декоратора, а не файловый ввод-вывод. Файлы диагностики пишутся в
временную папку вывода (temp_run_dir), реальная _OUTPUT_DATA не трогается.

Запуск: python -u _smoke_reference_pause.py
"""
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import REFERENCE_PAUSE_RESOLVED_KEY
from io_module.output_manager import configure_run, get_run_dir
from pipeline.base import ProcessingContext, Step
from pipeline.decorators import _restore_context
from pipeline.errors import (
    ConvergenceError,
    MissingContractorError,
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


def test_pause_text_points_at_right_file() -> None:
    """Инструкция паузы называет тот файл, который на самом деле надо править.

    Шаги ручных корректировок бросают ReferenceMismatchError про
    Правки.xlsx (reference_name), а не про Справочники.xlsx. Блок паузы,
    печатавший всегда «Внесите правки в файл Справочники.xlsx», уводил
    бухгалтера в другой файл — править Правки.xlsx пришлось бы вслепую.
    """
    from config.settings import CORRECTIONS_FILE_NAME, REFERENCE_DATA_FILE

    def pause_output(reference_name):
        buffer = StringIO()
        with patch("pipeline.executors._stdin_is_tty", return_value=True), \
                patch("builtins.input", side_effect=EOFError), \
                redirect_stdout(buffer):
            wait_for_reference_update(make_context(), "причина",
                                      reference_name=reference_name)
        return buffer.getvalue()

    reference_text = pause_output("Меппинг_бб")
    check(
        REFERENCE_DATA_FILE.name in reference_text
        and CORRECTIONS_FILE_NAME not in reference_text,
        "ошибка справочника — инструкция про Справочники.xlsx, без Правки.xlsx",
    )

    corrections_text = pause_output("Правки (ручные корректировки)")
    check(
        CORRECTIONS_FILE_NAME in corrections_text
        and f"1. Внесите правки в файл {CORRECTIONS_FILE_NAME}" in corrections_text,
        "ошибка ручных правок — инструкция называет Правки.xlsx",
    )
    check(
        "ручных корректировок" in corrections_text,
        "заголовок паузы про ручные корректировки, а не про справочник",
    )


def test_reason_prefix_by_error_type() -> None:
    """Текст причины паузы различается по типу ошибки, включая подклассы."""
    from pipeline.decorators import _reason_prefix
    from pipeline.errors import (
        MissingMappingError,
        MissingOSGroupError,
        ReferenceMismatchError,
    )

    check(
        "группы ОС" in _reason_prefix(MissingOSGroupError("x")),
        "MissingOSGroupError — причина про группы ОС",
    )
    check(
        "контрагент" in _reason_prefix(MissingContractorError("x")),
        "MissingContractorError — причина про контрагентов",
    )
    check(
        "справочникам" in _reason_prefix(ReferenceMismatchError("x")),
        "ReferenceMismatchError — причина про несоответствие справочникам",
    )
    check(
        "справочникам" in _reason_prefix(MissingMappingError("x")),
        "подкласс MissingMappingError находит префикс родителя по MRO",
    )


def test_snapshots_skipped_when_pause_disabled() -> None:
    """Без паузы шаг не платит за снимки контекста и обход папки mismatches/.

    Снимок нужен только откату перед повтором. Если пауза в этом запуске
    невозможна (--no-interactive либо PAUSE_ON_REFERENCE_ERROR=False),
    обход папки и копирование атрибутов на каждом шаге — чистые накладные
    расходы.
    """
    from pipeline.decorators import _snapshot_context

    with patch("pipeline.decorators._snapshot_context") as mock_snapshot, \
            patch.object(Step, "_snapshot_diagnostics", return_value=frozenset()):
        step = _FailsNSteps(fails=0)
        context = make_context()

        context.data[REFERENCE_PAUSE_ENABLED_KEY] = False
        step.execute(context)
        check(
            mock_snapshot.call_count == 0,
            f"при --no-interactive снимок не снимается (вызовов: {mock_snapshot.call_count})",
        )

        context.data[REFERENCE_PAUSE_ENABLED_KEY] = True
        step.execute(context)
        check(
            mock_snapshot.call_count == 1,
            f"при интерактивном запуске снимок снимается (вызовов: {mock_snapshot.call_count})",
        )

    # Штатный снимок откатывает атрибуты (база поведения не сломалась).
    ctx = make_context()
    ctx.summary_osv_df = pd.DataFrame({"счет": ["60.01"], "сумма": [1.0]})
    snap = _snapshot_context(ctx)
    ctx.summary_osv_df = ctx.summary_osv_df.assign(метка="стало")
    _restore_context(ctx, snap)
    check(
        "метка" not in ctx.summary_osv_df.columns,
        "штатный снимок по-прежнему откатывает замену таблицы",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Актуальность справочника после паузы (регресс ТокПФ 14.10.2026)
#
# Шаг 5 читал меппинг баланса из context.references и после паузы видел
# правку бухгалтера, а шаг 11 (и шаг 4) — из снимка context.data['mapping'],
# сделанного шагом 1а ДО паузы. Пользователь обновлял Меппинг_бб, а прогон
# снова останавливался на тех же позициях; полный перезапуск помогал, потому
# что шаг 1а перечитывал справочник заново.
# ─────────────────────────────────────────────────────────────────────────────

UNSPECIFIED = "не_указано"
DEBIT = "Дебиторская задолженность"
CREDIT = "Кредиторская задолженность"


def balance_mapping(rows: list[dict]) -> pd.DataFrame:
    """Меппинг_бб с полным набором ключевых колонок (как его ждёт resolve_company_view)."""
    return pd.DataFrame(rows).astype("string").assign(счет_фо="320010101")


def mapping_row(account: str, sub: str, debt: str, subtype: str) -> dict:
    return {
        "счет": account,
        "субконто": sub,
        "компания": "все",
        "вид_задолженности": debt,
        "подвид_задолженности": subtype,
        "группа_ос_аренды_лизинга": UNSPECIFIED,
        "долгая_короткая_часть": UNSPECIFIED,
        "сегмент_биоактивов_для_01_02": UNSPECIFIED,
        "вид_связи": "3 лица",
        "инвест_договор": "нет",
    }


def summary_osv(rows: list[dict]) -> pd.DataFrame:
    """Сводная ОСВ с целочисленным нулевым итогом (сходимость баланса — допуск шага)."""
    df = pd.DataFrame(rows)
    for col in ("счет", "субконто", "вид_задолженности"):
        df[col] = df[col].astype("string")
    return df


def company_directory(company: str, period: str) -> pd.DataFrame:
    """КомпанииГруппы с одной строкой — refresh_references повторяет валидацию компании."""
    from pipeline.constants import ColumnNames

    return pd.DataFrame([{
        ColumnNames.SHORT_COMPANY_NAME: company,
        ColumnNames.SEGMENT: "Собственность",
        ColumnNames.PERIOD_TYPE: "8 мес",
        ColumnNames.PERIOD_REPORT: period,
        ColumnNames.CURRENCY: "RUB",
    }]).astype("string")


@contextmanager
def resume_after_reference_update(updated_references: dict):
    """Пауза, Enter и НАСТОЯЩИЙ refresh_references с подменённой загрузкой файла.

    Подменяется только чтение Справочники.xlsx и лист «Параметры»: сам
    refresh_references (включая пересборку производных от справочника данных)
    выполняется по-настоящему.
    """
    with patch("pipeline.executors._stdin_is_tty", return_value=True), \
            patch("pipeline.executors.wait_for_reference_update", return_value=True), \
            patch("pipeline.executors._load_all_references", return_value=updated_references), \
            patch("pipeline.executors.load_params", return_value={"tolerance_balance": 5000}):
        yield


def test_retried_step_sees_updated_mapping() -> None:
    """Повтор шага после паузы видит обновлённый Меппинг_бб (регресс ТокПФ).

    Сценарий 1С: субконто «услуги охраны» по 60.01/60.02 классифицирован как
    дебиторская задолженность (остаток с минусом по кредиту), а в Меппинг_бб
    такой комбинации нет — шаг 5 падает MissingSubtypeError. Бухгалтер
    добавляет строки и жмёт Enter: повтор должен пройти.
    """
    from pipeline.steps.step_05_add_debt_subtype import Step5AddReceivableSubtypeStep

    stale = balance_mapping([
        mapping_row("60.01", "услуги охраны", CREDIT, "Торговая КЗ"),
        mapping_row("60.02", "услуги охраны", CREDIT, "Торговая КЗ"),
        mapping_row("60.01", "поставщик ООО", CREDIT, "Торговая КЗ"),
    ])
    fixed = balance_mapping([
        mapping_row("60.01", "услуги охраны", DEBIT, "Торговая ДЗ"),
        mapping_row("60.02", "услуги охраны", DEBIT, "Торговая ДЗ"),
        *[
            mapping_row(a, s, d, p)
            for a, s, d, p in (
                ("60.01", "услуги охраны", CREDIT, "Торговая КЗ"),
                ("60.02", "услуги охраны", CREDIT, "Торговая КЗ"),
                ("60.01", "поставщик ООО", CREDIT, "Торговая КЗ"),
            )
        ],
    ])
    osv = summary_osv([
        {"счет": "60.01", "субконто": "услуги охраны", "вид_задолженности": DEBIT, "сальдо, тыс.ед.": 750.0},
        {"счет": "60.02", "субконто": "услуги охраны", "вид_задолженности": DEBIT, "сальдо, тыс.ед.": 7640.2},
        {"счет": "60.01", "субконто": "поставщик ООО", "вид_задолженности": CREDIT, "сальдо, тыс.ед.": -8390.2},
    ])

    references = {
        "компании_группы": company_directory("Тест", "202608"),
        "меппинг_баланс": stale,
        "меппинг_опу": pd.DataFrame(),
    }
    updated = dict(references, меппинг_баланс=fixed)

    context = make_context()
    context.summary_osv_df = osv
    context.references = references
    context.data["osv_account_codes"] = ["60.01", "60.02"]
    step = Step5AddReceivableSubtypeStep()

    with temp_run_dir(), resume_after_reference_update(updated):
        step.execute(context)

    subtypes = {
        (account, sub): subtype
        for account, sub, subtype in zip(
            context.summary_osv_df["счет"].tolist(),
            context.summary_osv_df["субконто"].tolist(),
            context.summary_osv_df["подвид_задолженности"].tolist(),
        )
    }
    check(
        subtypes.get(("60.01", "услуги охраны")) == "Торговая ДЗ"
        and subtypes.get(("60.02", "услуги охраны")) == "Торговая ДЗ",
        f"повтор шага увидел добавленные бухгалтером подвиды: {subtypes}",
    )
    check(
        "mapping" not in context.data,
        "меппинг больше не хранится снимком в context.data (иначе он устареет при паузе)",
    )
    statuses = [m["status"] for m in context.step_metrics]
    check(
        statuses == ["error", "ok"],
        f"первый провал, затем успешный повтор: {statuses}",
    )
    check(
        not list(get_run_dir().glob("mismatches/mismatch_*.xlsx")),
        "диагностика первой попытки удалена после успешного повтора",
    )


def test_no_pause_run_is_unaffected() -> None:
    """Без паузы шаг работает как раньше: свежий меппинг, без лишних пересборок."""
    from pipeline.steps.step_05_add_debt_subtype import Step5AddReceivableSubtypeStep

    mapping = balance_mapping([
        mapping_row("60.01", "услуги охраны", DEBIT, "Торговая ДЗ"),
        mapping_row("60.01", "поставщик ООО", CREDIT, "Торговая КЗ"),
    ])
    context = make_context()
    context.summary_osv_df = summary_osv([
        {"счет": "60.01", "субконто": "услуги охраны", "вид_задолженности": DEBIT, "сальдо, тыс.ед.": 750.0},
        {"счет": "60.01", "субконто": "поставщик ООО", "вид_задолженности": CREDIT, "сальдо, тыс.ед.": -750.0},
    ])
    context.references = {"компании_группы": company_directory("Тест", "202608"), "меппинг_баланс": mapping}
    context.data[REFERENCE_PAUSE_ENABLED_KEY] = False

    Step5AddReceivableSubtypeStep().execute(context)
    check(
        [m["status"] for m in context.step_metrics] == ["ok"],
        "без паузы шаг проходит с первого раза и без повторов",
    )


def test_partial_matches_rebuilt_on_resume() -> None:
    """После паузы сопоставление счетов ОСВ пересобрано по новому Меппинг_бб.

    Шаги 4/11 читают результат сопоставления из context.data. Пока он был
    снимком шага 1а, правка справочника до них не доходила: счёт, добавленный
    бухгалтером, так и подставлялся бы по старому родительскому уровню.
    """
    osv_codes = ["68.22.2", "60.01"]
    stale_mapping = balance_mapping([
        mapping_row("68", "договор", UNSPECIFIED, UNSPECIFIED),
        mapping_row("60.01", "услуги охраны", DEBIT, "Торговая ДЗ"),
    ])
    fixed_mapping = balance_mapping([
        mapping_row("68.22.2", "договор", UNSPECIFIED, UNSPECIFIED),  # счёт появился как есть
        mapping_row("60.01", "услуги охраны", DEBIT, "Торговая ДЗ"),
    ])

    references = {
        "компании_группы": company_directory("Тест", "202608"),
        "меппинг_баланс": stale_mapping,
        "меппинг_опу": pd.DataFrame(),
    }
    updated = dict(references, меппинг_баланс=fixed_mapping)

    context = make_context()
    context.references = references
    context.data["osv_account_codes"] = osv_codes
    # Что записал бы шаг 1а до паузы:
    context.data["partially_matching_accounts_df"] = Step.build_partial_account_matches(
        osv_codes, stale_mapping,
    )
    before = dict(zip(
        context.data["partially_matching_accounts_df"]["Счет из ОСВ"],
        context.data["partially_matching_accounts_df"]["Совпадающие счета из справочника"],
    ))
    check(
        before == {"68.22.2": "68"},
        f"до паузы 68.22.2 сопоставлен с родителем: {before}",
    )

    from pipeline.executors import refresh_references

    with temp_run_dir(), resume_after_reference_update(updated):
        refresh_references(context)

    after_df = context.data["partially_matching_accounts_df"]
    check(
        after_df.empty,
        "добавленный бухгалтером счёт 68.22.2 перестал быть частичным совпадением",
    )
    check(
        list(after_df.columns) == ["Счет из ОСВ", "Совпадающие счета из справочника"],
        "пересобранный кадр сохраняет колонки (пустой кадр читается шагами 4/11)",
    )


def test_ambiguity_in_updated_mapping_stops_honestly() -> None:
    """Неоднозначность в ОБНОВЛЁННОМ справочнике — честная ошибка, а не тихий сюрприз.

    Ошибку поднимает пересборка сопоставления внутри refresh_references.
    Это ReferenceMismatchError — тот же класс, что и у шага, поэтому
    декоратор превращает её в понятный [STOP] с problem_data, а не в паузу
    и не в тихую подстановку произвольного счёта.
    """
    from pipeline.executors import refresh_references

    osv_codes = ["68.22.2"]
    stale_mapping = balance_mapping([mapping_row("68", "договор", UNSPECIFIED, UNSPECIFIED)])
    broken_mapping = balance_mapping([
        mapping_row("68.01", "договор", UNSPECIFIED, UNSPECIFIED),
        mapping_row("68.02", "договор", UNSPECIFIED, UNSPECIFIED),
    ])
    references = {
        "компании_группы": company_directory("Тест", "202608"),
        "меппинг_баланс": stale_mapping,
        "меппинг_опу": pd.DataFrame(),
    }
    context = make_context()
    context.references = references
    context.data["osv_account_codes"] = osv_codes
    context.data["partially_matching_accounts_df"] = Step.build_partial_account_matches(
        osv_codes, stale_mapping,
    )

    with temp_run_dir(), resume_after_reference_update(dict(references, меппинг_баланс=broken_mapping)):
        try:
            refresh_references(context)
            raised = None
        except ReferenceMismatchError as exc:
            raised = exc

    check(raised is not None, "неоднозначность под уровнем '68' поднимает ошибку справочника")
    check(
        type(raised) is ReferenceMismatchError,
        f"именно ReferenceMismatchError — декоратор даст [STOP], а не паузу: {type(raised)}",
    )
    check(
        getattr(raised, "reference_name", None) == "Меппинг_бб",
        f"в ошибке назван справочник Меппинг_бб (факт: {getattr(raised, 'reference_name', None)})",
    )
    problem_data = getattr(raised, "problem_data", None)
    check(
        isinstance(problem_data, pd.DataFrame) and "68.22.2" in problem_data["счет_осв"].tolist(),
        f"problem_data указывает проблемный счёт: {None if problem_data is None else problem_data.to_dict('records')}",
    )


def test_empty_partial_matches_are_readable() -> None:
    """Пустой кадр частичных совпадений — штатное состояние, а не ошибка контекста.

    Все счета ОСВ есть в справочнике как есть — сопоставлять нечего. Раньше
    шаги 4/11 брали кадр через get_df_from_context, который пустой кадр
    отвергал, и прогон компании без частичных совпадений падал с вводящей в
    заблуждение ошибкой «в контексте нет таблицы (пуста)».
    """
    from pipeline.classifiers import ReceivableClassifier

    empty = Step.build_partial_account_matches(["60.01"], balance_mapping([
        mapping_row("60.01", "услуги охраны", DEBIT, "Торговая ДЗ"),
    ]))
    context = make_context()
    context.data["partially_matching_accounts_df"] = empty

    read = _FailsNSteps(fails=0).get_partial_account_matches(context)
    check(read.empty, "пустой кадр частичных совпадений читается, а не отвергается")

    accounts = pd.DataFrame({"счет": pd.Series(["60.01"], dtype="string")})
    mapped = ReceivableClassifier.map_accounts_to_mapping(accounts, read)
    check(
        mapped["счет"].tolist() == ["60.01"],
        f"пустое сопоставление ничего не подменяет: {mapped['счет'].tolist()}",
    )

    missing_context = make_context()
    try:
        _FailsNSteps(fails=0).get_partial_account_matches(missing_context)
        raised = None
    except ValueError as exc:
        raised = exc
    check(
        raised is not None and "Шаг 1а" in str(raised),
        f"отсутствие кадра (шаг 1а не отработал) — внятная ошибка: {raised}",
    )


def test_steps_do_not_cache_reference_in_context_data() -> None:
    """Инвариант: шаги не читают справочники из снимков в context.data.

    Снимок reference в context.data не обновляется при паузе — это и был баг
    ТокПФ. Проверка по исходникам шагов, а не по кадру в памяти: вернуть
    чтение `context.data['mapping']` можно молча, и тесты поведения это
    поймают только в том прогоне, где справочник правят по ходу.
    """
    import re

    root = Path(__file__).parent
    # Обе формы прошлого кэша: прямую запись ключа и чтение через
    # get_df_from_context по тому же ключу.
    stale_pattern = re.compile(r"""data\[(['"])mapping\1\]|get_df_from_context\([^)]*['"]mapping['"]""")
    for relative in (
        "pipeline/steps/step_01a_list_registers.py",
        "pipeline/steps/step_04_add_debt_type.py",
        "pipeline/steps/step_11_split_60.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        found = stale_pattern.search(source)
        check(
            found is None,
            f"{relative}: меппинг не кэшируется в context.data"
            + (f" (найдено: {found.group(0)!r})" if found else ""),
        )
    check(
        "context.references['меппинг_баланс']" in (root / "pipeline/steps/step_11_split_60.py").read_text(encoding="utf-8"),
        "шаг 11 читает меппинг баланса из context.references",
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
    test_pause_text_points_at_right_file()
    test_reason_prefix_by_error_type()
    test_snapshots_skipped_when_pause_disabled()
    test_retried_step_sees_updated_mapping()
    test_no_pause_run_is_unaffected()
    test_partial_matches_rebuilt_on_resume()
    test_ambiguity_in_updated_mapping_stops_honestly()
    test_empty_partial_matches_are_readable()
    test_steps_do_not_cache_reference_in_context_data()

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


