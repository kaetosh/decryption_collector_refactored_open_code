import sys
sys.path.insert(0, r'C:\Users\a.karabedyan\Documents\PythonProject\decryption_collector_refactored_open_code')

from pathlib import Path
from data_processors.file_handler import FileHandler
import warnings
warnings.filterwarnings('ignore')

fh = FileHandler()
folder = Path(r'C:\Users\a.karabedyan\Documents\PythonProject\decryption_collector_refactored_open_code\_INPUT_DATA\accounts_osv')

result = fh.handle_input(folder, 'accountosv')
df, check = result['accountosv']

output_path = r'C:\Users\a.karabedyan\diag_output.txt'
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(f'Total rows: {len(df)}\n')
    f.write(f'Columns: {df.columns.tolist()}\n\n')
    
    # Ищем строки, где в любой колонке есть '05'
    mask = df.apply(lambda col: col.astype(str).str.contains('05', na=False)).any(axis=1)
    f.write(f'Rows containing 05: {mask.sum()}\n')
    if mask.sum() > 0:
        f.write(df[mask][['Субконто', 'Дебет_конец', 'Кредит_конец', 'Level_0', 'Level_1']].to_string())
    else:
        f.write('No rows with 05 found\n')
    
    # Ищем строки с 'Итого'
    f.write('\n\nRows with Итого:\n')
    mask_itogo = df['Субконто'].astype(str).str.contains('Итого', na=False)
    f.write(f'Count: {mask_itogo.sum()}\n')
    if mask_itogo.sum() > 0:
        f.write(df[mask_itogo][['Субконто', 'Дебет_конец', 'Кредит_конец', 'Level_0', 'Level_1']].head(20).to_string())
    
print(f'Output written to {output_path}')
