# Fix double-encoded UTF-8 in master_runner.py
with open('master_runner.py', 'rb') as f:
    content = f.read()

# The file was UTF-8 encoded twice. Decode as latin-1 (preserves bytes),
# re-encode to latin-1 bytes, then decode as UTF-8
step1 = content.decode('latin-1')
step2 = step1.encode('latin-1')
fixed = step2.decode('utf-8')

with open('master_runner_fixed.py', 'w', encoding='utf-8') as f:
    f.write(fixed)

print('Fixed file written to master_runner_fixed.py')