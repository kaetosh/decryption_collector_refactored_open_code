
"""
Кастомные исключения для пайплайна обработки данных.

Иерархия:
    PipelineError (базовое)
    └── ReferenceMismatchError (несоответствие справочникам)
        ├── MissingMappingError (нет записи в меппинге)
        ├── MissingContractorError (нет контрагента)
        ├── MissingOSGroupError (нет группы ОС)
        └── ConvergenceError (расхождение сумм)
"""
import pandas as pd
from typing import Optional, Any


class PipelineError(Exception):
    """Базовое исключение для всех ошибок пайплайна."""
    pass

class InputDataError(PipelineError):
    """
    Базовое исключение для ошибок, связанных с входными данными.
    
    Используется, когда проблема не в справочниках, а в самих данных:
    - отсутствуют файлы
    - файлы повреждены
    - неверный формат
    """
    pass

class MissingFilesError(InputDataError):
    """
    Исключение для случая, когда отсутствуют необходимые файлы выгрузок.
    
    Attributes:
        message: Человекочитаемое сообщение
        missing_files: Список отсутствующих файлов
        expected_dir: Ожидаемая директория
        metadata: Дополнительные данные
        
    """
    
    def __init__(
        self,
        message: str,
        missing_files: list = None,
        expected_dir: str = None,
        **metadata
    ):
        self.message = message
        self.missing_files = missing_files or []
        self.expected_dir = expected_dir
        self.metadata = metadata
        self.step_name = None
        
        super().__init__(message)
    
    def __str__(self) -> str:
        parts = [self.message]
        if self.expected_dir:
            parts.append(f"[Директория: {self.expected_dir}]")
        parts.append(f"[Отсутствует файлов: {len(self.missing_files)}]")
        return " | ".join(parts)

class ReferenceMismatchError(PipelineError):
    """
    Исключение для ошибок несоответствия данных справочникам.
    
    Содержит всю информацию, необходимую для сохранения проблемных данных
    в Excel и последующего обновления справочника.
    
    Attributes:
        message: Человекочитаемое сообщение об ошибке
        problem_data: DataFrame с проблемными данными (для сохранения в Excel)
        reference_name: Название справочника, которому не соответствуют данные
        step_name: Название шага, на котором возникла ошибка (заполняется автоматически)
        metadata: Дополнительные данные (словарь)
    """
    
    def __init__(
        self,
        message: str,
        problem_data: Optional[pd.DataFrame] = None,
        reference_name: Optional[str] = None,
        **metadata: Any
    ):
        self.message = message
        self.problem_data = problem_data
        self.reference_name = reference_name
        self.metadata = metadata
        self.step_name = None  # Заполняется в base.py
        
        super().__init__(message)
    
    def __str__(self) -> str:
        parts = [self.message]
        if self.reference_name:
            parts.append(f"[Справочник: {self.reference_name}]")
        if self.problem_data is not None:
            parts.append(f"[Строк с проблемами: {len(self.problem_data)}]")
        return " | ".join(parts)


# =========================================================================
# СПЕЦИАЛИЗИРОВАННЫЕ ИСКЛЮЧЕНИЯ
# =========================================================================

class MissingMappingError(ReferenceMismatchError):
    """Нет записи в справочнике Меппинг."""
    pass

class MissingOSGroupError(ReferenceMismatchError):
    """Группа ОС не найдена в справочнике ППА."""
    pass


class ConvergenceError(ReferenceMismatchError):
    """Расхождение сумм при сверке."""
    pass


class MissingSubtypeError(ReferenceMismatchError):
    """Подвид задолженности не учтён в Меппинге."""
    pass

class MissingContractorError(ReferenceMismatchError):
    """
    Контрагент не найден в справочнике или основной ОСВ.
    
    Attributes:
        target_column: Столбец, в котором нужно заменить значения (в мягком режиме)
        replacement_value: Значение для замены (обычно '3 лица')
    """
    
    def __init__(
        self,
        message: str,
        problem_data: Optional[pd.DataFrame] = None,
        reference_name: Optional[str] = None,
        target_column: str = 'вид_связи',
        replacement_value: str = '3 лица',
        **metadata
    ):
        self.target_column = target_column
        self.replacement_value = replacement_value
        super().__init__(
            message=message,
            problem_data=problem_data,
            reference_name=reference_name,
            **metadata
        )

class MissingCardError(MissingFilesError):
    """Карточка счета не найдена в папке account_cards."""
    pass