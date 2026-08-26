# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
Шаг 1c: Сверка остатков и оборотов между общей ОСВ и выгрузками (реконциляция)
Оптимизированная версия с векторизацией (how='inner' сохранен по бизнес-требованиям)
"""
import pandas as pd
from loguru import logger
from pipeline.base import Step, ProcessingContext
from io_module import DataLoader
from utils import find_target_column
from pipeline.errors import ConvergenceError


def normalize_account(series: pd.Series) -> pd.Series:
    """
    Векторизованное приведение счета к синтетическому уровню.
    По умолчанию 2 символа, для счетов 90 и 91 - 5 символов.
    Работает в разы быстрее, чем apply с lambda.
    """
    s = series.astype(str)
    res = s.str[:2].copy()
    mask_90_91 = s.str.startswith(('90', '91'))
    res.loc[mask_90_91] = s.loc[mask_90_91].str[:5]
    return res


class Step1cReconcileTotalsStep(Step):
    """
    Шаг 1в: Сверка остатков и оборотов между общей ОСВ и детальных выгрузок.
    """
    CONVERGENCE_TOLERANCE = 1000

    def __init__(self):
        super().__init__(
            name="Шаг 1в: Реконциляция",
            description="Проверка сходимости итогов общей ОСВ и детальных выгрузок. Расхождения вызывают ошибку."
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.info("Начало шага 1в: Реконциляция итогов")
        
        # ==========================================
        # 1. Подготовка данных: Отчеты по проводкам
        # ==========================================
        transactions_all_df = DataLoader.load_transaction_report()
        #  сохраняем в контекст
        context.journal_df = transactions_all_df
        transactions_all_df = self.clean_whitespace(transactions_all_df)
        
        col_transac = ['Имя_файла', 'Дт', 'Кт', 'Сумма']
        transactions_all_df = transactions_all_df.loc[:, col_transac].copy()
        
        # Извлекаем номер счета из имени файла
        transactions_all_df['синтетический_счет'] = (
            transactions_all_df['Имя_файла']
            .str.split('_', n=3, expand=True)[2]
            .astype(str)
        )
        
        # Приводим Дт и Кт к единому формату синтетического счета (ВЕКТОРИЗОВАННО)
        transactions_all_df['Дт'] = normalize_account(transactions_all_df['Дт'])
        transactions_all_df['Кт'] = normalize_account(transactions_all_df['Кт'])
        
        # Векторизованное вычисление сумм (вместо медленного apply axis=1)
        mask_dt = transactions_all_df['синтетический_счет'] == transactions_all_df['Дт']
        mask_ct = transactions_all_df['синтетический_счет'] == transactions_all_df['Кт']
        
        transactions_all_df['dt_amount'] = transactions_all_df['Сумма'] * mask_dt
        transactions_all_df['ct_amount'] = transactions_all_df['Сумма'] * mask_ct
        
        # Группируем и считаем итоги
        agg_df = transactions_all_df.groupby('синтетический_счет').agg(
            debit_turnover=('dt_amount', 'sum'),
            credit_turnover=('ct_amount', 'sum')
        ).reset_index()
        
        result_df = agg_df.rename(columns={
            'debit_turnover': 'дебет_оборот, тыс. ед.',
            'credit_turnover': 'кредит_оборот, тыс. ед.'
        })
        result_df['дебет_оборот, тыс. ед.'] = result_df['дебет_оборот, тыс. ед.'].div(1_000).round(2)
        result_df['кредит_оборот, тыс. ед.'] = result_df['кредит_оборот, тыс. ед.'].div(1_000).round(2)

        # ==========================================
        # 2. Подготовка данных: Общая ОСВ
        # ==========================================
        osv_df = context.common_osv_df.copy()
        chart_accounts = context.references.get('план_счетов_бу', None)
        if chart_accounts is None:
            raise ValueError('Не загружен справочник: план счетов БУ.')
            
        unique_chart_accounts = set(normalize_account(chart_accounts['код']))
        
        osv_df['синтетический_счет'] = normalize_account(osv_df['Счет'])
        missing_acc = [x for x in osv_df['синтетический_счет'].unique() if x not in unique_chart_accounts]
        if missing_acc:
            raise ValueError(f"Обнаружены несуществующие синтетические счета в общей ОСВ: {missing_acc}")
            
        osv_agg = osv_df.groupby('синтетический_счет')[['Дебет_оборот', 'Кредит_оборот', 'Дебет_конец', 'Кредит_конец']].sum().reset_index()
        
        osv_agg['сальдо_свернуто, тыс. ед.'] = osv_agg['Дебет_конец'].sub(osv_agg['Кредит_конец'], fill_value=0).div(1_000).round(2)
        osv_agg['дебет_оборот, тыс. ед.'] = osv_agg['Дебет_оборот'].div(1_000).round(2)
        osv_agg['кредит_оборот, тыс. ед.'] = osv_agg['Кредит_оборот'].div(1_000).round(2)
        
        osv_agg = osv_agg[['синтетический_счет', 'дебет_оборот, тыс. ед.', 'кредит_оборот, тыс. ед.', 'сальдо_свернуто, тыс. ед.']]
        
        # Фильтруем полностью нулевые строки
        osv_agg = osv_agg[
            (osv_agg['сальдо_свернуто, тыс. ед.'] != 0) | 
            (osv_agg['дебет_оборот, тыс. ед.'] != 0) |
            (osv_agg['кредит_оборот, тыс. ед.'] != 0)
        ].copy()

        # ==========================================
        # 3. Подготовка данных: ОСВ по счетам
        # ==========================================
        osv_all_df = DataLoader.load_account_osv()
        
        # Сохраняем в контекст
        context.summary_osv_df = osv_all_df
        
        osv_all_df = self.clean_whitespace(osv_all_df)
        osv_all_df['сальдо_свернуто, тыс. ед.'] = osv_all_df['Дебет_конец'].sub(osv_all_df['Кредит_конец'], fill_value=0).div(1_000).round(2)
        osv_all_df = osv_all_df[osv_all_df['сальдо_свернуто, тыс. ед.'] != 0].copy()
        
        cols_to_drop = [
            'Дебет_начало', 'Кредит_начало', 'Дебет_оборот', 'Кредит_оборот',
            'Дебет_конец', 'Кредит_конец', 'Начало периода для вида связи', 
            'Конец периода для вида связи', 'Исх.файл'
        ]
        osv_all_df = osv_all_df.drop(columns=cols_to_drop, errors='ignore')
        
        name_col_with_all_account = find_target_column(
            osv_all_df, column_prefix='Level_', search_direction='rightmost', account_type='all_accounts', shift=0
        )
        if not name_col_with_all_account:
            raise ValueError("В сводной ОСВ по счетам не найден столбец Level_, содержащий только бухгалтерские счета")
            
        osv_all_df['синтетический_счет'] = osv_all_df[name_col_with_all_account].astype(str).str[:2]
        
        missing_acc_all = [x for x in osv_all_df['синтетический_счет'].unique() if x not in unique_chart_accounts]
        if missing_acc_all:
            logger.warning(
                "В детальной ОСВ обнаружены неизвестные синтетические счета: {}",
                missing_acc_all,
            )
            
        osv_all_agg = osv_all_df.groupby('синтетический_счет')[['сальдо_свернуто, тыс. ед.']].sum().reset_index()

        # ==========================================
        # 4. МЕРДЖ ОБОРОТОВ (Общая ОСВ ↔ Отчет по проводкам)
        # ==========================================
        # how='inner' оставлен намеренно: проверяем только те счета, по которым есть обе выгрузки
        merged_turnover = osv_agg.merge(
            result_df, 
            on='синтетический_счет', 
            how='inner', 
            suffixes=('_osv', '_проводки'),
            indicator=True
        )
        
        turnover_cols_to_check = ['дебет_оборот, тыс. ед.', 'кредит_оборот, тыс. ед.']
        for col in turnover_cols_to_check:
            diff_col_name = f"{col}_diff"
            merged_turnover[diff_col_name] = (
                merged_turnover[f"{col}_osv"].fillna(0) - merged_turnover[f"{col}_проводки"].fillna(0)
            ).round(2)
            
        discrepancies_turnover = merged_turnover[
            (merged_turnover['_merge'] != 'both') | 
            (merged_turnover['дебет_оборот, тыс. ед._diff'].abs() > self.CONVERGENCE_TOLERANCE) |
            (merged_turnover['кредит_оборот, тыс. ед._diff'].abs() > self.CONVERGENCE_TOLERANCE)
        ].copy()
        
        discrepancies_turnover = discrepancies_turnover.loc[:, [
            'синтетический_счет', 'дебет_оборот, тыс. ед._osv', 'кредит_оборот, тыс. ед._osv',
            'дебет_оборот, тыс. ед._проводки', 'кредит_оборот, тыс. ед._проводки',
            'дебет_оборот, тыс. ед._diff', 'кредит_оборот, тыс. ед._diff'
        ]]

        # ==========================================
        # 5. МЕРДЖ САЛЬДО (Общая ОСВ ↔ ОСВ по счетам)
        # ==========================================
        # how='inner' оставлен намеренно
        merged_balance = osv_agg.merge(
            osv_all_agg, 
            on='синтетический_счет', 
            how='inner', 
            suffixes=('_osv', '_osv_all'),
            indicator=True
        )
        
        balance_cols_to_check = ['сальдо_свернуто, тыс. ед.']
        for col in balance_cols_to_check:
            diff_col_name = f"{col}_diff"
            merged_balance[diff_col_name] = (
                merged_balance[f"{col}_osv"].fillna(0) - merged_balance[f"{col}_osv_all"].fillna(0)
            ).round(2)
            
        discrepancies_balance = merged_balance[
            (merged_balance['_merge'] != 'both') | 
            (merged_balance['сальдо_свернуто, тыс. ед._diff'].abs() > self.CONVERGENCE_TOLERANCE)
        ].copy()
        
        discrepancies_balance = discrepancies_balance.loc[:, [
            'синтетический_счет', 'сальдо_свернуто, тыс. ед._osv', 
            'сальдо_свернуто, тыс. ед._osv_all', 'сальдо_свернуто, тыс. ед._diff'
        ]]

        # ==========================================
        # 6. Обработка и выброс ошибок (агрегированный)
        # ==========================================
        has_balance_error = not discrepancies_balance.empty
        has_turnover_error = not discrepancies_turnover.empty

        if has_balance_error or has_turnover_error:
            logger.error("Обнаружены расхождения между выгрузками.")
            
            error_messages = []
            if has_balance_error:
                error_messages.append(f"• Сальдо (Общая ОСВ vs ОСВ по счетам): расхождения превышают порог {self.CONVERGENCE_TOLERANCE} тыс. ед.")
            if has_turnover_error:
                error_messages.append(f"• Обороты (Общая ОСВ vs Отчеты по проводкам): расхождения превышают порог {self.CONVERGENCE_TOLERANCE} тыс. ед.")
                
            full_message = (
                "Обнаружены расхождения, превышающие установленный порог. "
                "Необходимы актуальные выгрузки из 1С (возможно, было перезакрытие периода).\n\n"
                "Детали:\n" + "\n".join(error_messages)
            )
            
            if has_balance_error:
                discrepancies_balance['Тип_проверки'] = 'Сальдо (ОСВ vs ОСВ по счетам)'
            if has_turnover_error:
                discrepancies_turnover['Тип_проверки'] = 'Обороты (ОСВ vs Проводки)'
                
            dfs_to_combine = []
            if has_balance_error:
                dfs_to_combine.append(discrepancies_balance)
            if has_turnover_error:
                dfs_to_combine.append(discrepancies_turnover)
                
            combined_problem_data = pd.concat(dfs_to_combine, ignore_index=True)
            
            raise ConvergenceError(
                message=full_message,
                problem_data=combined_problem_data,
                reference_name='Реконциляция итогов (Сальдо и Обороты)',
                tolerance=self.CONVERGENCE_TOLERANCE
            )
            
        logger.info(
            "Реконциляция прошла успешно: расхождения в сальдо и оборотах "
            "не превышают порог {} тыс. ед.",
            self.CONVERGENCE_TOLERANCE,
        )
        return context
