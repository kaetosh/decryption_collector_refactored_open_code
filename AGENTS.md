# Руководство для ИИ-агента (AGENTS.md)

## Архитектура и запуск
* **Паттерн Pipeline:** шаги наследуются от `Step` (`pipeline/base.py:115`), реализуют только `_process(context)`. **НЕ переопределяйте `execute`** — обёрнут декоратором `handle_pipeline_errors` (`pipeline/decorators.py:47`).
* **Точка входа:** `main.py:1` -> `cli/main.py:38` (`main()` / `entry_point()`). Альтернатива: `python -m cli.main`. Аргументы: `cli/arguments.py:12`.
* **Фабрики:** `pipeline/factories.py:37` (`create_preparation_pipeline()`) и `:52` (`create_main_pipeline()`). Не хардкодьте шаги.
* **Оркестрация:** `pipeline/executors.py` — `initialize_context()`, `REFERENCE_REGISTRY`, паузы, `save_results()`. Сохранение: `io_module/data_io.py:490` + `io_module/output_manager.py:39`.
* **Контекст:** `ProcessingContext` (`pipeline/base.py:29`). Главные таблицы — `common_osv_df`, `summary_osv_df`, `journal_df`, `balance_df`, `pnl_df`. Промежуточные — в `context.data` (доступ через `Step.get_df_from_context()`). `context.run_id` = `ГГГГММДД_ЧЧММСС`.
* **Зависание на `input()` (КРИТИЧНО):** запуск без аргументов — интерактивный режим. Для CI/фона передавайте `-t`, `-v`, `--no-interactive`. Логика — `cli/main.py:121`.

## Автосортировка выгрузок (00_inbox)
* Пользователь выгружает файлы из 1С одним движением в `_INPUT_DATA/00_inbox`; раскладку выполняет `io_module/auto_sort.py` из пауз (`pipeline/executors.py`), под флагом `AUTO_SORT_ENABLED` (`config/settings.py`).
* **Волна 1** (`pause_for_osv_general_export`, до Фазы 0): `prepare_general_osv_from_inbox()` — перенос файлов с `_общаяосв_` в имени из inbox в `general_osv` (признак согласован с шаблоном `DataLoader.load_general_osv` = `*_общаяосв_*.xlsx`); не-ОСВ файлы `general_osv` — в архив.
* **Волна 2** (`pause_for_1c_export`, после шага 1a): `sort_inbox_by_expected_list(context)` — источник истины список выгрузок `Выгрузить_<компания>_<период>.xlsx` (листы «Обязательные выгрузки» + «Спецотчеты», колонки «Имя файла для сохранения» → «Куда класть»). `build_expected_map()` возвращает `{норм.имя: (целевая_папка, эталонное_имя)}`; `sort_all()` выполняет ревизию целевых папок (левое — в архив, misplaced — по назначению, кривое имя — `_rename_to_canonical`) и раскладку inbox (`_move_to_dir`).
* **Чтение списка устойчиво к формату шага 1a:** колонки ищутся без учёта регистра (`Куда класть` vs `куда класть` из справочника «Выгрузки»), строка заголовка — в первых 10 строках листа (лист «Спецотчеты» отформатирован с двумя служебными строками сверху).
* **Правила:** классификация только по имени файла (`normalize_name`: нижний регистр, пробелы → `_`, хвостовое `_` опционально; расширение значимо); при совпадении со списком кривое имя **переименовывается к эталону** — иначе `find_register_file` (`utils/file_utils.py`) не распарсит период/счет; содержимое файлов не читается (кроме сравнения хешей при конфликте имён).
* **Конфликты/архив:** ничего не удаляется безвозвратно — `_INPUT_DATA/_archive/<run_id>/<имя_папки>/` (левые файлы, старые версии при перезаписи), нераспознанное — `_archive/<run_id>/unsorted/` с WARNING; конвейер не останавливается (нехватку обязательных файлов штатно сообщит шаг 1b). Идентичный по содержимому дубликат — копия удаляется. Сам файл списка `Выгрузить_*.xlsx` из inbox и `~$`-файлы Excel не трогаются; копия общей ОСВ, оставшаяся в inbox (её нет в списке выгрузок), обрабатывается как в волне 1 — в `general_osv` с разрешением конфликтов.
* **Отчёт:** каждая волна пишет `sort_report.xlsx` (файл/действие/откуда/куда) в папку запуска + INFO-сводка `[SORT]` в лог. Служебные `~$`-файлы Excel не трогаются. Смоук — `_smoke_auto_sort.py` (временные папки, реальные `_INPUT_DATA` не затрагиваются).

