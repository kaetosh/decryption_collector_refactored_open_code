from pathlib import Path
import pandas as pd
from io_module import DataLoader
import warnings
warnings.filterwarnings('ignore')

# Загружаем все ОСВ по счетам
df = DataLoader.load_account_osv()
print('Total rows:', len(df))

# Ищем строки с 05
mask = df.apply(lambda col: col.astype(str).str.contains('05')).any(axis=1)
print(f'Rows with 05: {mask.sum()}')
if mask.sum() > 0:
    print(df[mask][['Субконто', 'Дебет_конец', 'Кредит_конец', 'Level_0', 'Level_1', 'Исх.файл']].to_string())

# Проверяем файл 05 отдельно
file05 = df[df['Исх.файл'].str.contains('05', na=False)]
print(f'\nRows from 05 file: {len(file05)}')
if len(file05) > 0:
    print(file05[['Субконто', 'Дебет_конец', 'Кредит_конец', 'Level_0', 'Level_1']].to_string())