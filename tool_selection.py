#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
measure_tool_accuracy.py (v3)
================================
주요 변경사항 (v2 -> v3):
  - llama_tool_selection.py의 11개 tool(estimate_position 제외, 7개 KPM tool
    추가) 전체를 커버하도록 테스트셋을 96개로 확장 (약 100개)
    (단일 Tool 56개: 11개 tool x 5개씩 + compare_position만 6개 /
     Compound 40개: 2-tool 조합 10종류 x 4개씩)
  - estimate_position은 더 이상 독립 tool이 아니므로 (compare_position 내부
    helper로만 존재) 테스트셋에서 완전히 제거
  - simple/compound 개수를 하드코딩(25/25)하지 않고 실제 생성된 개수를 그대로
    사용하도록 집계 로직 수정

실행:
    python measure_tool_accuracy.py
"""

import time
import json
import re
import random
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from llama_tool_selection import react_agent

RESULTS_PATH = Path("tool_accuracy_results.json")

# ========= UE / Cell 풀 =========
_UE_POOL = [3, 7, 12, 20, 25, 30, 2, 8, 14, 18, 22, 28, 33, 44, 48,
            1, 10, 16, 21, 27, 34, 38, 42, 47, 49, 5, 9, 15, 40, 4,
            11, 17, 24, 31, 36, 41, 46, 6, 19, 13, 23, 29, 35, 39,
            43, 45, 26, 32, 37]
_CELLS = ["2", "3", "4", "5", "6", "7", "8"]


def _ue(i):
    return _UE_POOL[i % len(_UE_POOL)]


def _cell(i):
    return _CELLS[i % len(_CELLS)]


# ========= 단일 Tool 쿼리 템플릿 (11개 tool x 템플릿 여러 개) =========
# 각 항목: (tool_name, is_cell_based, [질문 템플릿 (문자열에 {x} 채워넣음), ...])
SINGLE_TOOL_SPECS = [
    ("get_actual_position", False, [
        "UE {x}의 실제 위치는?",
        "UE {x} actual position",
        "UE {x}의 진짜 좌표 알려줘",
        "UE {x}의 실제 좌표가 어디야?",
        "What is the actual position of UE {x}?",
    ]),
    ("compare_position", False, [
        "UE {x}의 추정 위치와 실제 위치를 비교해줘",
        "UE {x} 위치 오차 알려줘",
        "UE {x} compare estimated and actual position",
        "UE {x}의 측위 정확도는 어느 정도야?",
        "UE {x}는 실제 위치랑 얼마나 차이나?",
        "How far off is the position estimate for UE {x}?",
    ]),
    ("get_ue_throughput", False, [
        "UE {x} 처리량 알려줘",
        "UE {x} throughput",
        "UE {x}의 다운링크 속도는?",
        "UE {x}의 데이터 전송 속도 알려줘",
        "What is the downlink throughput for UE {x}?",
    ]),
    ("get_cell_connections", True, [
        "셀 {x} RRC 연결 수는?",
        "cell {x} connected UEs",
        "기지국 {x}에 몇 명 연결됐어?",
        "셀 {x}의 평균 RRC 연결 수 알려줘",
        "How many UEs are RRC-connected to cell {x}?",
    ]),
    ("get_ue_latency", False, [
        "UE {x} 지연시간 알려줘",
        "UE {x} latency",
        "UE {x}의 PDCP 지연은?",
        "UE {x}는 얼마나 느려?",
        "What is the downlink latency for UE {x}?",
    ]),
    ("get_ue_packet_error_rate", False, [
        "UE {x} 패킷 오류율은?",
        "UE {x} error rate",
        "UE {x}의 전송 실패율 알려줘",
        "UE {x}의 TB 에러율은 얼마야?",
        "What is the packet error rate for UE {x}?",
    ]),
    ("get_ue_prb_usage", False, [
        "UE {x} PRB 사용량 알려줘",
        "UE {x} resource block usage",
        "UE {x}가 쓰는 자원블록 수는?",
        "UE {x}의 PRB 점유량 알려줘",
        "How many PRBs does UE {x} use?",
    ]),
    ("get_cell_prb_usage", True, [
        "셀 {x} PRB 사용률은?",
        "cell {x} PRB usage",
        "기지국 {x} 자원 사용률 알려줘",
        "셀 {x}이 얼마나 혼잡해?",
        "What is the PRB utilization of cell {x}?",
    ]),
    ("get_cell_active_ue_count", True, [
        "셀 {x}에 활성 UE 몇 명이야?",
        "cell {x} active UE count",
        "기지국 {x} 활성 단말 수는?",
        "셀 {x}에서 실제로 스케줄링되는 UE 수는?",
        "How many active UEs are in cell {x}?",
    ]),
    ("get_serving_cell", False, [
        "UE {x}는 어느 셀에 붙어있어?",
        "UE {x} serving cell",
        "UE {x}의 서빙 셀 알려줘",
        "UE {x}가 지금 어느 기지국에 연결돼있어?",
        "Which cell is currently serving UE {x}?",
    ]),
    ("get_ue_buffer_size", False, [
        "UE {x} 버퍼 크기는?",
        "UE {x} buffer size",
        "UE {x}의 큐에 쌓인 데이터량은?",
        "UE {x}가 밀려있는 데이터가 많아?",
        "What is the queued buffer size for UE {x}?",
    ]),
]

TEST_CASES = []
_counter = 0
for tool_name, is_cell_based, templates in SINGLE_TOOL_SPECS:
    n_for_tool = 6 if tool_name in ("compare_position",) else 5
    for k in range(n_for_tool):
        x = _cell(_counter) if is_cell_based else _ue(_counter)
        tmpl = templates[k % len(templates)]
        TEST_CASES.append((tmpl.format(x=x), {tool_name}))
        _counter += 1

N_SINGLE = len(TEST_CASES)  # 11개 tool, 5~6개씩 = 60개

# ========= Compound (2-tool) 쿼리 템플릿 =========
# 각 항목: (질문 템플릿, {tool1, tool2}, ue_based인지/cell도 필요한지)
COMPOUND_SPECS = [
    ("UE {ue}의 추정 위치와 실제 위치를 비교하고 처리량도 알려줘",
     {"compare_position", "get_ue_throughput"}, True),
    ("UE {ue} 위치 비교하고 셀 {cell} 연결 수도 알려줘",
     {"compare_position", "get_cell_connections"}, True),
    ("UE {ue}의 지연시간과 처리량을 같이 알려줘",
     {"get_ue_latency", "get_ue_throughput"}, False),
    ("UE {ue} PRB 사용량과 셀 {cell} PRB 사용률을 같이 알려줘",
     {"get_ue_prb_usage", "get_cell_prb_usage"}, True),
    ("UE {ue}는 어느 셀에 붙어있고 그 셀 연결 수는 몇 명이야?",
     {"get_serving_cell", "get_cell_connections"}, True),
    ("UE {ue}의 패킷 오류율과 지연시간을 같이 알려줘",
     {"get_ue_packet_error_rate", "get_ue_latency"}, False),
    ("UE {ue} 버퍼 크기와 처리량을 같이 알려줘",
     {"get_ue_buffer_size", "get_ue_throughput"}, False),
    ("셀 {cell}의 활성 UE 수와 PRB 사용률을 같이 알려줘",
     {"get_cell_active_ue_count", "get_cell_prb_usage"}, "cell_only"),
    ("UE {ue}의 실제 위치와 서빙 셀을 같이 알려줘",
     {"get_actual_position", "get_serving_cell"}, False),
    ("UE {ue}의 위치 오차와 지연시간을 같이 알려줘",
     {"compare_position", "get_ue_latency"}, False),
]

_ccounter = 0
for tmpl, tools, needs_cell in COMPOUND_SPECS:
    for k in range(4):
        ue = _ue(_ccounter + 100)
        cell = _cell(_ccounter)
        if needs_cell == "cell_only":
            q = tmpl.format(cell=cell)
        elif needs_cell is True:
            q = tmpl.format(ue=ue, cell=cell)
        else:
            q = tmpl.format(ue=ue)
        TEST_CASES.append((q, set(tools)))
        _ccounter += 1

N_COMPOUND = len(TEST_CASES) - N_SINGLE  # 10 templates x 4 = 40개

random.seed(42)
random.shuffle(TEST_CASES)


def extract_tools_called(trajectory) -> set:
    called = set()
    if not trajectory:
        return called
    i = 0
    while f"tool_name_{i}" in trajectory:
        name = trajectory[f"tool_name_{i}"]
        if name and name.lower() != "finish":
            called.add(name)
        i += 1
    return called


def extract_thoughts(trajectory) -> list:
    """디버깅용: 각 스텝의 thought도 뽑아서, Tool을 왜 못 불렀는지 단서로 쓴다."""
    thoughts = []
    if not trajectory:
        return thoughts
    i = 0
    while f"thought_{i}" in trajectory:
        thoughts.append(trajectory[f"thought_{i}"])
        i += 1
    return thoughts


def run_one(query: str, expected: set):
    t0 = time.time()
    try:
        result = react_agent(question=query)
        ok = True
        answer = getattr(result, "answer", "")
        trajectory = getattr(result, "trajectory", None)
        error_msg = None
    except Exception as e:
        ok = False
        answer = ""
        trajectory = None
        error_msg = str(e)
    elapsed = time.time() - t0

    called = extract_tools_called(trajectory)
    thoughts = extract_thoughts(trajectory)
    tool_match = (called == expected)

    return {
        "query": query,
        "expected_tools": sorted(expected),
        "called_tools": sorted(called),
        "tool_selection_correct": tool_match,
        "success": ok,
        "error": error_msg,
        "elapsed_sec": round(elapsed, 2),
        "answer": answer if isinstance(answer, str) else str(answer),
        "n_thoughts": len(thoughts),
        "thoughts_preview": [t[:80] for t in thoughts],
    }


def save_progress(records):
    """매 쿼리마다 호출 -- 중간에 끊겨도 여기까지는 보존됨."""
    RESULTS_PATH.write_text(
        json.dumps({"records": records}, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def main():
    print("Tool Selection Accuracy 측정 시작 (v3, 중간저장 O)")
    print(f"총 {len(TEST_CASES)}개 쿼리 (단일 Tool {N_SINGLE} + Compound {N_COMPOUND})")
    print("=" * 70)

    records = []
    for idx, (query, expected) in enumerate(TEST_CASES, start=1):
        print(f"\n[{idx}/{len(TEST_CASES)}] 질의: {query}")
        print(f"  정답 Tool: {sorted(expected)}")
        r = run_one(query, expected)
        records.append(r)
        save_progress(records)  # <-- 매번 즉시 저장

        if not r["success"]:
            print(f"  !! 예외 발생: {r['error']} ({r['elapsed_sec']}s)")
        else:
            status = "OK" if r["tool_selection_correct"] else "MISMATCH"
            print(f"  실제 호출: {r['called_tools']} -> {status} ({r['elapsed_sec']}s)")
            print(f"  답변: {r['answer'][:150]}")
            if not r["called_tools"]:
                print(f"  !! Tool을 하나도 안 불렀는데 답을 냄 -- hallucination 의심")
                print(f"     Thought 개수: {r['n_thoughts']}")
                for t in r["thoughts_preview"]:
                    print(f"     - {t}")

    # ===== 집계 =====
    # TEST_CASES를 shuffle했으므로 인덱스로 자를 수 없음 -> expected_tools
    # 개수(1개=단일, 2개=compound)로 분류
    simple_records = [r for r in records if len(r["expected_tools"]) == 1]
    compound_records = [r for r in records if len(r["expected_tools"]) == 2]

    def accuracy(recs):
        successful = [r for r in recs if r["success"]]
        if not successful:
            return 0.0, 0, 0
        correct = sum(1 for r in successful if r["tool_selection_correct"])
        return correct / len(successful) * 100, correct, len(successful)

    simple_acc, simple_correct, simple_n = accuracy(simple_records)
    compound_acc, compound_correct, compound_n = accuracy(compound_records)
    overall_acc, overall_correct, overall_n = accuracy(records)

    print("\n" + "=" * 70)
    print("=== Tool Selection Accuracy 결과 ===")
    print(f"단일 Tool 쿼리:   {simple_correct}/{simple_n} = {simple_acc:.1f}%")
    print(f"Compound 쿼리:    {compound_correct}/{compound_n} = {compound_acc:.1f}%")
    print(f"전체:             {overall_correct}/{overall_n} = {overall_acc:.1f}%")

    zero_tool_cases = [r for r in records if r["success"] and not r["called_tools"]]
    if zero_tool_cases:
        print(f"\n!! Tool 0개로 답변한 사례: {len(zero_tool_cases)}건 (hallucination 의심, 위 로그 참고)")

    mismatches = [r for r in records if r["success"] and not r["tool_selection_correct"]]
    if mismatches:
        print(f"\n불일치 사례 ({len(mismatches)}개):")
        for m in mismatches:
            print(f"  질의: {m['query']}")
            print(f"    정답: {m['expected_tools']} / 실제: {m['called_tools']}")

    output = {
        "summary": {
            "simple_accuracy_pct": round(simple_acc, 1),
            "simple_correct": simple_correct,
            "simple_n": simple_n,
            "compound_accuracy_pct": round(compound_acc, 1),
            "compound_correct": compound_correct,
            "compound_n": compound_n,
            "overall_accuracy_pct": round(overall_acc, 1),
            "overall_correct": overall_correct,
            "overall_n": overall_n,
            "zero_tool_cases": len(zero_tool_cases),
        },
        "records": records,
    }
    RESULTS_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n최종 결과 저장: {RESULTS_PATH}")
    print("\n논문 반영 문구 예시:")
    print(f'"Across {overall_n} queries spanning {len(SINGLE_TOOL_SPECS)} tools ({N_SINGLE}')
    print(f'single-tool + {N_COMPOUND} compound), the agent selected the expected')
    print(f'tool set in {overall_correct}/{overall_n} cases ({overall_acc:.1f}%)."')


if __name__ == "__main__":
    main()