#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raw-Input LLM Baseline (진짜 "Tool 없이 알아서" 버전)
======================================================
기존 pure_llm_baseline.py의 문제: WCL 알고리즘이 이미 걸러놓은 입력
(시간평균 + -20dB 필터 + 상위 6개 선별)을 그대로 LLM에게 줬기 때문에,
LLM은 "WCL이 골라준 재료로 마지막 가중평균 한 스텝"만 하면 됐음.
이건 "Tool 없이 LLM 혼자"를 테스트한 게 아니라 "WCL의 전처리 + LLM의
계산 능력"을 테스트한 것에 가까움.

이 스크립트는 그 세 가지 전처리(시간평균/필터/선별)를 전부 걷어내고,
7개 기지국 x 18개 시점의 원시 SINR을 통째로 준다. 어떤 기지국을 쓸지,
어떻게 시간 축을 다룰지도 전부 LLM이 알아서 판단해야 한다.

WCL과 비교할 때 이 버전이 훨씬 나쁘게 나올 가능성이 높은데, 그게 나쁜
게 아니라 오히려 "Tool 없이는 전처리·특징선택·계산을 전부 스스로 해야
하는 게 왜 어려운가"를 훨씬 정직하게 보여주는 것.

실행:
    python pure_llm_baseline.py
"""

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')

import os, json, math, re, sys
sys.path.insert(0, str(Path(__file__).parent))

from influxdb_client import InfluxDBClient
import dspy

INFLUX_URL    = os.getenv("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "mytoken")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "oran")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "ns3")

client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=60_000)
query_api = client.query_api()

GNB_COORDS = {
    "2": (2000.0, 2000.0), "3": (3000.0, 2000.0), "4": (2500.0, 2866.0),
    "5": (1500.0, 2866.0), "6": (1000.0, 2000.0), "7": (1500.0, 1134.0),
    "8": (2500.0, 1134.0),
}
CELL_IDS = ["2", "3", "4", "5", "6", "7", "8"]
UE_LIST = [str(i) for i in range(1, 50)]


def run_flux(flux: str):
    try:
        tables = query_api.query(flux)
        rows = []
        for t in tables:
            for r in t.records:
                rows.append({"time": r.get_time().isoformat() if r.get_time() else None,
                             "value": r.get_value()})
        return rows
    except Exception as e:
        print(f"[run_flux 오류] {e}")
        return []


def get_raw_sinr_and_true_position(ue_id: str):
    """
    전처리 없이: 7개 기지국 전부, 18개 시점 SINR 원시값을 시간순으로 그대로 반환.
    필터링(-20dB)도, 시간평균도, 상위 6개 선별도 하지 않는다 -- 그건 WCL의
    설계 선택이지 "raw data"가 아니기 때문.
    """
    ue_padded = ue_id.zfill(5)
    raw_series = {}  # cell_id -> [sinr_t0, sinr_t1, ..., sinr_t17]

    for cell_id in CELL_IDS:
        meas = f"SINR_cell_{cell_id}_ue_{ue_padded}_cp"
        rows = run_flux(f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
  |> sort(columns: ["_time"])
''')
        vals = [round(float(r["value"]), 2) for r in rows if r.get("value") is not None]
        if vals:
            raw_series[cell_id] = vals  # 값이 없는 기지국은 그냥 제외 (관측 자체가 없는 것)

    if not raw_series:
        return None, None

    pos = {}
    tables = query_api.query(f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "UE_Position")
  |> filter(fn: (r) => r.ue == "{str(int(ue_id))}")
  |> filter(fn: (r) => r._field == "x" or r._field == "y")
  |> last()
''')
    for t in tables:
        for r in t.records:
            pos[r.get_field()] = r.get_value()
    if "x" not in pos or "y" not in pos:
        return None, None

    return raw_series, (pos["x"], pos["y"])


PROMPT_TEMPLATE = """You are analyzing raw 5G network telemetry with no preprocessing applied.

