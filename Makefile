.PHONY: test test-contracts test-api test-transcription test-minutes test-swift test-extension openapi lint

test: test-contracts test-api test-transcription test-minutes

test-contracts:
	uv run --project packages/python/audio_minutes_contracts --extra dev pytest -q packages/python/audio_minutes_contracts/tests tests/contract

test-api:
	cd services/minutes-api && uv run pytest -q

test-transcription:
	cd services/transcription-worker && uv run pytest -q

test-minutes:
	cd services/minutes-worker && uv run pytest -q

test-swift:
	swift test --package-path packages/swift
	swift build --package-path apps/cli
	swift test --package-path apps/macos

test-extension:
	cd extensions/chrome && npm test && npm run build

openapi:
	cd services/minutes-api && uv run python -m minutes_api.export_openapi ../../contracts/api/openapi.json

lint:
	uv run --project packages/python/audio_minutes_contracts --extra dev ruff check packages services benchmarks
