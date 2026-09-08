#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llama_tool_selection.py
========================
llama_influx_agent.py / llama_react_average_time.py 기반. Tool selection accuracy
실험용으로 두 가지를 변경:

1. estimate_position을 ReAct에 노출되는 tool 목록에서 제거.
   (compare_position이 내부적으로 estimate_position + get_actual_position을
   이미 호출하므로, estimate_position을 별도 tool로도 노출하면 ReAct가
   compare_position 호출 후 estimate_position을 중복으로 또 부르는 경향이
   있었음 - tool_accuracy_results.json 확인 결과 31/50건이 이 패턴으로 불일치.
   estimate_position은 compare_position 내부 helper로만 남겨둠.)

2. 다른 KPM을 조회하는 tool 7개 추가 (get_ue_latency, get_ue_packet_error_rate,
   get_ue_prb_usage, get_cell_prb_usage, get_cell_active_ue_count,
   get_serving_cell, get_ue_buffer_size). measurement 이름은 실제 InfluxDB
   `SHOW MEASUREMENTS` 결과에서 확인한 이름을 그대로 사용.

주의: get_actual_position이 조회하는 measurement 이름을 "ue_position"에서
"UE_Position"으로 수정함 (실제 InfluxDB에는 UE_Position만 존재, 기존 코드의
소문자 "ue_position"은 계속 빈 결과를 받았을 가능성이 있음 - 확인 필요).
"""

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


def _mean_value(measurement: str) -> float | None:
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => r._field == "value")
'''
    data = json.loads(run_flux(flux))
    if not isinstance(data, list) or not data:
        return None
    vals = [float(d["value"]) for d in data if d.get("value") is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


# ========= 기존 Tools =========

def estimate_position(ue_id: str) -> str:
    """(compare_position 내부 helper. ReAct tool 목록에는 노출하지 않음.)
    Estimate the (x, y) position of a UE using SINR-based Weighted Centroid
    Localization (WCL).
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
    'UE_Position'. Use this when the user asks for the *actual* or *real*
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
  |> filter(fn: (r) => r._measurement == "UE_Position")
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


# ========= 새로 추가된 7개 KPM Tools =========