## Автоматическая постобработка и валидация
При вызове `Step.execute()` (`base.py:124`) автоматически (не пишите ручной код):
1. `_validate_input(context)` — валидация входа
2. `_process(context)` — бизнес-логика (единственное место для кода шага)
3. `_clean_whitespace(context)` (`base.py:395`) — очистка пробелов в `summary_osv_df`/`journal_df`
4. `_move_and_sort_level_columns(context)` (`base.py:405`) — `Level_*` в конец по возрастанию
5. `_validate_output(context)` (`base.py:210`) — запрет `object`-колонок (иначе `TypeError`). Приводите через `cast_columns_to_types()` (`utils/dataframe_utils.py:13`)
6. Контроль сходимости баланса: если есть `сальдо, тыс.ед.`, сумма проверяется на `0` с допуском `tolerance_balance` (дефолт `config/defaults.py:12`). Пропуск — через `self._skip_balance_validation = True` в `__init__` шага (`base.py:131`). Применяется в Step 2 и Step 2а, где данные неполные до добавления синтетических счетов. Для ОПУ-расходов — `_validate_against_osv()` в `base_expenses_step.py:451`.

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
| `ConvergenceError` (увязка ЧП=НРП, шаг 19) | `EXPORT_REPORT_ON_MISMATCH=False` -> отчёт в Excel (mismatches/) + `ProcessingStepError`; `True` (по умолчанию) -> ERROR/WARNING в лог, диагностика в `context.data['pnl_balance_mismatch']`, шаг продолжается, финальный отчёт выгружается как есть |
| `ReferenceMismatchError` и подвиды | сохранение `problem_data` в Excel (`_save_reference_mismatch_report()` -> `output_manager.py:88`), `ProcessingStepError` |
| `ReferenceMismatchError` (необязательные спецотчёты, шаги 7/11) | мягкий режим (`SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR=True`, по умолчанию) — `problem_data` в Excel (mismatches/), WARNING «проверьте файл на полноту и формат», шаг продолжается без этой детализации; `False` -> `ProcessingStepError` |
| `MissingOSGroupError` (справочник ППА, шаг 6, кейс «нет вообще записей по компании») | **всегда стоп** независимо от `STRICT_OS_GROUP_CHECK`: в `problem_data` (mismatches/) — полный список недостающих для меппинга позиций — договоры аренды (76.07/76.05.3) и РБП (97.21, Аренда/Лизинг) из данных ОСВ (`_raise_ppa_company_missing`). Если таких позиций в данных нет — WARNING в лог, шаг продолжается. `STRICT_OS_GROUP_CHECK` управляет только кейсом «список неполный» |
| `ReferenceMismatchError` (ПланСчетовБУ, шаги 1c/17) | вместо сырого `ValueError` — штатный `[STOP]`: нет записей по компании -> `problem_data` = список компаний в справочнике; записи есть, но нет субконто `Контрагенты` -> `problem_data` = строки плана по компании (шаг 17 `_load_plan_schetov_bu`); нет синтетических счетов из общей ОСВ -> `MissingMappingError` со списком счетов (шаг 1c `step_01c_reconcile_totals.py`) |
| `ReferenceMismatchError` (сопоставление счетов, шаг 1а, `_raise_balance_mapping_error`) | жёсткая при неоднозначности — несколько кандидатов под уровнем-остановкой (`68.22.2 -> '68'` -> 12 субсчетов; `60.01.1 -> '60.01'` -> 2 кандидата); счёт/родители не заведены. Единственное совпадение — штатная подстановка, включая синтетический уровень (`04.01 -> '04'`, `90.01.1 -> '90.01'`). Смоук — `_smoke_01a_match.py` |
| `MissingMappingError` (шаг 17, справочник ППА, `_validate_ppa_mapping`) | при отсутствии записей по компании (в т.ч. пустой справочник целиком) и при неполном списке — `problem_data` = недостающие объекты ОС по `ос_ппа`/`ос_после_перехода_в_собственность` + подсказка со списком компаний в справочнике |
| `MissingFilesError` / `MissingCardError` | сохранение списка файлов, `ProcessingStepError` |
| `TooManyFilesError` | сохранение списка избыточных файлов в Excel (mismatches/), `ProcessingStepError`. Возникает, когда в папке найдено более одного файла по паттерну (например, два файла общей ОСВ) |
| `Exception` | обёртка в `ProcessingStepError` (`from e`) |

