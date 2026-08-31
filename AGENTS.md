# Руководство для ИИ-агента (AGENTS.md)

## Архитектура и запуск
* **Паттерн Pipeline (Конвейер):** Все шаги наследуются от `Step` (`pipeline/base.py:115`) и реализуют только метод `_process(context)`. **НЕ переопределяйте `execute`** — он обёрнут декоратором `handle_pipeline_errors` (`pipeline/decorators.py:47`).
* **Точка входа:** `main.py:1` — тонкий лаунчер для совместимости (`python main.py`). Вся оркестрация — в `cli/main.py:38` (`main()` / `entry_point()`). Альтернативный запуск: `python -m cli.main`. Аргументы CLI — в `cli/arguments.py:12` (`parse_arguments()`, `ask_user_about_traceback()`).
* **Фабрики пайплайнов:** Состав конвейера задаётся в `pipeline/factories.py:37` (`create_preparation_pipeline()`) и `pipeline/factories.py:52` (`create_main_pipeline()`). Не хардкодьте список шагов в `main.py`/`cli/main.py`.
* **Оркестрация фаз:** Инициализация контекста, реестр справочников и паузы — в `pipeline/executors.py` (`initialize_context()`, `REFERENCE_REGISTRY`, `pause_for_osv_general_export()`, `pause_for_1c_export()`, `save_results()`). Сохранение — через `io_module/data_io.py:490` (`DataLoader`/`DataSaver`) и `io_module/output_manager.py:39`.
* **Контекст данных:** Состояние передаётся через `ProcessingContext` (`pipeline/base.py:29`). Главные таблицы — `common_osv_df` (общая ОСВ с Фазы 0), `summary_osv_df` (сводная ОСВ после Step 2), `journal_df` (проводки, с Step 1c), `balance_df` (Step 13), `pnl_df` (Step 19). Промежуточные таблицы — в `context.data` (доступ через `Step.get_df_from_context()`). Идентификатор запуска — `context.run_id` (ГГГГММДД_ЧЧММСС, совпадает с именем папки вывода), метрики шагов — `context.step_metrics`.
* **Запуск и зависание на `input()` (КРИТИЧНО):** При запуске без аргументов (F5 в IDE) приложение работает интерактивно: на Фазе 0 ждёт Enter после выкладки общей ОСВ (`executors.py:26`), на Фазе 1 — после выгрузки регистров (`executors.py:47`), плюс один вопрос `y/n` про трассировку (`cli/arguments.py:42`). Чтобы избежать зависания в фоне/CI/без TTY, передавайте аргументы CLI (`-t`, `-v`, `--no-interactive`). Логика выбора режима — `cli/main.py:121` (`entry_point()`).

## Автоматическая постобработка и валидация в базовом классе
Каждый шаг при вызове `Step.execute()` (`pipeline/base.py:124`, декоратор `pipeline/decorators.py:47`) автоматически выполняет цепочку. *Не пишите для них ручной код внутри своих шагов:*

1. **Валидация входа** — `_validate_input(context)` (переопределяется при необходимости).
2. **Бизнес-логика** — `_process(context)` (единственное место для кода шага).
3. **Очистка пробелов** — `_clean_whitespace(context)` (`base.py:395`) удаляет лишние пробелы во всех строковых колонках `summary_osv_df` / `journal_df`.
4. **Сортировка Level-колонок** — `_move_and_sort_level_columns(context)` (`base.py:405`) — колонки `Level_*` (регистронезависимо) в конец и по возрастанию номера.
5. **Строгий контроль типов на выходе** — `_validate_output(context)` (`base.py:201`): в `common_osv_df`/`summary_osv_df`/`journal_df` не должно остаться колонок `object` (иначе `TypeError`). Приводите типы через `utils.dataframe_utils.cast_columns_to_types()` (`utils/dataframe_utils.py:13`).
6. **Контроль сходимости баланса** — там же: если есть `сальдо, тыс.ед.` (`pipeline/constants.py:12`), сумма проверяется на `0` с допуском `context.tolerance_params['tolerance_balance']` (лист «Параметры», дефолт — `config/defaults.py:12`). При превышении — `ValueError`. Для ОПУ-шагов расходов — отдельная проверка через `_validate_against_osv()` в `pipeline/steps/base_expenses_step.py:451`.

