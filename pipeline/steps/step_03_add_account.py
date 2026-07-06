"""
Шаг 3: Добавление столбца 'счет'.

Извлекает из иерархических столбцов Level_* самый глубокий уровень
(полный счет с субсчетами) и добавляет его как отдельный столбец.
"""
from loguru import logger

from pipeline.base import Step, ProcessingContext
from utils import find_target_column

class Step3AddAccountColumnStep(Step):
    """
    Шаг 3: Добавление столбца 'счет'.
    
    Извлекает из иерархических столбцов Level_* самый глубокий уровень
    (полный счет с субсчетами) и добавляет его как отдельный столбец.
    """
    def __init__(self):
        super().__init__(
            name="Шаг 3: Счет БУ",
            description="Извлечение полного счета из иерархии Level_*"
        )
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Добавление столбца счета")
        
        # Делаем копию, чтобы не модифицировать оригинал в контексте
        osv_all_df = context.main_df.copy()
        
        # Поиск столбца содержащего только счета
        name_col_with_all_account = find_target_column(
            osv_all_df,
            column_prefix='level_',
            search_direction='rightmost',
            account_type='all_accounts',
            shift=0
        )
        
        if not name_col_with_all_account:
            raise ValueError(
                "В сводной ОСВ по счетам не найден столбец Level_ содержащий только бухгалтерские счета"
            )
        
        logger.debug(f"Найден столбец со счетами: {name_col_with_all_account}")
        
        # Добавляем столбец с явным типом (copy() не нужен, astype уже создает копию)
        osv_all_df['счет'] = osv_all_df[name_col_with_all_account].astype('string')
        
        # Удаляем Level-столбцы (переприсваивание вместо inplace)
        level_columns = [col for col in osv_all_df.columns if col.startswith('Level_')]
        osv_all_df = osv_all_df.drop(columns=level_columns, errors='ignore')
        
        logger.debug(f"Удалено {len(level_columns)} столбцов Level_")
        
        # Переименовываем для соответствия меппингу
        rename_dict = {'вид связи ка за период': 'вид_связи'}
        osv_all_df = osv_all_df.rename(columns=rename_dict)
        
        logger.debug(f"Переименованы столбцы: {rename_dict}")
        
        # Сохраняем результат
        context.main_df = osv_all_df
        
        return context