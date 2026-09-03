"""
Шаг 2: Объединение и выравнивание сводной ОСВ.
Загружает выгруженные регистры и собирает их в одну сводную таблицу.
Нормализует структуру данных.
"""
from loguru import logger
import pandas as pd

from pipeline.base import Step, ProcessingContext
from utils import find_target_column, needs_conversion, get_rate_for_date, convert_series

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
        
        osv_all_df = context.summary_osv_df.copy()
        
        # 1. Считаем сальдо (ДО удаления столбцов!)
        # ★ ИСПРАВЛЕНИЕ: приводим к numeric, чтобы избежать NaN при вычитании
        # (764 строки с нечисловыми значениями давали NaN в сальдо, которые
        # фильтр != 0 пропускал → баланс не сходился на -3 166 742 тыс.ед.)
        osv_all_df['Дебет_конец'] = pd.to_numeric(osv_all_df['Дебет_конец'], errors='coerce')
        osv_all_df['Кредит_конец'] = pd.to_numeric(osv_all_df['Кредит_конец'], errors='coerce')

        osv_all_df['Сальдо, тыс.ед.'] = (
            osv_all_df['Дебет_конец']
            .sub(osv_all_df['Кредит_конец'], fill_value=0)
            .div(1_000)
            .round(2)
        )

        # 2. Фильтруем нулевые и NaN сальдо
        # ★ ИСПРАВЛЕНИЕ: явно удаляем NaN (NaN != 0 → True, и строки оставались)
        osv_all_df = osv_all_df[osv_all_df['Сальдо, тыс.ед.'].notna() & (osv_all_df['Сальдо, тыс.ед.'] != 0)].copy()
        
        # 2а. Для валютных компаний добавляем рублёвый эквивалент сальдо
        # (курс на дату баланса, заданную в ask_balance_date_if_needed).
        # Оригинальный столбец 'Сальдо, тыс.ед.' не изменяется — все сверки
        # и проверки продолжают работать в валюте компании.
        if needs_conversion(context):
            rate = get_rate_for_date(context, context.balance_date)
            osv_all_df['Сальдо, тыс.руб.'] = convert_series(
                osv_all_df['Сальдо, тыс.ед.'], rate
            ).round(2)
            logger.info(
                "Сводная ОСВ: добавлен рублёвый эквивалент сальдо "
                "(курс {} на дату {}).",
                rate,
                context.balance_date,
            )
        
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
        context.summary_osv_df = osv_all_df
        
        return context