Дополнительно декоратор `handle_pipeline_errors` (`decorators.py:47`) после каждого шага (включая мягкий режим) пишет метрику в `context.step_metrics` (`status`: `ok`/`soft`/`error`, `duration_sec`, `rows`) и логирует сводку в `Pipeline._log_step_summary()` (`base.py:749`, уровень DEBUG — видно в `app.log`, не в консоли).

## Константы
* **Общие константы** — `pipeline/constants.py:10` (`ColumnNames`, `DataTypes`, `Prefixes`, `Values`). Импортируйте оттуда, не дублируйте строки типа `сальдо, тыс.ед.`, `level_`, `не_указано`.
* **Бизнес-константы шагов** — `pipeline/step_config.py:12` (`StepConstants`, `DebtTypeConstants`, `LeaseConstants`, `AccountConstants`, `OpuReportConstants`, `BalanceReportConstants`). Примеры: `StepConstants.THIRD_PARTY`, `LeaseConstants.TOLERANCE_76`, `AccountConstants.CONVERGENCE_TOLERANCE_84`.

## Работа со справочниками и обработка ошибок
* **Справочники:** Хранятся в `_REFERENCE_DATA/Справочники.xlsx`. Единая точка правды — `REFERENCE_REGISTRY` в `pipeline/executors.py:83` (`ReferenceSpec` + `REFERENCE_REGISTRY`). Загрузка — `_load_all_references()` -> `DataLoader.load_reference_data()` (`io_module/data_io.py:427`), очистка пробелов через `Step.clean_whitespace()`. Инициализация — `initialize_context()` (`executors.py:300`) вызывается в `cli/main.py:64`.
  - `ПланСчетов` (`план_счетов_фо`) — план счетов ФО (РСБУ Код отчетности, Итоговый номер счета)
  - `ПланСчетовБУ` (`план_счетов_бу`) — план счетов БУ (компания, код, наименование, субконто 1-3)
  - `Меппинг_бб` (`меппинг_баланс`) — маппинг баланса (вид/подвид задолженности, группы ОС, вид связи)
  - `Меппинг_опу` (`меппинг_опу`) — маппинг ОПУ (доход_расход, вид_дохода_расхода, сегмент, вид_связи)
  - `КомпанииГруппы` (`компании_группы`) — метаданные компаний (имя файла расшифровки, сегмент, тип_периода)
  - `Выгрузки` (`выгрузки`) — настройки регистров для экспорта из 1С
  - `СправочникУФР` (`справочник_уфр`) — сегменты по номенклатурным группам
  - `ВидСвязиКА` (`вид_связи_ка`) — группы контрагентов для вида связи
  - `ППА` (`справочник_ппа`) — ППА (IFRS 16): группы ОС, виды взаиморасчетов, контрагенты, договоры
  - `КредитОбслуж` (`кредит_обслуж`) — кредитные линии
  - `ПрочиеДоходыНДС` (`прочие_доходы_ндс`) — прочие доходы НДС
  - `ВидыРБП_АрендаЛизинг` (`виды_рбп_аренда_лизинг`) — виды РБП аренды/лизинга
  - `Параметры` (`параметры`) — допуски сходимости (лист **необязательный**: `required=False`, при отсутствии используются дефолты из `config/defaults.py`; см. подраздел ниже)

