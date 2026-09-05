"""
Шаг 2а: Добавление сальдо синтетических счетов из общей ОСВ.
"""
import pandas as pd
from loguru import logger
from pipeline.base import Step, ProcessingContext
from pipeline.step_config import StepConstants
from utils import needs_conversion, get_rate_for_date, convert_series


class Step2aInjectFromGeneralOSVStep(Step):
    """
    Шаг 2а: Добавление сальдо синтетических счетов из общей ОСВ.
    
    Добавляет в сводную ОСВ строки для синтетических счетов (2-значные счета
    с регистром 'осв' и детализацией = 'нет'), используя данные из общей ОСВ
    вместо выгрузок.
    """
    
    def __init__(self):
        super().__init__(
            name="Шаг 2а: Добавление сальдо синтетических счетов из общей ОСВ",
            description="Подстановка сальдо для синтетических счетов из общей ОСВ (вместо выгрузки)"
        )
        self._skip_balance_validation = True
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Обработка Шаг 2а: Добавление сальдо синтетических счетов из общей ОСВ")
        
        accounts_from_general_osv = context.data.get('accounts_from_general_osv', [])
        
        if not accounts_from_general_osv:
            logger.debug("Нет счетов для загрузки из общей ОСВ, пропуск шага")
            return context
        
        common_osv_df = context.common_osv_df.copy()
        
        logger.debug(f"Обрабатываем {len(accounts_from_general_osv)} синтетических счетов из общей ОСВ")
        
        rows_to_add = []
        
        for account_code in accounts_from_general_osv:
            mask = common_osv_df['Счет'].astype(str).str.startswith(account_code)
            account_rows = common_osv_df[mask]
            
            if account_rows.empty:
                logger.debug(f"Счет {account_code} не найден в общей ОСВ, пропуск")
                continue
            
            account_rows = account_rows.copy()
            account_rows['Дебет_конец'] = pd.to_numeric(account_rows['Дебет_конец'], errors='coerce')
            account_rows['Кредит_конец'] = pd.to_numeric(account_rows['Кредит_конец'], errors='coerce')
            account_rows['Сальдо_тыс_ед'] = (
                account_rows['Дебет_конец']
                .sub(account_rows['Кредит_конец'], fill_value=0)
                .div(1_000)
                .round(2)
            )
            
            total_saldo = account_rows['Сальдо_тыс_ед'].sum()
            
            if pd.isna(total_saldo) or total_saldo == 0:
                logger.debug(f"Счет {account_code} имеет нулевое сальдо, пропуск")
                continue
            
            new_row = {}
            
            new_row['счет'] = account_code
            new_row['сальдо, тыс.ед.'] = total_saldo
            
            if needs_conversion(context):
                rate = get_rate_for_date(context, context.balance_date)
                new_row['сальдо, тыс.руб.'] = convert_series(pd.Series([total_saldo]), rate).iloc[0]
            
            summary_columns = list(context.summary_osv_df.columns)
            
            for col in summary_columns:
                col_lower = col.lower() if isinstance(col, str) else col
                if col_lower in ('level_1', 'level_2', 'level_3', 'level_4', 'level_5', 'level_6'):
                    new_row[col] = account_code
                elif col == 'допсубконто':
                    new_row[col] = StepConstants.UNSPECIFIED
                elif col not in new_row:
                    new_row[col] = StepConstants.UNSPECIFIED
            
            rows_to_add.append(new_row)
        
        if not rows_to_add:
            logger.debug("Не найдено строк для добавления из общей ОСВ")
            return context
        
        injected_df = pd.DataFrame(rows_to_add)
        
        for col in injected_df.columns:
            if col.lower().startswith('level_') or col == 'счет' or col == 'допсубконто':
                injected_df[col] = injected_df[col].astype('string')
            elif col == 'сальдо, тыс.ед.' or col == 'сальдо, тыс.руб.':
                injected_df[col] = pd.to_numeric(injected_df[col], errors='coerce')
            else:
                injected_df[col] = injected_df[col].astype('string')
        
        injected_df = injected_df.reindex(columns=context.summary_osv_df.columns)
        
        context.summary_osv_df = pd.concat([
            context.summary_osv_df, 
            injected_df
        ], ignore_index=True)
        
        logger.info(
            "Добавлено {} строк из общей ОСВ для синтетических счетов: {}",
            len(rows_to_add),
            ', '.join(sorted([r['счет'] for r in rows_to_add]))
        )
        
        return context