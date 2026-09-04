# Руководство для ИИ-агента (AGENTS.md)

## Архитектура и запуск
* **Паттерн Pipeline:** шаги наследуются от `Step` (`pipeline/base.py:115`), реализуют только `_process(context)`. **НЕ переопределяйте `execute`** — обёрнут декоратором `handle_pipeline_errors` (`pipeline/decorators.py:47`).
* **Точка входа:** `main.py:1` -> `cli/main.py:38` (`main()` / `entry_point()`). Альтернатива: `python -m cli.main`. Аргументы: `cli/arguments.py:12`.
* **Фабрики:** `pipeline/factories.py:37` (`create_preparation_pipeline()`) и `:52` (`create_main_pipeline()`). Не хардкодьте шаги.
* **Оркестрация:** `pipeline/executors.py` — `initialize_context()`, `REFERENCE_REGISTRY`, паузы, `save_results()`. Сохранение: `io_module/data_io.py:490` + `io_module/output_manager.py:39`.
* **Контекст:** `ProcessingContext` (`pipeline/base.py:29`). Главные таблицы — `common_osv_df`, `summary_osv_df`, `journal_df`, `balance_df`, `pnl_df`. Промежуточные — в `context.data` (доступ через `Step.get_df_from_context()`). `context.run_id` = `ГГГГММДД_ЧЧММСС`.
* **Зависание на `input()` (КРИТИЧНО):** запуск без аргументов — интерактивный режим. Для CI/фона передавайте `-t`, `-v`, `--no-interactive`. Логика — `cli/main.py:121`.

## Автоматическая постобработка и валидация
При вызове `Step.execute()` (`base.py:124`) автоматически (не пишите ручной код):
1. `_validate_input(context)` — валидация входа
2. `_process(context)` — бизнес-логика (единственное место для кода шага)
3. `_clean_whitespace(context)` (`base.py:395`) — очистка пробелов в `summary_osv_df`/`journal_df`
4. `_move_and_sort_level_columns(context)` (`base.py:405`) — `Level_*` в конец по возрастанию
5. `_validate_output(context)` (`base.py:201`) — запрет `object`-колонок (иначе `TypeError`). Приводите через `cast_columns_to_types()` (`utils/dataframe_utils.py:13`)
6. Контроль сходимости баланса: если есть `сальдо, тыс.ед.`, сумма проверяется на `0` с допуском `tolerance_balance` (дефолт `config/defaults.py:12`). Для ОПУ-расходов — `_validate_against_osv()` в `base_expenses_step.py:451`.

Декоратор `handle_pipeline_errors` (`decorators.py:47`) пишет метрики в `context.step_metrics` и логирует сводку (`base.py:749`, DEBUG).

## Константы
* Общие — `pipeline/constants.py:10` (`ColumnNames`, `DataTypes`, `Prefixes`, `Values`). Импортируйте, не дублируйте.
* Бизнес-константы шагов — `pipeline/step_config.py:12` (`StepConstants`, `DebtTypeConstants`, `LeaseConstants`, `AccountConstants`, `OpuReportConstants`, `BalanceReportConstants`).

## Работа со справочниками и обработка ошибок
* **Справочники:** единая точка — `REFERENCE_REGISTRY` (`pipeline/executors.py:83`, `ReferenceSpec`). Загрузка -> `DataLoader.load_reference_data()` (`io_module/data_io.py:427`). Ключевые: `ПланСчетов`, `ПланСчетовБУ`, `Меппинг_бб`, `Меппинг_опу`, `КомпанииГруппы`, `Выгрузки`, `СправочникУФР`, `ВидСвязиКА`, `ППА`, `КредитОбслуж`, `ПрочиеДоходыНДС`, `ВидыРБП_АрендаЛизинг`, `Параметры`.
* **Допуски сходимости:** лист «Параметры» -> `load_params(context)` (`config/loader.py:14`) -> `context.tolerance_params`. Валидация по `SCHEMA` (`config/defaults.py:20`), fallback — `DEFAULTS`. Ключевые: `tolerance_balance` (5000), `tolerance_reconciliation` (1050), `tolerance_leased_os` (3000), `tolerance_pnl_balance` (1050), `tolerance_rate_deviation` (0.3).

