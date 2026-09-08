#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_seed_diversity.py
========================
multi_seed_experiment.py가 만든 multi_seed_results/pure_llm_seed*.json 들을
비교해서, --RngRun 시드가 실제로 UE ground-truth 좌표를 바꾸고 있는지 확인.

실행:
    python check_seed_diversity.py
"""

import json
from pathlib import Path

RESULTS_DIR = Path("multi_seed_results")

seed_files = sorted(RESULTS_DIR.glob("pure_llm_seed*.json"))
if len(seed_files) < 2:
    print(f"비교하려면 최소 2개 시드 결과가 필요합니다. 현재 {len(seed_files)}개 발견.")
    raise SystemExit

seed_data = {}
for f in seed_files:
    seed = f.stem.replace("pure_llm_seed", "")
    data = json.loads(f.read_text(encoding="utf-8"))
    # ue_id -> (actual_x, actual_y)
    seed_data[seed] = {
        r["ue_id"]: (r["actual_x"], r["actual_y"]) for r in data["results"]
    }

seeds = list(seed_data.keys())
base_seed = seeds[0]
print(f"기준 시드: {base_seed} (UE {len(seed_data[base_seed])}개)\n")

for other_seed in seeds[1:]:
    common_ues = set(seed_data[base_seed]) & set(seed_data[other_seed])
    identical = 0
    different = 0
    sample_diffs = []

    for ue in sorted(common_ues, key=int):
        pos_a = seed_data[base_seed][ue]
        pos_b = seed_data[other_seed][ue]
        if pos_a == pos_b:
            identical += 1
        else:
            different += 1
            if len(sample_diffs) < 5:
                sample_diffs.append((ue, pos_a, pos_b))

    print(f"[시드 {base_seed} vs 시드 {other_seed}]")
    print(f"  동일 좌표: {identical}개 / 다른 좌표: {different}개 (공통 UE {len(common_ues)}개)")
    if sample_diffs:
        print("  예시 (다른 경우):")
        for ue, a, b in sample_diffs:
            print(f"    UE {ue}: seed{base_seed}={a}  vs  seed{other_seed}={b}")
    print()

if all(seed_data[seeds[0]] == seed_data[s] for s in seeds[1:]):
    print("!! 모든 시드에서 UE 좌표가 완전히 동일합니다 -> --RngRun이 이 스크립트에서")
    print("   작동하지 않고 있는 것으로 보입니다. scenario-three.cc 확인이 필요합니다.")
else:
    print(">> UE 좌표가 시드마다 달라지고 있습니다 -> --RngRun이 정상 작동하는 것으로 보입니다.")