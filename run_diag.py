import subprocess
import sys
import os

os.chdir(r'C:\Users\a.karabedyan\Documents\PythonProject\decryption_collector_refactored_open_code')

# Запускаем пайплайн
result = subprocess.run(
    [sys.executable, '-m', 'cli.main', '-t', '-v', '--no-interactive'],
    capture_output=True, text=True, timeout=300
)

with open('run_output.txt', 'w', encoding='utf-8') as f:
    f.write(f'Return code: {result.returncode}\n\n')
    f.write('STDOUT:\n')
    f.write(result.stdout)
    f.write('\n\nSTDERR:\n')
    f.write(result.stderr)

print('Pipeline run completed. Check run_output.txt')