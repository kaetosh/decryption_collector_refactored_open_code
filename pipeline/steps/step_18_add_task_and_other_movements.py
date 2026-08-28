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
        name_company = context.company

        # Загружаем только проводки (ОСВ в этом шаге не используется)
        transactions_all_df = self._load_data_from_context(context)
        
        # Загружаем только нужный справочник
        companies_df = self._load_companies_reference(name_company, context)

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
                'счет': '99.02',
                'оборот, тыс.ед.': tax_profit,
                'доход_расход': 'Налог на прибыль',
                'вид_дохода_расхода': 'Налог на прибыль',
                'счет_фо': '1300000000'
            })
        if other_value != 0:
            new_rows_data.append({
                'счет': '99.09',
                'оборот, тыс.ед.': other_value,
                'доход_расход': 'Прочее',
                'вид_дохода_расхода': 'Прочее',
                'счет_фо': '1300000100'
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
            main_df = pd.concat([context.journal_df, new_rows], ignore_index=True)

            # Приводим типы и заполняем пропуски
            str_cols = ['контрагент', 'ном_группа', 'счет', 'доход_расход', 'вид_дохода_расхода',
                        'сегмент', 'группа_ка', 'сегмент_ка', 'вид_связи', 'объект для изм ппа', 'рбп_кредитные_линии']
            
            main_df[str_cols] = main_df[str_cols].astype('string').fillna('не_указано')
            main_df['счет_фо'] = main_df['счет_фо'].astype('string')
            
            main_df.loc[main_df['вид_связи'] == 'не_указано', 'вид_связи'] = '3 лица'
            
            main_df['счет'] = main_df['счет'].str[:5]

            context.journal_df = main_df

        logger.info(
            "[OK] Добавлен налог на прибыль ({:,.0f} тыс. ед.) и прочие движения ({:,.0f} тыс. ед.)",
            tax_profit,
            other_value,
        )
        return context

    # =========================================================================
    # ЗАГРУЗКА ДАННЫХ
    # =========================================================================
    def _load_companies_reference(self, name_company: str, context: ProcessingContext) -> pd.DataFrame:
        """Загружает справочник компаний (единственный нужный для этого шага)."""
        logger.debug("Загрузка справочника компаний")
        companies_df = context.references['компании_группы']
        return companies_df

    def _load_data_from_context(self, context: ProcessingContext) -> pd.DataFrame:
        """Загружает проводки из контекста."""
        transactions_all_df = self.get_df_from_context(
            context,
            'transactions_all_df',
            hint="Убедитесь, что предыдущий шаг (14) выполнен успешно.",
        )
        logger.debug(
            "Загружено из контекста: проводки={} строк",
            len(transactions_all_df),
        )
        return transactions_all_df

