# Voice eval ledger

One row per scorer run (eval/score.py). Compare rows with the same stage and sha256. The CI is the first metric's.

| date | stage | option | dataset | n | 95% CI | metrics | sha256 |
|---|---|---|---|---|---|---|---|
| 2026-10-23 | turn-taking | owner-live silence450 | recordings2 | 183 | [0.580, 0.739] | false_split 0.656 delta +0.000 [+0.000, +0.000] wait p50 450 p95 450 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live silence600 | recordings2 | 183 | [0.500, 0.665] | false_split 0.579 delta -0.077 [-0.129, -0.031] wait p50 600 p95 600 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live silence800 | recordings2 | 183 | [0.480, 0.653] | false_split 0.563 delta -0.093 [-0.152, -0.043] wait p50 800 p95 800 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live silence1000 | recordings2 | 183 | [0.447, 0.622] | false_split 0.530 delta -0.126 [-0.191, -0.069] wait p50 1000 p95 1000 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live silence1200 | recordings2 | 183 | [0.370, 0.519] | false_split 0.443 delta -0.213 [-0.286, -0.144] wait p50 1200 p95 1200 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live hold450/1500 | recordings2 | 183 | [0.521, 0.677] | false_split 0.596 delta -0.060 [-0.093, -0.031] wait p50 450 p95 1500 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live hold450/2000 | recordings2 | 183 | [0.500, 0.656] | false_split 0.574 delta -0.082 [-0.125, -0.045] wait p50 450 p95 2000 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live hold600/1500 | recordings2 | 183 | [0.457, 0.618] | false_split 0.536 delta -0.120 [-0.179, -0.070] wait p50 600 p95 1500 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live hold600/2000 | recordings2 | 183 | [0.437, 0.596] | false_split 0.514 delta -0.142 [-0.206, -0.086] wait p50 600 p95 2000 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live hold800/2000 | recordings2 | 183 | [0.415, 0.584] | false_split 0.497 delta -0.158 [-0.228, -0.097] wait p50 800 p95 2000 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live smart1000 | recordings2 | 183 | [0.807, 0.938] | false_split 0.880 delta +0.224 [+0.123, +0.315] wait p50 200 p95 1200 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
| 2026-10-23 | turn-taking | owner-live smart1500 | recordings2 | 183 | [0.796, 0.929] | false_split 0.869 delta +0.213 [+0.107, +0.309] wait p50 200 p95 1700 ms | f8429f1093ebb588efd190d608351239aef6ae72f7c095ee020f33ed1fdc397e |
