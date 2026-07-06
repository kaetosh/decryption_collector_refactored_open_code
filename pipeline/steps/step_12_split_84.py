"""
Шаг 12: Разбиение счета 84 на НРП прошлых и текущего периода.

Для годовой расшифровки разбивает остаток по счету 84 
(нераспределённая прибыль) на:
- НРП прошлых периодов (накопленный результат на начало периода)
- Финансовый результат текущего периода (оборот со счётом 99)
"""

from typing import Optional
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.errors import ConvergenceError  # ← НОВОЕ
from io_module import DataLoader
from utils import find_register_file
from config.settings import SPECIAL_REPORTS_DIR

class Step12Split84AccountBalanceStep(Step):
    """
    Шаг 12: Разбиение счета 84 на НРП прошлых и текущего периода.
    
    Для годовой расшифровки разбивает остаток по счету 84 
    (нераспределённая прибыль) на:
    - НРП прошлых периодов (накопленный результат на начало периода)
    - Финансовый результат текущего периода (оборот со счётом 99)
    """
    
    # Константы
    ACCOUNT_84 = '84'  # Нераспределённая прибыль
    ACCOUNT_99 = '99'  # Финансовые результаты текущего периода
    
    # Допуск для проверки сходимости анализа 84 (в единицах)
    # 100 000 руб = 100 тыс. руб
    CONVERGENCE_TOLERANCE_84 = 100_000
    
    def __init__(self):
        super().__init__(
            name="Шаг 12: Разбиение счета 84 (период год)",
            description="Разбиение НРП на прошлые и текущий период"
        )
    
    # =========================================================================
    # ЗАГРУЗКА И ОБРАБОТКА АНАЛИЗА ПО СЧЁТУ 84
    # =========================================================================
    
    def _load_and_process_84_analysis(
        self, 
        name_company: str,
        period: str
    ) -> Optional[pd.DataFrame]:
        """
        Оркестратор: ищет файл, загружает и обрабатывает анализ по счёту 84.
        
        Returns:
            Обработанный DataFrame или None, если файл не найден
        """
        input_path = find_register_file(
            folder_path=SPECIAL_REPORTS_DIR,
            type_register='анализ',
            account_number=self.ACCOUNT_84
        )
        
        expected_filename = f"{name_company}_анализ_{self.ACCOUNT_84}_{period}_.xlsx"
        
        if not input_path:
            logger.warning(
                f"Файл {expected_filename} не найден. "
                f"Рекласс счёта 84 на НРП и прибыль периода не проводим."
            )
            return None
        
        logger.debug(f"Файл {expected_filename} найден. Проводим рекласс.")
        
        # Загрузка сырых данных
        df, check_df = DataLoader.load_84_analysis(input_path)
        
        # Проверка сходимости
        self._validate_convergence(check_df)
        
        # Расчёт оборота и очистка
        df = self._calculate_turnover(df)
        
        return df
    
    def _validate_convergence(self, check_df: pd.DataFrame) -> None:
        """
        Проверяет сходимость анализа по счёту 84.
        
        При расхождении выбрасывает ConvergenceError с problem_data=check_df.
        Базовый класс Step сам сохранит проблемные данные в Excel.
        """
        if check_df.empty:
            logger.warning("Нет данных для проверки сходимости анализа 84")
            return
        
        # Проверяем наличие необходимых столбцов
        required_cols = ['Разница_С_кред', 'Разница_В_дебет']
        
        missing = [col for col in required_cols if col not in check_df.columns]
        if missing:
            logger.warning(f"Отсутствуют столбцы для проверки сходимости: {missing}")
            return
        
        sum_diff = abs(check_df[required_cols[0]].sum()) + abs(check_df[required_cols[1]].sum())
        
        if sum_diff > self.CONVERGENCE_TOLERANCE_84:
            # ★ Выбрасываем ConvergenceError с problem_data
            # Базовый класс сам сохранит в Excel и залогорирует
            raise ConvergenceError(
                message="Обороты по анализу 84 отличаются от исходного",
                problem_data=check_df,  # ← Вся контрольная таблица
                reference_name="Анализ 84",
                difference=sum_diff,
                tolerance=self.CONVERGENCE_TOLERANCE_84,
            )
        
        logger.debug(f"Сходимость анализа 84 подтверждена: разница {sum_diff:.2f} руб.")
    
    def _calculate_turnover(self, df: pd.DataFrame) -> pd.DataFrame:
        """Рассчитывает свёрнутый оборот и очищает DataFrame."""
        df = df.copy()
        
        # Расчёт оборота в тыс. ед.
        df['Оборот, тыс.ед.'] = (
            df['С кред. счетов']
            .sub(df['В дебет счетов'], fill_value=0)
            .div(1_000)
            .round(2)
        )
        
        # Фильтрация нулевых оборотов
        df = df[df['Оборот, тыс.ед.'] != 0].copy()
        
        # Удаление исходных столбцов
        df = df.drop(columns=['С кред. счетов', 'В дебет счетов'], errors='ignore')
        
        return df
    
    # =========================================================================
    # РАЗБИВКА СЧЁТА 84
    # =========================================================================
    
    def _calculate_current_period_turnover(self, df_analysis: pd.DataFrame) -> float:
        """
        Вычисляет оборот текущего периода (по счёту 99).
        
        Returns:
            Сумма оборота по счёту 99 в тыс.ед.
        """
        if 'Корр_счет' not in df_analysis.columns:
            logger.warning("Столбец 'Корр_счет' не найден в анализе 84")
            return 0.0
        
        # Фильтруем строки по счёту 99
        mask_99 = df_analysis['Корр_счет'].astype(str).str.startswith(self.ACCOUNT_99)
        turnover = df_analysis.loc[mask_99, 'Оборот, тыс.ед.'].sum()
        
        logger.debug(f"Оборот текущего периода (счёт 99): {turnover:.2f} тыс.ед.")
        
        return turnover
    
    def _split_account_84(
        self, 
        osv_all_df: pd.DataFrame, 
        current_period_turnover: float
    ) -> pd.DataFrame:
        """
        Разбивает счёт 84 на две строки:
        - Текущий период (счёт 99) — финансовый результат
        - Прошлые периоды (счёт 84) — накопленная прибыль
        
        Returns:
            DataFrame с разбитым счётом 84
        """
        df = osv_all_df.copy()
        
        # Находим строку со счётом 84
        mask_84 = df['счет'] == self.ACCOUNT_84
        df_84 = df[mask_84]
        
        # Валидация
        if df_84.empty:
            raise ValueError(f"Счёт {self.ACCOUNT_84} не найден в сводной ОСВ")
        
        if len(df_84) > 1:
            raise ValueError(
                f"Найдено несколько строк со счётом {self.ACCOUNT_84}: {len(df_84)}"
            )
        
        # Получаем исходное значение
        original_balance = df_84['сальдо, тыс.ед.'].iloc[0]
        
        logger.debug(
            f"Разбивка счёта {self.ACCOUNT_84}: "
            f"исходное сальдо={original_balance:.2f}, "
            f"финрезультат текущего периода={current_period_turnover:.2f}, "
            f"НРП прошлых периодов={original_balance - current_period_turnover:.2f}"
        )
        
        # Создаём две новые строки
        row_current = df_84.iloc[0].copy()
        row_current['сальдо, тыс.ед.'] = current_period_turnover
        row_current['счет'] = self.ACCOUNT_99
        
        row_accumulated = df_84.iloc[0].copy()
        row_accumulated['сальдо, тыс.ед.'] = original_balance - current_period_turnover
        # Счёт остаётся 84 — это НРП прошлых периодов
        
        # Удаляем исходную строку
        df = df[~mask_84].copy()
        
        # Создаём DataFrame с новыми строками
        new_rows = pd.DataFrame.from_records([
            row_current.to_dict(),
            row_accumulated.to_dict()
        ])
        
        # Восстанавливаем типы
        new_rows = self._restore_dtypes(new_rows, df)
        
        # Конкатенируем
        df = pd.concat([df, new_rows], ignore_index=True)
        
        return df
    
    def _restore_dtypes(
        self, 
        new_rows: pd.DataFrame, 
        reference_df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Восстанавливает типы данных new_rows на основе reference_df.
        """
        for col in reference_df.columns:
            if col not in new_rows.columns:
                continue
            
            ref_dtype = reference_df[col].dtype
            
            if pd.api.types.is_string_dtype(ref_dtype):
                new_rows[col] = new_rows[col].astype('string')
            elif pd.api.types.is_numeric_dtype(ref_dtype):
                new_rows[col] = new_rows[col].astype(ref_dtype)
        
        return new_rows
    
    # =========================================================================
    # ОСНОВНОЙ МЕТОД ОБРАБОТКИ
    # =========================================================================
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки шага 12."""
        logger.debug("Разбиение счета 84")
        
        osv_all_df = context.main_df.copy()
        name_company = context.get_metadata('company_name')
        period = context.get_metadata('period')
        
        # 1. Загрузка анализа по счёту 84
        df_analysis = self._load_and_process_84_analysis(name_company, period)
        
        if df_analysis is None or df_analysis.empty:
            logger.debug("Анализ по счёту 84 не найден или пуст, шаг пропущен")
            return context
        
        # 2. Вычисление оборота текущего периода (по счёту 99)
        current_period_turnover = self._calculate_current_period_turnover(df_analysis)
        
        # 3. Разбивка счёта 84
        osv_all_df = self._split_account_84(osv_all_df, current_period_turnover)
        
        logger.debug("Разбиение счёта 84 завершено.")
        
        context.main_df = osv_all_df
        
        return context