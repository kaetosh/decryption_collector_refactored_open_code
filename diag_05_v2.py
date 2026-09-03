from pathlib import Path
from data_processors.file_handler import FileHandler
import warnings
warnings.filterwarnings('ignore')

fh = FileHandler()
p = Path(r'C:\Users\a.karabedyan\Documents\PythonProject\decryption_collector_refactored_open_code\_INPUT_DATA\accounts_osv\МЭЗ_осв_05_6мес2026_.xlsx')

results_collector = []
fh._process_with_handler(p.parent, 'accountosv', results_collector)
df = results_collector[0]
print('Total rows:', len(df))
mask = df.apply(lambda col: col.astype(str).str.contains('05')).any(axis=1)
print('Rows containing 05:', mask.sum())
print(df[mask][['Субконто', 'Дебет_конец', 'Кредит_конец', 'Level_0', 'Level_1']].to_string())
