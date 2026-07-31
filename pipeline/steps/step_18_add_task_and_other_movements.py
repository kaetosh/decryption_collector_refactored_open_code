"""
Шаг 18: Добавление налога на прибыль и прочих движений за счет чистой прибыли в расшифровку ОПУ (счет 99)
Обработка:
- Загрузка отчета по проводкам 99 счета
- Очистка от проводок, связанных с реформацией баланса
- Поиск оборотов с 68 счетом для суммы налога на прибыль
- Поиск прочих оборотов
"""
import pandas as pd
from loguru import logger
from pipeline.base import Step, ProcessingContext
from io_module import DataLoader


class Step18AddTaskAndOtherMovementsStep(Step):
    """
    Шаг 18: Обработка налога на прибыль и прочих движений за счет чистой прибыли (счет 99).
    Добавляет в расшифровку ОПУ:
     - Сумму налога на прибыль (корреспонденция 99 и 68)
     - Прочие движения одной суммой
    """
    # Счета для обработки
    ACCOUNT_GAINS_AND_LOSSES = '99'
    ACCOUNT_TAX = '68'  # Переименовано с ACCOUNT_TASK для ясности
    ACCOUNTS_FOR_BALANCE_REFORMATION = ['90', '91', '84', '99']

    def __init__(self):
        super().__init__(
            name="Шаг 18: Налог на прибыль и прочие движения (99.02/99.09)",
            description="Добавление налога на прибыль и прочих движений за счет чистой прибыли"
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Начало обработки налога на прибыль и прочих движений (счет 99)")
        name_company = context.get_metadata('company_name')

        # Загружаем только проводки (ОСВ в этом шаге не используется)
        transactions_all_df = self._load_data_from_context(context)
        
        # Загружаем только нужный справочник
        companies_df = self._load_companies_reference(name_company)

        # Фильтруем проводки по 99 счету
        mask_99 = transactions_all_df['Имя_файла'].str.contains(f"_{self.ACCOUNT_GAINS_AND_LOSSES}_", na=False)
        df99 = transactions_all_df.loc[mask_99].copy()

        # Оставляем только синтетические счета (первые 2 символа)
        df99['Дт'] = df99['Дт'].str[:2]
        df99['Кт'] = df99['Кт'].str[:2]

        # Исключаем обороты, связанные с закрытием периода/реформацией баланса
        mask_reform = (
            ((df99['Дт'] == self.ACCOUNT_GAINS_AND_LOSSES) & df99['Кт'].isin(self.ACCOUNTS_FOR_BALANCE_REFORMATION)) |
            ((df99['Кт'] == self.ACCOUNT_GAINS_AND_LOSSES) & df99['Дт'].isin(self.ACCOUNTS_FOR_BALANCE_REFORMATION))
        )
        df99 = df99.loc[~mask_reform]

        # Рассчитываем обороты: доходы (кредит 99) со знаком минус, расходы с плюсом. Переводим в тыс.ед.
        df99['оборот, тыс.ед.'] = df99['Сумма'] / 1000
        mask_credit = df99['Кт'] == self.ACCOUNT_GAINS_AND_LOSSES
        df99.loc[mask_credit, 'оборот, тыс.ед.'] *= -1

        # Выделяем сумму налога на прибыль (корреспонденция 99 и 68)
        mask_tax = (
            ((df99['Дт'] == self.ACCOUNT_TAX) & (df99['Кт'] == self.ACCOUNT_GAINS_AND_LOSSES)) |
            ((df99['Дт'] == self.ACCOUNT_GAINS_AND_LOSSES) & (df99['Кт'] == self.ACCOUNT_TAX))
        )
        
        tax_profit = df99.loc[mask_tax, 'оборот, тыс.ед.'].sum()
        other_value = df99.loc[~mask_tax, 'оборот, тыс.ед.'].sum()

        # Получаем сегмент компании
        segment_company = companies_df.loc[
            companies_df['сокращенное_наименование_компании'] == name_company, 'сегмент'
        ].iloc[0]

        # Формируем новые строки (избавляемся от дублирования кода в if/elif/else)
        new_rows_data = []
        if tax_profit != 0:
            new_rows_data.append({
                'счет': '99.02', 'оборот, тыс.ед.': tax_profit,
                'доход_расход': 'Налог на прибыль', 'вид_дохода_расхода': 'Налог на прибыль'
            })
        if other_value != 0:
            new_rows_data.append({
                'счет': '99.09', 'оборот, тыс.ед.': other_value,
                'доход_расход': 'Прочее', 'вид_дохода_расхода': 'Прочее' # Исправлена опечатка оригинала
            })

        if new_rows_data:
            new_rows = pd.DataFrame(new_rows_data)
            
            # Заполняем стандартные значения для всех строк
            default_cols = ['контрагент', 'ном_группа', 'группа_ка', 'сегмент_ка', 
                            'вид_связи', 'объект для изм ппа', 'рбп_кредитные_линии']
            for col in default_cols:
                new_rows[col] = 'не_указано'
            new_rows['сегмент'] = segment_company

            # Добавляем в основной DataFrame
            main_df = pd.concat([context.main_df, new_rows], ignore_index=True)

            # Приводим типы и заполняем пропуски
            str_cols = ['контрагент', 'ном_группа', 'счет', 'доход_расход', 'вид_дохода_расхода',
                        'сегмент', 'группа_ка', 'сегмент_ка', 'вид_связи', 'объект для изм ппа', 'рбп_кредитные_линии']
            
            main_df[str_cols] = main_df[str_cols].astype('string').fillna('не_указано')
            main_df['счет'] = main_df['счет'].str[:5]

            context.main_df = main_df

        logger.info(f"✓ Добавлено: Налог на прибыль = {tax_profit:,.0f} тыс.ед., Прочее = {other_value:,.0f} тыс.ед.")
        return context

    # =========================================================================
    # ЗАГРУЗКА ДАННЫХ
    # =========================================================================
    def _load_companies_reference(self, name_company: str) -> pd.DataFrame:
        """Загружает справочник компаний (единственный нужный для этого шага)."""
        logger.debug("Загрузка справочника компаний")
        companies_df = DataLoader.load_reference_data(
            sheet_name='КомпанииГруппы',
            strings=['вид_продукции_переоценки', 'сокращенное_наименование_компании', 'сегмент']
        )
        return self.clean_whitespace(companies_df)

    def _load_data_from_context(self, context: ProcessingContext) -> pd.DataFrame:
        """Загружает проводки из контекста."""
        transactions_all_df = context.data.get('transactions_all_df', pd.DataFrame())
        if transactions_all_df.empty:
            raise ValueError(
                "В контексте нет сводного отчета по проводкам. "
                "Убедитесь, что предыдущий шаг (14) выполнен успешно."
            )
        logger.debug(f"Загружено из контекста: проводки={len(transactions_all_df)} строк")
        return transactions_all_df




# # ПРИВЕТ, МИР!!!*-
# """
# Created on Fri Jul 31 16:10:45 2026

# @author: a.karabedyan
# """

# """
# Шаг 17: Добавление налога на прибыль и прочих движений за счет чистой прибыли в расшифровку ОПУ (счет 99)

# Обработка:
# - Загрузка отчета по проводкам 99 счета
# - Очистка от проводок, связанных с реформацией баланса
# - Поиск оборотов с 68 счетом для суммы налога на прибыль
# - Поиск прочих оборотов
# """
# import numpy as np
# import pandas as pd
# from loguru import logger
# from pipeline.base import Step, ProcessingContext
# from pipeline.errors import MissingMappingError
# from io_module import DataLoader
# from config.settings import REFERENCE_CONFIGS


# class Step18AddTaskAndOtherMovementsStep(Step):
#     """
#     Шаг 18: Обработка налога на прибыль и прочих движений за счет чистой прибыли (счет 99).

#     Добавляет в расшифровку ОПУ:
#     - Сумму налога прибыль (корреспонденция 99 и 68) суммой
#     - Прочие движения одной суммой
#     """

#     # Счета для обработки
#     ACCOUNT_GAINS_AND_LOSSES = '99'
#     ACCOUNT_TASK = '68'
#     ACCOUNTS_FOR_BALANCE_REFORMATION = ['90', '91', '84', '99']

#     # Допуск для проверки сходимости с ОСВ (в тыс.ед.)
#     TOLERANCE_OSV = 1000

#     def __init__(self):
#         super().__init__(
#             name="Шаг 17: Прочие доходы и расходы (91.01/91.02)",
#             description="Добавление прочих доходов и расходов с детализацией по контрагентам и видам связи"
#         )

#     def _process(self, context: ProcessingContext) -> ProcessingContext:
#         logger.debug("Начало обработки прочих доходов и расходов")
#         name_company = context.get_metadata('company_name')
        
#         # Данные из контекста (ОСВ, проводки)
#         osv_df, transactions_all_df = self._load_data_from_context(context)
        
#         # справочники одним вызовом
#         refs = self._load_references(name_company)
        
#         # загружаем отчет по проводкам по 99 счету
#         mask = transactions_all_df['Имя_файла'].str.contains(f"_{self.ACCOUNT_GAINS_AND_LOSSES}_", na=False)
#         df99 = transactions_all_df.loc[mask].copy()
        
#         # уберем субсчета, оставив только синтетические счета
#         df99.loc[:, 'Дт'] = df99.loc[:, 'Дт'].str[:2]
#         df99.loc[:, 'Кт'] = df99.loc[:, 'Кт'].str[:2]
        
#         # уберм обороты связанные с закрытием периода/реформацией баланса
#         mask_a = (df99['Дт'] == self.ACCOUNT_GAINS_AND_LOSSES) & (df99['Кт'].isin(self.ACCOUNTS_FOR_BALANCE_REFORMATION))
#         mask_b = (df99['Кт'] == self.ACCOUNT_GAINS_AND_LOSSES) & (df99['Дт'].isin(self.ACCOUNTS_FOR_BALANCE_REFORMATION))
#         df99 = df99.loc[~(mask_a | mask_b)]
        
#         # доходы (кредит по 99 счету) сделаем с минусом, расходы знак не меняем, он с плюсом, переведем в тысячи.
#         mask_c = (df99['Кт']==self.ACCOUNT_GAINS_AND_LOSSES)
#         df99['оборот, тыс.ед.'] = df99.loc[:, 'Сумма']/1000
#         df99.loc[mask_c, 'оборот, тыс.ед.'] = -df99.loc[mask_c, 'оборот, тыс.ед.']
        
#         # получим сумму налога на прибыль для блока "Налог на прибыль"
#         mask_d = ((df99['Дт'] == self.ACCOUNT_TASK) & (df99['Кт'] == self.ACCOUNT_GAINS_AND_LOSSES)) | ((df99['Дт'] == self.ACCOUNT_GAINS_AND_LOSSES) & (df99['Кт'] == self.ACCOUNT_TASK))
#         main_task = df99.loc[mask_d, 'оборот, тыс.ед.'].sum()
        
#         # получим сумму прочих движений за чет чистой прибыли
#         other_value = df99.loc[~mask_d, 'оборот, тыс.ед.'].sum()
        
#         #сегмент компании
#         segment_company = refs['companies'].loc[
#             refs['companies']['сокращенное_наименование_компании'] == name_company,
#             'сегмент'
#         ].iloc[0]
        
#         # добавим в основной df строки с налогом на прибыль и прочие движения
#         main_df = context.main_df.copy()
#         if main_task !=0 and other_value !=0:
#             new_rows = pd.DataFrame({
#                 'контрагент': ['не_указано', 'не_указано'],
#                 'ном_группа': ['не_указано', 'не_указано'],
#                 'счет': ['99.02', '99.09'],
#                 'оборот, тыс.ед.': [main_task, other_value],
#                 'доход_расход': ['Налог на прибыль', 'Прочее'],
#                 'вид_дохода_расхода': ['Налог на прибыль', 'Налог на прибыль'],
#                 'сегмент': [segment_company, segment_company],
#                 'группа_ка':['не_указано', 'не_указано'],
#                 'сегмент_ка':['не_указано', 'не_указано'],
#                 'вид_связи':['не_указано', 'не_указано'],
#                 'объект для изм ппа':['не_указано', 'не_указано'],
#                 'рбп_кредитные_линии':['не_указано', 'не_указано'],
#             })
            
#             # Добавляем вниз
#             main_df = pd.concat([main_df, new_rows], ignore_index=True)
        
#         elif main_task !=0:
#             new_rows = pd.DataFrame({
#                 'контрагент': ['не_указано'],
#                 'ном_группа': ['не_указано',],
#                 'счет': ['99.02'],
#                 'оборот, тыс.ед.': [main_task],
#                 'доход_расход': ['Налог на прибыль'],
#                 'вид_дохода_расхода': ['Налог на прибыль'],
#                 'сегмент': [segment_company],
#                 'группа_ка':['не_указано'],
#                 'сегмент_ка':['не_указано'],
#                 'вид_связи':['не_указано'],
#                 'объект для изм ппа':['не_указано'],
#                 'рбп_кредитные_линии':['не_указано'],
#             })
#             # Добавляем вниз
#             main_df = pd.concat([main_df, new_rows], ignore_index=True)
        
#         elif other_value !=0:
#             new_rows = pd.DataFrame({
#                 'контрагент': ['не_указано'],
#                 'ном_группа': ['не_указано',],
#                 'счет': ['99.09'],
#                 'оборот, тыс.ед.': [other_value],
#                 'доход_расход': ['Налог на прибыль'],
#                 'вид_дохода_расхода': ['Налог на прибыль'],
#                 'сегмент': [segment_company],
#                 'группа_ка':['не_указано'],
#                 'сегмент_ка':['не_указано'],
#                 'вид_связи':['не_указано'],
#                 'объект для изм ппа':['не_указано'],
#                 'рбп_кредитные_линии':['не_указано'],
#             })
#             # Добавляем вниз
#             main_df = pd.concat([main_df, new_rows], ignore_index=True)
#         else:
#             pass
        
#         # concat сбивает тип данных, вернем строковый тип
#         col = ['контрагент', 'ном_группа', 'счет', 'доход_расход', 'вид_дохода_расхода', 'сегмент', 'группа_ка', 'сегмент_ка', 'вид_связи', 'объект для изм ппа', 'рбп_кредитные_линии']
#         main_df[col] = main_df.loc[:, col].astype('string')
        
#         # пустые значения заменим на "не_указано"
#         string_cols = main_df.select_dtypes(include=['string']).columns
#         main_df[string_cols] = main_df[string_cols].fillna('не_указано')
        
#         # в меппинге счета длиной 5 симвлов, включая точку, приведем счета к  символам
#         main_df['счет'] = main_df.loc[:, 'счет'].str[:5]
        
#         # вернем обновленный датафрейм в хранилище
#         context.main_df = main_df

#         logger.info(
#                     f"✓ налог на прибыль добавлен в сумме {main_task:,.0f} тыс.ед."
#                 )

#         return context
        
#     # =========================================================================
#     # ЗАГРУЗКА ДАННЫХ
#     # =========================================================================
    
#     def _load_references(self, name_company: str) -> dict:
#         """Загружает все справочники, необходимые для шага 17."""
#         logger.debug("Загрузка справочников для обработки 91.01/91.02")
        
#         # 1. Меппинг ОПУ
#         mapping_opu_df = DataLoader.load_reference_data(
#             'Меппинг_опу',
#             **REFERENCE_CONFIGS['Меппинг_опу']
#         )
#         mapping_opu_df = self.clean_whitespace(mapping_opu_df)
        
#         # 2. ППА
#         ppa_df = DataLoader.load_reference_data(
#             sheet_name='ППА',
#             strings=['группа_ос', 'вид_взаиморасчетов',
#                      'наименование_компании', 'рбп', 'ос_ппа',
#                      'ос_после_перехода_в_собственность', 'договор_аренды', 'контрагент']
#         )
#         ppa_df = ppa_df[ppa_df['наименование_компании'] == name_company]
#         ppa_df = self.clean_whitespace(ppa_df)
        
#         # 3. ВидСвязиКА
#         group_companies_df = DataLoader.load_reference_data(
#             sheet_name='ВидСвязиКА',
#             strings=['ВидСвязиКА', 'сегмент', 'ВариантыНазвания']
#         )
#         group_companies_df = self.clean_whitespace(group_companies_df)
        
#         # 4. КомпанииГруппы
#         companies_df = DataLoader.load_reference_data(
#             sheet_name='КомпанииГруппы',
#             strings=['вид_продукции_переоценки', 'сокращенное_наименование_компании', 'сегмент']
#         )
#         companies_df = self.clean_whitespace(companies_df)
        
#         # 5. КредитОбслуж
#         credit_df = DataLoader.load_reference_data(
#             sheet_name='КредитОбслуж',
#             strings=['компания', 'рбп_кредитные_линии', 'контрагент']
#         )
#         credit_df = credit_df[credit_df['компания'] == name_company]
#         credit_df = self.clean_whitespace(credit_df)
        
#         # ★ 6. НОВОЕ: План счетов БУ → список счетов с контрагентами
#         chart_accounts_df = DataLoader.load_reference_data(
#             sheet_name='ПланСчетовБУ',
#             strings=['компания', 'код', 'наименование', 'субконто_1', 'субконто_2', 'субконто_3']
#         )
#         chart_accounts_df = chart_accounts_df.loc[
#             chart_accounts_df['компания'] == name_company
#         ]
        
#         accounts_with_contractors = tuple(
#             chart_accounts_df.loc[
#                 chart_accounts_df['субконто_1'] == 'Контрагенты', 'код'
#             ]
#         )
        
#         if not accounts_with_contractors:
#             raise ValueError(
#                 f"План счетов БУ для компании '{name_company}' "
#                 f"не содержит значение 'Контрагенты' в поле субконто_1"
#             )
        
#         logger.debug(
#             f"Счета с контрагентами из ПланаСчетовБУ: {accounts_with_contractors}"
#         )
        
#             # ★ НОВОЕ: Справочник ПрочиеДоходыНДС → список видов доходов/расходов для обработки НДС
#         # Справочник НЕ мультикомпанийный, фильтрация по компании не нужна
#         other_income_vat_df = DataLoader.load_reference_data(
#             sheet_name='ПрочиеДоходыНДС',
#             strings=['прочие_доходы_ндс']  # ← единственный столбец
#         )
#         other_income_vat_df = self.clean_whitespace(other_income_vat_df)
        
#         asset_sale_types = (
#             other_income_vat_df['прочие_доходы_ндс']
#             .dropna()
#             .astype(str)
#             .str.strip()
#             .replace('', pd.NA)
#             .dropna()
#             .unique()
#             .tolist()
#         )
        
#         if not asset_sale_types:
#             logger.warning(
#                 "⚠️ В справочнике 'ПрочиеДоходыНДС' нет записей. "
#                 "Обработка продажи активов будет пропущена."
#             )
        
#         logger.debug(
#             f"Виды доходов/расходов для обработки НДС из 'ПрочиеДоходыНДС': {asset_sale_types}"
#         )
        
#         return {
#             'mapping_opu': mapping_opu_df,
#             'ppa': ppa_df,
#             'group_companies': group_companies_df,
#             'companies': companies_df,
#             'credit': credit_df,
#             'accounts_with_contractors': accounts_with_contractors,
#             'asset_sale_types': asset_sale_types,  # ★ НОВОЕ
#         }
        
#     def _load_data_from_context(
#         self, context: ProcessingContext
#     ) -> tuple[pd.DataFrame, pd.DataFrame]:
#         """Загружает ОСВ и проводки из контекста."""
#         osv_df = context.data.get('osv', pd.DataFrame())
#         if osv_df.empty:
#             raise ValueError(
#                 "В контексте нет общей ОСВ. "
#                 "Убедитесь, что предыдущие шаги (1-13) выполнены успешно."
#             )

#         transactions_all_df = context.data.get('transactions_all_df', pd.DataFrame())
#         if transactions_all_df.empty:
#             raise ValueError(
#                 "В контексте нет сводного отчета по проводкам. "
#                 "Убедитесь, что предыдущий шаг (14) выполнен успешно."
#             )

#         logger.debug(
#             f"Загружено из контекста: ОСВ={len(osv_df)} строк, "
#             f"проводки={len(transactions_all_df)} строк"
#         )
#         return osv_df, transactions_all_df
        
        
        