# Live discovery (a real model run)

This folder holds a discovery run driven by the live model, committed exactly as recorded and never
regenerated (`rote demo` leaves it alone). To record one:

```bash
export ANTHROPIC_API_KEY=...
rote discover specs/get_savings_balance.yaml --live
cp -r runs/discover-<run-id>* evidence/01-discovery-live/
cp runs/discover-<run-id>/cassette.json tests/fixtures/cassettes/get_savings_balance.live.json
```

The cassette lets CI and `rote demo` replay the model's decisions offline.
