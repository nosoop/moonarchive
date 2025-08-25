test:
  ruff check src
  mypy -p src

format:
  ruff check src tests --select I001 --fix
  ruff format src tests