`STRICT_CONTRACTOR_CHECK = False` (`config/settings.py:48`) — мягкий режим. Реализация — `Step._apply_soft_contractor_handling()` (`base.py:652`).
`STRICT_CREDIT_CONTRACTOR_CHECK = True` (`config/settings.py:58`) — режим для справочника КредитОбслуж (шаг 17, `_process_credit_lines`). `True` (по умолчанию): при отсутствии РБП в справочнике — отчёт в Excel (mismatches/) + `ProcessingStepError`; `False`: отчёт в Excel + замена контрагента на `3 лица`, шаг продолжается. Исключение — `MissingCreditContractorError`.
`STRICT_OS_GROUP_CHECK = True` (`config/settings.py:54`) — строгий режим по умолчанию для групп ОС аренды/лизинга (шаг 6, справочник ППА). Проверяются: договоры/РБП, отсутствующие в ППА, и значения групп вне допустимого списка — выбрасывается `MissingOSGroupError`, проблемные строки сохраняются в Excel (mismatches/). При `False` (мягкий режим) — WARNING, замена на `не_указано` внутри шага (реализация — `_validate_mapping` и этапы 5/7 в `pipeline/steps/step_06_add_os_group.py`). Актуализация справочника ППА под новый период — штатная часть работы с отчётностью.

`EXPORT_REPORT_ON_MISMATCH = True` (`config/settings.py`) — режим увязки ЧП (ОПУ) = НРП (баланс) в шаге 19 (`_check_profit_vs_balance`, `_step19_validation.py`). `True` (по умолчанию, мягкий): при расхождении сверх `tolerance_pnl_balance` — ERROR в лог, диагностика (НРП, ЧП, разница, порог) в `context.data[OpuReportConstants.MISMATCH_DIAGNOSTICS_KEY]`, шаг продолжается, отчёт собирается и выгружается «как есть» (несведённый, для анализа); `save_results()` дополнительно предупреждает о несведённом отчёте. `False` (строгий): `ConvergenceError` с однострочным `problem_data` -> декоратор сохраняет его в mismatches/, конвейер останавливается, отчёт не выгружается. Структурные ошибки (`_get_retained_earnings`: нет строки НРП `240010200` / значение NaN) жёсткие в обоих режимах. Смоук — `_smoke_pnl_mismatch.py` (флаг патчится как атрибут модуля `pipeline.steps._step19_validation`).

`SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR = True` (`config/settings.py`) — мягкий режим для необязательных спецотчётов `_арендареклассдолгкорт_7697_`, `_лизингреклассдолгкорт_7697_`, `_осв_60инвест_`, `_реклассдолгкорт_97_` (шаги 7 и 11). При ошибке обработки найденного файла (пустой/неполный файл, нет ожидаемых столбцов, несходимость) — единый хелпер `Step._run_optional_special_report()` (`base.py:204`): WARNING «проверьте файл на полноту и формат», шаг продолжается, как будто файла не было (расшифровка собирается без этой детализации). Для `ReferenceMismatchError`/`ConvergenceError` `problem_data` дополнительно сохраняется в mismatches/. Отсутствие файла — не ошибка (прежние WARNING). `False` — прежнее жёсткое поведение (ошибка останавливает конвейер). `_apply_97_reclass` (шаг 7) под защиту не заводится: мутирует `osv_all_df` in-place — ошибка там означает баг кода. Смоук — `_smoke_optional_special_reports.py`.