| Исключение | Поведение |
|---|---|
| `MissingContractorError` | `STRICT_CONTRACTOR_CHECK=True` -> `ProcessingStepError`; `False` -> замена на `3 лица`, статус `soft` |
| `MissingCreditContractorError` (справочник КредитОбслуж, шаг 17) | `STRICT_CREDIT_CONTRACTOR_CHECK=True` -> отчёт в Excel + `ProcessingStepError`; `False` -> отчёт в Excel + замена на `3 лица`, шаг продолжается |
| `MissingOSGroupError` (справочник ППА, шаг 6) | `STRICT_OS_GROUP_CHECK=True` -> `ProcessingStepError`; `False` -> WARNING в лог, замена на `не_указано` внутри шага |
| `ReferenceMismatchError` и подвиды | сохранение `problem_data` в Excel (`_save_reference_mismatch_report()` -> `output_manager.py:88`), `ProcessingStepError` |
| `MissingFilesError` / `MissingCardError` | сохранение списка файлов, `ProcessingStepError` |
| `Exception` | обёртка в `ProcessingStepError` (`from e`) |

`STRICT_CONTRACTOR_CHECK = False` (`config/settings.py:48`) — мягкий режим. Реализация — `Step._apply_soft_contractor_handling()` (`base.py:652`).
`STRICT_CREDIT_CONTRACTOR_CHECK = True` (`config/settings.py:53`) — режим для справочника КредитОбслуж (шаг 17, `_process_credit_lines`). `True` (по умолчанию): при отсутствии РБП в справочнике — отчёт в Excel (mismatches/) + `ProcessingStepError`; `False`: отчёт в Excel + замена контрагента на `3 лица`, шаг продолжается. Исключение — `MissingCreditContractorError`.
`STRICT_OS_GROUP_CHECK = True` (`config/settings.py:49`) — строгий режим для групп ОС аренды/лизинга (шаг 6, справочник ППА). Проверяются: договоры/РБП, отсутствующие в ППА, и значения групп вне допустимого списка. При `False` — WARNING, замена на `не_указано` внутри шага (реализация — `_validate_mapping` и этапы 5/7 в `pipeline/steps/step_06_add_os_group.py`).

