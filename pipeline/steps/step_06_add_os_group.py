"""
Шаг 6: Добавление группы ОС для аренды/лизинга.
"""
import numpy as np
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.errors import (                          # ← НОВОЕ
    MissingOSGroupError,
    ReferenceMismatchError,
    ConvergenceError,
)
from io_module import DataLoader
from utils import find_register_file, find_target_column
from config.settings import ACCOUNTS_OSV_DIR, CONTRACTOR_KEYWORDS, CALC_TYPE_KEYWORDS

class Step6AddOSGroupColumnStep(Step):
    """
    Шаг 6: Группы ОС для аренды/лизинга.
    
    Для счетов 01 и 02 определяет группу основных средств
    (здания, машины, оборудование и т.д.) на основе справочника.
    """
    
    # Константы
    UNSPECIFIED = 'не_указано'
    ACCOUNT_97_21 = '97.21'
    ACCOUNT_76_07_PREFIX = '76.07'
    ACCOUNT_76_05_3_PREFIX = '76.05.3'
    SUBTYPE_RENT = 'Аренда'
    SUBTYPE_LEASING = 'Лизинг'
    
    # Допуск для сверки ОСВ 76.07 (в единицах, т.к. check_df считается до перевода в тысячи)
    TOLERANCE_76 = 100_000  # 100 тыс. руб в единицах
    
    def __init__(self):
        super().__init__(
            name="Шаг 6: Добавление группы ОС для 76.07/76.05.3/97",
            description="Классификация ОС по группам на счетах учета аренды"
        )
    
    def _load_and_process_76_lease(self, name_company: str) -> pd.DataFrame:
        """Загружает и обрабатывает ОСВ по счету 76.07/76.05.3 с проверкой сходимости."""
        logger.debug(f"Загрузка и обработка ОСВ 76.07/76.05.3 для {name_company}")
        
        df, check_df = DataLoader.load_account_osv76_lease()
        
        # 2. Проверка сходимости ОСВ 76 аренда
        if not check_df.empty and 'Сальдо_конец_разница' in check_df.columns:
            sum_diff = check_df['Сальдо_конец_разница'].sum()
            if abs(sum_diff) > self.TOLERANCE_76:
                raise ConvergenceError(
                    message=(
                        f'Остатки по ОСВ 76.07/76.05.3 отличаются от исходной на {sum_diff:.2f} руб.'
                    ),
                    problem_data=check_df,  # ← Сохраняем всю контрольную таблицу
                    reference_name="ОСВ 76.07/76.05.3",
                    difference=sum_diff,
                    tolerance=self.TOLERANCE_76,
                )
        
        # 3. Удаление ненужных столбцов
        cols_to_drop = [
            'Дебет_начало', 'Кредит_начало', 'Дебет_оборот', 'Кредит_оборот',
            'Начало периода для вида связи', 'Конец периода для вида связи',
            'Вид связи КА за период'
        ]
        df = df.drop(columns=cols_to_drop, errors='ignore')
        
        # 4. Расчет сальдо в тыс. ед.
        df['сальдо, тыс.ед.'] = (
            df['Дебет_конец'].sub(df['Кредит_конец'], fill_value=0)
            .div(1_000).round(2)
        )
        
        # 5. Фильтрация нулевых сальдо и очистка
        df = df[df['сальдо, тыс.ед.'] != 0].copy()
        df = df.drop(columns=['Дебет_конец', 'Кредит_конец'], errors='ignore')
        df = df.rename(columns={'Субконто': 'вид взаиморасчетов'})
        
        # 6. Извлечение данных из Level_-столбцов с валидацией
        contract_col = find_target_column(df, 'Level_', 'rightmost', 'all_accounts', 1)
        account_col = find_target_column(df, 'Level_', 'rightmost', 'all_accounts', 0)
        contractor_col = find_target_column(df, 'Level_', 'rightmost', 'all_accounts', 2)
        # calc_type_col = find_target_column(df, 'Level_', 'rightmost', 'all_accounts', 3)
        
        # Проверяем, что все столбцы найдены
        missing_cols = {
            'счет (account_col)': account_col,
            'контрагент (contractor_col)': contractor_col,
            'договор (contract_col)': contract_col,
        }
        missing = [name for name, col in missing_cols.items() if col is None]
        if missing:
            raise ValueError(
                f"Не удалось найти необходимые столбцы в ОСВ 76.07: {missing}. "
                f"Проверьте структуру выгрузки из 1С."
            )
        
        df['счет'] = df[account_col].astype('string')
        df['контрагент'] = df[contractor_col].astype('string')
        df['договор'] = df[contract_col].astype('string')
        df['вид взаиморасчетов'] = df['вид взаиморасчетов'].astype('string')
        
        # валидация содержимого найденных столбцов
        self.validate_extracted_column(
            df=df,
            column_name='контрагент',
            keywords=CONTRACTOR_KEYWORDS,
            match_threshold=0.30,
            column_purpose="контрагентов"
        )
        
        self.validate_extracted_column(
            df=df,
            column_name='вид взаиморасчетов',
            keywords=CALC_TYPE_KEYWORDS,
            match_threshold=0.15,
            unique_threshold=30,
            column_purpose="видов расчётов"
        )
        
        # 7. Удаление Level_-столбцов и фильтрация
        df = df.loc[:, ~df.columns.str.startswith('Level_')]
        df = df[~df['счет'].str.startswith('76.07.2', na=False)]
        
        # Удаление лишних пробелов
        df = self.clean_whitespace(df)
        
        logger.debug(f"Обработана ОСВ 76 аренда: {len(df)} строк после фильтрации")
        
        return df
    
    def _merge_with_76_lease_detail(self, osv_all_df: pd.DataFrame, osv_76_lease_df: pd.DataFrame) -> pd.DataFrame:
        """Объединяет основную ОСВ с детализацией по 76 аренда с проверкой сходимости."""
        logger.debug("Объединение ОСВ с детализацией 76.07")
        
        # Валидация входных данных
        required_cols_main = ['счет', 'субконто', 'допсубконто', 'сальдо, тыс.ед.']
        required_cols_detail = ['счет', 'вид взаиморасчетов', 'контрагент', 'сальдо, тыс.ед.', 'договор']
        
        missing_main = [col for col in required_cols_main if col not in osv_all_df.columns]
        missing_detail = [col for col in required_cols_detail if col not in osv_76_lease_df.columns]
        
        if missing_main or missing_detail:
            raise ValueError(
                f"Отсутствуют необходимые столбцы:\n"
                f"  В основной ОСВ: {missing_main}\n"
                f"  В детализации 76.07: {missing_detail}"
            )
        
        # 1. Разделение на 76 аренда и остальные
        mask_76 = osv_all_df['счет'].astype(str).str.startswith((self.ACCOUNT_76_07_PREFIX, self.ACCOUNT_76_05_3_PREFIX))
        df_76 = osv_all_df[mask_76].copy()
        df_other = osv_all_df[~mask_76]
        
        if df_76.empty:
            logger.warning("В основной ОСВ нет счетов 76.07")
            return osv_all_df
        
        # 2. Подготовка детализации
        detail = osv_76_lease_df.rename(columns={'сальдо, тыс.ед.': 'сальдо_деталь'})
        
        # 3. Merge по ключам
        left_keys = ['счет', 'субконто', 'допсубконто']
        right_keys = ['счет', 'вид взаиморасчетов', 'контрагент']
        
        merged = df_76.merge(detail, left_on=left_keys, right_on=right_keys, how='left')
        
        # 4. Проверка сходимости сумм
        matched = merged[merged['договор'].notna()]
        if not matched.empty:
            check = matched.groupby(left_keys, observed=True).agg(
                orig=('сальдо, тыс.ед.', 'first'),
                detail_sum=('сальдо_деталь', 'sum')
            ).reset_index()
            
            is_mismatch = ~np.isclose(check['orig'], check['detail_sum'], rtol=1e-4, atol=0.1)
            
            if is_mismatch.any():
                mismatches = check[is_mismatch].copy()
                mismatches['разница'] = (mismatches['orig'] - mismatches['detail_sum']).round(3)
                
                raise ConvergenceError(
                    message=(
                        f"Расхождение сумм при расшифровке 76.07. "
                        f"Найдено {len(mismatches)} несовпадений."
                    ),
                    problem_data=mismatches,
                    reference_name="ОСВ 76.07/76.05.3 (детализация)",
                    mismatches_count=len(mismatches),
                )
        
        # 5. Формирование результата
        has_detail = merged[merged['договор'].notna()].copy()
        has_detail['сальдо, тыс.ед.'] = has_detail['сальдо_деталь']
        
        no_detail = merged[merged['договор'].isna()]
        
        result = pd.concat([df_other, no_detail, has_detail], ignore_index=True)
        
        # 6. Очистка служебных столбцов
        service_cols = ['вид взаиморасчетов', 'контрагент', 'сальдо_деталь']
        result = result.drop(columns=service_cols, errors='ignore')
        
        # 7. Заполнение пропусков в договоре (используем константу!)
        result['договор'] = result['договор'].fillna(self.UNSPECIFIED).astype('string')
        
        # ★ ПРОВЕРКА: есть ли хоть один договор?
        if (result['договор'] == self.UNSPECIFIED).all():
            # Формируем problem_data для анализа
            problem_data = result[[
                'счет', 'субконто', 'допсубконто', 
                'договор', 'сальдо, тыс.ед.'
            ]].copy()
            
            logger.error(
                f"Критическая ошибка: ни один договор из ОСВ по ОСВ 76.07/76.05.3 "
                f"не удалось замапить на сводную ОСВ.\n"
                f"Всего строк ОСВ 76.07/76.05.3: {len(result)}\n"
                f"Пример первых 5 строк:\n{problem_data.head().to_string(index=False)}"
            )
            
            raise ValueError(
                f"Ни один договор из ОСВ по ОСВ 76.07/76.05.3 не включён в сводную ОСВ. "
                f"Возможные причины:\n"
                f"  1. Неверная иерархия в ОСВ ОСВ 76.07/76.05.3 "
                f"(ожидается: счет → договор → КА → вид расчётов)\n"
                f"  2. Контрагенты или виды взаиморасчётов в ОСВ ОСВ 76.07/76.05.3 "
                f"не совпадают со сводной ОСВ\n"
                f"Проверьте структуру выгрузки ОСВ ОСВ 76.07/76.05.3 из 1С."
            )
        
        logger.debug(
            f"Замапплено договоров: "
            f"{(result['договор'] != self.UNSPECIFIED).sum()} из {len(result)}"
        )
        
        # 8. Восстановление порядка столбцов
        cols_orig = list(osv_all_df.columns)
        if 'допсубконто' in cols_orig:
            idx_dop = cols_orig.index('допсубконто') + 1
            cols_orig.insert(idx_dop, 'договор')
        
        final_cols = [col for col in cols_orig if col in result.columns]
        result = result[final_cols]
        
        logger.debug(f"Объединение завершено: {len(result)} строк")
        
        
        
        return result
    
    def _validate_mapping(
                        self, 
                        values: pd.Series, 
                        mapping: dict, 
                        value_type: str, 
                        mapping_name: str = 'справочнике',
                        df: pd.DataFrame = None,
                        column_name: str = None
                    ) -> None:
        """
        Проверяет наличие всех значений в справочнике.
        
        При несоответствии выбрасывает MissingOSGroupError с problem_data.
        Базовый класс Step сам сохранит проблемные данные в Excel.
        """
        valid_values = values.dropna()
        valid_values = valid_values[valid_values != self.UNSPECIFIED]
        
        if valid_values.empty:
            return
        
        unique_values = set(valid_values.unique())
        mapping_keys = set(mapping.keys())
        missing = unique_values - mapping_keys
        
        if not missing:
            return
        
        # ★ Формируем problem_data для сохранения
        if df is not None and column_name is not None and not df.empty:
            missing_mask = df[column_name].isin(missing)
            problem_data = df.loc[missing_mask].copy()
        else:
            # Если DataFrame не передан — создаём простой DataFrame со списком missing
            problem_data = pd.DataFrame({
                'отсутствующее_значение': sorted(missing),
                'тип': value_type,
                'справочник': mapping_name,
            })
        
        # ★ Выбрасываем MissingOSGroupError
        # Базовый класс сам сохранит в Excel и залогорирует
        raise MissingOSGroupError(
            message=(
                f"В ОСВ найдены {value_type}, отсутствующие в {mapping_name}"
            ),
            problem_data=problem_data,
            reference_name=mapping_name,
            missing_values=sorted(missing),
            value_type=value_type,
        )
    
    def _create_mapping(self, df: pd.DataFrame, key_col: str, value_col: str) -> dict:
        """
        Создает маппинг из DataFrame, исключая NaN в ключе.
        """
        return (
            df.dropna(subset=[key_col])
            .drop_duplicates(key_col)
            .set_index(key_col)[value_col]
            .to_dict()
        )
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Добавление группы ОС")
        osv_all_df = context.main_df.copy()
        name_company = context.get_metadata('company_name')
        
        # 1. Загрузка и обработка детализации по 76 аренда
        logger.debug("Этап 1: Загрузка ОСВ 76.07/76.05.3 по договорам аренды/лизинга")
        osv_76_lease_df = self._load_and_process_76_lease(name_company)
        osv_all_df = self._merge_with_76_lease_detail(osv_all_df, osv_76_lease_df)
        logger.debug(f"После merge с 76.07: {len(osv_all_df)} строк")

        # 2. Загрузка и фильтрация справочника ППА
        logger.debug("Этап 2: Загрузка справочника ППА")
        lease_df = DataLoader.load_reference_data(
            sheet_name='ППА',
            strings=['группа_ос', 'вид_взаиморасчетов',
                    'наименование_компании', 'рбп', 'ос_ппа',
                    'ос_после_перехода_в_собственность', 'договор_аренды', 'контрагент'],
        )
        lease_df = lease_df[lease_df['наименование_компании'] == name_company]
        if lease_df.empty:
            # ★ Формируем problem_data — список всех компаний в справочнике ППА
            # чтобы бухгалтер видел, какие компании есть, и мог понять, в чём проблема
            all_companies_df = DataLoader.load_reference_data(
                sheet_name='ППА',
                strings=['наименование_компании']
            )
            problem_data = (
                all_companies_df[['наименование_компании']]
                .drop_duplicates()
                .rename(columns={'наименование_компании': 'компания_в_справочнике'})
            )
            
            raise ReferenceMismatchError(
                message=f"Компания '{name_company}' не найдена в справочнике ППА",
                problem_data=problem_data,
                reference_name="ППА",
                searched_company=name_company,
            )
        logger.debug(f"Справочник ППА для {name_company}: {len(lease_df)} строк")
        
        # Очистка пробелов
        lease_df = self.clean_whitespace(lease_df)
        osv_all_df = self.clean_whitespace(osv_all_df)

        # 3. Создание маппингов
        logger.debug("Этап 3: Создание меппингов")
        os_group_by_contract = self._create_mapping(lease_df, 'договор_аренды', 'группа_ос')
        os_group_by_rbp = self._create_mapping(lease_df, 'рбп', 'группа_ос')
        contract_by_rbp = self._create_mapping(lease_df, 'рбп', 'договор_аренды')
        
        logger.debug(f"Меппинги созданы: {len(os_group_by_contract)} договоров, {len(os_group_by_rbp)} РБП")

        # 4. Проставление групп ОС по договору (ОСВ 76.07/76.05.3)
        logger.debug("Этап 4: Классификация по договорам (ОСВ 76.07/76.05.3)")
        contracts = osv_all_df.loc[osv_all_df['договор'] != self.UNSPECIFIED, 'договор'].unique()
        self._validate_mapping(
                                values=pd.Series(contracts),
                                mapping=os_group_by_contract,
                                value_type="договоры",
                                mapping_name="справочнике ППА",
                                df=osv_all_df,
                                column_name='договор'
                            )

        osv_all_df['группа_ос_аренды_лизинга'] = osv_all_df['договор'].map(os_group_by_contract)
        logger.debug(f"Группы ОС по договорам: {osv_all_df['группа_ос_аренды_лизинга'].notna().sum()} заполнено")

        # 5. Проставление групп ОС по РБП (97.21)
        logger.debug("Этап 5: Классификация по РБП (97.21)")
        mask_97 = (
            osv_all_df['подвид_задолженности'].isin([self.SUBTYPE_RENT, self.SUBTYPE_LEASING])
            & (osv_all_df['счет'] == self.ACCOUNT_97_21)
        )

        rbps = osv_all_df.loc[mask_97, 'допсубконто'].unique()
        rbps = [r for r in rbps if r != self.UNSPECIFIED]
        self._validate_mapping(
                                values=pd.Series(rbps),
                                mapping=os_group_by_rbp,
                                value_type="РБП",
                                mapping_name="справочнике ППА",
                                df=osv_all_df,
                                column_name='допсубконто'
                            )

        os_groups_97 = osv_all_df['допсубконто'].map(os_group_by_rbp)

        # Замена всех NaN на 'не_указано'
        osv_all_df['группа_ос_аренды_лизинга'] = (
            osv_all_df['группа_ос_аренды_лизинга']
            .fillna(os_groups_97)
            .fillna(self.UNSPECIFIED)
        )
        
        # Дополнительная проверка на NaN после fillna
        nan_count = osv_all_df['группа_ос_аренды_лизинга'].isna().sum()
        if nan_count > 0:
            logger.warning(f"Обнаружено {nan_count} NaN в 'группа_ос_аренды_лизинга', заменяем на '{self.UNSPECIFIED}'")
            osv_all_df['группа_ос_аренды_лизинга'] = osv_all_df['группа_ос_аренды_лизинга'].fillna(self.UNSPECIFIED)
        
        logger.debug(f"Итого групп ОС заполнено: {(osv_all_df['группа_ос_аренды_лизинга'] != self.UNSPECIFIED).sum()}")
        logger.debug(f"Строк с '{self.UNSPECIFIED}': {(osv_all_df['группа_ос_аренды_лизинга'] == self.UNSPECIFIED).sum()}")

        # 6. Проставление договоров по РБП для 97.21
        logger.debug("Этап 6: Заполнение договоров для РБП")
        contracts_97 = osv_all_df['допсубконто'].map(contract_by_rbp)
        
        mask_fill_contract = (
            mask_97
            & contracts_97.notna()
            & (osv_all_df['договор'].isna() | (osv_all_df['договор'] == self.UNSPECIFIED))
        )

        osv_all_df.loc[mask_fill_contract, 'договор'] = contracts_97[mask_fill_contract]
        logger.debug(f"Договоры заполнены для {mask_fill_contract.sum()} строк")

        # 7. Преобразование в строковый тип
        logger.debug("Этап 7: Преобразование в строковый тип")
        unique_groups = list(lease_df['группа_ос'].unique())
        
        # Проверяем валидность значений
        invalid_values = osv_all_df['группа_ос_аренды_лизинга'][
            ~osv_all_df['группа_ос_аренды_лизинга'].isin(unique_groups)
        ].unique()
        
        if len(invalid_values) > 0:
            logger.warning(f"Найдены значения вне разрешённого списка: {invalid_values}. Заменяем на '{self.UNSPECIFIED}'")
            osv_all_df.loc[
                osv_all_df['группа_ос_аренды_лизинга'].isin(invalid_values),
                'группа_ос_аренды_лизинга'
            ] = self.UNSPECIFIED
        
        osv_all_df['группа_ос_аренды_лизинга'] = osv_all_df['группа_ос_аренды_лизинга'].astype('string')
        
        # Финальная проверка: убеждаемся, что нет пустых значений
        final_nan_count = osv_all_df['группа_ос_аренды_лизинга'].isna().sum()
        if final_nan_count > 0:
            logger.error(f"КРИТИЧЕСКАЯ ОШИБКА: После всех обработок осталось {final_nan_count} NaN!")
            raise ValueError(f"Столбец 'группа_ос_аренды_лизинга' содержит {final_nan_count} пустых значений")
        
        context.main_df = osv_all_df
        logger.debug(f"Шаг 6 завершен: {len(osv_all_df)} строк, {len(unique_groups)} групп ОС")
        
        return context