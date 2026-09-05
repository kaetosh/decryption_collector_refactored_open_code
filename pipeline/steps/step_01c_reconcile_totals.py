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
from utils import find_target_column, normalize_account
from pipeline.errors import ConvergenceError


class Step1cReconcileTotalsStep(Step):
    """
    Шаг 1в: Сверка остатков и оборотов между общей ОСВ и детальных выгрузок.
    """
    # CONVERGENCE_TOLERANCE = 1000

    def __init__(self):
        super().__init__(
            name="Шаг 1в: Реконциляция",
            description="Проверка сходимости итогов общей ОСВ и детальных выгрузок. Расхождения вызывают ошибку."
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Начало реконциляции итогов")
        
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
        
        accounts_from_general_osv = context.data.get('accounts_from_general_osv', [])
        
        # Сохраняем СЫРОЙ вариант в data — Step 2 ожидает Дебет_конец/Кредит_конец
        # и сам считает сальдо (см. step_02_flat_osv.py:34-35).
        # В context.summary_osv_df сохраняем этот же сырой вариант (Step 2 его прочтёт,
        # посчитает сальдо, удалит лишние колонки и вернёт обратно).
        context.data['osv_all_raw'] = osv_all_df.copy()
        context.summary_osv_df = osv_all_df.copy()
        
        # Дальше работаем с копией для целей реконциляции (фильтруем, дропаем колонки).
        # Колонки Дебет_конец/Кредит_конец НЕ дропаем здесь — они нужны в Step 2.
        # Вместо этого сохраняем рабочую копию для реконциляции в локальную переменную.
        osv_for_recon = osv_all_df.copy()
        osv_for_recon = self.clean_whitespace(osv_for_recon)
        osv_for_recon['сальдо_свернуто, тыс. ед.'] = osv_for_recon['Дебет_конец'].sub(osv_for_recon['Кредит_конец'], fill_value=0).div(1_000).round(2)
        osv_for_recon = osv_for_recon[osv_for_recon['сальдо_свернуто, тыс. ед.'] != 0].copy()
        
        cols_to_drop = [
            'Дебет_начало', 'Кредит_начало', 'Дебет_оборот', 'Кредит_оборот',
            'Начало периода для вида связи', 
            'Конец периода для вида связи',
            'Исх.файл'
        ]
        osv_for_recon = osv_for_recon.drop(columns=cols_to_drop, errors='ignore')
        
        name_col_with_all_account = find_target_column(
            osv_for_recon, column_prefix='Level_', search_direction='rightmost', account_type='all_accounts', shift=0
        )
        if not name_col_with_all_account:
            raise ValueError("В сводной ОСВ по счетам не найден столбец Level_, содержащий только бухгалтерские счета")
            
        osv_for_recon['синтетический_счет'] = osv_for_recon[name_col_with_all_account].astype(str).str[:2]
        
        # ★ ИСПРАВЛЕНИЕ: исключаем синтетические счета, которые берутся из общей ОСВ.
        # Применяем фильтр и к osv_for_recon (для реконциляции), и к osv_all_df
        # (который сохранён в context.data['osv_all_raw'] и будет использован в Step 2).
        if accounts_from_general_osv:
            # Фильтруем данные для реконциляции
            before = len(osv_for_recon)
            osv_for_recon = osv_for_recon[~osv_for_recon['синтетический_счет'].isin(accounts_from_general_osv)].copy()
            removed = before - len(osv_for_recon)
            if removed > 0:
                logger.debug(
                    "Исключено {} строк по синтетическим счетам из сводной ОСВ (берутся из общей ОСВ): {}",
                    removed,
                    ', '.join(sorted(accounts_from_general_osv))
                )
            
            # Также фильтруем СЫРОЙ osv_all_df, чтобы Step 2 не получил лишние строки
            # Находим колонку Level_ с кодами счетов (так же как в Step 3)
            raw_clean = self.clean_whitespace(osv_all_df)
            raw_name_col = find_target_column(
                raw_clean, column_prefix='Level_', search_direction='rightmost',
                account_type='all_accounts', shift=0
            )
            if raw_name_col:
                raw_synth = raw_clean[raw_name_col].astype(str).str[:2]
                mask_keep = ~raw_synth.isin(accounts_from_general_osv)
                before_raw = len(osv_all_df)
                osv_all_df = osv_all_df[mask_keep].copy()
                removed_raw = before_raw - len(osv_all_df)
                if removed_raw > 0:
                    logger.debug(
                        "Из сырого osv_all_df исключено {} строк по синтетическим счетам",
                        removed_raw,
                    )
                    context.data['osv_all_raw'] = osv_all_df.copy()
                    context.summary_osv_df = osv_all_df.copy()
            else:
                logger.warning(
                    "Не удалось найти колонку Level_ с кодами счетов в сырых данных. "
                    "Синтетические счета {} НЕ будут отфильтрованы из сводной ОСВ. "
                    "Это может привести к дублированию в Step 2а.",
                    ', '.join(sorted(accounts_from_general_osv))
                )
        
        missing_acc_all = [x for x in osv_for_recon['синтетический_счет'].unique() if x not in unique_chart_accounts]
        if missing_acc_all:
            logger.warning(
                "В детальной ОСВ обнаружены неизвестные синтетические счета: {}",
                missing_acc_all,
            )
            
        osv_all_agg = osv_for_recon.groupby('синтетический_счет')[['сальдо_свернуто, тыс. ед.']].sum().reset_index()

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
            (merged_turnover['дебет_оборот, тыс. ед._diff'].abs() > context.tolerance_params['tolerance_reconciliation']) |
            (merged_turnover['кредит_оборот, тыс. ед._diff'].abs() > context.tolerance_params['tolerance_reconciliation'])
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
            (merged_balance['сальдо_свернуто, тыс. ед._diff'].abs() > context.tolerance_params['tolerance_reconciliation'])
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
                error_messages.append(f"• Сальдо (Общая ОСВ vs ОСВ по счетам): расхождения превышают порог {context.tolerance_params['tolerance_reconciliation']} тыс. ед.")
            if has_turnover_error:
                error_messages.append(f"• Обороты (Общая ОСВ vs Отчеты по проводкам): расхождения превышают порог {context.tolerance_params['tolerance_reconciliation']} тыс. ед.")
                
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
                tolerance=context.tolerance_params['tolerance_reconciliation']
            )
            
        logger.info(
            "[OK] Реконциляция пройдена: расхождения в сальдо и оборотах "
            "в пределах нормы (до {} тыс. ед.)",
            context.tolerance_params['tolerance_reconciliation'],
        )
        return context
