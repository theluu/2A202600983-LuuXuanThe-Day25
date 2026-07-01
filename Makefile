.PHONY: test lint typecheck run-chaos report clean docker-up docker-down serve

test:
	pytest -q

lint:
	ruff check src tests scripts

typecheck:
	mypy src

run-chaos:
	python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json

report:
	python scripts/generate_report.py --metrics reports/metrics.json --out reports/generated_report.md

serve:
	uvicorn app:app --host 127.0.0.1 --port 8000

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache reports/metrics.json reports/final_report.md
