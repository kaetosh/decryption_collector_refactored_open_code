import pandas as pd
from pathlib import Path
from typing import List, Literal, Dict, Tuple
import openpyxl
import zipfile
from io import BytesIO

from loguru import logger

from data_processors.file_processor import FileProcessor
from data_processors.osv_account import AccountOSV_UPPFileProcessor, AccountOSV_NonUPPFileProcessor
from data_processors.osv_general import GeneralOSV_UPPFileProcessor, GeneralOSV_NonUPPFileProcessor
from data_processors.analisys_account import Analisys_UPPFileProcessor, Analisys_NonUPPFileProcessor
from data_processors.transaction_report import Posting_UPPFileProcessor, Posting_NonUPPFileProcessor


class FileHandler:
    """Обработчик файлов с предварительной фиксацией регистров"""
    
    PROCESSORS_CONFIG = {
        'analisys': [
            ({'счет', 'кор.счет', 'с кред. счетов'}, Analisys_UPPFileProcessor),
            ({'счет', 'кор. счет', 'дебет'}, Analisys_NonUPPFileProcessor),
        ],
        'accountosv': [
            ({'субконто', 'сальдо на начало периода'}, AccountOSV_UPPFileProcessor),
            ({'счет', 'сальдо на начало периода', 'обороты за период'}, AccountOSV_NonUPPFileProcessor),
        ],
        'generalosv': [
            ({'счет', 'сальдо на начало периода', 'оборот за период'}, GeneralOSV_UPPFileProcessor),
            ({'наименование счета', 'обороты за период'}, GeneralOSV_NonUPPFileProcessor),
        ],
        'posting': [
            {'pattern': {'дата', 'документ', 'содержание', 'дт', 'кт', 'сумма'}, 'processor': Posting_UPPFileProcessor},
            {'pattern': {'период', 'аналитика дт', 'аналитика кт'}, 'processor': Posting_NonUPPFileProcessor}
        ],
    }

    def __init__(self):
        self.not_correct_files: Dict[str, str] = {}
        self.type_register: str = ""

    def handle_input(
        self, 
        input_path: Path, 
        type_register: Literal['analisys', 'accountosv', 'generalosv', 'posting']
    ) -> Dict[str, Tuple[pd.DataFrame, pd.DataFrame]]:
        
        self.type_register = type_register
        results_collector = {'results': [], 'checks': []}
        
        # =========================================================================
        # 0. ОПРЕДЕЛЕНИЕ СПИСКА ФАЙЛОВ ДЛЯ ОБРАБОТКИ
        # =========================================================================
        if input_path.is_file():
            # Валидация расширения одиночного файла
            if type_register == 'posting' and input_path.suffix.lower() != '.txt':
                raise ValueError(
                    f"Для type_register='posting' ожидается файл .txt, "
                    f"но получен '{input_path.name}'"
                )
            if type_register != 'posting' and input_path.suffix.lower() not in ('.xlsx', '.mxl'):
                raise ValueError(
                    f"Для type_register='{type_register}' ожидается файл .xlsx/.mxl, "
                    f"но получен '{input_path.name}'"
                )
            files_to_process = [input_path]
        elif input_path.is_dir():
            if type_register == 'posting':
                files_to_process = self._get_txt_files(input_path)
            else:
                files_to_process = self._get_excel_files(input_path)
        else:
            raise FileNotFoundError(f"Путь {input_path} не найден")
    
        # =========================================================================
        # РАЗДЕЛЕНИЕ ФАЙЛОВ ПО ТИПУ ОБРАБОТКИ
        # =========================================================================
        excel_files = [f for f in files_to_process if f.suffix.lower() in ('.xlsx', '.mxl')]
        txt_files = [f for f in files_to_process if f.suffix.lower() == '.txt']
        
        processor_class = None  # Будет определён при обработке первого файла
        
        # =========================================================================
        # 1. ОБРАБОТКА EXCEL/MXL ФАЙЛОВ (с фиксом регистра)
        # =========================================================================
        if excel_files:
            fixed_files_map: Dict[str, BytesIO] = {}
            
            for file_path in excel_files:
                try:
                    fixed_files_map[file_path.name] = self._fix_1c_excel_case(file_path)
                except Exception as e:
                    self.not_correct_files[file_path.name] = f"Ошибка предобработки: {str(e)}"
            
            for file_name, fixed_stream in fixed_files_map.items():
                try:
                    # Детекция процессора по содержимому Excel-потока
                    processor_class = self._detect_processor_from_stream(
                        fixed_stream, file_name, type_register
                    )
                    
                    # Сбрасываем указатель потока в начало
                    fixed_stream.seek(0)
                    
                    processor = processor_class()
                    result_df, check_df = processor.process_file(fixed_stream, file_name)
                    
                    results_collector['results'].append(result_df)
                    if not check_df.empty:
                        results_collector['checks'].append(check_df)
                        
                except Exception as e:
                    self.not_correct_files[file_name] = str(e)
        
        # =========================================================================
        # 2. ОБРАБОТКА TXT ФАЙЛОВ (с расширенной диагностикой)
        # =========================================================================
        if txt_files:
            for file_path in txt_files:
                try:
                    logger.debug(f"🔍 Пытаемся обработать TXT: {file_path.name}")
                    
                    # Детекция процессора
                    txt_processor_class = self._detect_processor_from_txt(
                        file_path, type_register
                    )
                    logger.debug(f"✓ Определён процессор: {txt_processor_class.__name__}")
                    
                    if processor_class is None:
                        processor_class = txt_processor_class
                    
                    processor = txt_processor_class()
                    result_df, check_df = processor.process_file(file_path, file_path.name)
                    
                    logger.debug(
                        f"✓ Обработан {file_path.name}: "
                        f"{len(result_df)} строк, {len(check_df)} проверок"
                    )
                    
                    results_collector['results'].append(result_df)
                    if not check_df.empty:
                        results_collector['checks'].append(check_df)
                        
                except Exception as e:
                    # ★ РАСШИРЕННОЕ логирование ошибки
                    logger.error(
                        f"❌ Ошибка обработки {file_path.name}: "
                        f"{type(e).__name__}: {e}"
                    )
                    import traceback
                    logger.error(traceback.format_exc())
                    self.not_correct_files[file_path.name] = str(e)
        
        # =========================================================================
        # 3. КОНСОЛИДАЦИЯ РЕЗУЛЬТАТОВ
        # =========================================================================
        if processor_class is None:
            raise ValueError(
                f"Не удалось определить процессор для файлов в {input_path}. "
                f"Проверьте структуру выгрузок."
            )
        
        return self._consolidate_results(results_collector, processor_class)
    
    def _detect_processor_from_txt(
        self, 
        file_path: Path, 
        type_register: str
    ) -> type:
        """
        Определяет процессор для TXT-файла по первым строкам.
        
        Читает первые 20 строк файла, ищет характерные паттерны
        из PROCESSORS_CONFIG['posting'].
        
        Args:
            file_path: Путь к TXT-файлу
            type_register: Тип регистра (должен быть 'posting')
            
        Returns:
            Класс процессора
            
        Raises:
            ValueError: Если не удалось определить процессор
        """
        if type_register != 'posting':
            raise ValueError(
                f"_detect_processor_from_txt поддерживает только type_register='posting', "
                f"получен '{type_register}'"
            )
        
        # Читаем первые 20 строк для анализа (разные кодировки)
        first_lines_text = ""
        for encoding in ['utf-8', 'cp1251', 'utf-8-sig']:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    first_lines_text = ''.join(f.readline() for _ in range(20)).lower()
                break
            except UnicodeDecodeError:
                continue
        
        if not first_lines_text:
            raise ValueError(
                f"Не удалось прочитать TXT-файл {file_path.name} "
                f"ни в одной из стандартных кодировок (utf-8, cp1251)"
            )
        
        # Анализируем паттерны из PROCESSORS_CONFIG['posting']
        posting_configs = self.PROCESSORS_CONFIG.get('posting', [])
        
        for config in posting_configs:
            pattern = config['pattern']
            processor = config['processor']
            
            # Проверяем, все ли ключевые слова паттерна присутствуют в файле
            # (в нижнем регистре, для регистронезависимого поиска)
            matches = sum(1 for keyword in pattern if keyword.lower() in first_lines_text)
            
            # Если совпало больше половины паттерна — это наш процессор
            if matches >= len(pattern) * 0.5:
                logger.debug(
                    f"Определён процессор {processor.__name__} для {file_path.name} "
                    f"(совпало {matches}/{len(pattern)} ключевых слов)"
                )
                return processor
        
        raise ValueError(
            f"Не удалось определить процессор для TXT-файла {file_path.name}. "
            f"Проверьте, что файл содержит ожидаемые заголовки."
        )
    
    def _fix_1c_excel_case(self, file_path: Path) -> BytesIO:
        """
        Исправляет регистр имен в xlsx-архивах 1С (проблема с SharedStrings.xml).
        Возвращает объект BytesIO, готовый для pd.read_excel.
        """
        try:
            # Открываем исходный файл
            with zipfile.ZipFile(file_path, 'r') as z:
                new_zip_buffer = BytesIO()
                
                # Создаем новый zip в памяти
                with zipfile.ZipFile(new_zip_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as new_z:
                    for item in z.infolist():
                        # Исправляем известную проблему 1С с регистром
                        if item.filename == 'xl/SharedStrings.xml':
                            new_name = 'xl/sharedStrings.xml'
                        else:
                            new_name = item.filename
                        
                        # Читаем данные и записываем с новым именем
                        data = z.read(item)
                        
                        # Опционально: можно сохранить оригинальные атрибуты даты/времени
                        # new_z.writestr(new_name, data, compress_type=item.compress_type)
                        new_z.writestr(new_name, data)
            
            # Возвращаем указатель в начало потока
            new_zip_buffer.seek(0)
            return new_zip_buffer
            
        except PermissionError as e:
            raise PermissionError(
                f"Файл '{file_path.name}' заблокирован другой программой. Закройте его и повторите попытку."
            ) from e
        except zipfile.BadZipFile:
            raise ValueError(f"Файл '{file_path.name}' поврежден или не является корректным Excel-файлом.")
        except Exception as e:
            raise RuntimeError(f"Непредвиденная ошибка при обработке '{file_path.name}': {e}") from e
    
    def _detect_processor_from_stream(self, stream: BytesIO, file_name: str, type_register: str) -> type:
        """Определяет процессор по потоку BytesIO"""
        try:
            # openpyxl умеет работать с файлоподобными объектами
            wb = openpyxl.load_workbook(stream, read_only=True, data_only=True)
            ws = wb.active
            header_values = set()
            for row in ws.iter_rows(min_row=1, max_row=15, values_only=True):
                for cell in row:
                    if cell and isinstance(cell, str):
                        header_values.add(cell.strip().lower())
            wb.close()
        except Exception as e:
            raise ValueError(f"Не удалось прочитать заголовки из {file_name}: {e}")

        configs = self.PROCESSORS_CONFIG.get(type_register, [])
        for keywords, processor_cls in configs:
            if keywords.issubset(header_values):
                return processor_cls

        raise ValueError(f"Не удалось определить тип регистра в файле {file_name}")

    def _get_excel_files(self, dir_path: Path) -> List[Path]:
        files = list(dir_path.glob("*.xlsx"))
        if not files:
            raise FileNotFoundError("В папке нет подходящих файлов Excel.")
        return sorted(files)
    
    def _get_txt_files(self, dir_path: Path) -> List[Path]:
        files = sorted(dir_path.glob("*.txt"))
        if not files:
            raise FileNotFoundError(
                f"В папке {dir_path.name} нет файлов .txt для отчётов по проводкам. "
                f"Проверьте, что выгрузки из 1С сохранены в формате TXT."
            )
        return files

    def _consolidate_results(self, collector: Dict, cls: FileProcessor) -> Dict[str, Tuple[pd.DataFrame, pd.DataFrame]]:
        """Объединяет результаты и применяет финальную сортировку/обработку"""
        all_results = collector['results']
        all_checks = collector['checks']
        
        if not all_results:
            return {'analisys': (pd.DataFrame(), pd.DataFrame()), 
                    'accountosv': (pd.DataFrame(), pd.DataFrame()), 
                    'generalosv': (pd.DataFrame(), pd.DataFrame())}
    
        # Объединяем все DF в один
        combined_df = pd.concat(all_results, ignore_index=True)
        combined_check = pd.concat(all_checks, ignore_index=True) if all_checks else pd.DataFrame()
        
        # Очистка заголовков и сортировка столбцов
        combined_df = self._clean_and_reorder_columns(combined_df)
        if not combined_check.empty:
            combined_check = self._clean_and_reorder_columns(combined_check)
        combined_df = cls.shiftable_level(combined_df)

        # Возвращаем в формате, совместимом с ожиданием
        return {
            self.type_register: (combined_df, combined_check)
        }
    
    def _clean_and_reorder_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Очищает заголовки от лишних пробелов и перемещает столбцы Level_ в конец.
        
        Выполняет:
        1. Удаление пробелов по краям и множественных пробелов внутри имен столбцов
        2. Перемещение всех столбцов, начинающихся с "Level_", в конец
        3. Сортировка столбцов Level_ в порядке Level_0, Level_1, Level_2...
        """
        # 1. Очистка заголовков от лишних пробелов
        # Сначала strip() для краев, затем замена множественных пробелов на одиночные
        df.columns = df.columns.astype(str).str.strip().str.replace(r'\s+', ' ', regex=True)
        
        # 2. Разделение столбцов на обычные и Level_
        level_cols = sorted(
            [col for col in df.columns if col.startswith('Level_')],
            key=lambda x: int(x.split('_')[1]) if x.split('_')[1].isdigit() else float('inf')
        )
        regular_cols = [col for col in df.columns if not col.startswith('Level_')]
        
        # 3. Формирование нового порядка: обычные столбцы + отсортированные Level_
        new_order = regular_cols + level_cols
        
        # 4. Переупорядочивание столбцов
        return df[new_order]