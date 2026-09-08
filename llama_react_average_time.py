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
                             if k not in ("_start", "_stop", "_time", "_value",
                                          "_field", "_measurement", "result", "table")})
                rows.append(row)
        return json.dumps(rows, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ========= Tools (ReAct가 호출할 함수들) =========

def estimate_position(ue_id: str) -> str:
    """Estimate the (x, y) position of a UE using SINR-based Weighted Centroid
    Localization (WCL). For each of the 7 base stations, queries the recent
    SINR time series (dB) from InfluxDB and takes their time-average to
    reduce channel noise, then converts the averaged SINR to a linear weight
    and computes a weighted average of base station coordinates. Use this
    when the user asks for an *estimated* or *predicted* UE position.

    Args:
        ue_id: The UE identifier as a string, e.g. "3".

    Returns:
        A JSON string with fields: ue_id, estimated_x, estimated_y,
        cells_used (list of {cell_id, sinr_db_mean}).
    """
    ue_padded = ue_id.zfill(5)
    sinr_map = {}

    for cell_id in CELL_IDS:
        meas = f"SINR_cell_{cell_id}_ue_{ue_padded}_cp"
        flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
'''
        data = json.loads(run_flux(flux))
        if isinstance(data, list) and data:
            vals = [float(d["value"]) for d in data if d.get("value") is not None]
            if vals:
                sinr_map[cell_id] = sum(vals) / len(vals)

    if not sinr_map:
        return json.dumps({"error": f"No SINR data found for UE {ue_id}"})

    valid = [(cid, sinr) for cid, sinr in sinr_map.items() if sinr >= -20]
    valid.sort(key=lambda x: -x[1])
    valid = valid[:6]

    if len(valid) < 3:
        return json.dumps({"error": f"Not enough usable cells (found {len(valid)})"})

    def to_linear(db):
        return 10 ** (db / 10)

    total_w = sum(to_linear(sinr) for _, sinr in valid)
    est_x = sum(to_linear(sinr) * GNB_COORDS[cid][0] for cid, sinr in valid) / total_w
    est_y = sum(to_linear(sinr) * GNB_COORDS[cid][1] for cid, sinr in valid) / total_w

    return json.dumps({
        "ue_id": ue_id,
        "estimated_x": round(est_x, 2),
        "estimated_y": round(est_y, 2),
        "cells_used": [{"cell_id": cid, "sinr_db_mean": round(sinr, 2)} for cid, sinr in valid],
    }, ensure_ascii=False)


def get_actual_position(ue_id: str) -> str:
    """Look up the ground-truth (actual) (x, y) position of a UE as recorded
    in the NS-3 simulation log, stored in InfluxDB under measurement
    'ue_position'. Use this when the user asks for the *actual* or *real*
    UE position, or as part of comparing estimated vs. actual position.

    Args:
        ue_id: The UE identifier as a string, e.g. "3".

    Returns:
        A JSON string with fields: ue_id, actual_x, actual_y.
    """
    ue_int = str(int(ue_id))
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "ue_position")
  |> filter(fn: (r) => r.ue_id == "{ue_int}")
  |> filter(fn: (r) => r._field == "x" or r._field == "y")
  |> last()
'''
    data = json.loads(run_flux(flux))
    if not data:
        return json.dumps({"error": f"No actual position found for UE {ue_id}"})
    pos = {}
    if isinstance(data, list):
        for row in data:
            pos[row.get("field")] = row.get("value")
    return json.dumps({"ue_id": ue_id, "actual_x": pos.get("x"), "actual_y": pos.get("y")},
                      ensure_ascii=False)


def compare_position(ue_id: str) -> str:
    """Compute the localization error (in meters) between the SINR-based
    estimated position and the actual position of a UE. Internally calls
    the WCL estimation and the ground-truth lookup, then computes Euclidean
    distance. Use this when the user asks to *compare* estimated vs actual
    position, or asks how accurate/far off the estimate is.

    Args:
        ue_id: The UE identifier as a string, e.g. "3".

    Returns:
        A JSON string with fields: ue_id, estimated_x, estimated_y,
        actual_x, actual_y, error_m, cells_used. If either lookup fails,
        returns a JSON string with an "error" field instead.
    """
    est = json.loads(estimate_position(ue_id))
    if "error" in est:
        return json.dumps({"error": f"estimate_position failed: {est['error']}"})

    act = json.loads(get_actual_position(ue_id))
    if "error" in act:
        return json.dumps({"error": f"get_actual_position failed: {act['error']}"})

    dx = est["estimated_x"] - act["actual_x"]
    dy = est["estimated_y"] - act["actual_y"]
    err = math.sqrt(dx**2 + dy**2)

    return json.dumps({
        "ue_id": ue_id,
        "estimated_x": est["estimated_x"],
        "estimated_y": est["estimated_y"],
        "actual_x": act["actual_x"],
        "actual_y": act["actual_y"],
        "error_m": round(err, 1),
        "cells_used": est["cells_used"],
    }, ensure_ascii=False)


def get_ue_throughput(ue_id: str) -> str:
    """Get the downlink throughput (Mbps) for a UE, computed from
    DRB.UEThpDl.UEID (an RLC-based DL throughput KPM whose raw unit is
    kbps), averaged over the recent measurement window in InfluxDB and
    converted to Mbps. Use this when the user asks about a UE's
    throughput, downlink speed, or data rate.

    Args:
        ue_id: The UE identifier as a string, e.g. "3".

    Returns:
        A JSON string with fields: ue_id, throughput_mbps, n_samples.
        Returns a JSON string with an "error" field if no data is found.
    """
    ue_padded = ue_id.zfill(5)
    meas = f"DRB.UEThpDl.UEID_ue_{ue_padded}_du"
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
'''
    data = json.loads(run_flux(flux))
    if not isinstance(data, list) or not data:
        return json.dumps({"error": f"No throughput data found for UE {ue_id}"})

    vals = [float(d["value"]) for d in data if d.get("value") is not None]
    if not vals:
        return json.dumps({"error": f"No throughput data found for UE {ue_id}"})

    mean_kbps = sum(vals) / len(vals)
    return json.dumps({
        "ue_id": ue_id,
        "throughput_mbps": round(mean_kbps / 1000.0, 3),
        "n_samples": len(vals),
    }, ensure_ascii=False)


def get_cell_connections(cell_id: str) -> str:
    """Get the mean number of RRC-connected UEs for a cell (RRC.ConnMean),
    averaged over the recent measurement window in InfluxDB. Use this when
    the user asks how many UEs are connected to a given cell/base station,
    or which cell is most/least loaded.

    Args:
        cell_id: The cell identifier as a string, e.g. "2" (valid range 1-8).

    Returns:
        A JSON string with fields: cell_id, conn_mean, n_samples.
        Returns a JSON string with an "error" field if no data is found.
    """
    meas = f"RRC.ConnMean_cell_{cell_id}"
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
'''
    data = json.loads(run_flux(flux))
    if not isinstance(data, list) or not data:
        return json.dumps({"error": f"No RRC connection data found for cell {cell_id}"})

    vals = [float(d["value"]) for d in data if d.get("value") is not None]
    if not vals:
        return json.dumps({"error": f"No RRC connection data found for cell {cell_id}"})

    return json.dumps({
        "cell_id": cell_id,
        "conn_mean": round(sum(vals) / len(vals), 2),
        "n_samples": len(vals),
    }, ensure_ascii=False)


# ========= DSPy ReAct 설정 =========
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
LLAMA_MODEL = os.getenv("LLAMA_MODEL", "qwen2.5:14b-instruct-q4_K_M")

lm = dspy.LM(
    model=f"ollama_chat/{LLAMA_MODEL}",
    api_base=OLLAMA_HOST,
    api_key="ollama",
    temperature=0.1,
    max_tokens=2048,   # ReAct는 multi-turn이라 더 많은 토큰 필요
    cache=False,
)
dspy.configure(lm=lm)


class UELocationQA(dspy.Signature):
    """You are a network monitoring assistant for a 5G NS-3 simulation.
    Users ask about UE (User Equipment) positions, downlink throughput, and
    per-cell RRC connection counts, in Korean or English. A UE ID is a
    number mentioned in the question (e.g. "UE 3", "UE12"). A cell ID is a
    number 1-8 referring to a base station (e.g. "cell 2", "셀 2",
    "기지국 2"). Use the available tools to query InfluxDB and answer the
    question accurately and concisely in Korean."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="사용자 질문에 대한 한국어 답변")


react_agent = dspy.ReAct(
    UELocationQA,
    tools=[estimate_position, get_actual_position, compare_position, get_ue_throughput, get_cell_connections],
    max_iters=5,
)


def run_query(q: str):
    result = react_agent(question=q)

    # ---- Agent가 어떤 tool을 어떤 순서로 호출했는지 출력 (포스터/발표용) ----
    print("\n[Agent Trajectory]")
    traj = getattr(result, "trajectory", None)
    if traj:
        i = 0
        while f"tool_name_{i}" in traj:
            thought = traj.get(f"thought_{i}", "")
            tool_name = traj.get(f"tool_name_{i}", "")
            tool_args = traj.get(f"tool_args_{i}", "")
            obs = traj.get(f"observation_{i}", "")
            print(f"  Step {i}: Thought = {thought}")
            print(f"          Action  = {tool_name}({tool_args})")
            print(f"          Observation = {obs}")
            i += 1
    print("\n[Answer]")
    print(result.answer)


# ========= 메인 =========
def main():
    print("=" * 60)
    print("네트워크 모니터링 Agent (DSPy ReAct + WCL)")
    print("=" * 60)
    print("자연어로 질문하세요. 예: 'UE 3 추정 위치와 실제 위치 비교해줘'")
    print("종료: Ctrl+C\n")

    while True:
        try:
            q = input("Q> ").strip()
            if not q:
                continue
            run_query(q)
            print()
        except KeyboardInterrupt:
            print("\n종료")
            break
        except Exception as e:
            print(f"\n[오류] {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()