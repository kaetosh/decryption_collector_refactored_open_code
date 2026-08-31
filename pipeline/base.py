# -*- coding: utf-8 -*-
"""
Базовые классы для построения конвейера обработки.
Реализуют паттерны Command и Chain of Responsibility.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import pandas as pd
from typing import Any, Dict, List, Optional
from loguru import logger
from io_module.output_manager import get_output_dir, get_run_id
from pipeline.errors import (
    ReferenceMismatchError,
    MissingFilesError,
    MissingContractorError,
    ProcessingStepError,
)
from pipeline.constants import (
    ColumnNames,
    DataTypes,
    Prefixes,
    Values,
)
from pipeline.decorators import handle_pipeline_errors


@dataclass(slots=True)
class ProcessingContext:
    # Параметры
    company: Optional[str] = None
    segment: Optional[str] = None
    period: Optional[str] = None
    type_period: Optional[str] = None
    currency: Optional[str] = "RUB"
    name_file_general_osv: Optional[str] = None

    # Идентификатор запуска (ГГГГММДД_ЧЧММСС) — совпадает с именем папки вывода
    run_id: Optional[str] = None

    # общая осв для списка выгрузок и сверки
    common_osv_df: Optional[pd.DataFrame] = None

    # Справочники
    references: Dict[str, pd.DataFrame] = field(default_factory=dict)
    
    # Параметры допусков сходимости
    tolerance_params: Dict[str, float] = field(default_factory=dict)

    # Рабочие таблицы
    summary_osv_df: Optional[pd.DataFrame] = None
    journal_df: Optional[pd.DataFrame] = None

    # Результаты
    balance_df: Optional[pd.DataFrame] = None
    pnl_df: Optional[pd.DataFrame] = None
    
    # Прочие данные
    data: Dict[str, Any] = field(default_factory=dict)

    # Метрики выполнения шагов: заполняются декоратором handle_pipeline_errors
    step_metrics: List[Dict[str, Any]] = field(default_factory=list)

    def record_step(
        self,
        step_name: str,
        status: str,
        duration_sec: float,
        error: str = None,
    ) -> None:
        """
        Записывает метрику выполнения шага.

        Используется для итоговой сводки Pipeline.run и для отладки
        (видно, какой шаг сколько выполнялся и где произошёл сбой).

        Args:
            step_name: Название шага.
            status: Статус завершения: 'ok' | 'soft' | 'error'.
            duration_sec: Длительность шага в секундах.
            error: Текст ошибки (если статус 'error').
        """
        self.step_metrics.append({
            "step": step_name,
            "status": status,
            "duration_sec": duration_sec,
            "error": error,
        })

    def __repr__(self) -> str:
        """
        Компактное представление для отладки: параметры запуска,
        размеры таблиц, ключи data и число записанных метрик.

        Сами DataFrame не выводятся, чтобы repr оставался читаемым
        (например, в сообщениях об ошибках и логах).
        """
        def shape(df: Optional[pd.DataFrame]) -> str:
            return "None" if df is None else f"{len(df)}x{len(df.columns)}"

        params = (
            f"run_id={self.run_id!r}, company={self.company!r}, "
            f"segment={self.segment!r}, period={self.period!r}, "
            f"type_period={self.type_period!r}"
        )
        tables = ", ".join(
            f"{name}={shape(getattr(self, name))}"
            for name in ("common_osv_df", "summary_osv_df", "journal_df", "balance_df", "pnl_df")
        )
        return (
            f"ProcessingContext({params}; {tables}; "
            f"data_keys={list(self.data.keys())}; metrics={len(self.step_metrics)})"
        )

class Step(ABC):
    """
    Абстрактный базовый класс для всех шагов обработки.
    """
    
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
    
    @handle_pipeline_errors
    def execute(self, context: 'ProcessingContext') -> 'ProcessingContext':
        """
        Публичный метод, который запускает шаг.

        Обработка ошибок (сохранение отчётов о проблемных данных, мягкий/
        строгий режим для контрагентов, оборачивание исключений в
        ProcessingStepError) вынесена в декоратор handle_pipeline_errors
        (pipeline/decorators.py).
        """
        # 1. Валидация входа
        self._validate_input(context)

        # 2. Бизнес-логика
        context = self._process(context)

        # 3. Удаление лишних пробелов
        context = self._clean_whitespace(context)

        # 4. Перенос Level_столбцов в конец таблицы
        context = self._move_and_sort_level_columns(context)

        # 5. Валидация выхода
        self._validate_output(context)

        logger.debug("--- Успешное завершение этапа: {} ---", self.name)

        return context
    
    @abstractmethod
    def _process(self, context: 'ProcessingContext') -> 'ProcessingContext':
        """
        Абстрактный метод для реализации конкретной логики шага.
        Наследники должны реализовывать именно его, а не execute.
        """
        pass

    def get_df_from_context(
        self,
        context: 'ProcessingContext',
        key: str,
        hint: str = "",
    ) -> pd.DataFrame:
        """
        Достаёт таблицу из context.data и проверяет её наличие и непустоту.

        Унифицирует повторяющийся в шагах паттерн
        "context.data.get(key) + проверка + raise ValueError".

        Args:
            context: Контекст конвейера.
            key: Ключ таблицы в context.data.
            hint: Подсказка к ошибке (например, какой шаг должен
                создать таблицу).

        Returns:
            Найденный непустой DataFrame.

        Raises:
            ValueError: Если таблица отсутствует или пуста.
        """
        df = context.data.get(key)
        if not isinstance(df, pd.DataFrame) or df.empty:
            reason = "отсутствует" if df is None else "пуста"
            message = f"В контексте нет таблицы '{key}' ({reason})."
            if hint:
                message += f" {hint}"
            raise ValueError(message)
        return df
    
    def _validate_input(self, context: 'ProcessingContext'):
        """
        Базовая валидация входа. 
        Переопределяется в наследниках, если нужна специфичная проверка.
        """
        pass
    
    def _validate_output(self, context: 'ProcessingContext'):
        """
        Универсальная валидация выхода для всех шагов.
    
        Проверяет для context.common_osv_df, context.summary_osv_df, context.journal_df:
        1. Сходимость сальдо, если есть столбец 'сальдо, тыс.ед.'
        2. Отсутствие столбцов с типом object
        """

    
        def validate_single_df(df: pd.DataFrame, df_name: str) -> None:
            # 1. Проверяем наличие данных ДО любых действий
            if df is None:
                logger.debug("Этап '{}': {} is None, пропускаем валидацию", self.name, df_name)
                return
    
            if df.empty:
                logger.warning("Этап '{}': {} пуст, пропускаем валидацию", self.name, df_name)
                return
    
            # 2. Проверка отсутствия object типов
            # Лучше выполнять до суммирования сальдо, чтобы не пытаться
            # сложить строки, если колонка случайно осталась в object.
            object_cols = [
                col for col, dtype in df.dtypes.items()
                if dtype == DataTypes.OBJECT
            ]
    
            if object_cols:
                raise TypeError(
                    f"После этапа '{self.name}' в {df_name} обнаружены столбцы "
                    f"с типом 'object': {object_cols}. "
                    f"Используйте 'string' или числовые типы."
                )
    
            # 3. Проверка сходимости сальдо
            if ColumnNames.BALANCE in df.columns:
                try:
                    # pd.to_numeric дополнительно страхует от случаев,
                    # когда тип не object, но значения не числовые.
                    balance_values = pd.to_numeric(df[ColumnNames.BALANCE], errors='raise')
                except (ValueError, TypeError) as exc:
                    raise TypeError(
                        f"После этапа '{self.name}' в {df_name} столбец "
                        f"'{ColumnNames.BALANCE}' содержит нечисловые значения."
                    ) from exc
    
                balance_sum = balance_values.sum()
    
                if abs(balance_sum) > context.tolerance_params['tolerance_balance']:
                    raise ValueError(
                        f"После этапа '{self.name}' в {df_name} ОСВ не сошлась: "
                        f"сумма сальдо = {balance_sum:.2f} тыс.ед. "
                        f"(допуск: {context.tolerance_params['tolerance_balance']})"
                    )
    
                logger.debug(
                    "Этап '{}', {}: сходимость сальдо = {:.2f} тыс.ед.",
                    self.name,
                    df_name,
                    balance_sum,
                )
    
        # Обрабатываем те же датафреймы, что и в предыдущих методах
        for attr_name in ('common_osv_df', 'summary_osv_df', 'journal_df'):
            df = getattr(context, attr_name, None)
            validate_single_df(df, attr_name)
    
    @staticmethod
    def clean_whitespace(df: pd.DataFrame) -> pd.DataFrame:
        """
        Очищает все строковые столбцы DataFrame от лишних пробелов.
        """
        df_clean = df.copy()
        
        string_columns = df_clean.select_dtypes(include=[DataTypes.STRING, DataTypes.OBJECT]).columns
        
        for col in string_columns:
            if df_clean[col].dtype == DataTypes.OBJECT:
                if df_clean[col].apply(lambda x: isinstance(x, str)).any():
                    df_clean[col] = (
                        df_clean[col]
                        .astype(str)
                        .str.strip()
                        .str.replace(r'\s+', ' ', regex=True)
                        .replace('nan', pd.NA)
                    )
            else:
                df_clean[col] = (
                    df_clean[col]
                    .str.strip()
                    .str.replace(r'\s+', ' ', regex=True)
                )
        
        return df_clean
    
    @staticmethod
    def validate_extracted_column(
        df: pd.DataFrame,
        column_name: str,
        keywords: list,
        match_threshold: float = 0.30,
        unique_threshold: int = None,
        column_purpose: str = "данные",
    ) -> None:
        """
        Валидирует содержимое столбца по ключевым словам.
        
        Универсальный метод для проверки, что в столбце действительно
        ожидаемые данные (контрагенты, виды расчётов, договоры и т.д.).
        
        Args:
            df: DataFrame с извлечёнными данными
            column_name: Имя проверяемого столбца
            keywords: Список ключевых слов для проверки
            match_threshold: Порог совпадений (0.0-1.0).
                Если доля значений, содержащих ключевые слова, ниже порога — ошибка.
            unique_threshold: Опциональный порог уникальных значений.
                Если указан и количество уникальных значений превышает порог,
                а совпадений мало — это усиливает проверку.
            column_purpose: Описание назначения столбца (для сообщения об ошибке).
                Например: "контрагентов", "видов расчётов", "договоров"
        
        Raises:
            ValueError: Если содержимое столбца не соответствует ожиданиям.
        
        Examples:
            # Проверка контрагентов
            self.validate_extracted_column(
                df=df,
                column_name='контрагент',
                keywords=CONTRACTOR_KEYWORDS,
                match_threshold=0.30,
                column_purpose="контрагентов"
            )
            
            # Проверка видов расчётов
            self.validate_extracted_column(
                df=df,
                column_name='вид_взаиморасчетов',
                keywords=CALC_TYPE_KEYWORDS,
                match_threshold=0.15,
                unique_threshold=30,
                column_purpose="видов расчётов"
            )
        """
        if column_name not in df.columns:
            raise ValueError(f"Столбец '{column_name}' отсутствует в DataFrame")
        
        values = df[column_name].dropna().astype(str)
        
        if values.empty:
            raise ValueError(
                f"Столбец '{column_name}' пуст — невозможно валидировать {column_purpose}."
            )
        
        # Проверка по ключевым словам
        match_rate = values.str.contains(
            '|'.join(keywords),
            case=False,
            regex=True
        ).mean()
        
        # Базовая проверка порога
        is_below_threshold = match_rate < match_threshold
        
        # Дополнительная проверка уникальности (если указан unique_threshold)
        is_too_diverse = (
            unique_threshold is not None
            and values.nunique() > unique_threshold
        )
        
        # Ошибка если:
        # - совпадений мало (базовая проверка)
        # - ИЛИ совпадений мало + много уникальных значений (усиленная проверка)
        if is_below_threshold and (unique_threshold is None or is_too_diverse):
            raise ValueError(
                f"Столбец '{column_name}' предположительно содержит {column_purpose}, "
                f"но только {match_rate:.0%} значений ({values.nunique()} уникальных) "
                f"содержат типичные признаки.\n"
                f"Порог совпадений: {match_threshold:.0%}\n"
                f"Примеры значений: {values.head(10).tolist()}\n"
                f"Возможно, порядок Level_-столбцов в выгрузке неверный."
            )
        
        logger.debug(
            "Валидация столбца '{}' ({}) пройдена: "
            "{:.0%} совпадений, {} уникальных значений",
            column_name,
            column_purpose,
            match_rate,
            values.nunique(),
        )
        
    def _clean_whitespace(self, context: 'ProcessingContext') -> 'ProcessingContext':
        """Обертка для очистки context"""
        if context.summary_osv_df is not None:
            context.summary_osv_df = self.clean_whitespace(context.summary_osv_df)
    
        if context.journal_df is not None:
            context.journal_df = self.clean_whitespace(context.journal_df)
    
        return context
    
    def _move_and_sort_level_columns(self, context: 'ProcessingContext') -> 'ProcessingContext':
        """
        Переносит столбцы Level_* в конец DataFrame и сортирует их по возрастанию.
        Обрабатывает common_osv_df, summary_osv_df и journal_df, если они не None.
        """
        
        # Функция для извлечения номера уровня (определяем один раз вне цикла)
        def extract_level_number(col_name: str) -> float:
            try:
                suffix = str(col_name).split('_', 1)[1]
                return int(suffix)
            except (IndexError, ValueError):
                return float('inf')
    
        # Внутренняя функция для обработки одного DataFrame
        def process_single_df(df: pd.DataFrame) -> pd.DataFrame:
            if df is None or df.empty:
                return df
    
            # Находим столбцы Level_* (регистронезависимо)
            level_cols = [
                col for col in df.columns 
                if str(col).lower().startswith(Prefixes.LEVEL)
            ]
            
            if not level_cols:
                return df
            
            # Сортируем level_* по числовому суффиксу
            level_cols_sorted = sorted(level_cols, key=extract_level_number)
            regular_cols = [col for col in df.columns if col not in level_cols]
            new_order = regular_cols + level_cols_sorted
            
            # df[new_order] и так возвращает новый DataFrame, 
            # поэтому явный .copy() здесь не обязателен
            return df[new_order]
    
        # Список атрибутов контекста, которые нужно обработать
        attrs_to_process = ['common_osv_df', 'summary_osv_df', 'journal_df']
    
        for attr in attrs_to_process:
            df = getattr(context, attr, None)
            # Проверяем, что датафрейм существует (не None)
            if df is not None:
                # Сохраняем результат обратно в тот же атрибут
                setattr(context, attr, process_single_df(df))
    
        return context
    
    # =========================================================================
    # ОБРАБОТКА ОШИБОК НЕСООТВЕТСТВИЯ СПРАВОЧНИКАМ
    # =========================================================================
    
    def _save_reference_mismatch_report(self, error: ReferenceMismatchError) -> None:
        """
        Сохраняет проблемные данные в Excel-файл.
        Обрабатывает PermissionError — когда файл открыт в Excel.
        """
        if error.problem_data is None or error.problem_data.empty:
            logger.warning("Нет проблемных данных для сохранения")
            return
        
        try:
            # Хвост имени файла = идентификатор запуска (совпадает с папкой вывода)
            timestamp = get_run_id()
            
            # ★ КОРОТКОЕ имя шага: только номер (например, "step_14", "step_11a", "step_1a")
            step_slug = self._short_step_slug()
            
            # ★ КОРОТКОЕ имя справочника (обрезка до 30 символов)
            ref_slug = self._short_slug(error.reference_name or 'unknown', max_len=30)
            
            filename = f"mismatch_{step_slug}_{ref_slug}_{timestamp}.xlsx"
            output_path = get_output_dir("mismatches") / filename
            
            error.problem_data.to_excel(output_path, index=False)
            
            logger.error(
                "[FOLDER] Проблемные данные сохранены в: {}/{}", output_path.parent.name, output_path.name
            )
            
        except PermissionError:
            logger.error(
                "[!] НЕ УДАЛОСЬ сохранить файл '{}': "
                "файл открыт в другой программе (Excel?) или нет прав на запись.\n"
                "Закройте файл и повторите попытку, либо проверьте права доступа к папке "
                "{}.\n"
                "Проблемные данные ({} строк) НЕ были сохранены.",
                filename,
                output_path.parent,
                len(error.problem_data),
            )
        except Exception as save_error:
            logger.error("Не удалось сохранить файл с проблемными данными: {}", save_error)

    
    @staticmethod
    def _slugify(text: str) -> str:
        """Преобразует текст в безопасное имя файла."""
        text = text.lower()
        text = re.sub(r'[^\wа-я]+', '_', text, flags=re.IGNORECASE)
        return text.strip('_')
    
    # =========================================================================
    # HELPER-МЕТОДЫ ДЛЯ ШАГОВ
    # =========================================================================
    
    def _raise_reference_mismatch(
        self,
        error_class: type,
        message: str,
        problem_data: pd.DataFrame,
        reference_name: str,
        **metadata
    ) -> None:
        """
        Helper для быстрого создания и выброса ReferenceMismatchError.
        
        Usage:
            self._raise_reference_mismatch(
                MissingMappingError,
                "Не найдены РБП в справочнике ППА",
                missing_rbps_df,
                "ППА"
            )
        """
        raise error_class(
            message=message,
            problem_data=problem_data,
            reference_name=reference_name,
            **metadata
        )
    
    def _save_missing_files_report(self, error: MissingFilesError) -> None:
        """Сохраняет список отсутствующих файлов в Excel."""
        if not error.missing_files:
            return
        
        try:
            # Хвост имени файла = идентификатор запуска (совпадает с папкой вывода)
            timestamp = get_run_id()
            
            # ★ КОРОТКОЕ имя шага
            step_slug = self._short_step_slug()
            
            filename = f"missing_files_{step_slug}_{timestamp}.xlsx"
            output_path = get_output_dir("mismatches") / filename
            
            df = pd.DataFrame({
                'отсутствующий_файл': error.missing_files,
                'ожидаемая_директория': error.expected_dir,
                'шаг': error.step_name
            })
            df.to_excel(output_path, index=False)
            
            logger.error(
                "[FOLDER] Список отсутствующих файлов сохранён в: {}/{}",
                output_path.parent.name,
                output_path.name,
            )
            
        except PermissionError:
            logger.error(
                "[!] НЕ УДАЛОСЬ сохранить файл '{}': "
                "файл открыт в другой программе (Excel?) или нет прав на запись.\n"
                "Закройте файл и повторите попытку.",
                filename,
            )
        except Exception as save_error:
            logger.error("Не удалось сохранить файл с отсутствующими файлами: {}", save_error)
    
    
    # =========================================================================
    # HELPER-МЕТОДЫ ДЛЯ ФОРМИРОВАНИЯ КОРОТКИХ ИМЁН ФАЙЛОВ
    # =========================================================================
    
    def _short_step_slug(self) -> str:
        """
        Извлекает номер шага из self.name и формирует короткий slug.
        
        Примеры:
            "Шаг 14: Формирование основы..." → "step_14"
            "Шаг 11a: Проверка похожих..." → "step_11a"
            "Шаг 1а: Формирование списка..." → "step_1a"
            "Шаг 1б: Проверка списка..." → "step_1b"
        """
        
        # Ищем паттерн "Шаг N" где N может содержать буквы (11a, 1а, 1б)
        match = re.search(r'Шаг\s+(\d+[a-zа-я]?)', self.name, re.IGNORECASE)
        
        if match:
            step_num = match.group(1).lower()
            # Транслитерация русских букв (а→a, б→b)
            step_num = step_num.replace('а', 'a').replace('б', 'b').replace('в', 'v')
            return f"step_{step_num}"
        
        # Fallback: обычный slug, но обрезанный
        return self._short_slug(self.name, max_len=15)
    
    
    def _short_slug(self, text: str, max_len: int = 30) -> str:
        """
        Формирует короткий slug из текста с ограничением длины.
        
        Приоритет:
        1. Если текст содержит английское слово в скобках — берём его
           "Справочник строк УФР (directory_ufr)" → "directory_ufr"
        2. Иначе — обычный slugify с обрезкой
        
        Args:
            text: Исходный текст
            max_len: Максимальная длина результата
            
        Returns:
            Короткий slug (гарантированно <= max_len символов)
        """
        
        if not text:
            return 'unknown'
        
        # ★ Приоритет 1: ищем английское слово в скобках
        match = re.search(r'\(([a-zA-Z_][a-zA-Z0-9_]*)\)', text)
        if match:
            result = match.group(1).lower()
            return result[:max_len]
        
        # ★ Приоритет 2: обычный slugify
        text = text.lower()
        text = re.sub(r'[^\wа-я]+', '_', text, flags=re.IGNORECASE)
        result = text.strip('_')
        
        # ★ Обрезка до max_len с сохранением целых слов (по возможности)
        if len(result) > max_len:
            # Пробуем обрезать по последнему _ до max_len
            truncated = result[:max_len]
            last_underscore = truncated.rfind('_')
            if last_underscore > max_len // 2:  # Если _ не в самом начале
                result = truncated[:last_underscore]
            else:
                result = truncated
        
        return result
    
    # =========================================================================
    # МЯГКАЯ ОБРАБОТКА НЕИЗВЕСТНЫХ КОНТРАГЕНТОВ
    # =========================================================================
    
    def _apply_soft_contractor_handling(
        self,
        context: 'ProcessingContext',
        error: MissingContractorError
    ) -> 'ProcessingContext':
        """
        Заменяет неизвестных контрагентов на значение по умолчанию (обычно '3 лица').
        
        Вызывается в мягком режиме (STRICT_CONTRACTOR_CHECK=False).
        """
        df = context.summary_osv_df.copy()
        
        # Находим строки с UNSPECIFIED в целевом столбце
        mask = df[error.target_column] == Values.UNSPECIFIED
        replaced_count = mask.sum()
        
        # Заменяем на значение из error
        df.loc[mask, error.target_column] = error.replacement_value
        
        logger.info(
            "Заменено {} неизвестных контрагентов на '{}' в столбце '{}'",
            replaced_count,
            error.replacement_value,
            error.target_column
        )
        
        context.summary_osv_df = df
        return context
    
    def __repr__(self) -> str:
        if self.description:
            return f"Step(name={self.name!r}, description={self.description!r})"
        return f"Step(name={self.name!r})"

class Pipeline:
    """
    Оркестратор для управления последовательным выполнением шагов.
    
    Реализует паттерн Chain of Responsibility.
    """
    def __init__(self, name: str = "Default Pipeline"):
        self.name = name
        self.steps: list[Step] = []
    
    def add_step(self, step: Step) -> 'Pipeline':
        """
        Добавить шаг в конвейер.
        
        Args:
            step: Объект шага для добавления
            
        Returns:
            self для цепочки вызовов (fluent interface)
        """
        self.steps.append(step)
        logger.debug("Добавлен шаг: {}", step.name)
        return self
    
    def __repr__(self) -> str:
        return f"Pipeline(name={self.name!r}, steps={len(self.steps)})"
    
    def run(self, initial_context: ProcessingContext) -> ProcessingContext:
        context = initial_context
        total_steps = len(self.steps)
        logger.info("Запуск конвейера '{}' (всего шагов: {})", self.name, total_steps)
        
        for i, step in enumerate(self.steps, 1):
            logger.info("[{:02d}/{:02d}] {}", i, total_steps, step.name)
            if step.description:
                logger.debug("Описание: {}", step.description)
            
            try:
                context = step.execute(context)
                logger.debug("[OK] Шаг '{}' успешно завершен", step.name)
            except ProcessingStepError:
                # Уже обработано в декораторе handle_pipeline_errors —
                # логируем промежуточную сводку и пробрасываем
                self._log_step_summary(context)
                raise
            except Exception as e:
                # ★ ИСПРАВЛЕНИЕ: logger.exception автоматически логирует traceback
                self._log_step_summary(context)
                logger.exception(
                    "[!!] Критическая ошибка на шаге '{}': {}: {}",
                    step.name,
                    type(e).__name__,
                    e,
                )
                # ★ Пробрасываем оригинал, сохраняя цепочку (raise ... from e)
                raise ProcessingStepError(
                    f"Сбой конвейера на шаге '{step.name}'"
                ) from e
        
        logger.info("Конвейер '{}' успешно завершен", self.name)
        self._log_step_summary(context)
        return context
    
    def _log_step_summary(self, context: ProcessingContext) -> None:
        """
        Логирует сводку выполнения шагов: статус и длительность каждого.

        Метрики заполняются декоратором handle_pipeline_errors
        (context.step_metrics). Сводка помогает при отладке: видно,
        какой шаг сколько выполнялся и на каком шаге произошёл сбой.
        """
        try:
            metrics = getattr(context, "step_metrics", None)
            if not metrics:
                return
            total = sum(m.get("duration_sec", 0.0) for m in metrics)
            # DEBUG, а не INFO: сводка — техническая информация для разработчика
            # (в app.log DEBUG пишется, на консоль пользователя не выводится)
            logger.debug(
                "=== Сводка шагов конвейера '{}' (итого: {:.2f} сек) ===",
                self.name, total,
            )
            for m in metrics:
                line = "  {:<45} {:<6} {:>8.2f} сек".format(
                    str(m.get("step", "?"))[:45],
                    str(m.get("status", "?")),
                    float(m.get("duration_sec", 0.0)),
                )
                error = m.get("error")
                if error:
                    line += f" | {error}"
                rows = m.get("rows")
                if rows:
                    rows_str = ", ".join(f"{k}={v}" for k, v in rows.items())
                    line += f" | строк: {rows_str}"
                logger.debug(line)
        except Exception:
            # Сводка — вспомогательный механизм, не должна ломать конвейер
            logger.debug("Не удалось сформировать сводку по шагам")