def get_ue_latency(ue_id: str) -> str:
    """Get the mean downlink PDCP SDU delay (ms) for a UE
    (DRB.PdcpSduDelayDl.UEID(pdcpLatency)), averaged over the recent
    measurement window. Use this when the user asks about a UE's latency,
    delay, or lag.

    Args:
        ue_id: The UE identifier as a string, e.g. "3".

    Returns:
        A JSON string with fields: ue_id, latency_ms, n_samples.
        Returns a JSON string with an "error" field if no data is found.
    """
    ue_padded = ue_id.zfill(5)
    meas = f"DRB.PdcpSduDelayDl.UEID(pdcpLatency)_ue_{ue_padded}_up"
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
'''
    data = json.loads(run_flux(flux))
    if not isinstance(data, list) or not data:
        return json.dumps({"error": f"No latency data found for UE {ue_id}"})

    vals = [float(d["value"]) for d in data if d.get("value") is not None]
    if not vals:
        return json.dumps({"error": f"No latency data found for UE {ue_id}"})

    return json.dumps({
        "ue_id": ue_id,
        "latency_ms": round(sum(vals) / len(vals), 3),
        "n_samples": len(vals),
    }, ensure_ascii=False)


def get_ue_packet_error_rate(ue_id: str) -> str:
    """Get the downlink transport-block error rate (%) for a UE, computed as
    TB.ErrTotalNbrDl.1.UEID / TB.TotNbrDl.1.UEID, averaged over the recent
    measurement window. Use this when the user asks about a UE's packet
    loss, error rate, or transmission reliability.

    Args:
        ue_id: The UE identifier as a string, e.g. "3".

    Returns:
        A JSON string with fields: ue_id, error_rate_pct, mean_err,
        mean_tot. Returns a JSON string with an "error" field if no data
        is found.
    """
    ue_padded = ue_id.zfill(5)
    mean_err = _mean_value(f"TB.ErrTotalNbrDl.1.UEID_ue_{ue_padded}_du")
    mean_tot = _mean_value(f"TB.TotNbrDl.1.UEID_ue_{ue_padded}_du")

    if mean_err is None or mean_tot is None:
        return json.dumps({"error": f"No TB error/total data found for UE {ue_id}"})
    if mean_tot == 0:
        return json.dumps({"error": f"TB.TotNbrDl is 0 for UE {ue_id}, cannot compute rate"})

    return json.dumps({
        "ue_id": ue_id,
        "error_rate_pct": round(100.0 * mean_err / mean_tot, 3),
        "mean_err": round(mean_err, 3),
        "mean_tot": round(mean_tot, 3),
    }, ensure_ascii=False)


def get_ue_prb_usage(ue_id: str) -> str:
    """Get the mean number of downlink PRBs (Physical Resource Blocks) used
    by a UE (RRU.PrbUsedDl.UEID), averaged over the recent measurement
    window. Use this when the user asks about a UE's resource block usage
    or radio resource allocation.

    Args:
        ue_id: The UE identifier as a string, e.g. "3".

    Returns:
        A JSON string with fields: ue_id, prb_used_mean, n_samples.
        Returns a JSON string with an "error" field if no data is found.
    """
    ue_padded = ue_id.zfill(5)
    meas = f"RRU.PrbUsedDl.UEID_ue_{ue_padded}_du"
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
'''
    data = json.loads(run_flux(flux))
    if not isinstance(data, list) or not data:
        return json.dumps({"error": f"No PRB usage data found for UE {ue_id}"})

    vals = [float(d["value"]) for d in data if d.get("value") is not None]
    if not vals:
        return json.dumps({"error": f"No PRB usage data found for UE {ue_id}"})

    return json.dumps({
        "ue_id": ue_id,
        "prb_used_mean": round(sum(vals) / len(vals), 2),
        "n_samples": len(vals),
    }, ensure_ascii=False)


def get_cell_prb_usage(cell_id: str) -> str:
    """Get the mean downlink PRB (Physical Resource Block) utilization for
    a cell (dlPrbUsage), averaged over the recent measurement window. Use
    this when the user asks how congested/loaded a cell's radio resources
    are, or which cell has the highest PRB utilization.

    Args:
        cell_id: The cell identifier as a string, e.g. "2" (valid range 1-8).

    Returns:
        A JSON string with fields: cell_id, dl_prb_usage_mean, n_samples.
        Returns a JSON string with an "error" field if no data is found.
    """
    meas = f"dlPrbUsage_cell_{cell_id}"
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
'''
    data = json.loads(run_flux(flux))
    if not isinstance(data, list) or not data:
        return json.dumps({"error": f"No PRB usage data found for cell {cell_id}"})

    vals = [float(d["value"]) for d in data if d.get("value") is not None]
    if not vals:
        return json.dumps({"error": f"No PRB usage data found for cell {cell_id}"})

    return json.dumps({
        "cell_id": cell_id,
        "dl_prb_usage_mean": round(sum(vals) / len(vals), 3),
        "n_samples": len(vals),
    }, ensure_ascii=False)


def get_cell_active_ue_count(cell_id: str) -> str:
    """Get the mean number of *active* UEs for a cell (numActiveUes),
    averaged over the recent measurement window. This differs from RRC
    connection count: it reflects UEs actively scheduled/transmitting
    rather than merely RRC-connected. Use this when the user asks how many
    UEs are actively using a cell.

    Args:
        cell_id: The cell identifier as a string, e.g. "2" (valid range 1-8).

    Returns:
        A JSON string with fields: cell_id, active_ue_mean, n_samples.
        Returns a JSON string with an "error" field if no data is found.
    """
    meas = f"numActiveUes_cell_{cell_id}"
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
'''
    data = json.loads(run_flux(flux))
    if not isinstance(data, list) or not data:
        return json.dumps({"error": f"No active UE count data found for cell {cell_id}"})

    vals = [float(d["value"]) for d in data if d.get("value") is not None]
    if not vals:
        return json.dumps({"error": f"No active UE count data found for cell {cell_id}"})

    return json.dumps({
        "cell_id": cell_id,
        "active_ue_mean": round(sum(vals) / len(vals), 2),
        "n_samples": len(vals),
    }, ensure_ascii=False)


