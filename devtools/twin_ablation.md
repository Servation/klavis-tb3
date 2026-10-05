Bundles (of 8 per regime) whose event log differs from the reference.

| Variant | steady | reactivation | storms | longtail | caps | mixed |
|---|---|---|---|---|---|---|
| no latency correction (charge the estimate only) | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 7/8 |
| charge the real latency at dispatch (clairvoyant) | 8/8 | 6/8 | 8/8 | 8/8 | 8/8 | 7/8 |
| no idle clamp (start tag = finish tag) | 8/8 | 7/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| virtual time = finish tag of last dispatch | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| cut the limit on every throttle | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 7/8 |
| +1 to the limit on every success | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| halve rounding up | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| no per-tenant in-flight cap | 4/8 | 6/8 | 7/8 | 7/8 | 8/8 | 8/8 |
