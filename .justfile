test:
  ruff check src
  mypy -p src

format:
  ruff check src --select I001 --fix
  ruff format src
