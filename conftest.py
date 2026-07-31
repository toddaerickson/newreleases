"""Present so pytest puts the repo root on sys.path.

The modules under test live flat at the repo root (main.py, filters.py, ...), not
in a package, so tests/ can only `import main` if the root is importable.
"""
