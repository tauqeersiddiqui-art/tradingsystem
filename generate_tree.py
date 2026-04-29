# generate_tree.py
# # SAFE FULL PROJECT TREE GENERATOR (READ ONLY)

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "env"}
EXCLUDE_EXT = {".pyc", ".log"}

OUTPUT_FILE = "PROJECT_STRUCTURE.md"

def generate_tree(startpath, file_handle):
    for root, dirs, files in os.walk(startpath):
        # Remove excluded dirs
        dirs[:] = sorted([d for d in dirs if d not in EXCLUDE_DIRS])
        files = sorted(files)

        level = root.replace(startpath, '').count(os.sep)
        indent = '│   ' * level
        line = f"{indent}├── {os.path.basename(root)}/"
        print(line)
        file_handle.write(line + "\n")

        subindent = '│   ' * (level + 1)

        for f in files:
            if any(f.endswith(ext) for ext in EXCLUDE_EXT):
                continue
            line = f"{subindent}├── {f}"
            print(line)
            file_handle.write(line + "\n")

if __name__ == "__main__":
    header = (
        "==========================================\n"
        " AI_Trading_System_Pro FULL STRUCTURE\n"
        "==========================================\n\n"
    )
    print(header)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("# AI_Trading_System_Pro FULL STRUCTURE\n\n")
        generate_tree(BASE_DIR, f)

    print(f"\nProject tree has also been saved to {OUTPUT_FILE}")