## Встроенные хелперы и утилиты
* Чтение Excel: `engine=\'openpyxl\''
* Заголовки из 1С: `utils.dataframe_utils.set_header_from_row(df, search_text)`
* Приведение типов: `utils.dataframe_utils.cast_columns_to_types(df, type_mapping)` (`utils/dataframe_utils.py:13`)
* Доступ к `context.data`: `Step.get_df_from_context(context, key, hint=\'\')` (`base.py:161`) — единственный корректный способ
* Нормализация счетов: `utils.column_utils.process_account(acc)` / `normalize_account(series)`
* Fuzzy Matching: `utils.text_utils.find_similar_companies(series_a, series_b)` (rapidfuzz)
* Логирование: `from loguru import logger` (`logging_handling/logger_config.py:40`). Консоль — INFO, `app.log` — DEBUG (перезаписывается).
* Пути вывода: только `io_module/output_manager.py:88` `get_output_dir(subfolder)` + `get_run_id()`. Не используйте `OUTPUT_DATA_DIR` напрямую.

## Структура пайплайна (Фаза 2)
Состав — `pipeline/factories.py:52`. Шаги 1b-19, 6 этапов:

| Этап | Шаги | Назначение |
|---|---|---|
| Загрузка и подготовка | 1b, 1c, 2 | Проверка файлов, реконциляция ОСВ, флэттенинг |
| Классификация (баланс) | 3-9 | Счета, тип/подвид задолженности, группы ОС, долгая/короткая часть, биоактивный, вид связи |
| Специальные расчёты | 10-12 | Источник аренды, разбиение 60 (инвест/не-инвест), разбиение 84 (НРП) |
| Сборка баланса | 13 | Расшифровка по маппингу ФО |
| Классификация (ОПУ) | 14-18 | Выручка/себестоимость, управленческие/коммерческие расходы, прочие доходы/расходы, налог |
| Сборка ОПУ | 19 | Маппинг на счета ФО, увязка ЧП = НРП |

**Ключевые шаги:**
- **1c** (`step_01c_reconcile_totals.py`) — реконциляция итогов ОСВ vs выгрузки, загрузка `journal_df`
- **14** (`step_14_build_opu_foundation.py`) — 90.01/90.02, распределение себестоимости, `transactions_all_df` в `context.data`
- **15-16** (`base_expenses_step.py`) — управленческие (90.08/26) и коммерческие (90.07/44) расходы
- **17** (`step_17_add_other_income_and_expenses.py`) — прочие доходы/расходы (91.01/91.02), ~1270 строк
- **18** (`step_18_add_task_and_other_movements.py`) — налог на прибыль (99), исключение реформации
- **19** (`step_19_build_opu.py`) — сборка ОПУ, увязка ЧП = НРП (`tolerance_pnl_balance`)

## ProcessingContext — структура данных
`ProcessingContext` (`pipeline/base.py:29`, `__repr__` — `base.py:90`):

| Поле | Тип | Описание |
|---|---|---|
| `company`, `segment`, `period`, `type_period` | `str` | Метаданные компании |
| `name_file_general_osv` | `str` | Имя файла общей ОСВ |
| `run_id` | `str` | Идентификатор запуска `ГГГГММДД_ЧЧММСС` |
| `common_osv_df` | DataFrame | Общая ОСВ (Фаза 0) |
| `summary_osv_df` | DataFrame | Сводная ОСВ (после Step 2) |
| `journal_df` | DataFrame | Проводки (Step 1c -> обновляется в 14/19) |
| `balance_df` | DataFrame | Расшифровка баланса (Step 13) |
| `pnl_df` | DataFrame | Расшифровка ОПУ (Step 19) |
| `references` | `dict[str, DataFrame]` | Справочники из `Справочники.xlsx` |
| `tolerance_params` | `dict[str, float]` | Допуски (`load_params()`) |
| `step_metrics` | `list[dict]` | Метрики шагов (`decorators.py:85`) |
| `data` | `dict[str, Any]` | Вспомогательное (читайте через `get_df_from_context()`) |

## Логирование и отладка
* Консоль — INFO, `app.log` — DEBUG (перезаписывается при старте)
* Сводка шагов — `Pipeline._log_step_summary()` (`base.py:749`, DEBUG): `Шаг 11 … ok 9.26 сек | строк: journal_df=12500`
* Проблемные данные — автоматически в `mismatches/` внутри папки запуска
* Трассировка — флаг `-t` или интерактивный `y` (`cli/arguments.py:19`). `__cause__` — первопричина (`cli/main.py:106`)

## Замечания для ИИ-агента
* Не используйте `OUTPUT_DATA_DIR` напрямую — только `get_output_dir()` / `get_run_id()`
* Не дублируйте константы — импортируйте из `pipeline/constants.py` / `pipeline/step_config.py`
* Для чтения `context.data` — только `get_df_from_context()`
* Для новой ошибки: класс в `pipeline/errors.py` + ветка в `pipeline/decorators.py`
* Проверяйте `py_compile` после правок — структура CLI/пайплайна хрупка к циклическим импортам

## Запланировано
Текущие и завершённые задачи — в `TASKS.md`. Активных задач нет.
