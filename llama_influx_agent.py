#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')

import os, json, math
from influxdb_client import InfluxDBClient
from influxdb_client.client.query_api import QueryApi
import dspy

# ========= InfluxDB 2.x 연결 정보 =========
INFLUX_URL    = os.getenv("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "mytoken")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "oran")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "ns3")

client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
query_api: QueryApi = client.query_api()

# ========= 기지국 좌표 (scenario-three.cc 기반) =========
GNB_COORDS = {
    "2": (2000.0, 2000.0),
    "3": (3000.0, 2000.0),
    "4": (2500.0, 2866.0),
    "5": (1500.0, 2866.0),
    "6": (1000.0, 2000.0),
    "7": (1500.0, 1134.0),
    "8": (2500.0, 1134.0),
}
CELL_IDS = ["2", "3", "4", "5", "6", "7", "8"]


# ========= Flux 실행 유틸 =========
def run_flux(flux: str) -> str:
    try:
        tables = query_api.query(flux)
        rows = []
        for t in tables:
            for r in t.records:
                row = {
                    "measurement": r.get_measurement(),
                    "time": r.get_time().isoformat() if r.get_time() else None,
                    "field": r.get_field(),
                    "value": r.get_value(),
                }
                row.update({k: v for k, v in r.values.items()
                             if k not in ("_start","_stop","_time","_value",
                                          "_field","_measurement","result","table")})
                rows.append(row)
        return json.dumps(rows, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ========= Tools =========

def query_influx(flux_query: str) -> str:
    """InfluxDB 2.x Flux 쿼리 실행. bucket: ns3"""
    print(f"\n[Flux Query]\n{flux_query.strip()}\n")
    return run_flux(flux_query)


def estimate_position(ue_id: str) -> str:
    """특정 UE의 SINR 값으로 weighted centroid 위치 추정"""
    ue_padded = ue_id.zfill(5)
    sinr_map = {}

    for cell_id in CELL_IDS:
        meas = f"SINR_cell_{cell_id}_ue_{ue_padded}_cp"
        flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
  |> last()
'''
        data = json.loads(run_flux(flux))
        if isinstance(data, list) and data:
            val = data[0].get("value")
            if val is not None:
                sinr_map[cell_id] = float(val)

    if not sinr_map:
        return json.dumps({"error": f"UE {ue_id} SINR 데이터 없음"})

    valid = [(cid, sinr) for cid, sinr in sinr_map.items() if sinr >= -20]
    valid.sort(key=lambda x: -x[1])
    valid = valid[:6]

    if len(valid) < 3:
        return json.dumps({"error": f"셀 수 부족 (확보: {len(valid)}개)"})

    def to_linear(db):
        return 10 ** (db / 10)

    total_w = sum(to_linear(sinr) for _, sinr in valid)
    est_x = sum(to_linear(sinr) * GNB_COORDS[cid][0] for cid, sinr in valid) / total_w
    est_y = sum(to_linear(sinr) * GNB_COORDS[cid][1] for cid, sinr in valid) / total_w

    return json.dumps({
        "ue_id": ue_id,
        "estimated_x": round(est_x, 2),
        "estimated_y": round(est_y, 2),
        "cells_used": [{"cell_id": cid, "sinr_db": round(sinr, 2)} for cid, sinr in valid],
    }, ensure_ascii=False)


def get_actual_position(ue_id: str) -> str:
    """InfluxDB에서 UE 실제 위치 조회"""
    ue_int = str(int(ue_id))
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "ue_position")
  |> filter(fn: (r) => r.ue_id == "{ue_int}")
  |> filter(fn: (r) => r._field == "x" or r._field == "y")
  |> last()
'''
    print(f"\n[Flux Query]\n{flux.strip()}\n")
    data = json.loads(run_flux(flux))
    if not data:
        return json.dumps({"error": f"UE {ue_id} 실제 위치 없음"})
    pos = {}
    if isinstance(data, list):
        for row in data:
            pos[row.get("field")] = row.get("value")
    return json.dumps({"ue_id": ue_id, "actual_x": pos.get("x"), "actual_y": pos.get("y")},
                      ensure_ascii=False)


# ========= 직접 파이프라인 =========
def compare_position(ue_id: str):
    est = json.loads(estimate_position(ue_id))
    act = json.loads(get_actual_position(ue_id))

    if "error" in est:
        print(f"[추정 오류] {est['error']}")
        return
    if "error" in act:
        print(f"[실제위치 오류] {act['error']}")
        return

    dx = est["estimated_x"] - act["actual_x"]
    dy = est["estimated_y"] - act["actual_y"]
    err = math.sqrt(dx**2 + dy**2)

    print(f"\n{'='*50}")
    print(f"UE {ue_id} 위치 비교")
    print(f"{'='*50}")
    print(f"  추정 위치: ({est['estimated_x']:.1f}, {est['estimated_y']:.1f})")
    print(f"  실제 위치: ({act['actual_x']:.1f}, {act['actual_y']:.1f})")
    print(f"  오차:      {err:.1f} m")
    print(f"\n  사용된 셀:")
    for c in est["cells_used"]:
        print(f"    cell {c['cell_id']}: SINR={c['sinr_db']}dB")


# ========= DSPy CoT 설정 =========
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
LLAMA_MODEL = os.getenv("LLAMA_MODEL", "qwen2.5:14b-instruct-q4_K_M")

lm = dspy.LM(
    model=f"ollama_chat/{LLAMA_MODEL}",
    api_base=OLLAMA_HOST,
    api_key="ollama",
    temperature=0.1,
    max_tokens=512,
    cache=False,
)
dspy.configure(lm=lm)


class PositionQuery(dspy.Signature):
    """Analyze the user question about UE location. Extract UE ID and action."""
    question: str = dspy.InputField()
    ue_id: str = dspy.OutputField(desc="UE ID number only, e.g. '12'")
    action: str = dspy.OutputField(desc="one of: compare, estimate, actual")

cot_agent = dspy.ChainOfThought(PositionQuery)


# ========= 메인 =========
def main():
    print("=" * 60)
    print("네트워크 모니터링 Agent (DSPy CoT + 삼각측위)")
    print("=" * 60)
    print("직접 명령어:")
    print("  compare <ue_id>   - 추정 vs 실제 위치 비교")
    print("  estimate <ue_id>  - 위치 추정만")
    print("  actual <ue_id>    - 실제 위치만")
    print("  또는 자연어로 질문하세요")
    print("종료: Ctrl+C\n")

    while True:
        try:
            q = input("Q> ").strip()
            if not q:
                continue

            parts = q.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "compare":
                compare_position(arg.strip())
            elif cmd == "estimate":
                r = json.loads(estimate_position(arg.strip()))
                print(json.dumps(r, indent=2, ensure_ascii=False))
            elif cmd == "actual":
                print(get_actual_position(arg.strip()))
            else:
                # DSPy CoT로 자연어 파싱
                result = cot_agent(question=q)
                ue_id = result.ue_id.strip()
                action = result.action.strip().lower()
                print(f"\n[LLM 판단] ue_id={ue_id}, action={action}")

                if action == "compare":
                    compare_position(ue_id)
                elif action == "estimate":
                    r = json.loads(estimate_position(ue_id))
                    print(json.dumps(r, indent=2, ensure_ascii=False))
                elif action == "actual":
                    print(get_actual_position(ue_id))
                else:
                    print(f"[오류] 알 수 없는 action: {action}")

        except KeyboardInterrupt:
            print("\n종료")
            break
        except Exception as e:
            print(f"\n[오류] {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()