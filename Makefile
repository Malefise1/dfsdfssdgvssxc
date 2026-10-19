PYTHON ?= .venv/bin/python
.PHONY: test lint format doctor audit
test:
	$(PYTHON) -m pytest -q
lint:
	$(PYTHON) -m ruff check app tests scripts manage.py doctor.py
	$(PYTHON) -m ruff format --check app tests scripts manage.py doctor.py
format:
	$(PYTHON) -m ruff format app tests scripts manage.py doctor.py
doctor:
	$(PYTHON) doctor.py
audit:
	$(PYTHON) -m pip_audit -r requirements.txt --disable-pip --no-deps
