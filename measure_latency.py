#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E2E Latency Measurement for the DSPy ReAct Local Agent
========================================================
llama_react.py의 react_agent를 그대로 재사용하고, 그 위에 wall-clock
타이머만 감싼다. 기존 코드는 전혀 수정하지 않는다 (import만 함).

측정 대상: run_query() 한 번 호출 -> 최종 답변이 나오기까지 걸리는 시간.
이건 논문 Section III "Mapping to O-RAN"에서 추정으로만 적어둔
"low single-digit seconds" 주장을 실측치로 바꾸기 위한 것.

실행:
    python3 measure_latency.py

출력:
    - 쿼리별 레이턴시 (콘솔)
    - 통계 요약 (median, mean, min, max, p95)
    - latency_results.json 저장
"""

import time
import json
import statistics
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

# 기존 llama_react.py의 에이전트를 그대로 가져온다 (수정 없음)
from llama_react import react_agent

# 논문 Table III 평가에 쓴 것과 동일한 UE 범위(1~49)에서 폭넓게 샘플링.
# N=10~30은 p95/max 추정에 여전히 다소 부족할 수 있으므로 N=50으로 확장.
# 단순 질의와 compound 질의를 절반씩(25/25) 섞는다.

SIMPLE_TEMPLATES = [
    "UE {ue}의 추정 위치를 알려줘",
    "UE {ue} estimate position",
    "UE {ue}의 실제 위치는?",
    "UE {ue} 처리량 알려줘",
]

COMPOUND_TEMPLATES = [
    "UE {ue}의 추정 위치와 실제 위치를 비교하고 처리량도 알려줘",
    "UE {ue} 위치 비교하고 셀 {cell} 연결 수도 알려줘",
    "UE {ue}의 오차와 처리량을 같이 알려줘",
    "UE {ue}의 위치 비교와 셀 {cell} 연결 수를 같이 알려줘",
]

# UE 1~49 전체 범위를 최대한 고르게 커버 (25+25=50개, 일부는 UE를 겹쳐
# 쓰되 쿼리 템플릿을 다르게 해서 다양성 확보)
_simple_ues = [3, 7, 12, 20, 25, 30, 2, 8, 14, 18, 22, 28, 33, 44, 48,
               1, 10, 16, 21, 27, 34, 38, 42, 47, 49]
_compound_ues = [5, 9, 15, 40, 4, 11, 17, 24, 31, 36, 41, 46, 6, 19,
                 13, 23, 29, 35, 39, 43, 45, 26, 32, 37, 48]
_cells = ["2", "3", "4", "5", "6", "7", "8"]

TEST_QUERIES = (
    [SIMPLE_TEMPLATES[i % len(SIMPLE_TEMPLATES)].format(ue=ue)
     for i, ue in enumerate(_simple_ues)]
    + [COMPOUND_TEMPLATES[i % len(COMPOUND_TEMPLATES)].format(
        ue=ue, cell=_cells[i % len(_cells)])
       for i, ue in enumerate(_compound_ues)]
)
# 총 50개 (단순 25 + compound 25), 매번 다른 순서로 부하가 튀지 않도록 섞음
import random
random.seed(42)
random.shuffle(TEST_QUERIES)


def measure_one(query: str):
    """단일 쿼리에 대해 시작~끝 wall-clock 시간 측정."""
    t0 = time.perf_counter()
    try:
        result = react_agent(question=query)
        ok = True
        answer = getattr(result, "answer", None)
    except Exception as e:
        ok = False
        answer = f"ERROR: {e}"
    t1 = time.perf_counter()
    elapsed = t1 - t0

    # trajectory 길이(=실제 몇 step 돌았는지)도 같이 기록 -> 논문에서
    # "3-step ReAct 루프"라고 쓴 부분의 실측 검증 겸용
    n_steps = 0
    traj = getattr(result, "trajectory", None) if ok else None
    if traj:
        while f"tool_name_{n_steps}" in traj:
            n_steps += 1

    return {
        "query": query,
        "elapsed_sec": round(elapsed, 3),
        "success": ok,
        "n_steps": n_steps,
        "answer_preview": (answer[:80] if isinstance(answer, str) else str(answer)[:80]),
    }


def main():
    print("E2E 레이턴시 실측 시작")
    print("=" * 70)

    records = []
    for q in TEST_QUERIES:
        print(f"\n질의: {q}")
        r = measure_one(q)
        records.append(r)
        status = "OK" if r["success"] else "FAIL"
        print(f"  -> {status}, {r['elapsed_sec']}s, steps={r['n_steps']}")
        print(f"  answer: {r['answer_preview']}")

    ok_records = [r for r in records if r["success"]]
    if not ok_records:
        print("\n성공한 쿼리가 없습니다. Ollama/InfluxDB 연결 상태를 확인하세요.")
        return

    times = [r["elapsed_sec"] for r in ok_records]
    times_sorted = sorted(times)
    p95_idx = max(0, int(len(times_sorted) * 0.95) - 1)

    summary = {
        "n_queries": len(records),
        "n_success": len(ok_records),
        "mean_sec": round(statistics.mean(times), 3),
        "median_sec": round(statistics.median(times), 3),
        "min_sec": round(min(times), 3),
        "max_sec": round(max(times), 3),
        "p95_sec": round(times_sorted[p95_idx], 3),
    }

    print("\n" + "=" * 70)
    print("=== E2E Latency 요약 (논문 Section III 근거용) ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    with open("latency_results.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "records": records}, f, indent=2, ensure_ascii=False)
    print("\n결과 저장: latency_results.json")

    print("\n참고: 논문에 넣을 때는 median과 max(또는 p95)를 같이 보고하는 것을")
    print("권장합니다 -- median은 '평상시', p95/max는 'near-RT 예산 초과가")
    print("얼마나 심한가'를 보여주는 근거로 각각 쓸 수 있습니다.")


if __name__ == "__main__":
    main()