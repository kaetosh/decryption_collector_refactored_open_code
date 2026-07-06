"""
Шаг 7: Добавление признака долгая/короткая часть.

Для счета 97.21 разбивает на долгосрочную и краткосрочную части
на основе данных справочника и периода отчетности.
"""
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger
from pathlib import Path
from time import time

from pipeline.base import Step, ProcessingContext
from pipeline.errors import ReferenceMismatchError
from io_module import DataLoader
from utils import find_register_file, set_header_from_row
from config.settings import SPECIAL_REPORTS_DIR, OUTPUT_DATA_DIR



# Константы для спецотчета долг/кор
LONG_SHORT_SUBCONTO_COL = 'Субконто1'
LONG_SHORT_TOTAL_COL = 'Итого'
LONG_SHORT_ITOGO_VALUE = 'Итого'

# Константы для отчета расшифровки 1450/1550/1230
LEASE_REPORT_COLUMNS = {
    'unnamed_0': 'договор',
    'краткосрочные': 'короткая часть_76',
    'долгосрочные': 'долгая часть_76',
    'краткосрочные_1': 'короткая часть_97.21',
    'долгосрочные_1': 'долгая часть_97.21',
}

# Коэффициенты перевода в тыс.ед. (отрицательные для 76 — особенность отчета)
SCALE_FACTORS = {
    'короткая часть_76': -1000,
    'долгая часть_76': -1000,
    'короткая часть_97.21': 1000,
    'долгая часть_97.21': 1000,
}