### Параметры допусков сходимости (лист «Параметры»)
Допуски вынесены в лист «Параметры» и **не задаются в коде**. Механизм:
- **Загрузка:** `config/loader.py:14` (`load_params(context)`) читает `context.references['параметры']` и возвращает `dict` -> `context.tolerance_params` (вызывается в `cli/main.py:66` сразу после `initialize_context()`). При старте логируются фактические допуски (`cli/main.py:79`, описания — `config/defaults.py:27` `TOLERANCE_DESCRIPTIONS`).
- **Колонки листа:** `параметр`, `описание`, `ед. изм.`, `тип данных значения`, `значение`.
- **Валидация:** каждый параметр сверяется со `SCHEMA` в `config/defaults.py:20` (тип, `min`/`max`, `nullable`). Неизвестные — warning и пропуск; невалидные — warning и дефолт.
- **Fallback:** лист отсутствует/пуст/ошибка — `DEFAULTS` из `config/defaults.py:12`.
- **Доступ:** только `context.tolerance_params['<имя>']`. Новые допуски — в справочник + при необходимости в `SCHEMA`/`DEFAULTS`/`TOLERANCE_DESCRIPTIONS`.

Параметры и где применяются (дефолты):
- `tolerance_balance` (`5000.0`) — Актив = Пассив. `base.py:250` + `step_13`.
- `tolerance_reconciliation` (`1050.0`) — регистры vs Общая ОСВ. `step_01c`, `step_14`, `base_expenses_step`.
- `tolerance_leased_os` (`3000.0`) — арендованные ОС (01.03/02.03 vs Ведомость амортизации). `step_10`.
- `tolerance_pnl_balance` (`1050.0`) — ОПУ vs Баланс (ЧП = НРП). `step_19`.

*Примечание:* в шагах 6/11/12 ещё внутренние допуски `TOLERANCE_76`, `CONVERGENCE_TOLERANCE_84` (100 000) — в справочник не вынесены, см. `pipeline/step_config.py:44`.

* **Кастомные исключения пайплайна (`pipeline/errors.py:13`):**
  - `PipelineError` — базовое. `InputDataError` -> `MissingFilesError` / `MissingCardError` — проблемы входных данных.
  - `ReferenceMismatchError` — несоответствие справочникам (проблемные строки сохраняются в Excel в `_OUTPUT_DATA/run_<run_id>/mismatches/`). Подклассы: `MissingMappingError`, `MissingContractorError`, `MissingOSGroupError`, `MissingSubtypeError`, `ConvergenceError`.
  - `ProcessingStepError` (`errors.py:24`, ранее был в `base.py`) — обёртка сбоя шага; создаётся декоратором `handle_pipeline_errors`, оригинал — в `__cause__`. Не бросайте его вручную из шагов — бросайте доменные исключения.

* **Обработка ошибок (декоратор `handle_pipeline_errors`, `pipeline/decorators.py:47`):**

  | Исключение | Поведение |
  |---|---|
  | `MissingContractorError` | `STRICT_CONTRACTOR_CHECK=True` -> лог `error` + `ProcessingStepError`; `False` -> сохранение отчёта + замена на `replacement_value` (`3 лица`), статус `soft`, шаг продолжается |
  | `ReferenceMismatchError` и подвиды | сохранение `problem_data` в Excel (`Step._save_reference_mismatch_report()` -> `io_module/output_manager.py:88` `get_output_dir('mismatches')`), лог `error`, `ProcessingStepError` |
  | `MissingFilesError` / `MissingCardError` | сохранение списка файлов (`_save_missing_files_report()`), лог `error`, `ProcessingStepError` |
  | `Exception` | лог `error`, оборачивание в `ProcessingStepError` (`from e` — цепочка сохраняется) |

  `Pipeline.run()` (`base.py:713`) дополнительно логирует сводку шагов при сбое и пробрасывает `ProcessingStepError`. Для нового типа ошибки: добавьте класс в `errors.py` и ветку в `decorators.py` (см. `README.md:112`).

* **Мягкий режим для контрагентов:** `config/settings.py:48` `STRICT_CONTRACTOR_CHECK = False` — неизвестные контрагенты заменяются на `3 лица` (столбец из `error.target_column`) вместо падения. Реализация — `Step._apply_soft_contractor_handling()` (`base.py:652`).

