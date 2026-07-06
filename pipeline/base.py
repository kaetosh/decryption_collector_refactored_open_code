# -*- coding: utf-8 -*-
"""
Базовые классы для построения конвейера обработки.
Реализуют паттерны Command и Chain of Responsibility.
"""


from abc import ABC, abstractmethod
from datetime import datetime
import pandas as pd
from typing import Any, Dict, Optional
from loguru import logger
from config.settings import (TOLERANCE,
                             OUTPUT_DATA_DIR,
                             STRICT_CONTRACTOR_CHECK)
from pipeline.errors import (
    ReferenceMismatchError,
    MissingFilesError,
    MissingContractorError
)


class ProcessingStepError(Exception):
    pass

class ProcessingContext:
    """
    Контекст для передачи данных между шагами конвейера.
    
    Атрибуты:
        main_df: Основной DataFrame для обработки
        data: Словарь для хранения вспомогательных данных (справочники, метаданные)
        metadata: Метаданные о процессе (имя компании, период, и т.д.)
    """
    def __init__(self):
        self.main_df: Optional[pd.DataFrame] = None
        self.data: Dict[str, Any] = {}
        self.metadata: Dict[str, Any] = {}
    
    def set_metadata(self, key: str, value: Any):
        """Установить метаданные."""
        self.metadata[key] = value
    
    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Получить метаданные."""
        return self.metadata.get(key, default)

class Step(ABC):
    """
    Абстрактный базовый класс для всех шагов обработки.
    """
    
    UNSPECIFIED = 'не_указано'
    
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
    
    def execute(self, context: 'ProcessingContext') -> 'ProcessingContext':
        """
        Публичный метод, который запускает шаг.
        """
        logger.info(f"--- Начало этапа: {self.name} ---")
        
        try:
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
            
            logger.info(f"--- Успешное завершение этапа: {self.name} ---")
            
            return context
        
        except MissingContractorError as e:
            # ★ Специальная обработка для неизвестных контрагентов
            e.step_name = self.name
            self._save_reference_mismatch_report(e)
            
            if STRICT_CONTRACTOR_CHECK:
                # Строгий режим: падаем
                logger.error(
                    f"❌ Критическая ошибка: неизвестные контрагенты "
                    f"на этапе '{self.name}': {e}"
                )
                raise ProcessingStepError(f"Сбой на этапе '{self.name}'") from e
            else:
                # Мягкий режим: заменяем на '3 лица' и продолжаем
                logger.warning(
                    f"⚠️ Мягкий режим: неизвестные контрагенты заменены на "
                    f"'{e.replacement_value}'"
                )
                context = self._apply_soft_contractor_handling(context, e)
                return context
            
        except ReferenceMismatchError as e:
            # Существующая обработка для других ошибок справочников
            e.step_name = self.name
            self._save_reference_mismatch_report(e)
            logger.error(f"❌ Ошибка несоответствия справочнику на этапе '{self.name}': {e}")
            raise ProcessingStepError(f"Сбой на этапе '{self.name}'") from e
            
        except MissingFilesError as e:
            e.step_name = self.name
            self._save_missing_files_report(e)
            logger.error(f"❌ Ошибка: отсутствуют файлы выгрузок на этапе '{self.name}': {e}")
            raise ProcessingStepError(f"Сбой на этапе '{self.name}'") from e
            
        except Exception as e:
            logger.error(f"❌ Ошибка на этапе '{self.name}': {e}")
            raise ProcessingStepError(f"Сбой на этапе '{self.name}'") from e
    
    @abstractmethod
    def _process(self, context: 'ProcessingContext') -> 'ProcessingContext':
        """
        Абстрактный метод для реализации конкретной логики шага.
        Наследники должны реализовывать именно его, а не execute.
        """
        pass
    
    def _validate_input(self, context: 'ProcessingContext'):
        """
        Базовая валидация входа. 
        Переопределяется в наследниках, если нужна специфичная проверка.
        """
        pass
    
    def _validate_output(self, context: 'ProcessingContext'):
        """
        Универсальная валидация выхода для всех шагов.
        Проверяет:
        1. Сходимость сальдо (если есть столбец 'сальдо, тыс.ед.')
        2. Отсутствие столбцов с типом object
        """
        df = context.main_df
        
        # 1. Проверяем наличие данных ДО любых действий
        if df is None or df.empty:
            logger.warning(f"Этап '{self.name}': main_df пуст, пропускаем валидацию")
            return
        
        # 2. Проверка сходимости сальдо
        # ★ ИСПРАВЛЕНИЕ: используем правильное имя столбца (в нижнем регистре)
        balance_col = 'сальдо, тыс.ед.'
        if balance_col in df.columns:
            balance_sum = df[balance_col].sum()
            if abs(balance_sum) > TOLERANCE:
                raise ValueError(
                    f"После этапа '{self.name}' ОСВ не сошлась: "
                    f"сумма сальдо = {balance_sum:.2f} тыс.ед. (допуск: {TOLERANCE})"
                )
            logger.debug(f"Сходимость сальдо: {balance_sum:.2f} тыс.ед.")
        
        # 3. Проверка отсутствия object типов
        object_cols = [col for col, dtype in df.dtypes.items() if dtype == 'object']
        if object_cols:
            raise TypeError(
                f"После этапа '{self.name}' обнаружены столбцы с типом 'object': {object_cols}. "
                f"Используйте 'string' или числовые типы."
            )
    
    @staticmethod
    def clean_whitespace(df: pd.DataFrame) -> pd.DataFrame:
        """
        Очищает все строковые столбцы DataFrame от лишних пробелов.
        """
        df_clean = df.copy()
        
        string_columns = df_clean.select_dtypes(include=['string', 'object']).columns
        
        for col in string_columns:
            if df_clean[col].dtype == 'object':
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
            f"Валидация столбца '{column_name}' ({column_purpose}) пройдена: "
            f"{match_rate:.0%} совпадений, {values.nunique()} уникальных значений"
        )
        
    def _clean_whitespace(self, context: 'ProcessingContext') -> 'ProcessingContext':
        """Обертка для очистки context.main_df."""
        context.main_df = self.clean_whitespace(context.main_df)
        return context
    
    def _move_and_sort_level_columns(self, context: 'ProcessingContext') -> 'ProcessingContext':
        """
        Переносит столбцы Level_* в конец DataFrame и сортирует их по возрастанию.
        """
        df = context.main_df.copy()
        
        if df.empty:
            return context
        
        # Находим столбцы Level_* (регистронезависимо)
        level_cols = [
            col for col in df.columns 
            if str(col).lower().startswith('level_')
        ]
        
        if not level_cols:
            return context
        
        # Сортируем level_* по числовому суффиксу
        def extract_level_number(col_name: str) -> int:
            try:
                suffix = str(col_name).split('_', 1)[1]
                return int(suffix)
            except (IndexError, ValueError):
                return float('inf')
        
        level_cols_sorted = sorted(level_cols, key=extract_level_number)
        regular_cols = [col for col in df.columns if col not in level_cols]
        new_order = regular_cols + level_cols_sorted
        
        context.main_df = df[new_order]
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
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            
            # ★ КОРОТКОЕ имя шага: только номер (например, "step_14", "step_11a", "step_1a")
            step_slug = self._short_step_slug()
            
            # ★ КОРОТКОЕ имя справочника (обрезка до 30 символов)
            ref_slug = self._short_slug(error.reference_name or 'unknown', max_len=30)
            
            filename = f"mismatch_{step_slug}_{ref_slug}_{timestamp}.xlsx"
            output_path = OUTPUT_DATA_DIR / "mismatches" / filename
            
            output_path.parent.mkdir(parents=True, exist_ok=True)
            error.problem_data.to_excel(output_path, index=False)
            
            logger.error(
                f"📁 Проблемные данные сохранены в: "
                f"{output_path.parent.name}/{output_path.name}"
            )
            
        except PermissionError:
            logger.error(
                f"⚠️ НЕ УДАЛОСЬ сохранить файл '{filename}': "
                f"файл открыт в другой программе (Excel?) или нет прав на запись.\n"
                f"Закройте файл и повторите попытку, либо проверьте права доступа к папке "
                f"{output_path.parent}.\n"
                f"Проблемные данные ({len(error.problem_data)} строк) НЕ были сохранены."
            )
        except Exception as save_error:
            logger.error(f"Не удалось сохранить файл с проблемными данными: {save_error}")

    
    @staticmethod
    def _slugify(text: str) -> str:
        """Преобразует текст в безопасное имя файла."""
        import re
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
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            
            # ★ КОРОТКОЕ имя шага
            step_slug = self._short_step_slug()
            
            filename = f"missing_files_{step_slug}_{timestamp}.xlsx"
            output_path = OUTPUT_DATA_DIR / "mismatches" / filename
            
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            df = pd.DataFrame({
                'отсутствующий_файл': error.missing_files,
                'ожидаемая_директория': error.expected_dir,
                'шаг': error.step_name
            })
            df.to_excel(output_path, index=False)
            
            logger.error(
                f"📁 Список отсутствующих файлов сохранён в: "
                f"{output_path.parent.name}/{output_path.name}"
            )
            
        except PermissionError:
            logger.error(
                f"⚠️ НЕ УДАЛОСЬ сохранить файл '{filename}': "
                f"файл открыт в другой программе (Excel?) или нет прав на запись.\n"
                f"Закройте файл и повторите попытку."
            )
        except Exception as save_error:
            logger.error(f"Не удалось сохранить файл с отсутствующими файлами: {save_error}")
    
    
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
        import re
        
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
        import re
        
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
    # МЯГКАЯ ОБРАБОТКА MISSING SUBTYPE
    # =========================================================================
    
    # def _apply_soft_subtype_handling(
    #     self, 
    #     context: 'ProcessingContext', 
    #     error: MissingSubtypeError
    # ) -> 'ProcessingContext':
    #     """
    #     Заменяет неучтённые подвиды на 'Прочая ДЗ'/'Прочая КЗ'.
        
    #     Вызывается в мягком режиме (STRICT_SUBTYPE_CHECK=False).
    #     """
    #     df = context.main_df.copy()
        
    #     mask_unspecified = df['подвид_задолженности'] == ReceivableClassifier.UNSPECIFIED
    #     mask_debit = mask_unspecified & (df['вид_задолженности'] == ReceivableClassifier.DEBIT)
    #     mask_credit = mask_unspecified & (df['вид_задолженности'] == ReceivableClassifier.CREDIT)
        
    #     replaced_count = mask_debit.sum() + mask_credit.sum()
        
    #     df.loc[mask_debit, 'подвид_задолженности'] = ReceivableClassifier.OTHER_DEBIT
    #     df.loc[mask_credit, 'подвид_задолженности'] = ReceivableClassifier.OTHER_CREDIT
        
    #     logger.info(
    #         f"Заменено {replaced_count} неучтённых позиций: "
    #         f"{mask_debit.sum()} → '{ReceivableClassifier.OTHER_DEBIT}', "
    #         f"{mask_credit.sum()} → '{ReceivableClassifier.OTHER_CREDIT}'"
    #     )
        
    #     context.main_df = df
    #     return context
    
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
        df = context.main_df.copy()
        
        # Находим строки с UNSPECIFIED в целевом столбце
        mask = df[error.target_column] == 'не_указано'
        replaced_count = mask.sum()
        
        # Заменяем на значение из error
        df.loc[mask, error.target_column] = error.replacement_value
        
        logger.info(
            f"Заменено {replaced_count} неизвестных контрагентов "
            f"на '{error.replacement_value}' в столбце '{error.target_column}'"
        )
        
        context.main_df = df
        return context
    
    def __repr__(self) -> str:
        return f"Step({self.name})"

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
        logger.debug(f"Добавлен шаг: {step.name}")
        return self
    
    def run(self, initial_context: ProcessingContext) -> ProcessingContext:
        context = initial_context
        logger.info(f"Запуск конвейера '{self.name}' из {len(self.steps)} шагов")
        
        for i, step in enumerate(self.steps, 1):
            logger.info(f"[{i}/{len(self.steps)}] Выполнение: {step.name}")
            if step.description:
                logger.debug(f"Описание: {step.description}")
            
            try:
                context = step.execute(context)
                logger.debug(f"✓ Шаг '{step.name}' успешно завершен")
            except ProcessingStepError:
                # Уже обработано в Step.execute() — просто пробрасываем
                raise
            except Exception as e:
                # ★ ИСПРАВЛЕНИЕ: logger.exception автоматически логирует traceback
                logger.exception(
                    f"✗ Критическая ошибка на шаге '{step.name}': {type(e).__name__}: {e}"
                )
                # ★ Пробрасываем оригинал, сохраняя цепочку (raise ... from e)
                raise ProcessingStepError(
                    f"Сбой конвейера на шаге '{step.name}'"
                ) from e
        
        logger.info(f"Конвейер '{self.name}' успешно завершен")
        return context







