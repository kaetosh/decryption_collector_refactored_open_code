"""
Шаг 1b: Проверка наличия всех необходимых файлов выгрузок.
"""
from loguru import logger
import pandas as pd
from pipeline.base import Step, ProcessingContext
from pipeline.errors import MissingFilesError
from config.settings import (
    ACCOUNT_CARDS_DIR,
    REQUIRED_OSV_DIRS,
    SPECIAL_REPORTS_DIR
)
from utils import find_missing_files


class Step1bVerifyFilesStep(Step):
    """
    Шаг 1б: Проверка списка необходимых регистров для выгрузки.
    
    Проверяет наличие:
    - ОСВ для баланса (в accounts_osv и accounts_osv_lease)
    - Отчеты по проводкам для ОПУ (в transaction_report)
    - Спецотчеты для баланса (в special_reports)
    """

    def __init__(self):
        super().__init__(
            name="Шаг 1б: Проверка списка выгрузок",
            description="Отсутствие необходимых регистров на основе общей ОСВ вызовет ошибку"
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        # =========================================================================
        # ПРОВЕРКА ОСВ ДЛЯ БАЛАНСА
        # =========================================================================
        balance_filenames = context.data.get('expected_filenames', [])
        
        missing_balance = []
        missing_by_dir = {}
        
        for dir_config in REQUIRED_OSV_DIRS:
            dir_path = dir_config['path']
            dir_name = dir_path.name
            account_filter = dir_config.get('account_filter')
            exclude_accounts = dir_config.get('exclude_accounts')
            
            # Фильтруем файлы для этой папки
            dir_filenames = self._filter_filenames_for_dir(
                balance_filenames,
                account_filter=account_filter,
                exclude_accounts=exclude_accounts
            )
            
            if not dir_filenames:
                logger.debug("Нет ожидаемых ОСВ для папки {}", dir_name)
                continue
            
            # Проверяем наличие файлов
            missing = find_missing_files(dir_filenames, dir_path)
            
            if missing:
                missing_balance.extend(missing)
                missing_by_dir[dir_name] = missing
                logger.warning(
                    "В папке {} отсутствует {} файл(ов) ОСВ",
                    dir_name,
                    len(missing),
                )
            else:
                logger.debug(
                    "✓ Все {} ОСВ найдены в {}", len(dir_filenames), dir_name
                )
        
        
        # =========================================================================
        # ПРОВЕРКА СПЕЦОТЧЕТОВ, точно нужны:
            # если период год, то анализ по 84 обязателен, чтобы разбить НРП на накопленную на начало года и текущего периода
            # ведомость амортизации арендованных ОС в разбивке по арендодателям, чтобы исключить ВГО
            # остальные - спецотчеты для УПП выгрузок, если их нет, скрипт заполняет значениями по умолчанию
        # =========================================================================
        if context.type_period == 'год':
            context.data['special_reports_obligatory_list'] = [f'{context.company}_анализ_84_{context.period}_.xlsx',
                                           f'{context.company}_ведамор_0102_{context.period}_.xlsx']
        else:
            context.data['special_reports_obligatory_list'] = [f'{context.company}_ведамор_0102_{context.period}_.xlsx']
        
        special_reports_filenames = context.data.get('special_reports_obligatory_list', [])
        
        missing_special_reports = []
        
        if special_reports_filenames:
            missing_special_reports = find_missing_files(special_reports_filenames, SPECIAL_REPORTS_DIR)
            
            if missing_special_reports:
                missing_by_dir['special_reports'] = missing_special_reports
                logger.warning(
                    "В папке special_reports отсутствует обязательные спецотчеты: {} шт.",
                    len(missing_special_reports),
                )
            else:
                logger.debug(
                    "✓ Все {} спецотчеты в special_reports",
                    len(special_reports_filenames),
                )
        
        # =========================================================================
        # ПРОВЕРКА ОТЧЕТОВ ПО ПРОВОДКАМ ДЛЯ ОПУ
        # =========================================================================
        card_filenames = context.data.get('expected_card_filenames', [])
        
        missing_cards = []
        
        if card_filenames:
            missing_cards = find_missing_files(card_filenames, ACCOUNT_CARDS_DIR)
            
            if missing_cards:
                missing_by_dir['transaction_report'] = missing_cards
                logger.warning(
                    "В папке transaction_report отсутствует отчеты по проводкам: {} шт.",
                    len(missing_cards),
                )
            else:
                logger.debug(
                    "✓ Все {} отчеты по проводкам найдены в account_cards",
                    len(card_filenames),
                )
        
        # =========================================================================
        # ОБЪЕДИНЕНИЕ ОШИБОК И ВЫБРОС ИСКЛЮЧЕНИЯ
        # =========================================================================
        all_missing = missing_balance + missing_cards + missing_special_reports
        
        if all_missing:
            # Формируем problem_data для Excel
            problem_data = self._build_missing_files_report(
                missing_by_dir,
                context.company,
                context.period
            )
            
            raise MissingFilesError(
                            message=f"Отсутствуют {len(all_missing)} обязательных выгрузок из 1С",
                            missing_files=all_missing,
                            problem_data=problem_data,
                            reference_name="Папки с выгрузками",
                            expected_dir=", ".join(missing_by_dir.keys()),  # ← ИСПРАВЛЕНО: только папки с проблемами
                            total_missing=len(all_missing),
                            missing_by_dir={k: len(v) for k, v in missing_by_dir.items()},
                        )
        
        logger.info("✓ Все обязательные файлы выгрузок найдены!")
        return context

    def _filter_filenames_for_dir(
        self,
        filenames: list,
        account_filter: list = None,
        exclude_accounts: list = None
    ) -> list:
        """Фильтрует список файлов для конкретной папки."""
        if account_filter is None and exclude_accounts is None:
            return filenames
        
        filtered = []
        
        for filename in filenames:
            parts = filename.replace('.xlsx', '').split('_')
            if len(parts) < 3:
                continue
            
            account = parts[2]
            
            if account_filter and account not in account_filter:
                continue
            
            if exclude_accounts and account in exclude_accounts:
                continue
            
            filtered.append(filename)
        
        return filtered

    def _build_missing_files_report(
        self,
        missing_by_dir: dict,
        company_name: str,
        period: str
    ) -> pd.DataFrame:
        """Формирует DataFrame с информацией об отсутствующих файлах."""
        rows = []
        
        for dir_name, missing_files in missing_by_dir.items():
            for filename in missing_files:
                parts = filename.replace('.xlsx', '').split('_')
                
                if len(parts) >= 4:
                    rows.append({
                        'папка': dir_name,
                        'имя_файла': filename,
                        'компания': parts[0],
                        'регистр': parts[1],
                        'счет': parts[2],
                        'период': parts[3],
                        'статус': 'ОТСУТСТВУЕТ'
                    })
                else:
                    rows.append({
                        'папка': dir_name,
                        'имя_файла': filename,
                        'компания': '',
                        'регистр': '',
                        'счет': '',
                        'период': '',
                        'статус': 'ОТСУТСТВУЕТ'
                    })
        
        return pd.DataFrame(rows)
