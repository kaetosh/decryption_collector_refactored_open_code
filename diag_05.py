from pathlib import Path
from data_processors.file_handler import FileHandler
from data_processors.osv_account import AccountOSV_UPPFileProcessor

fh = FileHandler()
p = Path(r'C:\Users\a.karabedyan\Documents\PythonProject\decryption_collector_refactored_open_code\_INPUT_DATA\accounts_osv\МЭЗ_осв_05_6мес2026_.xlsx')
stream = fh._fix_1c_excel_case(p)
df ,check = AccountOSV_UPPFileProcessor().process_file(stream, p.name)
print('Итог. строк после процессора:', len(df))
if not df.empty:
    print(df[['Субконто','Дебет_конец','Кредит_конец','Level_0','Level_1']].to_string())
else:
    print('ФАЙЛ ДАЛ ПУСТОЙ РЕЗУЛЬТАТ')
print('--- check ---')
print(check)
