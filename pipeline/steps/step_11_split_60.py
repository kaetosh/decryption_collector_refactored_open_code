"""
Шаг 11: Разбиение счета 60 по признаку ОС/не-ОС.

Рекласс задолженности по счету 60 на две части:
- Задолженность за ОС (инвестиционные договоры)
- Задолженность за не-ОС
"""
from typing import Optional
import pandas as pd
from loguru import logger
from pathlib import Path
from datetime import datetime

from pipeline.base import Step, ProcessingContext
from pipeline.classifiers import ReceivableClassifier
from pipeline.errors import ConvergenceError
from io_module import DataLoader
from utils import find_register_file, find_target_column
from config.settings import SPECIAL_REPORTS_DIR, STRICT_CONTRACTOR_CHECK, OUTPUT_DATA_DIR

class Step11Split60AccountDebtByOSStatusStep(Step):
    """
    Шаг 11: Разбиение счета 60 по признаку ОС/не-ОС.
    
    Рекласс задолженности по счету 60 на две части:
    - Задолженность за ОС (инвестиционные договоры)
    - Задолженность за не-ОС
    """
    
    # Константы
    UNSPECIFIED = 'не_указано'
    THIRD_PARTY = '3 лица'
    ACCOUNT_60_PREFIX = '60'
    INVEST_YES = 'да'
    INVEST_NO = 'нет'
    
    # Допуск для проверки сходимости ОСВ 60 (в единицах, т.к. check_df в единицах)
    # 100 000 руб = 100 тыс. руб
    CONVERGENCE_TOLERANCE_60 = 100_000
    
    def __init__(self):
        super().__init__(
            name="Шаг 11: Разбиение счета 60 на инвест и неинвест задолженность",
            description="Рекласс задолженности на основе ГАП отчета ОСВ 60 Инвест.Договор"
        )
    
    # =========================================================================
    # ЗАГРУЗКА И ОБРАБОТКА СПЕЦОТЧЕТА 60 ИНВЕСТ
    # =========================================================================
    
    def _load_and_process_60_invest(
        self, 
        name_company: str,
        period: str
    ) -> Optional[pd.DataFrame]:
        """
        Оркестратор: ищет файл, загружает и обрабатывает ОСВ 60 Инвест.
        
        Returns:
            Обработанный DataFrame или None, если файл не найден
        """
        input_path = find_register_file(
            folder_path=SPECIAL_REPORTS_DIR,
            type_register='осв',
            account_number='60инвест'
        )
        
        expected_filename = f"{name_company}_осв_60инвест_{period}_.xlsx"
        
        if not input_path:
            logger.warning(
                f"Файл {expected_filename} не найден. "
                f"Рекласс остатков на 60 счете не проводим."
            )
            return None
        
        logger.debug(f"Файл {expected_filename} найден. Проводим рекласс.")
        
        # Загрузка сырых данных
        df, check_df = DataLoader.load_process_60_invest(input_path)
        
        # Проверка сходимости
        self._validate_convergence(check_df)
        
        # Базовая очистка
        df = self._clean_raw_60_invest(df)
        
        # Извлечение Level_-столбцов
        df = self._extract_level_columns(df)
        
        # Нормализация invest_договор
        df = self._normalize_invest_contract(df)
        
        return df
    
    def _validate_convergence(self, check_df: pd.DataFrame) -> None:
        """
        Проверяет сходимость ОСВ 60 Инвест.
        
        При расхождении выбрасывает ConvergenceError с problem_data=check_df.
        Базовый класс Step сам сохранит проблемные данные в Excel.
        """
        if check_df.empty or 'Сальдо_конец_разница' not in check_df.columns:
            logger.warning("Нет данных для проверки сходимости ОСВ 60")
            return
        
        sum_diff = check_df['Сальдо_конец_разница'].sum()
        
        if abs(sum_diff) > self.CONVERGENCE_TOLERANCE_60:
            raise ConvergenceError(
                message="Остатки по ОСВ 60 Инвест отличаются от исходной",
                problem_data=check_df,  # ← Вся контрольная таблица
                reference_name="ОСВ 60 Инвест",
                difference=abs(sum_diff),
                tolerance=self.CONVERGENCE_TOLERANCE_60,
            )
        
        logger.debug(f"Сходимость ОСВ 60 подтверждена: разница {sum_diff:.2f} руб.")
    
    def _clean_raw_60_invest(self, df: pd.DataFrame) -> pd.DataFrame:
        """Базовая очистка сырого DataFrame ОСВ 60."""
        df = df.copy()
        
        # Удаление ненужных столбцов
        cols_to_drop = [
            'Дебет_начало', 'Кредит_начало', 'Дебет_оборот', 'Кредит_оборот',
            'Начало периода для вида связи', 'Конец периода для вида связи',
            'Вид связи КА за период', 'Исх.файл'
        ]
        df = df.drop(columns=cols_to_drop, errors='ignore')
        
        # Расчет сальдо в тыс. ед.
        df['сальдо, тыс.ед.'] = (
            df['Дебет_конец']
            .sub(df['Кредит_конец'], fill_value=0)
            .div(1_000)
            .round(2)
        )
        
        # Фильтрация нулевых сальдо
        df = df[df['сальдо, тыс.ед.'] != 0].copy()
        df = df.drop(columns=['Дебет_конец', 'Кредит_конец'], errors='ignore')
        
        # Очистка лишних пробелов
        df = self.clean_whitespace(df)
        return df
    
    def _extract_level_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Извлекает данные из Level_-столбцов."""
        df = df.copy()
        
        # Поиск столбцов с валидацией
        account_col = find_target_column(df, 'Level_', 'rightmost', 'all_accounts', 0)
        invest_contract_col = find_target_column(df, 'Level_', 'rightmost', 'all_accounts', 1)
        contractor_col = find_target_column(df, 'Level_', 'rightmost', 'all_accounts', 2)
        
        # Валидация
        missing = {
            name: col for name, col in [
                ('счет', account_col),
                ('инвест_договор', invest_contract_col),
                ('допсубконто', contractor_col),
            ] if col is None
        }
        if missing:
            raise ValueError(
                f"Не найдены столбцы в ОСВ 60 Инвест: {list(missing.keys())}. "
                f"Проверьте структуру выгрузки."
            )
        
        # Извлечение данных
        df['счет'] = df[account_col].astype('string')
        df['инвест_договор'] = df[invest_contract_col].astype('string')
        df['допсубконто'] = df[contractor_col].astype('string')
        
        # Удаление Level_-столбцов
        df = df.loc[:, ~df.columns.str.startswith('Level_')]
        
        # Переименование Субконто
        df = df.rename(columns={'Субконто': 'субконто'})
        
        return df
    
    def _normalize_invest_contract(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Нормализует столбец 'инвест_договор': 
        все значения кроме 'да'/'нет' заменяет на 'нет'.
        """
        df = df.copy()
        
        # Приводим к нижнему регистру для унификации (пока еще string тип)
        df['инвест_договор'] = df['инвест_договор'].str.lower()
        
        # Все значения кроме 'да'/'нет' → 'нет'
        mask_invalid = ~df['инвест_договор'].isin([self.INVEST_YES, self.INVEST_NO])
        df.loc[mask_invalid, 'инвест_договор'] = self.INVEST_NO
        
        return df
    
    # =========================================================================
    # КЛАССИФИКАЦИЯ КАЧЕСТВЕННЫХ ПРИЗНАКОВ
    # =========================================================================
    
    def _add_related_party_type(
        self, 
        df: pd.DataFrame, 
        osv_all_df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Добавляет столбец 'вид_связи' на основе данных из основной ОСВ.
        
        Маппинг выполняется по допсубконто (контрагенту).
        """
        df = df.copy()
        
        # Создаём маппинг допсубконто → вид_связи из основной ОСВ
        osv_unique = osv_all_df.drop_duplicates(subset=['допсубконто'], keep='first')
        mapping_series = osv_unique.set_index('допсубконто')['вид_связи']
        
        df['вид_связи'] = df['допсубконто'].map(mapping_series).fillna(self.UNSPECIFIED)
        df['вид_связи'] = df['вид_связи'].astype('string')
        
        return df
    
    def _save_unknown_contractors(self, unknown_df: pd.DataFrame) -> Path:
        """
        Сохраняет список неизвестных контрагентов в Excel-файл.
        
        Args:
            unknown_df: DataFrame с контрагентами и количеством строк
            
        Returns:
            Путь к сохранённому файлу
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = (
            Path(OUTPUT_DATA_DIR) / 
            f'unknown_contractors_60_invest_{timestamp}.xlsx'
        )
        
        # Создаём директорию, если её нет
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Сохраняем в Excel
        unknown_df.to_excel(output_path, index=False)
        
        return output_path
    
    def _handle_unknown_contractors(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Обрабатывает контрагентов, которых нет в основной ОСВ.
        
        В строгом режиме (STRICT_CONTRACTOR_CHECK=True) — выбрасывает ошибку.
        В мягком режиме — заменяет на '3 лица' и сохраняет список
        неизвестных контрагентов в Excel-файл.
        """
        unknown_mask = df['вид_связи'] == self.UNSPECIFIED
        
        if not unknown_mask.any():
            return df
        
        # Формируем DataFrame с неизвестными контрагентами
        unknown_df = (
            df.loc[unknown_mask, 'допсубконто']
            .value_counts()
            .reset_index()
            .rename(columns={
                'допсубконто': 'контрагент',
                'count': 'количество_строк'
            })
        )
        
        error_msg = (
            f"В спецотчете по 60 инвестдоговоры есть КА, "
            f"которые отсутствуют в ОСВ по 60: "
            f"{len(unknown_df)} контрагентов"
        )
        
        if STRICT_CONTRACTOR_CHECK:
            # Строгий режим: сохраняем файл и выбрасываем ошибку
            output_path = self._save_unknown_contractors(unknown_df)
            logger.error(
                f"{error_msg}. "
                f"Список сохранён в {output_path.parent.name}/{output_path.name}"
            )
            raise ValueError(error_msg)
        
        # Мягкий режим: сохраняем файл и заменяем на '3 лица'
        output_path = self._save_unknown_contractors(unknown_df)
        logger.warning(
            f"{error_msg}. Заменяем на '{self.THIRD_PARTY}'. "
            f"Список сохранён в {output_path.parent.name}/{output_path.name}"
        )
        
        # Работаем со string типом, а не category
        df.loc[unknown_mask, 'вид_связи'] = self.THIRD_PARTY
        
        return df  # ← ВАЖНО: возвращаем обработанный df!
    
    def _restore_dtypes_after_concat(
        self, 
        df: pd.DataFrame, 
        target_dtypes: dict
    ) -> pd.DataFrame:
        """Восстанавливает типы данных после pd.concat."""
        # Восстанавливаем типы из target_dtypes
        for col, dtype in target_dtypes.items():
            if col not in df.columns:
                continue
            if pd.api.types.is_string_dtype(dtype):
                df[col] = df[col].astype('string')
            elif pd.api.types.is_numeric_dtype(dtype):
                df[col] = df[col].astype(dtype)
        
        # ★ ДОПОЛНИТЕЛЬНАЯ ЗАЩИТА: явно приводим все текстовые столбцы к string
        text_cols = [
            'инвест_договор', 'вид_связи', 'вид_задолженности', 
            'подвид_задолженности', 'допсубконто', 'субконто'
        ]
        for col in text_cols:
            if col in df.columns and df[col].dtype == 'object':
                df[col] = df[col].astype('string')
        
        return df
    
    def _ensure_invest_contract_column(self, osv_all_df: pd.DataFrame) -> pd.DataFrame:
        """
        Гарантирует наличие столбца 'инвест_договор' в DataFrame.
        
        Используется когда спецотчёт ОСВ 60 Инвест отсутствует.
        Заполняет столбец по правилам:
        - Для счетов, начинающихся с '60': значение 'нет'
          (инвестиционных договоров нет, т.к. нет спецотчёта)
        - Для всех остальных счетов: значение 'не_указано'
          (признак неприменим к этим счетам)
        
        Приводит столбец к категориальному типу с полным набором категорий.
        """
        df = osv_all_df.copy()
        
        # Маска для счетов 60.xx
        mask_60 = df['счет'].astype(str).str.startswith(self.ACCOUNT_60_PREFIX, na=False)
        
        # Инициализируем столбец значением 'не_указано' для всех строк
        df['инвест_договор'] = self.UNSPECIFIED
        
        # Для счетов 60 ставим 'нет' (инвестиционных договоров нет)
        df.loc[mask_60, 'инвест_договор'] = self.INVEST_NO
        
        # Приводим к строковому типу
        df['инвест_договор'] = df['инвест_договор'].astype('string')
        
        logger.debug(
            f"Добавлен столбец 'инвест_договор': "
            f"{mask_60.sum()} строк со значением '{self.INVEST_NO}', "
            f"{(~mask_60).sum()} строк со значением '{self.UNSPECIFIED}'"
        )
        
        return df
    
    def _prepare_non_60_rows(
        self, 
        osv_all_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Подготавливает строки основной ОСВ, которые НЕ относятся к счету 60."""
        osv_filtered = osv_all_df[
            ~osv_all_df['счет'].str.startswith(self.ACCOUNT_60_PREFIX)
        ].copy()
        osv_filtered['инвест_договор'] = self.UNSPECIFIED
        osv_filtered['инвест_договор'] = osv_filtered['инвест_договор'].astype('string')  # ← Добавить
        return osv_filtered
    
    def _cast_qualitative_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Приводит качественные столбцы к нужным типам."""
        df['вид_связи'] = df['вид_связи'].astype('string')
        df['инвест_договор'] = df['инвест_договор'].astype('string')
        
        # 'Договор' → string
        if 'Договор' in df.columns:
            df['Договор'] = df['Договор'].astype('string')
        
        return df
    
    # =========================================================================
    # ОСНОВНОЙ МЕТОД ОБРАБОТКИ
    # =========================================================================
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки шага 11."""
        logger.debug("Разбиение счета 60")
        
        osv_all_df = context.main_df.copy()
        name_company = context.get_metadata('company_name')
        period = context.get_metadata('period')
        
        # 1. Загрузка спецотчета 60 Инвест
        df = self._load_and_process_60_invest(name_company, period)
        
        if df is None or df.empty:
            # ★ КРИТИЧЕСКИ ВАЖНО: даже без спецотчёта добавляем столбец 'инвест_договор'
            # Иначе следующие шаги (особенно шаг 13) упадут с KeyError
            logger.info(
                "Спецотчет 60 Инвест не найден или пуст. "
                "Добавляем столбец 'инвест_договор' с дефолтными значениями."
            )
            osv_all_df = self._ensure_invest_contract_column(osv_all_df)
            context.main_df = osv_all_df
            return context
        
        # 2. Получение данных из контекста
        partially_matching_accounts_df = context.data.get('partially_matching_accounts_df')
        mapping_df = context.data.get('mapping')
        
        if partially_matching_accounts_df is None or mapping_df is None:
            raise ValueError("Необходимые данные отсутствуют в контексте")
        
        # 3. Классификация задолженности (используем ReceivableClassifier)
        df = ReceivableClassifier.map_accounts_to_mapping(df, partially_matching_accounts_df)
        accounts_with_debt_type = ReceivableClassifier.get_accounts_with_debt_type(mapping_df)
        df = ReceivableClassifier.classify_debt_type(df, accounts_with_debt_type)
        df = ReceivableClassifier.handle_special_cases(df)
        df = ReceivableClassifier.clean_subaccounts(df, mapping_df)
        
        # 4. Добавление подвида задолженности
        subtype_mapping = ReceivableClassifier.get_subtype_mapping(mapping_df)
        df = ReceivableClassifier.merge_subtypes(df, subtype_mapping)
        df = ReceivableClassifier.handle_missing_subtypes(df)
        df = ReceivableClassifier.apply_categorical_subtype(df, mapping_df)

        # 5. Добавление вида связи
        df = self._add_related_party_type(df, osv_all_df)
        
        # 6. Обработка неизвестных контрагентов
        df = self._handle_unknown_contractors(df)
        
        # 7. Приведение типов для качественных столбцов
        df = self._cast_qualitative_columns(df)
        
        # 8. Сохраняем исходные типы основной ОСВ
        target_dtypes = osv_all_df.dtypes.to_dict()
        
        # 9. Подготовка НЕ-60 строк основной ОСВ
        osv_filtered = self._prepare_non_60_rows(osv_all_df)
        
        # 10. Объединение
        osv_all_df = pd.concat([osv_filtered, df], ignore_index=True)
        
        # 11. Восстановление типов
        osv_all_df = self._restore_dtypes_after_concat(osv_all_df, target_dtypes)
        
        # убеждаемся, что нет NaN в ключевых столбцах
        critical_cols = ['счет',
                         'субконто',
                         'вид_задолженности',
                         'подвид_задолженности',
                         'группа_ос_аренды_лизинга',
                         'долгая_короткая_часть',
                         'сегмент_биоактивов_для_01_02',
                         'вид_связи',
                         'инвест_договор',
                         'договор',
                         'допсубконто',
                         ]
        for col in critical_cols:
            if col not in osv_all_df.columns:
                continue
            
            nan_count = osv_all_df[col].isna().sum()
            if nan_count > 0:
                logger.debug(
                    f"Обнаружено {nan_count} NaN в столбце '{col}' после шага 11. "
                    f"Заменяем на '{self.UNSPECIFIED}'."
                )
                
                # Заполняем NaN
                osv_all_df[col] = osv_all_df[col].fillna(self.UNSPECIFIED)
        
        logger.debug(f"Разбиение счета 60 завершено. Добавлено {len(df)} строк.")
        
        context.main_df = osv_all_df
        return context
