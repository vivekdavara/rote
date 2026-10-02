# Live discovery (a real model run)

This folder holds a discovery run driven by the live model, committed exactly as recorded and never
regenerated (`rote demo` leaves it alone). To record one, start the mock (`rote app`) and then:

```bash
export ANTHROPIC_API_KEY=...        # or put it in .env
rote doctor --probe
rote discover specs/get_savings_balance.yaml --live --headed
cp -r runs/discover-<run-id>* evidence/01-discovery-live/
cp runs/discover-<run-id>/cassette.json tests/fixtures/cassettes/get_savings_balance.live.json
```

With the cassette in place, CI (`tests/integration/test_discovery.py`) and `rote demo` replay the model's
decisions offline against a fresh mock and check that they still compile to a working capability.