class Step7AddLongShortTermColumnStep(Step):
    """
    Шаг 7: Добавление признака долгая/короткая часть для аренды/лизинга на 97.21.
    
    Для счета 97.21 разбивает на долгосрочную и краткосрочную части
    на основе данных справочника и периода отчетности.
    """
    
    # Константы для типов задолженности
    LONG_TERM = 'долгая часть'
    SHORT_TERM = 'короткая часть'
    UNSPECIFIED = 'не_указано'
    
    # Константы для типов договоров
    LEASE_TYPE = 'лизинг'
    RENT_TYPE = 'аренда'
    
    def __init__(self):
        super().__init__(
            name="Шаг 7: Долгая/короткая часть 97.21, 76.07, 76.05.3 (аренда/лизинга)",
            description="Использует отчет Расшифровка строк Баланса по долгосрочной и краткосрочной задолженности (97.21), Расшифровка заполнения строк 1450, 1550, 1230..."
        )
    
    # =========================================================================
    # МЕТОДЫ ВЕРИФИКАЦИИ
    # =========================================================================
    
    def _validate_split(self, df_original: pd.DataFrame, df_split: pd.DataFrame) -> bool:
        """
        Проверяет, что суммы после разбивки совпадают с исходными.
        """
        group_keys = ['договор', 'долгая_короткая_часть']
        
        orig_sums = df_original.groupby(group_keys, observed=True)['сальдо, тыс.ед.'].sum()
        split_sums = df_split.groupby(group_keys, observed=True)['сальдо, тыс.ед.'].sum()
        
        comparison = pd.DataFrame({
            'original': orig_sums,
            'split': split_sums
        }).fillna(0)
        
        comparison['разница'] = (comparison['original'] - comparison['split']).abs()
        mismatches = comparison[comparison['разница'] > 0.01]
        
        if not mismatches.empty:
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = (
                Path(OUTPUT_DATA_DIR) / 
                f'validation_split_mismatches_{timestamp}.xlsx'
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            mismatches.to_excel(output_path)
            
            logger.error(
                f"⚠️ Расхождения сумм после разбивки: {len(mismatches)} строк. "
                f"Все данные сохранены в {output_path.parent.name}/{output_path.name}"
            )
            return False
        
        logger.debug(f"✓ Валидация разбивки пройдена: {len(comparison)} групп")
        return True
    
    def _validate_replacement(self, osv_original: pd.DataFrame, 
                             osv_updated: pd.DataFrame,
                             decoded_report: pd.DataFrame,
                             type_contract: str) -> bool:
        """
        Проверяет корректность замены данных.
        """
        keys = ['договор', 'счет']
        
        orig_sums = osv_original.groupby(keys, observed=True)['сальдо, тыс.ед.'].sum()
        updated_sums = osv_updated.groupby(keys, observed=True)['сальдо, тыс.ед.'].sum()
        decoded_sums = decoded_report.groupby(keys, observed=True)['сальдо, тыс.ед.'].sum()
                
        comparison = pd.DataFrame({
            'original': orig_sums,
            'updated': updated_sums,
            'decoded': decoded_sums
        }).fillna(0)
        
        replaced_mask = comparison['decoded'] != 0
        comparison_replaced = comparison[replaced_mask].copy()
        
        comparison_replaced['разница'] = (
            comparison_replaced['updated'] - 
            comparison_replaced['decoded']
        ).abs()
        
        mismatches = comparison_replaced[comparison_replaced['разница'] > 0.1]
        
        if not mismatches.empty:
            # Формируем путь к файлу для сохранения расхождений
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = Path(OUTPUT_DATA_DIR) / f'validation_mismatches_{type_contract}_{timestamp}.xlsx'
            
            # Создаём директорию, если её нет
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Сохраняем ВСЕ расхождения в Excel
            mismatches.to_excel(output_path)
            
            logger.error(
                f"⚠️ Расхождения сумм после замены: {len(mismatches)} комбинаций. "
                f"Все данные сохранены в {output_path.parent.name}/{output_path.name}"
            )
            return False
        
        logger.debug(f"✓ Валидация замены пройдена: {len(comparison_replaced)} комбинаций")
        return True
    
    # =========================================================================
    # МЕТОДЫ ОБРАБОТКИ ОТЧЕТОВ
    # =========================================================================
    
    def _process_lease_report_decoding(
        self, 
        type_register: str,
        name_company: str,
        period: str
    ) -> Optional[pd.DataFrame]:
        """Оркестратор: ищет файл, загружает и обрабатывает отчет расшифровки."""
        input_path = find_register_file(
            folder_path=SPECIAL_REPORTS_DIR,
            type_register=type_register
        )
        
        expected_filename = f"{name_company}_{type_register}_7697_{period}_.xlsx"
        
        if not input_path:
            logger.warning(
                f"Файл {expected_filename} не найден. "
                f"Рекласс на долгую/короткую части по 76 и 97 счету не проводим."
            )
            return None
        
        logger.debug(f"Файл {expected_filename} найден. Проводим рекласс.")
        
        raw_df = DataLoader.load_lease_report_decoding(input_path)
        return self._transform_lease_report(raw_df)
    
    def _transform_lease_report(self, raw_df: pd.DataFrame) -> pd.DataFrame:
        """Трансформирует сырой отчет расшифровки в нормализованный DataFrame."""
        df = raw_df.copy()
        
        # Базовая очистка
        df = df.dropna(how='all').dropna(axis=1, how='all')
        df = set_header_from_row(df, search_text='Краткосрочные', offset=0)
        df.columns = df.columns.str.lower()
        
        # Валидация структуры
        missing_cols = [col for col in LEASE_REPORT_COLUMNS if col not in df.columns]
        if missing_cols:
            raise ValueError(f"Отсутствуют столбцы: {missing_cols}")
        
        # Переименование
        df = df.rename(columns=LEASE_REPORT_COLUMNS)
        
        # Векторизованный перевод в тыс.ед.
        for col, factor in SCALE_FACTORS.items():
            df[col] = pd.to_numeric(df[col], errors='coerce').div(factor)
        
        # Unpivot в длинный формат
        value_vars = list(SCALE_FACTORS.keys())
        df_long = df.melt(
            id_vars='договор',
            value_vars=value_vars,
            var_name='tmp',
            value_name='сальдо, тыс.ед.'
        )
        
        # Разбор составного имени столбца
        df_long[['долгая_короткая_часть', 'группа_счетов']] = (
            df_long['tmp'].str.rsplit('_', n=1, expand=True)
        )
        df_long = df_long.drop(columns='tmp')
        
        # Приведение типов
        df_long = df_long.astype({
            'договор': 'string',
            'группа_счетов': 'string',
            'долгая_короткая_часть': 'string',
            'сальдо, тыс.ед.': 'float64',
        })
        
        # Фильтрация нулевых значений
        mask_valid = df_long['сальдо, тыс.ед.'].notna() & df_long['сальдо, тыс.ед.'].ne(0)
        df_long = df_long.loc[mask_valid].copy()
        
        # Очистка пробелов
        df_long = self.clean_whitespace(df_long)
        
        return df_long
    
    def _split_long_short_debt(self, df_long: pd.DataFrame, 
                           osv_all_df: pd.DataFrame) -> pd.DataFrame:
        """
        Векторизованная разбивка долгой/короткой части задолженности.
        
        Заменяет iterrows() на merge (декартово произведение по договору),
        что эквивалентно explode/unnest и работает в 10-100 раз быстрее.
        """
        # =========================================================================
        # 1. Подготовка данных из ОСВ (расчёт долей)
        # =========================================================================
        mask_filter = (
            osv_all_df['подвид_задолженности'].isin(['Аренда', 'Лизинг']) &
            osv_all_df['счет'].str.startswith('76')
        )
        osv_filtered = osv_all_df[mask_filter].copy()
        
        if osv_filtered.empty:
            logger.warning("Нет строк для разбивки по счетам 76")
            # Возвращаем df_long с заполненными заглушками
            df_long['счет'] = df_long['группа_счетов']
            df_long['доля'] = np.where(df_long['группа_счетов'] == "76", np.nan, 1.0)
            df_long['тип_задолженности'] = np.where(
                df_long['группа_счетов'] == "76", 'Не определен', 'Не применимо'
            )
            return df_long
        
        # Векторизованный расчёт долей
        group_sums = osv_filtered.groupby('договор')['сальдо, тыс.ед.'].transform('sum')
        abs_sums = osv_filtered['сальдо, тыс.ед.'].abs().groupby(osv_filtered['договор']).transform('sum')
        denominator = np.where(group_sums != 0, group_sums, abs_sums)
        denominator = np.where(denominator != 0, denominator, 1)
        osv_filtered['доля'] = osv_filtered['сальдо, тыс.ед.'] / denominator
        
        # =========================================================================
        # 2. Разделяем df_long на "76" и "не-76" (97.21)
        # =========================================================================
        mask_76 = df_long['группа_счетов'] == "76"
        df_long_76 = df_long[mask_76].copy()
        df_long_other = df_long[~mask_76].copy()
        
        # =========================================================================
        # 3. Для "76": MERGE = декартово произведение (explode)
        #    Одна строка df_long × N строк osv_filtered для того же договора
        # =========================================================================
        if not df_long_76.empty:
            # Подготовка ОСВ для merge (переименовываем конфликтующие столбцы)
            osv_for_merge = osv_filtered[['договор', 'счет', 'доля', 'сальдо, тыс.ед.']].rename(
                columns={
                    'счет': 'счет_осв',
                    'сальдо, тыс.ед.': 'сальдо_осв_исходное'
                }
            )
            
            # ★ КЛЮЧЕВАЯ ОПЕРАЦИЯ: merge создаёт все комбинации
            merged_76 = df_long_76.merge(osv_for_merge, on='договор', how='left')
            
            # Определяем строки без совпадений (договор не найден в ОСВ)
            no_match_mask = merged_76['счет_осв'].isna()
            has_match_mask = ~no_match_mask
            
            # --- Для найденных договоров: пересчёт сальдо и заполнение ---
            merged_76.loc[has_match_mask, 'сальдо, тыс.ед.'] = (
                merged_76.loc[has_match_mask, 'сальдо, тыс.ед.'] *
                merged_76.loc[has_match_mask, 'доля']
            )
            merged_76.loc[has_match_mask, 'счет'] = merged_76.loc[has_match_mask, 'счет_осв']
            merged_76.loc[has_match_mask, 'тип_задолженности'] = np.where(
                merged_76.loc[has_match_mask, 'сальдо_осв_исходное'] < 0,
                'Кредиторская',
                'Дебиторская'
            )
            
            # --- Для ненайденных договоров: заглушки ---
            merged_76.loc[no_match_mask, 'счет'] = 'Не определен'
            merged_76.loc[no_match_mask, 'доля'] = np.nan
            merged_76.loc[no_match_mask, 'тип_задолженности'] = 'Не определен'
            
            # Удаляем служебные столбцы
            merged_76 = merged_76.drop(
                columns=['счет_осв', 'сальдо_осв_исходное'],
                errors='ignore'
            )
        else:
            merged_76 = df_long_76  # пустой DataFrame
        
        # =========================================================================
        # 4. Для не-76 (97.21): просто заполняем поля
        # =========================================================================
        if not df_long_other.empty:
            df_long_other['счет'] = df_long_other['группа_счетов']
            df_long_other['доля'] = 1.0
            df_long_other['тип_задолженности'] = 'Не применимо'
        
        # =========================================================================
        # 5. Объединяем результаты и сортируем
        # =========================================================================
        result_df = pd.concat([merged_76, df_long_other], ignore_index=True)
        
        result_df = result_df.sort_values(
            ['договор', 'долгая_короткая_часть', 'счет']
        ).reset_index(drop=True)
        
        return result_df
    
    # def _split_long_short_debt(self, df_long: pd.DataFrame, 
    #                            osv_all_df: pd.DataFrame) -> pd.DataFrame:
    #     """
    #     Разбивает долгую и короткую части задолженности на дебиторку и кредиторку.
    #     """
    #     # Фильтруем osv_all_df
    #     mask_filter = (
    #         osv_all_df['подвид_задолженности'].isin(['Аренда', 'Лизинг']) &
    #         osv_all_df['счет'].str.startswith('76')
    #     )
    #     osv_filtered = osv_all_df[mask_filter].copy()
        
    #     if osv_filtered.empty:
    #         logger.warning("Нет строк для разбивки по счетам 76")
    #         return df_long
        
    #     # ВЕКТОРИЗИРОВАННЫЙ РАСЧЕТ ДОЛЕЙ
    #     group_sums = osv_filtered.groupby('договор')['сальдо, тыс.ед.'].transform('sum')
    #     abs_sums = osv_filtered['сальдо, тыс.ед.'].abs().groupby(osv_filtered['договор']).transform('sum')
        
    #     denominator = np.where(group_sums != 0, group_sums, abs_sums)
    #     denominator = np.where(denominator != 0, denominator, 1)
        
    #     osv_filtered['доля'] = osv_filtered['сальдо, тыс.ед.'] / denominator
        
    #     # Создаем словарь долей
    #     contract_shares = (
    #                         osv_filtered[['договор', 'счет', 'доля', 'сальдо, тыс.ед.']]
    #                         .groupby('договор')
    #                         .apply(
    #                             lambda x: x[['счет', 'доля', 'сальдо, тыс.ед.']].to_dict('records'),
    #                             include_groups=False  # ← ДОБАВЛЕНО
    #                         )
    #                         .to_dict()
    #                     )
        
    #     # ВЕКТОРИЗИРОВАННАЯ РАЗБИВКА
    #     result_rows = []
        
    #     for idx, row in df_long.iterrows():
    #         contract = row['договор']
    #         saldo = row['сальдо, тыс.ед.']
    #         group_account = row['группа_счетов']
            
    #         if group_account == "76" and contract in contract_shares:
    #             for share_info in contract_shares[contract]:
    #                 new_row = row.to_dict()
    #                 new_row['счет'] = share_info['счет']
    #                 new_row['доля'] = share_info['доля']
    #                 new_row['сальдо, тыс.ед.'] = saldo * share_info['доля']
    #                 new_row['тип_задолженности'] = (
    #                     'Кредиторская' if share_info['сальдо, тыс.ед.'] < 0 else 'Дебиторская'
    #                 )
    #                 result_rows.append(new_row)
    #         else:
    #             new_row = row.to_dict()
    #             if group_account != "76":
    #                 new_row['счет'] = group_account
    #                 new_row['доля'] = 1.0
    #                 new_row['тип_задолженности'] = 'Не применимо'
    #             else:
    #                 new_row['счет'] = 'Не определен'
    #                 new_row['доля'] = np.nan
    #                 new_row['тип_задолженности'] = 'Не определен'
    #             result_rows.append(new_row)
        
    #     result_df = pd.DataFrame(result_rows)
    #     result_df = result_df.sort_values(
    #         ['договор', 'долгая_короткая_часть', 'счет']
    #     ).reset_index(drop=True)
        
    #     return result_df
    
    def _replace_long_short_split(self, osv_all_df: pd.DataFrame, 
                                  decoded_lease_report: pd.DataFrame) -> pd.DataFrame:
        """Векторизованная версия замены данных с разбивкой на долгую/короткую части."""
        # Сохраняем исходные типы
        original_dtypes = osv_all_df.dtypes.to_dict()
        
        # Агрегируем decoded_lease_report
        decoded_grouped = (
            decoded_lease_report.groupby(['договор', 'счет', 'долгая_короткая_часть'], observed=True)['сальдо, тыс.ед.']
            .sum()
            .unstack(fill_value=0)
            .reset_index()
        )
        
        # Гарантируем наличие обоих столбцов
        for col in [self.LONG_TERM, self.SHORT_TERM]:
            if col not in decoded_grouped.columns:
                decoded_grouped[col] = 0
        
        decoded_grouped['Итого'] = decoded_grouped[self.LONG_TERM] + decoded_grouped[self.SHORT_TERM]
        
        # Вычисляем коэффициенты
        decoded_grouped['ratio_L'] = np.where(
            decoded_grouped['Итого'] == 0,
            0.0,
            decoded_grouped[self.LONG_TERM] / decoded_grouped['Итого']
        )
        decoded_grouped['ratio_K'] = np.where(
            decoded_grouped['Итого'] == 0,
            0.0,
            decoded_grouped[self.SHORT_TERM] / decoded_grouped['Итого']
        )
        
        # Объединяем коэффициенты с основным DataFrame
        merge_cols = ['договор', 'счет', 'ratio_L', 'ratio_K']
        df_merged = osv_all_df.merge(
            decoded_grouped[merge_cols], 
            on=['договор', 'счет'], 
            how='left'
        )
        
        # Маска для строк с разбивкой
        has_split = df_merged['ratio_L'].notna()
        
        # Формируем разбитые строки
        df_split_base = df_merged[has_split].copy()
        
        df_long_part = df_split_base.copy()
        df_long_part['долгая_короткая_часть'] = self.LONG_TERM
        df_long_part['сальдо, тыс.ед.'] = df_long_part['сальдо, тыс.ед.'] * df_long_part['ratio_L']
        df_long_part = df_long_part[df_long_part['сальдо, тыс.ед.'] != 0]
        
        df_short_part = df_split_base.copy()
        df_short_part['долгая_короткая_часть'] = self.SHORT_TERM
        df_short_part['сальдо, тыс.ед.'] = df_short_part['сальдо, тыс.ед.'] * df_short_part['ratio_K']
        df_short_part = df_short_part[df_short_part['сальдо, тыс.ед.'] != 0]
        
        df_split_result = pd.concat([df_long_part, df_short_part], ignore_index=True)
        
        df_unsplit = df_merged[~has_split].copy()
        
        result_df = pd.concat([df_unsplit, df_split_result], ignore_index=True)
        result_df = result_df.drop(
            columns=['ratio_L', 'ratio_K', 'Итого', self.LONG_TERM, self.SHORT_TERM],
            errors='ignore'
        )
        
        # Восстанавливаем типы
        for col, dtype in original_dtypes.items():
            if col not in result_df.columns:
                continue
            result_df[col] = result_df[col].astype(dtype)
        
        return result_df
    
    # =========================================================================
    # МЕТОДЫ ОБРАБОТКИ РЕКЛАССА ПО 97 СЧЕТУ
    # =========================================================================
    
    def _process_97_reclass(self, osv_all_df: pd.DataFrame, 
                           name_company: str, 
                           period: str) -> pd.DataFrame:
        """Обрабатывает рекласс долгие/короткие по 97 счету (кроме аренды/лизинга)."""
        file_path = find_register_file(
            folder_path=SPECIAL_REPORTS_DIR,
            type_register='реклассдолгкорт'
        )
        
        if not file_path:
            logger.warning(
                f"Файл с реклассом рбп на 97.21 на долгие/короткие не найден. "
                f"Рекласс по 97 счету (кроме аренды/лизинга) не проводим."
            )
            return osv_all_df
        
        logger.debug(f"Файл {file_path.name} найден. Проводим рекласс по 97 счету.")
        
        reclass_97_df = DataLoader.load_long_short_register(file_path)
        reclass_97_df = self._clean_reclass_97(reclass_97_df)
                
        # Проверка отсутствующих значений
        missing_values = set(reclass_97_df[LONG_SHORT_SUBCONTO_COL]) - set(osv_all_df['допсубконто'])
        
        if missing_values:
            # ★ Формируем problem_data — строки из отчёта с отсутствующими значениями
            problem_data = (
                reclass_97_df[reclass_97_df[LONG_SHORT_SUBCONTO_COL].isin(missing_values)]
                .copy()
            )
            
            # ★ Выбрасываем ReferenceMismatchError
            # Базовый класс сам сохранит в Excel и залогорирует
            raise ReferenceMismatchError(
                message=(
                    f"Найдено {len(missing_values)} отсутствующих значений "
                    f"Отчета долгие/короткие в сводной ОСВ"
                ),
                problem_data=problem_data,
                reference_name="Сводная ОСВ (допсубконто)",
                missing_values=sorted(missing_values),
            )
        
        reclass_dict = dict(zip(
            reclass_97_df[LONG_SHORT_SUBCONTO_COL],
            reclass_97_df[LONG_SHORT_TOTAL_COL]
        ))
        
        mask = (
            (osv_all_df['долгая_короткая_часть'] == self.SHORT_TERM) &
            (osv_all_df['допсубконто'].isin(reclass_97_df[LONG_SHORT_SUBCONTO_COL]))
        )
        
        if not mask.any():
            logger.debug("Нет строк для рекласса по 97 счету")
            return osv_all_df
        
        return self._apply_97_reclass(osv_all_df, mask, reclass_dict)
    
    def _clean_reclass_97(self, df: pd.DataFrame) -> pd.DataFrame:
        """Очищает DataFrame рекласса по 97 счету."""
        df = df.dropna(how='all').dropna(axis=1, how='all')
        df = set_header_from_row(df, LONG_SHORT_SUBCONTO_COL)
        
        key_columns = [LONG_SHORT_SUBCONTO_COL, LONG_SHORT_TOTAL_COL]
        existing_key_cols = [col for col in key_columns if col in df.columns]
        if existing_key_cols:
            df = df.dropna(subset=existing_key_cols)
        
        df = df[df[LONG_SHORT_SUBCONTO_COL] != LONG_SHORT_ITOGO_VALUE]
        
        df[LONG_SHORT_SUBCONTO_COL] = df[LONG_SHORT_SUBCONTO_COL].astype('string')
        df[LONG_SHORT_TOTAL_COL] = (
            pd.to_numeric(df[LONG_SHORT_TOTAL_COL], errors='coerce')
            .div(1000)
            .round(2)
        )
        
        df = self.clean_whitespace(df)
        
        return df
    
    def _apply_97_reclass(self, osv_all_df: pd.DataFrame, 
                         mask: pd.Series, 
                         reclass_dict: dict) -> pd.DataFrame:
        """Применяет рекласс долгие/короткие по 97 счету."""
        original_sums = osv_all_df[mask].groupby('допсубконто', observed=True)['сальдо, тыс.ед.'].sum()
        
        rows_to_split = osv_all_df[mask].copy()
        long_term_amounts = rows_to_split['допсубконто'].map(reclass_dict)
        
        osv_all_df.loc[mask, 'сальдо, тыс.ед.'] = (
            osv_all_df.loc[mask, 'сальдо, тыс.ед.'] - long_term_amounts
        )
        
        new_rows = rows_to_split.copy()
        
        new_rows['долгая_короткая_часть'] = self.LONG_TERM
        new_rows['сальдо, тыс.ед.'] = long_term_amounts.values
        
        result_df = pd.concat([osv_all_df, new_rows], ignore_index=True)
        result_df['долгая_короткая_часть'] = result_df['долгая_короткая_часть'].astype('string')
        
        updated_sums = result_df[result_df['допсубконто'].isin(long_term_amounts.index)].groupby(
            'допсубконто', observed=True
        )['сальдо, тыс.ед.'].sum()
        
        diff = (original_sums - updated_sums).abs()
        if (diff > 0.01).any():
            logger.error(f"⚠️ Расхождения сумм после рекласса по 97: {diff[diff > 0.01].to_dict()}")
        
        logger.debug(f"Рекласс по 97 выполнен: {mask.sum()} строк разбито")
        
        return result_df
    
    # =========================================================================
    # ОСНОВНОЙ МЕТОД ОБРАБОТКИ
    # =========================================================================
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки шага 7."""
        logger.debug("Добавление признака долгая/короткая часть")
        
        osv_all_df = context.main_df.copy()
        name_company = context.get_metadata('company_name')
        period = context.get_metadata('period')
        mapping_df = context.data.get('mapping')
        
        # 1. Инициализация столбца долгая/короткая часть из mapping
        osv_all_df = self._initialize_long_short_column(osv_all_df, mapping_df)
        
        # 2. Рекласс по 97 счету (кроме аренды/лизинга)
        osv_all_df = self._process_97_reclass(osv_all_df, name_company, period)
        
        # 3. Обработка лизинговых договоров
        osv_all_df = self._process_lease_type(
            osv_all_df, 
            type_register='лизингреклассдолгкорт',
            lease_type=self.LEASE_TYPE,
            name_company=name_company,
            period=period
        )
        
        # 4. Обработка арендных договоров
        osv_all_df = self._process_lease_type(
            osv_all_df,
            type_register='арендареклассдолгкорт',
            lease_type=self.RENT_TYPE,
            name_company=name_company,
            period=period
        )
        
        # Финальная очистка
        osv_all_df = osv_all_df.reset_index(drop=True)
        context.main_df = osv_all_df
        
        return context
    
    def _initialize_long_short_column(self, osv_all_df: pd.DataFrame, 
                                 mapping_df: pd.DataFrame) -> pd.DataFrame:
        """Инициализирует столбец долгая/короткая часть из mapping."""
        # Фильтруем mapping для строк с признаком долгая/короткая
        mapping_df_long_short = mapping_df[
            mapping_df['долгая_короткая_часть'].isin([self.LONG_TERM, self.SHORT_TERM])
        ]
        
        if mapping_df_long_short.empty:
            # Если нет записей с признаком, заполняем всё UNSPECIFIED
            osv_all_df['долгая_короткая_часть'] = self.UNSPECIFIED
        else:
            # Создаём MultiIndex для быстрого поиска
            mapping_index = pd.MultiIndex.from_frame(
                mapping_df_long_short[['счет', 'субконто']]
            )
            
            # ★ ИСПРАВЛЕНИЕ: все значения в словаре = SHORT_TERM
            # (игнорируем реальные значения из mapping_df['долгая_короткая_часть'])
            mapping_dict = dict.fromkeys(mapping_index, self.SHORT_TERM)
            
            # Создаём MultiIndex из osv_all_df
            osv_index = pd.MultiIndex.from_frame(osv_all_df[['счет', 'субконто']])
            
            # Применяем маппинг векторизованно
            osv_all_df['долгая_короткая_часть'] = (
                osv_index.to_series()
                .map(mapping_dict)
                .fillna(self.UNSPECIFIED)
                .values
            )
        
        # ★ Устанавливаем строковый тип
        osv_all_df['долгая_короткая_часть'] = osv_all_df['долгая_короткая_часть'].astype('string')
        
        logger.debug(f"Инициализация столбца завершена: {(osv_all_df['долгая_короткая_часть'] != self.UNSPECIFIED).sum()} строк классифицировано")
        
        return osv_all_df
    
    def _process_lease_type(self, osv_all_df: pd.DataFrame,
                           type_register: str,
                           lease_type: str,
                           name_company: str,
                           period: str) -> pd.DataFrame:
        """Обрабатывает один тип договоров (лизинг или аренда)."""
        logger.debug(f"Обработка {lease_type}-договоров")
        
        decoded_report = self._process_lease_report_decoding(
            type_register=type_register,
            name_company=name_company,
            period=period
        )
        
        if not isinstance(decoded_report, pd.DataFrame) or decoded_report.empty:
            logger.debug(f"Отчет по {lease_type}ым договорам пуст или не найден")
            return osv_all_df
        
        decoded_report_original = decoded_report.copy()
        decoded_report_split = self._split_long_short_debt(decoded_report, osv_all_df)
        
        self._validate_split(decoded_report_original, decoded_report_split)
        
        decoded_report_split = decoded_report_split.loc[:, [
            'договор', 'счет', 'долгая_короткая_часть', 'сальдо, тыс.ед.'
        ]]
        
        osv_all_df_updated = self._replace_long_short_split(osv_all_df, decoded_report_split)
        
        self._validate_replacement(osv_all_df, osv_all_df_updated, decoded_report_split, lease_type)
        
        logger.debug(f"Обработка {lease_type}-договоров завершена")
        
        return osv_all_df_updated