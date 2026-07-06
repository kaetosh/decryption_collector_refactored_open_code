"""
Шаг 2: Объединение и выравнивание сводной ОСВ.
Загружает выгруженные регистры и собирает их в одну сводную таблицу.
Нормализует структуру данных.
"""
from loguru import logger

from pipeline.base import Step, ProcessingContext
from io_module import DataLoader
from utils import find_target_column

class Step2FlatSummaryOSVStep(Step):
    """
    Шаг 2: Объединение и выравнивание сводной ОСВ.
    
    Загружает выгруженные регистры и собирает их в одну сводную таблицу.
    Нормализует структуру данных.
    """
    def __init__(self):
        super().__init__(
            name="Шаг 2: Объединение в Свод ОСВ по счетам",
            description="Загрузка и объединение выгруженных регистров"
        )
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Объединение данных из выгруженных регистров")
        
        osv_all_df = DataLoader.load_account_osv()
        
        # 1. Считаем сальдо (ДО удаления столбцов!)
        osv_all_df['Сальдо, тыс.ед.'] = (
            osv_all_df['Дебет_конец']
            .sub(osv_all_df['Кредит_конец'], fill_value=0)
            .div(1_000)
            .round(2)
        )
        
        # 2. Фильтруем нулевые сальдо
        osv_all_df = osv_all_df[osv_all_df['Сальдо, тыс.ед.'] != 0].copy()
        
        # 3. Удаляем все ненужные столбцы одним вызовом
        cols_to_drop = [
            'Дебет_начало', 'Кредит_начало', 
            'Дебет_оборот', 'Кредит_оборот',
            'Дебет_конец', 'Кредит_конец',
            'Начало периода для вида связи', 
            'Конец периода для вида связи',
            'Исх.файл'
        ]
        osv_all_df = osv_all_df.drop(columns=cols_to_drop, errors='ignore')
        
        # 4. Определяем допсубконто
        name_leftcol = find_target_column(
            osv_all_df,
            column_prefix='Level_',
            search_direction='leftmost',
            account_type='no_accounts',
            shift=0
        )
        osv_all_df['допсубконто'] = osv_all_df[name_leftcol]
        
        # Приведем имена столбцов в нижний регистр для универсальности в следующих изменениях
        osv_all_df.columns = osv_all_df.columns.str.lower()
        
        # Сохраняем в контекст
        context.main_df = osv_all_df
        
        return context