def get_serving_cell(ue_id: str) -> str:
    """Look up which cell is currently serving a UE (Serv_Cellid_ue_),
    using the most recent reported value. Use this when the user asks
    which cell/base station a UE is attached to, or wants to know a UE's
    serving cell before asking about that cell's other metrics.

    Args:
        ue_id: The UE identifier as a string, e.g. "3".

    Returns:
        A JSON string with fields: ue_id, serving_cell_id.
        Returns a JSON string with an "error" field if no data is found.
    """
    ue_padded = ue_id.zfill(5)
    meas = f"Serv_Cellid_ue__ue_{ue_padded}_cp"
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
  |> last()
'''
    data = json.loads(run_flux(flux))
    if not isinstance(data, list) or not data:
        return json.dumps({"error": f"No serving cell data found for UE {ue_id}"})

    val = data[0].get("value")
    if val is None:
        return json.dumps({"error": f"No serving cell data found for UE {ue_id}"})

    return json.dumps({
        "ue_id": ue_id,
        "serving_cell_id": str(int(val)),
    }, ensure_ascii=False)


def get_ue_buffer_size(ue_id: str) -> str:
    """Get the mean RLC/PDCP QoS buffer size (bytes queued, awaiting
    transmission) for a UE (DRB.BufferSize.Qos.UEID), averaged over the
    recent measurement window. A growing buffer indicates congestion or
    that the UE cannot be scheduled fast enough. Use this when the user
    asks about a UE's buffer/queue status or congestion.

    Args:
        ue_id: The UE identifier as a string, e.g. "3".

    Returns:
        A JSON string with fields: ue_id, buffer_size_mean, n_samples.
        Returns a JSON string with an "error" field if no data is found.
    """
    ue_padded = ue_id.zfill(5)
    meas = f"DRB.BufferSize.Qos.UEID_ue_{ue_padded}_du"
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
'''
    data = json.loads(run_flux(flux))
    if not isinstance(data, list) or not data:
        return json.dumps({"error": f"No buffer size data found for UE {ue_id}"})

    vals = [float(d["value"]) for d in data if d.get("value") is not None]
    if not vals:
        return json.dumps({"error": f"No buffer size data found for UE {ue_id}"})

    return json.dumps({
        "ue_id": ue_id,
        "buffer_size_mean": round(sum(vals) / len(vals), 2),
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
    Users ask about UE (User Equipment) positions, throughput, latency,
    packet error rate, PRB usage, buffer status, serving cell, and per-cell
    RRC connection / active UE counts, in Korean or English. A UE ID is a
    number mentioned in the question (e.g. "UE 3", "UE12"). A cell ID is a
    number 1-8 referring to a base station (e.g. "cell 2", "셀 2",
    "기지국 2"). Use the available tools to query InfluxDB and answer the
    question accurately and concisely in Korean."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="사용자 질문에 대한 한국어 답변")


react_agent = dspy.ReAct(
    UELocationQA,
    tools=[
        get_actual_position,
        compare_position,
        get_ue_throughput,
        get_cell_connections,
        get_ue_latency,
        get_ue_packet_error_rate,
        get_ue_prb_usage,
        get_cell_prb_usage,
        get_cell_active_ue_count,
        get_serving_cell,
        get_ue_buffer_size,
    ],
    max_iters=5,
)


def run_query(q: str):
    result = react_agent(question=q)

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


def main():
    print("=" * 60)
    print("네트워크 모니터링 Agent (DSPy ReAct + WCL + 7 KPM tools)")
    print("=" * 60)
    print("자연어로 질문하세요. 예: 'UE 3 지연시간과 PRB 사용량 알려줘'")
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