## Встроенные хелперы и утилиты
*Не изобретайте велосипед, используйте готовое:*
* **Чтение Excel:** всегда `engine='openpyxl'`.
* **Определение заголовков из 1С:** `utils.dataframe_utils.set_header_from_row(df, search_text)` (`utils/dataframe_utils.py`).
* **Приведение типов:** `utils.dataframe_utils.cast_columns_to_types(df, type_mapping)` (`utils/dataframe_utils.py:13`) — используйте перед `_validate_output`, чтобы не остаться с `object`.
* **Доступ к таблицам контекста:** `Step.get_df_from_context(context, key, hint='')` (`pipeline/base.py:161`) — достаёт `context.data[key]` и проверяет наличие/непустоту с понятным `ValueError`. Используйте вместо ручного `context.data.get()` + `if None/empty`. Применяется в `step_04`, `step_11`, `step_14`, `step_17`, `step_18`, `base_expenses_step`.
* **Нормализация счетов:** `utils.column_utils.process_account(acc)` (скалярная) / `normalize_account(series)` (векторизованная, 5 символов для 90/91). (`utils/column_utils.py`).
* **Fuzzy Matching:** `utils.text_utils.find_similar_companies(series_a, series_b)` (`utils/text_utils.py`, `rapidfuzz`) — используется в `step_11a`.
* **Логирование:** только `from loguru import logger` (`logging_handling/logger_config.py:40` `setup_logger()`). Консоль — `INFO`, файл `app.log` — `DEBUG` (перезаписывается при каждом запуске). Уровни `ERROR`/`CRITICAL` не обрезаются (`logger_config.py:33`).
* **Пути вывода:** никогда не используйте `OUTPUT_DATA_DIR` напрямую для записи результатов шага. Используйте `io_module/output_manager.py:88` `get_output_dir(subfolder)` и `get_run_id()` — хвост имён файлов `_<run_id>` совпадает с папкой запуска. Ручной `Path(OUTPUT_DATA_DIR) / ...` и `datetime.now()` в шагах — антипаттерн (см. правки `step_07`, `step_11`).

## Структура проекта
```
├── main.py                     # тонкий лаунчер -> cli/main.py  (совместимость: python main.py)
├── cli/
│   ├── arguments.py            # argparse-флаги (-t/-v/--no-interactive) + интерактивный вопрос
│   └── main.py                 # main(): Фаза 0 -> Фаза 1 -> пауза -> Фаза 2 -> save_results
├── pipeline/
│   ├── base.py                 # Step, ProcessingContext, Pipeline (+ _log_step_summary)
│   ├── decorators.py           # handle_pipeline_errors — единая обработка ошибок + метрики
│   ├── errors.py               # иерархия исключений (включая ProcessingStepError)
│   ├── constants.py            # ColumnNames, DataTypes, Prefixes, Values
│   ├── step_config.py          # бизнес-константы шагов (DebtTypeConstants, LeaseConstants …)
│   ├── factories.py            # create_preparation_pipeline / create_main_pipeline
│   ├── executors.py            # REFERENCE_REGISTRY, initialize_context, паузы, save_results
│   ├── classifiers/            # ReceivableClassifier
│   └── steps/                  # 1a–19 + base_expenses_step
├── data_processors/            # парсеры выгрузок 1С (osv_general.py: fix find_column_index — .eq() безопасен для pd.NA)
├── io_module/
│   ├── data_io.py              # DataLoader / DataSaver (save_combined_report — 4 листа)
│   └── output_manager.py       # run_id / run_dir / get_output_dir / cleanup_old_runs (KEEP_LAST_RUNS)
├── config/
│   ├── settings.py             # пути, REFERENCE_CONFIGS, STRICT_CONTRACTOR_CHECK, KEEP_LAST_RUNS, LOG_LEVEL
│   ├── defaults.py             # DEFAULTS / SCHEMA / TOLERANCE_DESCRIPTIONS
│   └── loader.py               # load_params()
├── utils/                      # column_utils, dataframe_utils, text_utils, file_utils
└── logging_handling/           # logger_config.py — консоль INFO, app.log DEBUG
```

