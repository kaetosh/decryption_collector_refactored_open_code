"""
Шаг 10: Классификация источника аренды (ГСК/ГАП).
На основе ведомости амортизации определяет признаки ГСК/ГАП
для арендованных основных средств.
"""
from typing import Optional
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.errors import ConvergenceError  # ← НОВОЕ
from io_module import DataLoader, DataSaver
from utils import find_register_file, cast_columns_to_types, get_required_columns_df
from config.settings import SPECIAL_REPORTS_DIR

class Step10ClassifyLeaseSourceStep(Step):
    """
    Шаг 10: Классификация источника аренды (ГСК/ГАП).
    
    На основе ведомости амортизации определяет признаки ГСК/ГАП
    для арендованных основных средств.
    """
    
    # Константы
    UNSPECIFIED = 'не_указано'
    THIRD_PARTY = '3 лица'
    ACCOUNTS_01_03 = ['01.03', '02.03']
    
    # Допуск для проверки сходимости (в тыс.ед.)
    # 1000 тыс.ед. = 1 млн руб — экспертный допуск для остаточной стоимости ОС
    CONVERGENCE_TOLERANCE = 3000
    
    # Имя справочника для кэширования
    RELATED_PARTIES_SHEET = 'ВидСвязиКА'
    
    def __init__(self):
        super().__init__(
            name="Шаг 10: Классификация групп ОС (аренда) по видам связи арендодателей",
            description="Определение ГСК/ГАП для арендованных ОС на основе Ведомости амортизации"
        )
    
    def _get_related_parties_mapping(self, context: ProcessingContext) -> dict:
        """
        Загружает и кэширует маппинг контрагентов на вид связи.
        
        Кэширование в контексте позволяет избежать повторной загрузки
        справочника (используется также в Step 9).
        
        Returns:
            Словарь {ВариантыНазвания: ВидСвязиКА}
        """
        cache_key = f'{self.RELATED_PARTIES_SHEET}_mapping'
        
        if cache_key in context.data:
            logger.debug(f"Используем кэшированный маппинг {self.RELATED_PARTIES_SHEET}")
            return context.data[cache_key]
        
        # Загрузка справочника
        group_companies_df = DataLoader.load_reference_data(
            sheet_name=self.RELATED_PARTIES_SHEET,
            strings=['ВидСвязиКА', 'ВариантыНазвания']
        )
        
        # Очистка пробелов
        group_companies_df = self.clean_whitespace(group_companies_df)
        
        # Создание маппинга
        mapping_dict = dict(zip(
            group_companies_df['ВариантыНазвания'],
            group_companies_df['ВидСвязиКА']
        ))
        
        # Кэшируем в контексте
        context.data[cache_key] = mapping_dict
        logger.debug(f"Загружен и кэширован маппинг {self.RELATED_PARTIES_SHEET}: {len(mapping_dict)} записей")
        
        return mapping_dict
    
    def _validate_input_convergence(
        self, 
        osv_all_df: pd.DataFrame, 
        depreciation_df: pd.DataFrame
    ) -> None:
        """
        Проверяет сходимость сумм по 01.03/02.03 между сводной ОСВ и ведомостью амортизации
        ДО начала расшифровки.
        
        Если суммы не совпадают — выбрасывает ConvergenceError.
        Базовый класс Step сам сохранит проблемные данные в Excel.
        
        Args:
            osv_all_df: Сводная ОСВ (до расшифровки)
            depreciation_df: Подготовленная ведомость амортизации
        """
        # Сумма из сводной ОСВ (ДО расшифровки)
        mask_osv = osv_all_df['счет'].isin(self.ACCOUNTS_01_03)
        sum_osv = osv_all_df.loc[mask_osv, 'сальдо, тыс.ед.'].sum()
        
        # Сумма из ведомости амортизации
        sum_depr = depreciation_df['сальдо, тыс.ед.'].sum()
        
        diff = abs(sum_osv - sum_depr)
        
        if diff > self.CONVERGENCE_TOLERANCE:
            # Формируем problem_data для диагностики
            problem_data = pd.DataFrame({
                'показатель': [
                    'Сумма 01.03/02.03 в сводной ОСВ (до расшифровки)',
                    'Сумма 01.03/02.03 в ведомости амортизации',
                    'Разница',
                    'Допуск'
                ],
                'значение': [sum_osv, sum_depr, diff, self.CONVERGENCE_TOLERANCE],
                'единица_измерения': ['тыс.ед.', 'тыс.ед.', 'тыс.ед.', 'тыс.ед.'],
            })
            
            raise ConvergenceError(
                message=(
                    f"ВХОДНАЯ ПРОВЕРКА: сумма по счетам 01.03/02.03 в сводной ОСВ "
                    f"не совпадает с ведомостью амортизации"
                ),
                problem_data=problem_data,
                reference_name="Входная сверка: ОСВ vs Ведомость амортизации",
                difference=diff,
                tolerance=self.CONVERGENCE_TOLERANCE,
                sum_before=sum_osv,
                sum_after=sum_depr,
            )
        
        logger.debug(
            f"✓ Входная сходимость подтверждена: "
            f"ОСВ={sum_osv:.2f}, Ведомость={sum_depr:.2f}, разница={diff:.2f} тыс.ед., допуск={self.CONVERGENCE_TOLERANCE} тыс.ед."
        )
    
    def _process_depreciation_statement_decoding(
        self,
        context: ProcessingContext,
        type_register: str,
        name_company: str,
        period: str
    ) -> Optional[pd.DataFrame]:
        """
        Загружает и обрабатывает сырую выгрузку Ведомости амортизации из 1С.
        
        Обрабатывает особенности выгрузки:
        - Двухстрочная шапка (из-за объединённых ячеек в Excel)
        - Итоговая строка с маркером "Итог" в первом столбце
        """
        input_path = find_register_file(
            folder_path=SPECIAL_REPORTS_DIR,
            type_register=type_register
        )
        
        expected_filename = f"{name_company}_{type_register}_0102_{period}_.xlsx"
        
        if not input_path:
            logger.warning(
                f"Файл {expected_filename} не найден. "
                f"Рекласс по виду связи для арендованных ОС не проводим."
            )
            return None
        
        logger.debug(f"Файл {expected_filename} найден. Проводим рекласс.")
        
        # 1. Загрузка сырого файла БЕЗ заголовков
        # (header=None — чтобы сохранить все строки, включая шапку)
        # df_raw = pd.read_excel(input_path, header=None, sheet_name=0)
        
        df_raw = DataLoader.process_depreciation_statement_decoding(input_path)
        df_raw = df_raw.dropna(how="all").dropna(axis=1, how="all")
        
        # 2. Поиск строки-шапки
        header_row_idx = self._find_header_row(df_raw)
        if header_row_idx is None:
            raise ValueError(
                f"Не удалось найти строку-шапку в файле {input_path.name}. "
                f"Ожидается строка со значением 'Основное средство'."
            )
        
        # 3. Объединение двух строк шапки в единый заголовок
        header_top = df_raw.iloc[header_row_idx]
        header_bot = df_raw.iloc[header_row_idx + 1]
        combined_header = self._combine_header_rows(header_top, header_bot)
        
        logger.debug(
            f"Найдена двухстрочная шапка на строках {header_row_idx} и "
            f"{header_row_idx + 1}. Объединено в {len(combined_header)} столбцов."
        )
        
        # 4. Формирование DataFrame: данные начинаются со строки header_row_idx + 2
        df = df_raw.iloc[header_row_idx + 2:].copy()
        df.columns = combined_header
        df = df.reset_index(drop=True)
        
        # 5. Удаление итоговой строки
        df = self._remove_total_row(df)
        
        # 6. Базовая очистка (пустые строки и столбцы)
        df = df.dropna(how='all').dropna(axis=1, how='all')
        
        # 7. Проверка обязательных столбцов
        required_columns = [
            'Стоимость на конец периода',
            'Амортизация на конец периода',
            'Группа учета ОС',
            'Контрагент'
        ]
        df = get_required_columns_df(df=df, required_columns=required_columns)
        
        # 8. Очистка пробелов в данных
        df = self.clean_whitespace(df)
        
        # 9. Получаем маппинг (с кэшированием)
        mapping_dict = self._get_related_parties_mapping(context)
        
        # 10. Объединяем map + fillna + astype в одну цепочку
        df['вид_связи'] = (
            df['Контрагент']
            .map(mapping_dict)
            .fillna(self.THIRD_PARTY)
            .astype('string')
        )
        
        # 11. Приведение типов
        type_mapping = {
            'string': ['Контрагент', 'Основное средство', 'Договор аренды', 'Вид взаиморасчетов',
                       'Группа учета ОС', 'вид_связи'],
            'numeric': [
                'Стоимость первоначальная',
                'Стоимость на начало периода',
                'Стоимость изменение',
                'Стоимость на конец периода',
                'Стоимость остаточная',
                'Амортизация на начало периода',
                'Амортизация за период',
                'Амортизация на конец периода',
            ]
        }
        df = cast_columns_to_types(df=df, type_mapping=type_mapping)
        
        # 12. Сохраним в OUTPUTDATA
        df_filtered = df.dropna(subset=["Стоимость на конец периода", "Амортизация на конец периода"],
                                how="all")
        DataSaver.save_to_excel(df_filtered, f'ведомостьОС_обработка_{name_company}_{period}.xlsx')
        
        # Сохраняем в контекст для последующей проверки в шаге 11a
        context.data['depreciation_statement_df'] = df_filtered
        logger.debug(f"Ведомость амортизации сохранена в контекст: {len(df_filtered)} строк")
        
        return df


    def _find_header_row(self, df: pd.DataFrame) -> Optional[int]:
        """
        Находит индекс строки-шапки таблицы.
        
        Ищет строку, содержащую значение "Основное средство" (регистронезависимо).
        Проверяет только первые 20 строк, чтобы не сканировать весь файл.
        
        Returns:
            Индекс строки-шапки или None, если не найдена.
        """
        for idx in range(min(20, len(df))):
            row_values = df.iloc[idx].astype(str).str.strip().str.lower()
            if (row_values == 'основное средство').any():
                logger.debug(f"Строка-шапка найдена на индексе {idx}")
                return idx
        return None
    
    
    def _combine_header_rows(
        self,
        header_top: pd.Series,
        header_bot: pd.Series
    ) -> list:
        """
        Объединяет две строки шапки в единый список заголовков.
        
        Алгоритм:
        - Проходим слева направо, запоминая последний непустой заголовок верхней строки
          (это "родительский" заголовок для объединённых ячеек).
        - Если обе ячейки непустые — конкатенируем через пробел.
        - Если только верхняя непустая — берём её (обновляя родительский).
        - Если только нижняя непустая — добавляем к родительскому префиксу.
        
        Пример:
            Верх:  [Основное средство, Стоимость, NaN, NaN, Амортизация, NaN]
            Низ:   [NaN, первоначальная, на начало, изменение, на начало, за период]
            Итог:  [Основное средство, Стоимость первоначальная, Стоимость на начало,
                    Стоимость изменение, Амортизация на начало, Амортизация за период]
        """
        combined = []
        current_parent = ""
        
        for top_val, bot_val in zip(header_top, header_bot):
            top_str = str(top_val).strip() if pd.notna(top_val) else ""
            bot_str = str(bot_val).strip() if pd.notna(bot_val) else ""
            
            # Обновляем родительский заголовок, если верхняя ячейка не пустая
            if top_str:
                current_parent = top_str
            
            if top_str and bot_str:
                # Обе ячейки заполнены — конкатенируем
                combined.append(f"{top_str} {bot_str}")
            elif top_str:
                # Только верхняя (родительский заголовок без подзаголовка)
                combined.append(top_str)
            elif bot_str:
                # Только нижняя — добавляем родительский префикс
                combined.append(f"{current_parent} {bot_str}")
            else:
                # Обе пустые — пропускаем
                combined.append("")
        
        return combined
    
    
    def _remove_total_row(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Удаляет итоговую строку по ключевому слову "Итог" в первом столбце.
        
        Ищет подстроку "итог" регистронезависимо, чтобы поймать варианты:
        "Итог", "Итого", "Итого:" и т.д.
        """
        first_col = df.columns[0]
        
        # Маска: в первом столбце есть "итог" (регистронезависимо)
        mask_total = (
            df[first_col]
            .astype(str)
            .str.lower()
            .str.contains('итог', na=False)
        )
        
        if mask_total.any():
            removed_count = mask_total.sum()
            logger.debug(
                f"Удалено {removed_count} итоговых строк из ведомости амортизации"
            )
            df = df[~mask_total].copy()
        
        return df
    
    def _prepare_depreciation_data(self, depreciation_df: pd.DataFrame) -> pd.DataFrame:
        """
        Подготавливает данные ведомости амортизации:
        unpivot, агрегация, приведение типов.
        """
        df = depreciation_df.copy()
        
        # Переименование для melt
        df['01.03'] = df['Стоимость на конец периода']
        df['02.03'] = df['Амортизация на конец периода'] * -1
        
        # Unpivot — явно указываем id_vars
        id_vars = [col for col in df.columns if col not in self.ACCOUNTS_01_03]
        df = df.melt(
            id_vars=id_vars,
            value_vars=self.ACCOUNTS_01_03,
            var_name='счет',
            value_name='сальдо, тыс.ед.'
        )
        
        # Оставляем только нужные столбцы
        df = df.loc[:, ['счет', 'вид_связи', 'Группа учета ОС', 'сальдо, тыс.ед.']]
        
        # Агрегация
        df = (
            df.groupby(['счет', 'вид_связи', 'Группа учета ОС'], as_index=False, observed=True)['сальдо, тыс.ед.']
            .sum()
        )
        
        # Фильтрация нулевых и перевод в тыс.ед.
        df = df[df['сальдо, тыс.ед.'] != 0]
        df['сальдо, тыс.ед.'] = df['сальдо, тыс.ед.'] / 1000
        
        # Переименование
        df = df.rename(columns={'Группа учета ОС': 'допсубконто'})
        
        # ★ Явное приведение типов для всех строковых столбцов
        string_cols = ['счет', 'вид_связи', 'допсубконто']
        for col in string_cols:
            df[col] = df[col].astype('string')
        
        # Субконто = допсубконто (копия)
        df['субконто'] = df['допсубконто']
        
        return df
    
    def _align_columns(
        self,
        depreciation_df: pd.DataFrame,
        reference_df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Выравнивает столбцы depreciation_df с reference_df,
        добавляя недостающие с правильными типами.
        """
        df = depreciation_df.copy()
        missing_cols = set(reference_df.columns) - set(df.columns)
        
        for col in missing_cols:
            ref_dtype = reference_df[col].dtype
            
            if pd.api.types.is_string_dtype(ref_dtype):
                df[col] = pd.Series(
                    [self.UNSPECIFIED] * len(df),
                    dtype='string',
                    index=df.index
                )
            else:
                # Для числовых и других типов
                df[col] = self.UNSPECIFIED
        
        return df
    
    def _restore_dtypes(
        self,
        df: pd.DataFrame,
        original_dtypes: dict
    ) -> pd.DataFrame:
        """
        Восстанавливает типы данных после concat.
        """
        for col, dtype in original_dtypes.items():
            if col not in df.columns:
                continue
            
            if pd.api.types.is_string_dtype(dtype):
                df[col] = df[col].astype('string')
            elif pd.api.types.is_numeric_dtype(dtype):
                df[col] = df[col].astype(dtype)
        
        return df
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки шага 10."""
        logger.debug("Классификация источника аренды")
        
        osv_all_df = context.main_df.copy()
        
        name_company = context.get_metadata('company_name')
        period = context.get_metadata('period')
        
        # 1. Загрузка ведомости амортизации
        depreciation_df = self._process_depreciation_statement_decoding(
            context=context,  # ★ Передаём контекст для кэширования
            type_register='ведамор',
            name_company=name_company,
            period=period
        )
        
        if depreciation_df is None:
            logger.debug("Ведомость амортизации не найдена, шаг пропущен")
            return context
        
        # 2. Подготовка данных ведомости
        depreciation_df = self._prepare_depreciation_data(depreciation_df)
        
        # Проверка ВХОДНОЙ сходимости (ДО расшифровки)
        self._validate_input_convergence(osv_all_df, depreciation_df)
        
        # 3. Выравнивание столбцов с osv_all_df
        depreciation_df = self._align_columns(depreciation_df, osv_all_df)
        
        # 4. Сохраняем исходные типы osv_all_df для восстановления после concat
        original_dtypes = osv_all_df.dtypes.to_dict()
        
        # 5. ★ Вычисляем фильтр ОДИН раз
        mask_accounts = depreciation_df['счет'].isin(self.ACCOUNTS_01_03)
        to_add = depreciation_df[mask_accounts].copy()
        
        # Сумма до объединения
        sum_before = to_add['сальдо, тыс.ед.'].sum()
        
        # 6. Замена строк с счетами 01.03 и 02.03
        osv_all_df = osv_all_df[~osv_all_df['счет'].isin(self.ACCOUNTS_01_03)].copy()
        osv_all_df = pd.concat([osv_all_df, to_add], ignore_index=True)
        
        # 7. Восстанавливаем ВСЕ типы
        osv_all_df = self._restore_dtypes(osv_all_df, original_dtypes)
        
        # 8. Проверка сходимости сумм
        sum_after = osv_all_df[osv_all_df['счет'].isin(self.ACCOUNTS_01_03)]['сальдо, тыс.ед.'].sum()
        diff = abs(sum_before - sum_after)
        
        if diff > self.CONVERGENCE_TOLERANCE:
            # ★ Формируем problem_data — информацию о расхождении
            problem_data = pd.DataFrame({
                'показатель': ['Сумма до объединения', 'Сумма после объединения', 'Разница', 'Допуск'],
                'значение': [sum_before, sum_after, diff, self.CONVERGENCE_TOLERANCE],
                'единица_измерения': ['тыс.ед.', 'тыс.ед.', 'тыс.ед.', 'тыс.ед.'],
            })
            
            # ★ Выбрасываем ConvergenceError
            # Базовый класс сам сохранит в Excel и залогорирует
            raise ConvergenceError(
                message=(
                    f"Сумма остаточной стоимости ОС на счетах 01.03/02.03 "
                    f"не совпадает с ведомостью амортизации"
                ),
                problem_data=problem_data,
                reference_name="Ведомость амортизации vs ОСВ",
                difference=diff,
                tolerance=self.CONVERGENCE_TOLERANCE,
                sum_before=sum_before,
                sum_after=sum_after,
            )
        
        logger.debug(f"Классификация источника аренды завершена. Добавлено {len(to_add)} строк.")
        
        context.main_df = osv_all_df
        return context