UE {ue_id} recorded SINR (dB) from nearby base stations at 18 time points
(0.1s to 1.8s, in order). Not every base station necessarily has usable
signal quality, and not every reading is equally reliable -- you must decide
which base stations and which time points to trust.

All base station coordinates (x, y) in meters:
{bs_coords_str}

Raw SINR time series per base station (dB), 18 samples each, in time order:
{sinr_series_str}

Using only this raw data, estimate UE {ue_id}'s (x, y) coordinate in meters.
You decide what preprocessing, filtering, or weighting (if any) is appropriate.
Do not explain your reasoning. Respond with ONLY the final coordinate in
the exact format:
x, y
"""

COORD_PATTERN = re.compile(r"(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)")


def build_prompt(ue_id, raw_series):
    bs_coords_str = "\n".join(
        f"  BS {cid}: ({GNB_COORDS[cid][0]:.1f}, {GNB_COORDS[cid][1]:.1f})"
        for cid in raw_series
    )
    sinr_series_str = "\n".join(
        f"  BS {cid}: {vals}" for cid, vals in raw_series.items()
    )
    return PROMPT_TEMPLATE.format(
        ue_id=ue_id, bs_coords_str=bs_coords_str, sinr_series_str=sinr_series_str,
    )


def query_llm(lm, prompt: str):
    response = lm(prompt)
    text = response[0] if isinstance(response, list) else response
    match = COORD_PATTERN.search(text)
    if not match:
        return None, text
    return (float(match.group(1)), float(match.group(2))), text


OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
LLAMA_MODEL = os.getenv("LLAMA_MODEL", "qwen2.5:14b-instruct-q4_K_M")

lm = dspy.LM(
    model=f"ollama_chat/{LLAMA_MODEL}",
    api_base=OLLAMA_HOST,
    api_key="ollama",
    temperature=0.1,
    max_tokens=256,
    cache=False,
)


def main():
    print("Raw-Input LLM 베이스라인 (전처리 없음, Tool 없음)")
    print("=" * 70)

    results = []
    parse_failures = []

    for ue_id in UE_LIST:
        raw_series, true_pos = get_raw_sinr_and_true_position(ue_id)
        if raw_series is None:
            continue

        prompt = build_prompt(ue_id, raw_series)
        est, raw_text = query_llm(lm, prompt)

        if est is None:
            parse_failures.append({"ue_id": ue_id, "raw_output": raw_text})
            print(f"UE {ue_id:>3}: 파싱 실패 -> {raw_text[:80]!r}")
            continue

        err = math.hypot(est[0] - true_pos[0], est[1] - true_pos[1])
        results.append({
            "ue_id": ue_id,
            "estimated_x": est[0], "estimated_y": est[1],
            "actual_x": true_pos[0], "actual_y": true_pos[1],
            "error_m": round(err, 1),
        })
        print(f"UE {ue_id:>3}: 추정({est[0]:7.1f}, {est[1]:7.1f}) "
              f"실제({true_pos[0]:7.1f}, {true_pos[1]:7.1f}) 오차={err:6.1f}m")

    if not results:
        print("\n유효한 결과 없음.")
        return

    errs = [r["error_m"] for r in results]
    n = len(errs)
    mean_err = sum(errs) / n
    rmse = math.sqrt(sum(e ** 2 for e in errs) / n)

    print("\n" + "=" * 70)
    print("=== Raw-Input LLM 결과 ===")
    print(f"N={n}, parse_failures={len(parse_failures)}, "
          f"mean={mean_err:.1f}, RMSE={rmse:.1f}, min={min(errs):.1f}, max={max(errs):.1f}")

    output = {
        "summary": {
            "total": n, "parse_failures": len(parse_failures),
            "mean_error_m": round(mean_err, 1), "rmse_m": round(rmse, 1),
            "min_error_m": round(min(errs), 1), "max_error_m": round(max(errs), 1),
        },
        "results": results, "parse_failures": parse_failures,
    }
    with open("pure_llm_results.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print("\n결과 저장: pure_llm_results.json")


if __name__ == "__main__":
    main()