## Структура пайплайна (6 этапов, 21 шаг)
Приложение — 3 фазы (`cli/main.py:38`):
1. **Фаза 0:** `pause_for_osv_general_export()` -> `initialize_context()` (общая ОСВ + справочники + `load_params()`)
2. **Фаза 1:** `create_preparation_pipeline()` (Step 1a) -> `pause_for_1c_export()` — ручная выгрузка из 1С
3. **Фаза 2:** `create_main_pipeline()` (Steps 1b–19, 6 этапов)

Состав Фазы 2 задаётся в `pipeline/factories.py:52`:

### Этап 1: Загрузка и подготовка данных (баланс + ОПУ)
| Шаг | Файл | Описание |
|-----|------|----------|
| 1b | `step_01b_verify_files.py` | Проверка наличия файлов выгрузок |
| 1c | `step_01c_reconcile_totals.py` | Реконциляция итогов ОСВ vs выгрузки; загружает `journal_df` для всех шагов ОПУ |
| 2 | `step_02_flat_osv.py` | Флэттенинг сводной ОСВ |

### Этап 2: Добавление классификационных столбцов (баланс)
| Шаг | Файл | Описание |
|-----|------|----------|
| 3 | `step_03_add_account.py` | deepest Level_ -> `счет` |
| 4 | `step_04_add_debt_type.py` | ДЗ/КЗ через `ReceivableClassifier` |
| 5 | `step_05_add_debt_subtype.py` | Подвид задолженности |
| 6 | `step_06_add_os_group.py` | Группы ОС аренды/лизинга |
| 7 | `step_07_add_long_short.py` | Долгая/короткая часть |
| 8 | `step_08_add_bioactive.py` | Биоактивный сегмент (01/02) |
| 9 | `step_09_add_related_party.py` | Вид связи |

### Этап 3: Специальные расчёты (баланс)
| Шаг | Файл | Описание |
|-----|------|----------|
| 10 | `step_10_classify_lease.py` | Источник аренды (ГСК/ГАП) |
| 11 | `step_11_split_60.py` | Разбиение 60 на инвест/не-инвест |
| 11a | `step_11a_check_contractor_similarity.py` | Fuzzy-проверка контрагентов (неблокирующий) |
| 12 | `step_12_split_84.py` | Разбиение 84 (НРП) |

### Этап 4: Сборка баланса
| Шаг | Файл | Описание |
|-----|------|----------|
| 13 | `step_13_build_balance.py` | Сборка расшифровки баланса по маппингу ФО; `_finalize_columns()` удаляет тех. столбцы плана счетов и переносит `Отчетность`/`Статья отчетности` в конец |

### Этап 5: Добавление классификационных столбцов (ОПУ)
| Шаг | Файл | Описание |
|-----|------|----------|
| 14 | `step_14_build_opu_foundation.py` | Основа ОПУ: 90.01/90.02, распределение себестоимости |
| 15 | `step_15_add_admin_expenses_to_opu.py` | Управленческие расходы (90.08/26) — `StepAddExpensesToOpuBase` |
| 16 | `step_16_add_comm_expenses_to_opu.py` | Коммерческие расходы (90.07/44) — `StepAddExpensesToOpuBase` |
| 17 | `step_17_add_other_income_and_expenses.py` | Прочие доходы/расходы (91.01/91.02) |
| 18 | `step_18_add_task_and_other_movements.py` | Налог на прибыль и прочие движения (99) |

### Этап 6: Сборка ОПУ
| Шаг | Файл | Описание |
|-----|------|----------|
| 19 | `step_19_build_opu.py` | Сборка ОПУ, увязка ЧП = НРП (`tolerance_pnl_balance`) |

## Ключевые шаги — подробное описание
### Step 1c — Реконциляция (`step_01c_reconcile_totals.py`)
Сверяет обороты/остатки общей ОСВ и выгрузок. При расхождении > `tolerance_reconciliation` — `ReferenceMismatchError`. Загружает `journal_df` в контекст.

