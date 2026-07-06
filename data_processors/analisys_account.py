"""
Обработчики анализа счета 1С (УПП и не-УПП).

Извлекает плоскую таблицу из иерархического отчёта "Анализ счета",
удаляет промежуточные (суммирующие) строки и выполняет сверку оборотов.
"""
import pandas as pd
import numpy as np
from typing import Tuple, List, Set, Dict
from io import BytesIO
from loguru import logger

from data_processors.file_processor import FileProcessor, exclude_values


accounts_without_subaccount = ['50', '51', '52', '55', '57']

class BaseAnalysisProcessor(FileProcessor):
    """
    Базовый класс для обработчиков Анализа счета 1С.
    
    Содержит общую логику:
    - Поиск заголовка и очистка
    - Формирование уровней иерархии
    - Удаление промежуточных (суммирующих) строк
    - Сверка оборотов с автоматическим удалением "мешающих" строк
    """
    
    def __init__(self):
        super().__init__()
        self.df_type_connection = pd.DataFrame()
        self.table_for_check = pd.DataFrame()
        self.file = None
    
    # =========================================================================
    # БАЗОВЫЕ УТИЛИТЫ
    # =========================================================================
    
    @staticmethod
    def _is_accounting_code_vectorized(series: pd.Series) -> pd.Series:
        """Проверяет, является ли значение бухгалтерским счетом."""
        return series.astype(str).str.match(r'^\d+(\.\d+)?$') & series.notna()
    
    def _is_parent(self, acc: str, all_accounts: List[str]) -> bool:
        """Проверяет, является ли счёт родителем для других счетов."""
        prefix = acc + '.'
        return any(sub.startswith(prefix) for sub in all_accounts if sub != acc)
    
    # =========================================================================
    # ПОИСК ЗАГОЛОВКА И ОЧИСТКА
    # =========================================================================
    
    @staticmethod
    def _process_header_and_clean(df: pd.DataFrame) -> pd.DataFrame:
        """
        Универсальная очистка и поиск заголовка для файлов Анализа счета.
        """
        MAX_HEADER_ROWS = 30
        
        # Удаляем полностью пустые строки и столбцы
        df = df.dropna(axis=1, how='all').dropna(axis=0, how='all')
        
        if df.empty:
            raise ValueError('Файл пуст после первоначальной очистки.')
        
        max_rows_to_check = min(MAX_HEADER_ROWS, df.shape[0])
        account_col_idx = None
        
        # Поиск столбца "Счет"
        for col_idx in range(df.shape[1]):
            col_values = df.iloc[:max_rows_to_check, col_idx].astype('string').str.strip().str.lower()
            if 'счет' in col_values.values:
                account_col_idx = col_idx
                break
        
        if account_col_idx is None:
            raise ValueError('Не найден столбец с "Счет" в первых 30 строках.')
        
        # Ищем строку заголовка
        first_col = df.iloc[:, account_col_idx].astype('string')
        mask = first_col.str.strip() == 'Счет'
        
        if not mask.any():
            mask = first_col.str.contains('Счет', na=False)
        
        if not mask.any():
            raise ValueError('Файл не является корректным Анализом счета 1С.')
        
        date_row_idx = mask.idxmax()
        
        # Присваиваем заголовки
        df.columns = df.iloc[date_row_idx]
        df = df.iloc[date_row_idx + 1:].copy()
        
        # Переименовываем первые два служебных столбца
        current_cols = df.columns.tolist()
        if len(current_cols) >= 2:
            df.columns = ['Уровень', 'Курсив'] + current_cols[2:]
        else:
            df.columns = ['Уровень', 'Курсив'] + current_cols
        
        # Удаляем столбцы с NA в имени
        df = df.loc[:, df.columns.notna()]
        df.columns = df.columns.astype('string')
        
        if 'Уровень' not in df.columns or df['Уровень'].isnull().all():
            raise ValueError('Отсутствует или пуст столбец "Уровень".')
        
        if df['Уровень'].isnull().any():
            df['Уровень'] = df['Уровень'].ffill().fillna(0)
        
        if df['Курсив'].isnull().any():
            df['Курсив'] = df['Курсив'].fillna(0)
        
        return df
    
    # =========================================================================
    # ФОРМИРОВАНИЕ УРОВНЕЙ И СЧЕТОВ
    # =========================================================================
    
    def _prepare_levels_and_accounts(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
        """Распределяет счета по уровням и формирует основные столбцы."""
        # Приводим 'Счет' к строке
        df['Счет'] = df['Счет'].astype('string').str.strip()
        
        # Заполняем пропуски в счетах
        df['Счет'] = df['Счет'].replace('', np.nan).ffill()
        
        # Добавляем ведущий ноль для односимвольных счетов
        account_is_valid = self._is_accounting_code_vectorized(df['Счет'])
        mask_pad = account_is_valid & (df['Счет'].str.len() == 1)
        df.loc[mask_pad, 'Счет'] = '0' + df.loc[mask_pad, 'Счет']
        
        # Формирование уровней (иерархия)
        max_level = int(df['Уровень'].max())
        level_cols = []
        
        for level in range(max_level + 1):
            col_name = f'Level_{level}'
            level_mask = df['Уровень'] == level
            df[col_name] = df['Счет'].where(level_mask)
            level_cols.append(col_name)
        
        # Заполняем иерархию через ffill
        for col in level_cols:
            df[col] = df[col].ffill()
        
        # Корреспондирующий счет
        korr_col_name = 'Кор.счет' if 'Кор.счет' in df.columns else 'Кор. Счет'
        if korr_col_name not in df.columns:
            candidates = [c for c in df.columns if 'кор' in c.lower()]
            korr_col_name = candidates[0] if candidates else None
        
        if korr_col_name:
            df['Корр_счет'] = df[korr_col_name].astype('string')
            valid_korr_mask = self._is_accounting_code_vectorized(df['Корр_счет'])
            df.loc[~valid_korr_mask, 'Корр_счет'] = np.nan
            
            # Добавляем ведущий ноль
            korr_str = df['Корр_счет'].astype('string')
            single_digit_mask = df['Корр_счет'].notna() & (korr_str.str.len() == 1)
            df.loc[single_digit_mask, 'Корр_счет'] = '0' + korr_str.loc[single_digit_mask]
        else:
            df['Корр_счет'] = np.nan
        
        return df, level_cols
    
    # =========================================================================
    # УДАЛЕНИЕ ПРОМЕЖУТОЧНЫХ СТРОК (КЛЮЧЕВАЯ ЛОГИКА!)
    # =========================================================================
    
    def _get_del_accounts(self, df: pd.DataFrame, korr_col: str) -> Set[str]:
        """
        Определяет список счетов, которые нужно удалить
        (родительские счета, у которых есть субсчета).
        
        Это КЛЮЧЕВАЯ логика для получения плоского отчёта!
        """
        # Считаем уникальные значения корр.счетов
        unique_df = df[~df[korr_col].isin(exclude_values)].dropna(subset=[korr_col])
        unique_df = unique_df[unique_df['Курсив'] == 0][[korr_col, 'Корр_счет']]
        unique_df = unique_df.drop_duplicates(subset=[korr_col, 'Корр_счет']).dropna(subset=['Корр_счет'])
        
        all_acc_dict = unique_df['Корр_счет'].value_counts().to_dict()
        
        # Находим счета-родители (у которых есть субсчета)
        acc_with_sub = [i for i in all_acc_dict if self._is_parent(i, list(all_acc_dict.keys()))]
        clean_acc = [i for i in all_acc_dict if all_acc_dict[i] == 1 and i not in acc_with_sub]
        del_acc = set(all_acc_dict.keys()) - set(clean_acc)
        
        # Специальная обработка счетов 94 (НДФЛ)
        acc_with_94 = [i for i in all_acc_dict if '94' in i]
        if '94.Н' in acc_with_94:
            del_acc.update(i for i in acc_with_94 if i != '94.Н')
        
        # Убираем ведущие нули
        accounts_without_zeros = {
            str(int(item)) for item in del_acc 
            if isinstance(item, str) and item.isdigit() and item.startswith('0') and len(item) > 1
        }
        del_acc.update(accounts_without_zeros)
        
        return del_acc
    
    def _filter_intermediate_rows(
        self, 
        df: pd.DataFrame, 
        korr_col: str,
        accounts_without_subaccount: List[str]
    ) -> pd.DataFrame:
        """
        Удаляет промежуточные (суммирующие) строки.
        
        Это основная логика для получения плоского отчёта!
        """
        # Получаем список счетов для удаления
        del_acc = self._get_del_accounts(df, korr_col)
        
        # Если в пользовательских настройках указаны счета без субсчетов,
        # заменяем субсчета на главный счет (например, 60.01 -> 60)
        main_account = df['Корр_счет'].str.split('.').str[0]
        mask = main_account.isin(accounts_without_subaccount)
        df.loc[mask, 'Корр_счет'] = main_account[mask]
        
        # Основная фильтрация
        df = df[
            ~df[korr_col].isin(exclude_values) &
            ~df[korr_col].isin(del_acc) &
            (df['Курсив'] == 0)
        ].copy()
        
        return df
    
    # =========================================================================
    # ПОИСК СУММИРУЮЩИХ СТРОК (для валютных выгрузок)
    # =========================================================================
    
    def find_sum_indices(
        self, 
        df: pd.DataFrame, 
        column_name: str, 
        tolerance: float = 0.005
    ) -> List:
        """
        Находит индексы значений, для которых можно подобрать сумму
        из следующих значений.
        
        Используется для валютных выгрузок, где промежуточные
        суммирующие строки могут не быть курсивными.
        """
        result_indices = []
        valid_data = df[column_name].dropna()
        values = valid_data.values
        indices = valid_data.index
        
        for i in range(len(values) - 1):
            target_value = values[i]
            current_sum = 0.0
            
            for j in range(i + 1, len(values)):
                current_sum += values[j]
                
                if abs(current_sum - target_value) <= tolerance:
                    result_indices.append(indices[i])
                    break
                elif current_sum > target_value + tolerance:
                    break
        
        return result_indices
    
    def _reconciliation_interim_results(
        self,
        df_proc: pd.DataFrame,
        ind_del_list: set,
        last_general_deviation: float,
        df_before: pd.DataFrame,
        diff_col: str,
        data_col: str,
        corr_account: str
    ) -> set:
        """
        Рекурсивно подбирает индексы строк для удаления,
        чтобы минимизировать расхождение оборотов.
        """
        indices_to_remove = set()
        sorted_ind_del_list = sorted(ind_del_list)
        
        for ind_del in sorted_ind_del_list:
            temp_to_remove = indices_to_remove | {ind_del}
            df_after = df_proc[['Корр_счет', 'С кред. счетов', 'В дебет счетов']].drop(
                index=temp_to_remove, errors='ignore'
            ).copy()
            df_after['Кор.счет_ЧЕК'] = df_after['Корр_счет']
            
            df_after = df_after.groupby('Кор.счет_ЧЕК', as_index=False).agg({
                'С кред. счетов': 'sum',
                'В дебет счетов': 'sum'
            })
            
            merged_df = df_before[df_before['Кор.счет_ЧЕК'] == corr_account].merge(
                df_after[df_after['Кор.счет_ЧЕК'] == corr_account],
                on='Кор.счет_ЧЕК',
                how='outer',
                suffixes=('_base', '_current')
            ).fillna(0)
            
            numeric_cols = [
                'С кред. счетов_base', 'В дебет счетов_base',
                'С кред. счетов_current', 'В дебет счетов_current'
            ]
            merged_df[numeric_cols] = merged_df[numeric_cols].apply(
                pd.to_numeric, errors='coerce'
            ).fillna(0)
            
            merged_df[diff_col] = abs(
                merged_df[f'{data_col}_base'] - merged_df[f'{data_col}_current']
            ).round()
            current_general_deviation = abs(merged_df[diff_col].sum())
            
            if current_general_deviation <= last_general_deviation:
                last_general_deviation = current_general_deviation
                indices_to_remove.add(ind_del)
            
            if 0 <= last_general_deviation <= 1:
                break
        
        return indices_to_remove
    
    # =========================================================================
    # СВЕРКА ОБОРОТОВ С АВТОМАТИЧЕСКИМ УДАЛЕНИЕМ СТРОК
    # =========================================================================
    
    def _reconcile_turnovers(
        self,
        df: pd.DataFrame,
        df_for_check: pd.DataFrame,
        use_find_sum_indices: bool = False
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Выполняет сверку оборотов с автоматическим удалением
        "мешающих" строк (до 3 итераций).
        
        Returns:
            (df, merged_df) - очищенный DataFrame и контрольная таблица
        """
        for _ in range(3):
            # Группируем текущий df по корр.счетам
            df_for_check_2 = df[['Корр_счет', 'С кред. счетов', 'В дебет счетов']].copy()
            df_for_check_2['Кор.счет_ЧЕК'] = df_for_check_2['Корр_счет']
            
            df_for_check_2 = df_for_check_2.groupby('Кор.счет_ЧЕК', as_index=False).agg({
                'С кред. счетов': 'sum',
                'В дебет счетов': 'sum'
            })
            
            # Объединяем с контрольной таблицей
            merged_df = df_for_check.merge(
                df_for_check_2,
                on='Кор.счет_ЧЕК',
                how='outer',
                suffixes=('_base', '_current')
            ).fillna(0)
            
            # Приводим к числовому типу
            numeric_cols = [
                'С кред. счетов_base', 'В дебет счетов_base',
                'С кред. счетов_current', 'В дебет счетов_current'
            ]
            merged_df[numeric_cols] = merged_df[numeric_cols].apply(
                pd.to_numeric, errors='coerce'
            ).fillna(0)
            
            # Вычисляем разницы
            merged_df['Разница_С_кред'] = (
                merged_df['С кред. счетов_base'] - merged_df['С кред. счетов_current']
            ).round()
            merged_df['Разница_В_дебет'] = (
                merged_df['В дебет счетов_base'] - merged_df['В дебет счетов_current']
            ).round()
            merged_df['Исх.файл'] = self.file
            
            # Если есть расхождения и включен режим поиска сумм
            if use_find_sum_indices:
                indices_to_remove = set()
                
                for diff_col, data_col in [
                    ('Разница_С_кред', 'С кред. счетов'),
                    ('Разница_В_дебет', 'В дебет счетов')
                ]:
                    if merged_df[diff_col].sum() != 0:
                        problem_accounts = merged_df.loc[
                            merged_df[diff_col] != 0, 'Кор.счет_ЧЕК'
                        ].tolist()
                        
                        for account_prefix in problem_accounts:
                            mask_pref = df['Корр_счет'].astype(str).str.startswith(account_prefix)
                            unique_corr_accounts = df.loc[mask_pref, 'Корр_счет'].unique()
                            
                            for corr_account in unique_corr_accounts:
                                filtered_df = df[
                                    (df['Корр_счет'] == corr_account) &
                                    (df['Субконто_корр_счета'] != "Не расшифровано")
                                ]
                                indices_to_remove.update(
                                    self.find_sum_indices(filtered_df, data_col)
                                )
                                
                                last_general_deviation = abs(
                                    merged_df.loc[
                                        merged_df['Кор.счет_ЧЕК'] == corr_account, 
                                        diff_col
                                    ].sum()
                                )
                                
                                if indices_to_remove:
                                    final_ind_to_remove = self._reconciliation_interim_results(
                                        df, indices_to_remove, last_general_deviation,
                                        df_for_check, diff_col, data_col, corr_account
                                    )
                                    indices_to_remove.clear()
                                    if final_ind_to_remove:
                                        df = df.drop(list(final_ind_to_remove), errors='ignore')
            
            # Если нет расхождений или они минимальны — выходим
            total_diff = abs(merged_df['Разница_С_кред'].sum()) + abs(merged_df['Разница_В_дебет'].sum())
            if total_diff <= 1:
                break
        
        return df, merged_df
    
    # =========================================================================
    # СОЗДАНИЕ КОНТРОЛЬНОЙ ТАБЛИЦЫ
    # =========================================================================
    
    def _filter_and_aggregate_check_table(
        self, 
        df: pd.DataFrame, 
        debit_col: str, 
        credit_col: str
    ) -> pd.DataFrame:
        """
        Создаёт контрольную таблицу агрегированных оборотов
        по корр.счетам для сверки.
        """
        df_check = df[['Корр_счет', debit_col, credit_col]].copy()
        
        # Фильтруем только валидные счета
        valid_mask = self._is_accounting_code_vectorized(df_check['Корр_счет'])
        df_check = df_check[valid_mask].copy()
        
        if df_check.empty:
            return pd.DataFrame(columns=['Кор.счет_ЧЕК', debit_col, credit_col])
        
        # Нормализуем имена счетов
        df_check['Кор.счет_ЧЕК'] = df_check['Корр_счет'].str.zfill(2)
        
        
        # Схлопываем субсчета для счетов без субсчетов
        main_accs = df_check['Кор.счет_ЧЕК'].str.split('.').str[0]
        mask_collapse = main_accs.isin(accounts_without_subaccount)
        df_check.loc[mask_collapse, 'Кор.счет_ЧЕК'] = main_accs[mask_collapse]
        
        # Группировка
        df_grouped = df_check.groupby('Кор.счет_ЧЕК', as_index=False).agg({
            debit_col: 'sum',
            credit_col: 'sum'
        })
        
        # Удаляем родительские счета
        all_acc_dict = df_grouped['Кор.счет_ЧЕК'].value_counts().to_dict()
        acc_with_sub = [i for i in all_acc_dict if self._is_parent(i, list(all_acc_dict.keys()))]
        clean_acc = [i for i in all_acc_dict if all_acc_dict[i] == 1 and i not in acc_with_sub]
        del_acc = set(all_acc_dict.keys()) - set(clean_acc)
        
        df_grouped = df_grouped[~df_grouped['Кор.счет_ЧЕК'].isin(del_acc)].copy()
        
        return df_grouped
    
    # =========================================================================
    # ОПРЕДЕЛЕНИЕ СУБСЧЕТА
    # =========================================================================
    
    def _determine_subaccount(
        self, 
        df: pd.DataFrame, 
        level_cols: List[str]
    ) -> pd.DataFrame:
        """
        Определяет самый глубокий уровень иерархии для формирования
        столбца "Субсчет".
        """
        shiftable_level = 'Level_0'
        for col in reversed(level_cols):
            if col in df.columns:
                if self._is_accounting_code_vectorized(df[col]).any():
                    shiftable_level = col
                    break
        
        df['Субсчет'] = df[shiftable_level].astype('string').str.zfill(2)
        mask_sub_valid = self._is_accounting_code_vectorized(df['Субсчет'])
        df.loc[~mask_sub_valid, 'Субсчет'] = 'Без_субсчетов'
        
        return df


class Analisys_UPPFileProcessor(BaseAnalysisProcessor):
    """Обработчик для Анализа счета 1С УПП."""
    
    def process_file(self, stream: BytesIO, file_name: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
        self.file = file_name
        
        # 1. Загрузка и предобработка
        df = self._preprocessor_openpyxl(stream)
        df = self._process_header_and_clean(df)
        df['Исх.файл'] = self.file
        
        # 2. Обработка денежных столбцов и дубликатов (для валютных выгрузок)
        money_cols = ['С кред. счетов', 'В дебет счетов']
        for col in money_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            else:
                df[col] = 0.0
        
        # Обработка дубликатов столбцов (для валютных выгрузок)
        cols = df.columns.tolist()
        for dup_name in ['С кред. счетов', 'В дебет счетов']:
            indices = [i for i, col in enumerate(cols) if col == dup_name]
            for count, idx in enumerate(indices[1:], start=2):
                cols[idx] = f"{dup_name}_ВАЛ"
        df.columns = cols
        
        use_find_sum_indices = any(df.columns.str.endswith('_ВАЛ'))
        
        # 3. Сохраняем вид связи КА
        if 'Вид связи КА за период' in df.columns and 'Счет' in df.columns:
            self.df_type_connection = (
                df.drop_duplicates(subset=['Счет', 'Вид связи КА за период'])
                .dropna(subset=['Счет', 'Вид связи КА за период'])
                .loc[:, ['Счет', 'Вид связи КА за период']]
            )
        
        # 4. Обработка пустых счетов
        kor_schet = df['Кор.счет'].astype('string')
        is_valid_account = self._is_accounting_code_vectorized(kor_schet)
        
        mask = (
            df['Счет'].isna() &
            ~is_valid_account &
            (kor_schet != 'Кол-во:') &
            kor_schet.isin(exclude_values)
        )
        df.loc[mask, 'Счет'] = 'Не_заполнено'
        df['Счет'] = df['Счет'].ffill().astype('string')
        
        # 5. Формирование уровней
        df, level_cols = self._prepare_levels_and_accounts(df)
        
        # 6. Контрольная таблица (ДО фильтрации)
        self.table_for_check = self._filter_and_aggregate_check_table(
            df, 'В дебет счетов', 'С кред. счетов'
        )
        
        # 7. Удаление промежуточных строк (КЛЮЧЕВОЙ ШАГ!)
        df_filtered = self._filter_intermediate_rows(
            df, korr_col='Кор.счет',
            accounts_without_subaccount=accounts_without_subaccount
        )
        
        # 8. Восстановление вида связи
        if 'Вид связи КА за период' in df_filtered.columns and not self.df_type_connection.empty:
            merged = df_filtered.merge(
                self.df_type_connection, on='Счет', how='left', suffixes=('', '_B')
            )
            df_filtered['Вид связи КА за период'] = df_filtered['Вид связи КА за период'].fillna(
                merged['Вид связи КА за период_B']
            )
        
        # 9. Определение субсчета
        df_filtered = self._determine_subaccount(df_filtered, level_cols)
        
        # 10. Финальная обработка корр.счетов
        df_filtered['Субконто_корр_счета'] = df_filtered['Кор.счет'].astype('string')
        mask_final = self._is_accounting_code_vectorized(df_filtered['Субконто_корр_счета'])
        df_filtered.loc[mask_final, 'Субконто_корр_счета'] = 'Не расшифровано'
        
        # 11. Переименование
        df_filtered = df_filtered.rename(columns={
            'Кор.счет': 'Субконто_корр_счета_orig',
            'Счет': 'Аналитика'
        })
        
        # 12. Порядок столбцов
        desired_order = [
            'Исх.файл', 'Субсчет', 'Аналитика', 'Вид связи КА за период',
            'Корр_счет', 'Субконто_корр_счета',
            'С кред. счетов', 'В дебет счетов'
        ]
        
        # Добавляем валютные столбцы, если есть
        if use_find_sum_indices:
            desired_order.extend(['С кред. счетов_ВАЛ', 'В дебет счетов_ВАЛ'])
        
        final_cols = [col for col in desired_order if col in df_filtered.columns]
        remaining_cols = [col for col in df_filtered.columns if col not in final_cols]
        df_final = df_filtered[final_cols + remaining_cols]
        
        # 13. Удаление пустых строк
        df_final = df_final.dropna(
            subset=['С кред. счетов', 'В дебет счетов'], how='all'
        )
        
        # 14. Сверка оборотов
        df_final, self.table_for_check = self._reconcile_turnovers(
            df_final, self.table_for_check, use_find_sum_indices
        )
        
        return df_final, self.table_for_check


class Analisys_NonUPPFileProcessor(BaseAnalysisProcessor):
    """Обработчик для Анализа счета 1С не-УПП (КА, ERP и др.)."""
    
    def process_file(self, stream: BytesIO, file_name: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
        self.file = file_name
        
        # 1. Загрузка и предобработка
        df = self._preprocessor_openpyxl(stream)
        df = self._process_header_and_clean(df)
        df['Исх.файл'] = self.file
        
        # 2. Обработка денежных столбцов (в Non-UPP называются 'Дебет'/'Кредит')
        money_cols_map = {
            'Дебет': 'В дебет счетов',
            'Кредит': 'С кред. счетов'
        }
        
        for orig_col, new_col in money_cols_map.items():
            if orig_col in df.columns:
                df[new_col] = pd.to_numeric(df[orig_col], errors='coerce')
            else:
                df[new_col] = 0.0
        
        # 3. Обработка колонок с количеством и валютой
        values_with_quantity = False
        values_with_currency = False
        
        if 'Показа-\nтели' in df.columns:
            if df['Показа-\nтели'].isin(['Кол.']).any():
                df['С кред. счетов_КОЛ'] = df['В дебет счетов'].shift(-1)
                df['В дебет счетов_КОЛ'] = df['С кред. счетов'].shift(-1)
                df = df[~df['Показа-\nтели'].isin(['Кол.', 'Вал.'])].copy()
                values_with_quantity = True
            elif df['Показа-\nтели'].isin(['Вал.']).any():
                df['С кред. счетов_ВАЛ'] = df['В дебет счетов'].shift(-1)
                df['В дебет счетов_ВАЛ'] = df['С кред. счетов'].shift(-1)
                df = df[~df['Показа-\nтели'].isin(['Кол.', 'Вал.'])].copy()
                values_with_currency = True
        
        # 4. Сохраняем вид связи КА
        if 'Вид связи КА за период' in df.columns and 'Счет' in df.columns:
            self.df_type_connection = (
                df.drop_duplicates(subset=['Счет', 'Вид связи КА за период'])
                .dropna(subset=['Счет', 'Вид связи КА за период'])
                .loc[:, ['Счет', 'Вид связи КА за период']]
            )
        
        # 5. Обработка пустых счетов
        kor_schet = df['Кор. Счет'].astype('string')
        is_valid_account = self._is_accounting_code_vectorized(kor_schet)
        
        mask = (
            df['Счет'].isna() &
            ~is_valid_account &
            (kor_schet != 'Кол-во:') &
            kor_schet.isin(exclude_values)
        )
        df.loc[mask, 'Счет'] = 'Не_заполнено'
        df['Счет'] = df['Счет'].ffill().astype('string')
        
        # 6. Формирование уровней
        df, level_cols = self._prepare_levels_and_accounts(df)
        
        # 7. Контрольная таблица (ДО фильтрации)
        self.table_for_check = self._filter_and_aggregate_check_table(
            df, 'В дебет счетов', 'С кред. счетов'
        )
        
        # 8. Удаление промежуточных строк (КЛЮЧЕВОЙ ШАГ!)
        accounts_without_subaccount = get_accounts_without_subaccount()
        df_filtered = self._filter_intermediate_rows(
            df, korr_col='Кор. Счет',
            accounts_without_subaccount=accounts_without_subaccount
        )
        
        # 9. Восстановление вида связи
        if 'Вид связи КА за период' in df_filtered.columns and not self.df_type_connection.empty:
            merged = df_filtered.merge(
                self.df_type_connection, on='Счет', how='left', suffixes=('', '_B')
            )
            df_filtered['Вид связи КА за период'] = df_filtered['Вид связи КА за период'].fillna(
                merged['Вид связи КА за период_B']
            )
        
        # 10. Определение субсчета
        df_filtered = self._determine_subaccount(df_filtered, level_cols)
        
        # 11. Финальная обработка корр.счетов
        orig_korr_col = 'Кор. Счет' if 'Кор. Счет' in df_filtered.columns else 'Кор.счет'
        if orig_korr_col in df_filtered.columns:
            df_filtered['Субконто_корр_счета'] = df_filtered[orig_korr_col].astype('string')
        else:
            df_filtered['Субконто_корр_счета'] = df_filtered['Корр_счет']
        
        mask_final = self._is_accounting_code_vectorized(df_filtered['Субконто_корр_счета'])
        df_filtered.loc[mask_final, 'Субконто_корр_счета'] = 'Не расшифровано'
        
        # 12. Переименование
        df_filtered = df_filtered.rename(columns={'Счет': 'Аналитика'})
        
        # 13. Порядок столбцов
        desired_order = [
            'Исх.файл', 'Субсчет', 'Аналитика', 'Вид связи КА за период',
            'Корр_счет', 'Субконто_корр_счета',
            'С кред. счетов', 'В дебет счетов'
        ]
        
        if values_with_quantity:
            desired_order.extend(['С кред. счетов_КОЛ', 'В дебет счетов_КОЛ'])
        if values_with_currency:
            desired_order.extend(['С кред. счетов_ВАЛ', 'В дебет счетов_ВАЛ'])
        
        final_cols = [col for col in desired_order if col in df_filtered.columns]
        remaining_cols = [col for col in df_filtered.columns if col not in final_cols]
        df_final = df_filtered[final_cols + remaining_cols]
        
        # 14. Удаление пустых строк
        df_final = df_final.dropna(
            subset=['С кред. счетов', 'В дебет счетов'], how='all'
        )
        
        # 15. Сверка оборотов
        df_final, self.table_for_check = self._reconcile_turnovers(
            df_final, self.table_for_check, use_find_sum_indices=False
        )
        
        return df_final, self.table_for_check


# import pandas as pd
# import numpy as np
# from typing import Tuple
# from io import BytesIO

# from data_processors.file_processor import FileProcessor, exclude_values

# class BaseAnalysisProcessor(FileProcessor):
#     """Базовый класс для Анализатора счетов с общей логикой."""

#     def __init__(self):
#         super().__init__()
#         self.df_type_connection = pd.DataFrame()
#         self.table_for_check = pd.DataFrame()

#     @staticmethod
#     def _is_accounting_code_vectorized(series: pd.Series) -> pd.Series:
#         """Проверяет, является ли значение бухгалтерским счетом (цифры и точки)."""
#         # Быстрая проверка: состоит из цифр и точек, не пустое
#         return series.astype(str).str.match(r'^\d+(\.\d+)?$') & series.notna()

#     @staticmethod
#     def _process_header_and_clean(df: pd.DataFrame) -> pd.DataFrame:
#         """
#         Универсальная очистка и поиск заголовка для файлов Анализа счета.
#         """
#         MAX_HEADER_ROWS = 30
        
#         # Удаляем полностью пустые строки и столбцы
#         df.dropna(axis=1, how='all', inplace=True)
#         df.dropna(axis=0, how='all', inplace=True)
        
#         if df.empty:
#             raise ValueError('Файл пуст после первоначальной очистки.')

#         max_rows_to_check = min(MAX_HEADER_ROWS, df.shape[0])
#         account_col_idx = None
        
#         # Поиск столбца "Счет"
#         for col_idx in range(df.shape[1]):
#             # Берем срез, приводим к строке, чистим пробелы и нижний регистр
#             col_values = df.iloc[:max_rows_to_check, col_idx].astype('string').str.strip().str.lower()
#             if 'счет' in col_values.values:
#                 account_col_idx = col_idx
#                 break
        
#         if account_col_idx is None:
#             raise ValueError('Не найден столбец с "Счет" в первых 30 строках.')
        
#         # Ищем строку, где в найденном столбце написано "Счет" (точное совпадение или часть)
#         first_col = df.iloc[:, account_col_idx].astype('string')
#         # Ищем точное совпадение "Счет" или начало строки "Счет"
#         mask = first_col.str.strip() == 'Счет'
        
#         if not mask.any():
#             # Пробуем менее строгий поиск, если точного нет
#             mask = first_col.str.contains('Счет', na=False)
            
#         if not mask.any():
#             raise ValueError('Файл не является корректным Анализом счета 1С (не найдена строка заголовка).')
        
#         date_row_idx = mask.idxmax()
    
#         # Присваиваем заголовки
#         df.columns = df.iloc[date_row_idx]
#         df = df.iloc[date_row_idx + 1:].copy()
        
#         # Переименовываем первые два служебных столбца (обычно это Уровень и Курсив/Жирность)
#         # Если их нет, добавляем заглушки, но обычно в анализе счета они есть
#         current_cols = df.columns.tolist()
#         if len(current_cols) >= 2:
#             new_cols = ['Уровень', 'Курсив'] + current_cols[2:]
#         else:
#             new_cols = ['Уровень', 'Курсив'] + current_cols
            
#         df.columns = new_cols
    
#         # Удаляем столбцы с NA в имени (если остались лишние)
#         df = df.loc[:, df.columns.notna()]
#         df.columns = df.columns.astype('string')
    
#         if 'Уровень' not in df.columns or df['Уровень'].isnull().all():
#              raise ValueError('Отсутствует или пуст столбец "Уровень".')

#         if df['Уровень'].isnull().any():
#             # Заполняем пропуски в уровне, если они есть, например, предыдущим значением или 0
#             df['Уровень'] = df['Уровень'].ffill().fillna(0)
            
#         return df

#     def _prepare_levels_and_accounts(self, df: pd.DataFrame) -> pd.DataFrame:
#         """
#         Распределяет счета по уровням и формирует основные столбцы.
#         """
#         # Приводим 'Счет' к строке
#         df['Счет'] = df['Счет'].astype('string').str.strip()
        
#         # Заполняем пропуски в счетах (если счет не указан, берем предыдущий)
#         # Но сначала обработаем случаи, когда счет явно не заполнен, но есть кор.счет
#         # kor_schet_raw = df.get('Кор.счет', df.get('Кор. Счет', pd.Series(dtype='string'))).astype('string')
        
#         # Маска валидных счетов для Кор.счета
#         # is_valid_korr = self._is_accounting_code_vectorized(kor_schet_raw)
        
#         # Если основной счет пуст, а корр.счет похож на исключение или не является счетом, помечаем
#         # mask_empty_main = df['Счет'].isna() | (df['Счет'] == '') | (df['Счет'] == 'nan')
#         # mask_korr_not_account = ~is_valid_korr & (kor_schet_raw != 'Кол-во:') & (kor_schet_raw != '')
        
#         # Если в исходных данных есть колонка 'Кор.счет' или 'Кор. Счет', используем её для логики
#         # В разных версиях 1С название может отличаться. 
#         # Здесь предполагаем, что мы уже нормализовали имена или работаем с тем, что есть.
        
#         # Для простоты: ffill основного счета
#         df['Счет'] = df['Счет'].replace('', np.nan).ffill()
        
#         # Добавляем ведущий ноль, если счет однознаный (например, "1" -> "01")
#         account_is_valid = self._is_accounting_code_vectorized(df['Счет'])
#         mask_pad = account_is_valid & (df['Счет'].str.len() == 1)
#         df.loc[mask_pad, 'Счет'] = '0' + df.loc[mask_pad, 'Счет']

#         # --- Формирование уровней (Иерархия) ---
#         max_level = int(df['Уровень'].max())
        
#         # Создаем столбцы Level_0, Level_1 и т.д.
#         # Логика: если строка имеет уровень N, то в Level_N попадает её счет.
#         # Затем делаем ffill вниз, чтобы заполнить иерархию.
        
#         level_cols = []
#         for level in range(max_level + 1):
#             col_name = f'Level_{level}'
#             level_mask = df['Уровень'] == level
#             # Если уровень совпадает, берем счет, иначе NaN
#             df[col_name] = df['Счет'].where(level_mask)
#             level_cols.append(col_name)
            
#         # Теперь заполняем иерархию: каждый уровень наследует значение от предыдущего, если свой пуст
#         # for i in range(1, len(level_cols)):
#         #     prev_col = level_cols[i-1]
#         #     curr_col = level_cols[i]
#             # Если текущий уровень пуст, берем из предыдущего (который уже заполнен ffill выше? Нет, надо аккуратно)
#             # Правильнее: сделать ffill для каждого столбца отдельно или каскадно.
#             # Стандартный подход для плоской таблицы из дерева:
#             # pass 
        
#         # Более надежный способ для "плоского" представления дерева из 1С:
#         # Столбец Level_0 всегда заполняется первым найденным счетом уровня 0 и тянется вниз.
#         # Столбец Level_1 заполняется счетом уровня 1, если он есть, иначе наследует Level_0? 
#         # Нет, в анализе счета обычно:
#         # Row 1: Level 0: 60
#         # Row 2: Level 1: 60.01
#         # Row 3: Level 2: 60.01.01
        
#         # Применим ffill ко всем Level_колумнам, чтобы они "тянулись" вниз до следующего значения того же уровня
#         for col in level_cols:
#             df[col] = df[col].ffill()
            
#         # Однако, если мы хотим получить "путь" к текущему элементу, нам нужно другое поведение.
#         # Но для задач расшифровки обычно достаточно знать "Родителя" текущего уровня.
#         # Оставим как есть: каждый Level_N содержит последний встреченный счет этого уровня.

#         # --- Корреспондирующий счет ---
#         # Определяем имя столбца корр.счета (зависит от версии 1С)
#         korr_col_name = 'Кор.счет' if 'Кор.счет' in df.columns else 'Кор. Счет'
#         if korr_col_name not in df.columns:
#             # Попробуем найти похожий
#             candidates = [c for c in df.columns if 'кор' in c.lower()]
#             if candidates:
#                 korr_col_name = candidates[0]
#             else:
#                 df['Корр_счет'] = np.nan
#                 korr_col_name = None

#         if korr_col_name:
#             df['Корр_счет'] = df[korr_col_name].astype('string')
#             # Очищаем корр.счет, оставляя только валидные счета
#             valid_korr_mask = self._is_accounting_code_vectorized(df['Корр_счет'])
#             df.loc[~valid_korr_mask, 'Корр_счет'] = np.nan
            
#             # Добавляем ведущий ноль
#             korr_str = df['Корр_счет'].astype('string')
#             single_digit_mask = df['Корр_счет'].notna() & (korr_str.str.len() == 1)
#             df.loc[single_digit_mask, 'Корр_счет'] = '0' + korr_str.loc[single_digit_mask]
            
#             # Заполняем пропуски в корр.счете (если нужно группировать по нему)
#             # Обычно в анализе счета корр.счет повторяется или пуст. 
#             # Если пуст, часто значит тот же, что выше. Но лучше оставить NaN, если не уверены.
#             # df['Корр_счет'] = df['Корр_счет'].ffill() 
#         else:
#             df['Корр_счет'] = np.nan

#         return df, level_cols

#     def _filter_and_aggregate_check_table(self, df: pd.DataFrame, debit_col: str, credit_col: str) -> pd.DataFrame:
#         """
#         Создает контрольную таблицу агрегированных оборотов по корр.счетам для сверки.
#         """
#         df_check = df[['Корр_счет', debit_col, credit_col]].copy()
        
#         # Фильтруем только валидные счета
#         valid_mask = self._is_accounting_code_vectorized(df_check['Корр_счет'])
#         df_check = df_check[valid_mask].copy()
        
#         if df_check.empty:
#             return pd.DataFrame(columns=['Кор.счет_ЧЕК', debit_col, credit_col])

#         # Нормализуем имена счетов (убираем лишние нули или приводим к единому виду)
#         # Например, 01 и 1 считаем одинаковыми? Лучше привести к формату с ведущими нулями или без.
#         # В предыдущем коде было zfill(2). Сделаем так же.
#         df_check['Кор.счет_ЧЕК'] = df_check['Корр_счет'].str.zfill(2)
        
#         # Специфика счета 94 (НДФЛ и прочие удержания часто висят на субсчетах)
#         # Если есть '94.Н', оставляем его, остальные 94.xx можем схлопнуть или оставить как есть.
#         # В упрощенной версии просто группируем по полному имени.
        
#         accounts_without_subaccount = ['50', '51', '52', '55']
        
#         # Если счет верхнего уровня не имеет субсчетов по справочнику, схлопываем его субсчета обратно в главный
#         main_accs = df_check['Кор.счет_ЧЕК'].str.split('.').str[0]
#         mask_collapse = main_accs.isin(accounts_without_subaccount)
#         # Если маска истинна, заменяем полное имя на главное
#         df_check.loc[mask_collapse, 'Кор.счет_ЧЕК'] = main_accs[mask_collapse]
        
#         # Группировка
#         df_grouped = df_check.groupby('Кор.счет_ЧЕК', as_index=False).agg({
#             debit_col: 'sum',
#             credit_col: 'sum'
#         })
        
#         return df_grouped


# class Analisys_UPPFileProcessor(BaseAnalysisProcessor):
#     """Обработчик для Анализа счета 1С УПП"""

#     def process_file(self, stream: BytesIO, file_name: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
#         self.file = file_name

#         df = self._preprocessor_openpyxl(stream)

        
#         df = self._process_header_and_clean(df)
#         df['Исх.файл'] = self.file
        
#         money_cols = ['С кред. счетов', 'В дебет счетов']
#         for col in money_cols:
#             if col in df.columns:
#                 df[col] = pd.to_numeric(df[col], errors='coerce')
#             else:
#                 df[col] = 0.0 # Гарантируем наличие столбцов

#         # Сохраняем вид связи, если есть
#         if 'Вид связи КА за период' in df.columns and 'Счет' in df.columns:
#             self.df_type_connection = (
#                 df.drop_duplicates(subset=['Счет', 'Вид связи КА за период'])
#                   .dropna(subset=['Счет', 'Вид связи КА за период'])
#                   .loc[:, ['Счет', 'Вид связи КА за период']]
#             )

#         df, level_cols = self._prepare_levels_and_accounts(df)
        
#         # Контрольная таблица
#         self.table_for_check = self._filter_and_aggregate_check_table(df, 'В дебет счетов', 'С кред. счетов')
        
#         # --- Фильтрация лишних строк ---
#         # Исключаем технические строки и дубликаты, которые не нужны в детализации
#         exclude_vals_set = set(exclude_values)
        
#         # Удаляем строки, где Корр.счет в списке исключений или пуст
#         # Также убираем строки с "Курсив" != 0 (итоги), если они не нужны
#         df_filtered = df[
#             (~df['Корр_счет'].isin(exclude_vals_set)) &
#             (df['Корр_счет'].notna()) &
#             (df['Курсив'] == 0)
#         ].copy()
        
#         # Если Вид связи есть, подтянем его, если он потерялся при фильтрации (хотя он был в df)
#         if 'Вид связи КА за период' in df_filtered.columns and not self.df_type_connection.empty:
#              merged = df_filtered.merge(self.df_type_connection, on='Счет', how='left', suffixes=('', '_B'))
#              # Предпочитаем оригинальное значение, если нет - берем из справочника
#              df_filtered['Вид связи КА за период'] = df_filtered['Вид связи КА за период'].fillna(merged.get('Вид связи КА за период_B'))

#         # Определяем Субсчет (самый глубокий уровень иерархии, который является счетом)
#         # Ищем последний Level_колумн, который не пуст и является счетом
#         shiftable_level = 'Level_0'
#         for col in reversed(level_cols):
#             if col in df_filtered.columns:
#                 # Проверяем, есть ли хоть одно валидное значение в столбце
#                 if self._is_accounting_code_vectorized(df_filtered[col]).any():
#                     shiftable_level = col
#                     break
        
#         df_filtered['Субсчет'] = df_filtered[shiftable_level].astype('string')
#         # Если субсчет не валиден (пуст или текст), ставим заглушку
#         mask_sub_valid = self._is_accounting_code_vectorized(df_filtered['Субсчет'])
#         df_filtered.loc[~mask_sub_valid, 'Субсчет'] = 'Без_субсчетов'
        
#         # Переименование для единообразия
#         df_filtered = df_filtered.rename(columns={
#             'Кор.счет': 'Субконто_корр_счета', # В УПП часто этот столбец называется так в источнике
#             'Счет': 'Аналитика'
#         })
        
#         # Финальный порядок столбцов
#         desired_order = [
#             'Исх.файл', 'Субсчет', 'Аналитика', 'Вид связи КА за период', 
#             'Корр_счет', 'Субконто_корр_счета', 
#             'С кред. счетов', 'В дебет счетов'
#         ]
        
#         # Добавляем уровни в конец, если нужны для отладки, иначе можно убрать
#         final_cols = [col for col in desired_order if col in df_filtered.columns]
#         # Добавляем остальные столбцы, которые не вошли в список (например, Level_)
#         remaining_cols = [col for col in df_filtered.columns if col not in final_cols]
#         final_cols += remaining_cols
        
#         df_final = df_filtered[final_cols]
        
#         # Убираем полностью пустые по суммам строки
#         df_final = df_final.dropna(subset=['С кред. счетов', 'В дебет счетов'], how='all')
        
#         # Финальная сверка (простая, векторизованная)
#         # Сравниваем table_for_check (агрегировано по Корр.счету) с суммами в df_final
#         df_check_detail = df_final.groupby('Корр_счет', as_index=False).agg({
#             'С кред. счетов': 'sum',
#             'В дебет счетов': 'sum'
#         })
#         df_check_detail['Кор.счет_ЧЕК'] = df_check_detail['Корр_счет'].str.zfill(2)
        
#         # Мердж с контрольной таблицей
#         if not self.table_for_check.empty:
#             reconciliation = self.table_for_check.merge(
#                 df_check_detail, 
#                 on='Кор.счет_ЧЕК', 
#                 how='outer', 
#                 suffixes=('_control', '_fact')
#             ).fillna(0)
            
#             reconciliation['Diff_Debit'] = reconciliation['В дебет счетов_control'] - reconciliation['В дебет счетов_fact']
#             reconciliation['Diff_Credit'] = reconciliation['С кред. счетов_control'] - reconciliation['С кред. счетов_fact']
#             self.table_for_check = reconciliation
#         else:
#             self.table_for_check = pd.DataFrame()

#         return df_final, self.table_for_check


# class Analisys_NonUPPFileProcessor(BaseAnalysisProcessor):
#     """Обработчик для Анализа счета 1С не УПП (КА, ERP и др.)"""

#     def process_file(self, stream: BytesIO, file_name: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
#         self.file = file_name
        
#         df = self._preprocessor_openpyxl(stream)

        
#         df = self._process_header_and_clean(df)
#         df['Исх.файл'] = self.file
        
#         # В Non-UPP столбцы могут называться 'Дебет' и 'Кредит'
#         money_cols_map = {
#             'Дебет': 'В дебет счетов',
#             'Кредит': 'С кред. счетов'
#         }
        
#         for orig_col, new_col in money_cols_map.items():
#             if orig_col in df.columns:
#                 df[new_col] = pd.to_numeric(df[orig_col], errors='coerce')
#             else:
#                 df[new_col] = 0.0
                
#         # Сохраняем вид связи
#         if 'Вид связи КА за период' in df.columns and 'Счет' in df.columns:
#             self.df_type_connection = (
#                 df.drop_duplicates(subset=['Счет', 'Вид связи КА за период'])
#                   .dropna(subset=['Счет', 'Вид связи КА за период'])
#                   .loc[:, ['Счет', 'Вид связи КА за период']]
#             )

#         df, level_cols = self._prepare_levels_and_accounts(df)
        
#         # Контрольная таблица
#         self.table_for_check = self._filter_and_aggregate_check_table(df, 'В дебет счетов', 'С кред. счетов')
        
#         # --- Фильтрация ---
#         exclude_vals_set = set(exclude_values)
        
#         df_filtered = df[
#             (~df['Корр_счет'].isin(exclude_vals_set)) &
#             (df['Корр_счет'].notna()) &
#             (df['Курсив'] == 0)
#         ].copy()
        
#         if 'Вид связи КА за период' in df_filtered.columns and not self.df_type_connection.empty:
#              merged = df_filtered.merge(self.df_type_connection, on='Счет', how='left', suffixes=('', '_B'))
#              df_filtered['Вид связи КА за период'] = df_filtered['Вид связи КА за период'].fillna(merged.get('Вид связи КА за период_B'))

#         # Субсчет
#         shiftable_level = 'Level_0'
#         for col in reversed(level_cols):
#             if col in df_filtered.columns:
#                 if self._is_accounting_code_vectorized(df_filtered[col]).any():
#                     shiftable_level = col
#                     break
        
#         df_filtered['Субсчет'] = df_filtered[shiftable_level].astype('string')
#         mask_sub_valid = self._is_accounting_code_vectorized(df_filtered['Субсчет'])
#         df_filtered.loc[~mask_sub_valid, 'Субсчет'] = 'Без_субсчетов'
        
#         # Переименование исходных столбцов корр.счета, если они еще не переименованы в base классе
#         # В base классе мы создали 'Корр_счет'. 
#         # Оригинальный столбец 'Кор. Счет' или 'Кор.счет' можно переименовать в 'Субконто_корр_счета' для совместимости
#         orig_korr_col = 'Кор. Счет' if 'Кор. Счет' in df.columns else 'Кор.счет'
#         if orig_korr_col in df_filtered.columns:
#             df_filtered = df_filtered.rename(columns={orig_korr_col: 'Субконто_корр_счета'})
#         else:
#             df_filtered['Субконто_корр_счета'] = df_filtered['Корр_счет'] # Дублируем, если отдельного нет
            
#         df_filtered = df_filtered.rename(columns={
#             'Счет': 'Аналитика'
#         })
        
#         desired_order = [
#             'Исх.файл', 'Субсчет', 'Аналитика', 'Вид связи КА за период', 
#             'Корр_счет', 'Субконто_корр_счета', 
#             'С кред. счетов', 'В дебет счетов'
#         ]
        
#         final_cols = [col for col in desired_order if col in df_filtered.columns]
#         remaining_cols = [col for col in df_filtered.columns if col not in final_cols]
#         final_cols += remaining_cols
        
#         df_final = df_filtered[final_cols]
#         df_final = df_final.dropna(subset=['С кред. счетов', 'В дебет счетов'], how='all')
        
#         # Сверка
#         df_check_detail = df_final.groupby('Корр_счет', as_index=False).agg({
#             'С кред. счетов': 'sum',
#             'В дебет счетов': 'sum'
#         })
#         df_check_detail['Кор.счет_ЧЕК'] = df_check_detail['Корр_счет'].str.zfill(2)
        
#         if not self.table_for_check.empty:
#             reconciliation = self.table_for_check.merge(
#                 df_check_detail, 
#                 on='Кор.счет_ЧЕК', 
#                 how='outer', 
#                 suffixes=('_control', '_fact')
#             ).fillna(0)
            
#             reconciliation['Diff_Debit'] = reconciliation['В дебет счетов_control'] - reconciliation['В дебет счетов_fact']
#             reconciliation['Diff_Credit'] = reconciliation['С кред. счетов_control'] - reconciliation['С кред. счетов_fact']
#             self.table_for_check = reconciliation
            
#         return df_final, self.table_for_check