### Единый стандарт для справочников, подтягиваемых по имени компании

Для справочников, данные которых выгружаются по имени компании (**ППА**, **СправочникУФР**, **ПланСчетовБУ**, **КредитОбслуж**), поведение при отсутствии данных приведено к единообразию:

* **Нет вообще записей по компании** — сразу выдаётся список недостающих для меппинга позиций (независимо от того, что записи по компании отсутствуют целиком). Для ППА (шаг 6) это перекрывает даже `STRICT_OS_GROUP_CHECK` — стоп со списком позиций; если меппинговых позиций в данных нет (нет аренды/лизинга) — WARNING и продолжение.
* **Записи есть, но список неполный** — прежняя логика: строгий режим -> стоп со списком недостающих позиций; мягкий -> WARNING + подстановка `не_указано` (для ППА — под управлением `STRICT_OS_GROUP_CHECK`).
* Во всех штатных остановках `problem_data` пишется в mismatches/ как список **недостающих позиций** (а не только список имеющихся компаний); подсказка «какие компании есть в справочнике» уходит в текст ошибки (лог).
* Единые хелперы в `pipeline/base.py`: `make_missing_values_problem_data()` (формат отчёта: `отсутствующее_значение`/`тип`/`компания`) и `hint_companies_in_reference()` (подсказка со списком компаний).

## Классификация ошибок в логах ([STOP] vs CRITICAL)
Верхний обработчик `cli/main.py` разделяет штатные остановки и настоящие сбои:
* `except (PipelineError, ProcessingStepError)` — предусмотренные ошибки пайплайна: `ProcessingStepError` с первопричиной-наследником `PipelineError` в `__cause__` (декоратор шагов сохраняет её) и «сырые» `PipelineError` вне шагов (например, `PeriodMismatchError` из Фазы 0). Лог — `ERROR [STOP] Обработка остановлена: <причина>` + подсказки про актуализацию справочников и mismatches/. CRITICAL не используется.
* `ProcessingStepError` с первопричиной НЕ из `PipelineError` и общий `except Exception` — непредвиденные сбои: `CRITICAL [!!] Неожиданная ошибка`.
* `FileNotFoundError` — `ERROR [STOP] Обработка остановлена: не найден файл...`.

Теги сообщений: `[STOP]` — штатная остановка конвейера (нужна актуализация справочников/входных данных), `[!!]` + CRITICAL — действительно непредвиденное падение. Сообщение о сохранении проблемных данных (`[FOLDER] Проблемные данные сохранены...`, `base.py:518`) — уровень INFO.

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
| Загрузка и подготовка | 1b, 1c, 2, 2а | Проверка файлов, реконциляция ОСВ, флэттенинг, подстановка сальдо синтетических счетов из Общей ОСВ |
| Классификация (баланс) | 3-9 | Счета, тип/подвид задолженности, группы ОС, долгая/короткая часть, биоактивный, вид связи |
| Специальные расчёты | 10-12 | Источник аренды, разбиение 60 (инвест/не-инвест), разбиение 84 (НРП) |
| Сборка баланса | 13 | Расшифровка по маппингу ФО |
| Классификация (ОПУ) | 14-18 | Выручка/себестоимость, управленческие/коммерческие расходы, прочие доходы/расходы, налог |
| Сборка ОПУ | 19 | Маппинг на счета ФО, увязка ЧП = НРП |