### Step 14 — Основа ОПУ (`step_14_build_opu_foundation.py`)
90.01 (выручка) + 90.02 (себестоимость), распределение себестоимости пропорционально выручке. Обогащение через `меппинг_опу`/`справочник_уфр`/`компании_группы`. Сохраняет `transactions_all_df` в `context.data` (читается через `get_df_from_context()` в 15–18). Результат — `context.journal_df`.

### Steps 15-16 — Расходы ОПУ (`pipeline/steps/base_expenses_step.py`)
Тонкие обёртки над `StepAddExpensesToOpuBase` (`account_opu`, `account_accumulation`, `opu_line_name`). Валидация сходимости с ОСВ — `_validate_against_osv()` (`tolerance_reconciliation`).

### Step 17 — Прочие доходы/расходы (`step_17_add_other_income_and_expenses.py`)
~1270 строк: продажа активов (НДС, контрагенты, осиротевшие расходы), кредитные линии (`КредитОбслуж`), проценты, изменение условий ППА (`ППА`), `группа_ка`/`сегмент_ка`/`вид_связи`.

### Step 18 — Налог и прочие движения (`step_18_add_task_and_other_movements.py`)
Проводки 99, исключение реформации (90/91/84/99), выделение налога (99 vs 68).

### Step 19 — Сборка ОПУ (`step_19_build_opu.py`)
Маппинг на счета ФО через `меппинг_опу`, контроль увязки ЧП vs НРП (`tolerance_pnl_balance`). Результат — `context.pnl_df`.

## ProcessingContext — структура данных
`ProcessingContext` (`pipeline/base.py:29`, `__repr__` — `base.py:90`) — dataclass через весь пайплайн:

| Поле | Тип | Описание |
|------|-----|----------|
| `company` | `str` | Сокращённое наименование компании |
| `segment` | `str` | Сегмент компании |
| `period` | `str` | Период (год) |
| `type_period` | `str` | Тип периода (из справочника) |
| `name_file_general_osv` | `str` | Имя файла общей ОСВ |
| `run_id` | `str` | Идентификатор запуска `ГГГГММДД_ЧЧММСС` (`output_manager.py:34`, совпадает с `run_<run_id>`) |
| `common_osv_df` | `DataFrame` | Общая ОСВ (Фаза 0) |
| `summary_osv_df` | `DataFrame` | Сводная ОСВ после Step 2 |
| `journal_df` | `DataFrame` | Проводки (Step 1c -> обновляется в 14/19) |
| `balance_df` | `DataFrame` | Расшифровка баланса (Step 13) |
| `pnl_df` | `DataFrame` | Расшифровка ОПУ (Step 19) |
| `references` | `dict[str, DataFrame]` | Справочники из `Справочники.xlsx` |
| `tolerance_params` | `dict[str, float]` | Допуски (`load_params()` в `cli/main.py:66`) |
| `step_metrics` | `list[dict]` | Метрики шагов (`decorators.py:85`, `record_step()` `base.py:64`, сводка `base.py:749`) |
| `data` | `dict[str, Any]` | Вспомогательное (`expected_filenames`, `transactions_all_df`, `mapping` …) — читайте через `get_df_from_context()` |

Хелпер `Step.get_df_from_context(context, key, hint='')` (`base.py:161`) — единственный корректный способ достать таблицу из `context.data`.

## Выводные данные
Каждый запуск пишет в `_OUTPUT_DATA/run_<run_id>/` (`io_module/output_manager.py:39` `configure_run()` / `get_run_dir()` / `get_output_dir()`). Внутри — комбинированный отчёт (`DataSaver.save_combined_report()` — листы `Расшифровка_ББЛ`/`исходники ББЛ`/`Расшифровка_ОПУ`/`исходники ОПУ`), `Выгрузить_<компания>_<период>.xlsx`, `mismatches/` и `warnings/` (хвост имён — `<run_id>`).

