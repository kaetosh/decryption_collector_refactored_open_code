"""
Модуль первичной обработки данных из 1С.

Этот модуль содержит обработчики для преобразования "грязных" выгрузок из 1С
в чистые "плоские" таблицы, готовые к дальнейшей обработке в конвейере.

Основные компоненты:
- FileHandler: назначает файловый процессор в зависимости от вида обрабатываемого регистра (ОСВ, анализ счета и т.д.)
- FileProcessor: базовый класс файлового процессора
- BaseAnalysisProcessor, Analisys_UPPFileProcessor, Analisys_NonUPPFileProcessor: Обработчики анализа счета в зависимости от версии 1С
- BaseAccountOSVProcessor, AccountOSV_UPPFileProcessor, AccountOSV_NonUPPFileProcessor: Обработчики ОСВ по счету в зависимости от версии 1С
- BaseOSVFileProcessor, GeneralOSV_UPPFileProcessor, GeneralOSV_NonUPPFileProcessor: Обработчики общей ОСВ в зависимости от версии 1С
- Posting_UPPFileProcessor, Posting_NonUPPFileProcessor: Обработчики Отчетов по проводкам в зависимости от версии 1С
"""
from .file_handler import FileHandler
from .analisys_account import Analisys_UPPFileProcessor, Analisys_NonUPPFileProcessor
from .osv_account import AccountOSV_UPPFileProcessor, AccountOSV_NonUPPFileProcessor
from .osv_general import GeneralOSV_UPPFileProcessor, GeneralOSV_NonUPPFileProcessor
from .transaction_report import Posting_UPPFileProcessor, Posting_NonUPPFileProcessor

__all__ = ['FileHandler',
           'Analisys_UPPFileProcessor', 'Analisys_NonUPPFileProcessor',
           'AccountOSV_UPPFileProcessor', 'AccountOSV_NonUPPFileProcessor',
           'GeneralOSV_UPPFileProcessor', 'GeneralOSV_NonUPPFileProcessor',
           'Posting_UPPFileProcessor', 'Posting_NonUPPFileProcessor']
