# Recordings

JSON-lines recordings of the plant's OPC UA values made with
`python -m dmf.cli.record --out recordings/<name>.jsonl` (read-only).

* Check a recording against the tracker: `python -m dmf.cli.replay_check recordings/<name>.jsonl`
* Replay it through the mock server: `MOCK_MODE=replay MOCK_REPLAY_FILE=/recordings/<name>.jsonl docker compose -f docker-compose.yml -f docker-compose.demo.yml up`

`*.jsonl` files are git-ignored (they can be large). Copy representative lab recordings to
`backend/tests/data/` to make them part of the regression tests.
