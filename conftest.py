import os
import sys

# Ensure project root is importable regardless of CWD.
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)