Хранение: `config/settings.py:91` `KEEP_LAST_RUNS` (1–5, дефолт 5, clamp в `output_manager.py:121`). При старте `cleanup_old_runs()` (`cli/main.py:57`) удаляет только папки `run_*` сверх лимита; остальные файлы `_OUTPUT_DATA` не трогает. Занятая папка (файл открыт в Excel) — `PermissionError` -> warning, удалится позже.

## Логирование и отладка
* **Консоль** — `INFO` (`logging_handling/logger_config.py:40`), формат — время/уровень/сообщение.
* **`app.log`** — `DEBUG`, перезаписывается при каждом запуске (`logger_config.py:63`), формат — время/уровень/модуль/функция/строка/сообщение. `ERROR`/`CRITICAL` не обрезаются.
* **Сводка шагов** — `Pipeline._log_step_summary()` (`base.py:749`, `DEBUG` в конце и при падении): `Шаг 11 … ok 9.26 сек | строк: journal_df=12500, data.mapping=412` (источник — `decorators.py:85` `step_metrics`).
* **Проблемные данные** — при `ReferenceMismatchError`/`MissingFilesError` автоматически в `mismatches/` внутри папки запуска.
* **Трассировка** — флаг `-t`/`--traceback` (`cli/arguments.py:19`) или интерактивный `y` (`cli/main.py:121`). В `cli/main.py:106` показывается `__cause__` (первопричина).

## Статус реализации ОПУ
**Пайплайн ОПУ полностью реализован (Шаги 14-19):**
1. Step 14 — Выручка/Себестоимость (90.01/90.02)
2. Step 15 — Управленческие расходы (90.08) — `StepAddExpensesToOpuBase`
3. Step 16 — Коммерческие расходы (90.07) — `StepAddExpensesToOpuBase`
4. Step 17 — Прочие доходы/расходы (91.01/91.02)
5. Step 18 — Налог на прибыль (99)
6. Step 19 — Сборка ОПУ

### Процесс разработки новых шагов ОПУ
1. Пользователь пишет код шага (наследник `Step`, только `_process`) без оглядки на архитектуру.
2. После блока — рефакторинг LLM Qwen.
3. Твоя (агента OpenCode) зона ответственности:
   - **Полный рефакторинг проекта** (по запросу)
   - **Фичи вне бизнес-логики** (ошибки, логирование, CLI/GUI, настройки, вывод)

## Замечания для ИИ-агента
* Не используйте `OUTPUT_DATA_DIR` напрямую — только `get_output_dir()` / `get_run_id()`.
* Не дублируйте константы — импортируйте из `pipeline/constants.py` / `pipeline/step_config.py`.
* Для чтения `context.data` — только `get_df_from_context()`.
* Для новой ошибки: класс в `pipeline/errors.py` + ветка в `pipeline/decorators.py`.
* Проверяйте `py_compile` после правок — структура CLI/пайплайна хрупка к циклическим импортам (см. `decorators.py:82` duck-typing вместо импорта `ProcessingContext`).

## Запланировано
- **Фича: перевод валютных значений в рубли (для иностранных компаний)**
  - *Бизнес-логика:* российские компании — без изменений; иностранные (напр., учёт в дирхамах) — конвертация сумм в рубли.
  - *Баланс:* остатки из сводной ОСВ умножаются на курс на дату расшифровки. Курс — из листа `Курс_дирхам` справочника `Справочники.xlsx` (две колонки: `дата`, `курс`), загружается аналогично другим справочникам.
  - *ОПУ:* каждая проводка имеет дату; сумма операции умножается на курс, соответствующий дате операции (не усреднённый за период).
  - *Справочник:* лист `Курс_дирхам` в `Справочники.xlsx` → в `REFERENCE_REGISTRY` (`executors.py`), загрузка через `DataLoader`.
  - *Шаги:* предполагаемо — перед шагом 2 (OSV), перед шагом 14 (OPU foundation).