**Ключевые шаги:**
- **1c** (`step_01c_reconcile_totals.py`) — реконциляция итогов ОСВ vs выгрузки, загрузка `journal_df`
- **14** (`step_14_build_opu_foundation.py`) — 90.01/90.02, распределение себестоимости, `transactions_all_df` в `context.data`. Шаг разбит на 4 миксина: `_step14_base`, `_step14_data`, `_step14_accounts`, `_step14_transform`.
- **15-16** (`base_expenses_step.py`) — управленческие (90.08/26) и коммерческие (90.07/44) расходы
- **17** (`step_17_add_other_income_and_expenses.py`) — прочие доходы/расходы (91.01/91.02). Шаг разбит на 5 миксинов: `_step17_base`, `_step17_data`, `_step17_processing`, `_step17_fx`, `_step17_merge`.
- **18** (`step_18_add_task_and_other_movements.py`) — налог на прибыль (99), исключение реформации
- **19** (`step_19_build_opu.py`) — сборка ОПУ, увязка ЧП = НРП (`tolerance_pnl_balance`). Шаг разбит на 4 миксина: `_step19_base`, `_step19_validation`, `_step19_mapping`, `_step19_report`.
- **20** (`step_20_collapse_opu.py`) — свёртывание прочих доходов/расходов по справочнику `Прочие_дох_рас_свернуто` (паринг по `номер_группы_сворачивания`, свёрнутый результат остаётся в `context.pnl_df` с **именованным индексом** `Итоговый номер счета` — `reset_index()` в `_prepare_pnl_df` не должен порождать лишний столбец `index`)

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

## Архитектура шагов: миксины
Крупные шаги (14, 17, 19) разбиты на логические миксины. Каждый миксин — законченный блок логики, легко находимый по имени файла.

**Принципы:**
- Миксины **не имеют `__init__`** — полагаются на родительский
- Бизнес-логика **не меняется** — только перемещение методов
- Главный класс наследует все миксины (MRO)
- При ошибке в конкретном блоке — открываешь один файл, а не монолит

**Структура миксинов:**

| Шаг | Миксин | Назначение |
|---|---|---|
| 14 | `_step14_base` | Константы класса |
| 14 | `_step14_data` | Загрузка данных и справочников |
| 14 | `_step14_accounts` | Обработка 90.01/90.02, распределение себестоимости |
| 14 | `_step14_transform` | Reshape, обогащение, merge с переоценкой |
| 17 | `_step17_base` | Константы (счета, маски, FX-константы) |
| 17 | `_step17_data` | Загрузка, фильтрация 91.x, извлечение контрагентов |
| 17 | `_step17_processing` | ППА, продажа активов (НДС: корректировка 91.01 с распределением по строкам документа — `_distribute_vat_by_rows`), кредитные линии, обогащение |
| 17 | `_step17_fx` | Курсовые разницы (Фаза 3.3) |
| 17 | `_step17_merge` | Финальное слияние с main_df |
| 19 | `_step19_base` | Константы, валидация структуры |
| 19 | `_step19_validation` | Приведение типов, чистка, сверка ЧП vs НРП |
| 19 | `_step19_mapping` | Маппинг ОПУ (счет_фо) |
| 19 | `_step19_report` | Финальная сборка отчёта |

**Debug-точки:**
- `DEBUG_DUMP_DFS = False` (`config/settings.py`) — включить для сохранения DataFrame на каждом шаге
- `Step._dump_df_for_debug(df, label)` (`base.py:203`) — сохраняет parquet в `_OUTPUT_DATA/_debug/`
- Включить: `set DEBUG_DUMP_DFS=True` перед запуском

## Замечания для ИИ-агента
* Не используйте `OUTPUT_DATA_DIR` напрямую — только `get_output_dir()` / `get_run_id()`
* Не дублируйте константы — импортируйте из `pipeline/constants.py` / `pipeline/step_config.py`
* Для чтения `context.data` — только `get_df_from_context()`
* Для новой ошибки: класс в `pipeline/errors.py` + ветка в `pipeline/decorators.py`
* Проверяйте `py_compile` после правок — структура CLI/пайплайна хрупка к циклическим импортам

## Запланировано
Текущие и завершённые задачи — в `TASKS.md`. Активных задач нет.
