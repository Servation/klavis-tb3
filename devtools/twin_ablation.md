Bundles (of 8 per regime) whose event log differs from the reference.

| Variant | steady | reactivation | storms | longtail | caps | mixed |
|---|---|---|---|---|---|---|
| no latency correction (charge the estimate only) | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 6/8 |
| charge the real latency at dispatch (clairvoyant) | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 6/8 |
| no idle clamp (start tag = finish tag) | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 6/8 |
| virtual time = finish tag of last dispatch | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 6/8 |
| cut the limit on every throttle | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 7/8 |
| +1 per success above the threshold too | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| halve rounding up | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| no slow start (+1 per full round from start-up) | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| slow start doubles after a full round | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| 503 cuts like 429 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| threshold = limit before the cut | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| no per-tenant in-flight cap | 0/8 | 2/8 | 3/8 | 3/8 | 8/8 | 8/8 |
| no burst credit | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 7/8 |
| retries do not spend credit | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 7/8 |
| credit added on a fixed clock (every refill_ms from t=0) | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 7/8 |
