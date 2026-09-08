#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')

import json, math, sys, os
from collections import defaultdict
from influxdb_client import InfluxDBClient

INFLUX_URL    = os.getenv("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "mytoken")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "oran")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "ns3")

client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
query_api = client.query_api()

GNB_COORDS = {
    "2": (2000.0, 2000.0), "3": (3000.0, 2000.0), "4": (2500.0, 2866.0),
    "5": (1500.0, 2866.0), "6": (1000.0, 2000.0), "7": (1500.0, 1134.0),
    "8": (2500.0, 1134.0),
}

def run_flux(flux):
    try:
        tables = query_api.query(flux)
        rows = []
        for t in tables:
            for r in t.records:
                row = {
                    "time":  r.get_time().isoformat() if r.get_time() else None,
                    "field": r.get_field(),
                    "value": r.get_value(),
                }
                row.update({k: v for k, v in r.values.items()
                             if k not in ("_start","_stop","_time","_value",
                                          "_field","_measurement","result","table")})
                rows.append(row)
        return rows
    except Exception as e:
        print(f"[run_flux 오류] {e}")
        return []

# ===== 디버그 테스트 =====
test = run_flux(f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "ue_position")
  |> filter(fn: (r) => r.ue_id == "1")
  |> filter(fn: (r) => r._field == "x" or r._field == "y")
  |> last()
''')
print(f"[DEBUG] ue_position UE1 샘플: {test[:2]}")
if not test:
    print("InfluxDB 연결 실패 또는 데이터 없음. .env 확인 필요.")
    sys.exit(1)

# ===== 전체 데이터 캐싱 =====
print("\n데이터 캐싱 중...")
cache_neighbor = {}
cache_actual   = {}

UE_LIST = [str(i) for i in range(1, 50)]

for ue_id in UE_LIST:
    ue_padded = ue_id.zfill(5)

    rows = run_flux(f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "sinr_neighbor_l3")
  |> filter(fn: (r) => r.ue_id == "{ue_padded}")
  |> filter(fn: (r) => r._field == "value")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 50)
''')
    cache_neighbor[ue_id] = rows

    rows2 = run_flux(f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "ue_position")
  |> filter(fn: (r) => r.ue_id == "{ue_id}")
  |> filter(fn: (r) => r._field == "x" or r._field == "y")
  |> last()
''')
    pos = {}
    for r in rows2:
        f = r.get("field")
        v = r.get("value")
        if f and v is not None:
            pos[f] = v
    if "x" in pos and "y" in pos:
        cache_actual[ue_id] = (pos["x"], pos["y"])

print(f"캐싱 완료: {len(cache_actual)}개 UE\n")

if len(cache_actual) == 0:
    print("실제 위치 데이터 없음. 종료.")
    sys.exit(1)

# ===== 추정 함수 =====
def estimate(ue_id, cutoff, top_n, power):
    neighbor_data = cache_neighbor.get(ue_id, [])
    if not neighbor_data:
        return None

    time_groups = defaultdict(dict)
    for row in neighbor_data:
        t = str(row.get("time", ""))[:19]
        serving_cid = str(row.get("cell_id", ""))
        key = (t, serving_cid)
        ncid = str(row.get("neighbor_cell_id", ""))
        val = row.get("value")
        if ncid and val is not None:
            time_groups[key][ncid] = float(val)

    if not time_groups:
        return None

    best_key = max(time_groups.keys(), key=lambda k: len(time_groups[k]))
    sinr_map = time_groups[best_key]

    valid = [(cid, sinr) for cid, sinr in sinr_map.items() if cid in GNB_COORDS]
    valid.sort(key=lambda x: -x[1])
    valid = [(cid, sinr) for cid, sinr in valid if sinr >= cutoff]
    valid = valid[:top_n]

    if len(valid) < 3:
        return None

    total_w = sum(sinr**power for _, sinr in valid)
    if total_w == 0:
        return None
    est_x = sum((sinr**power) * GNB_COORDS[cid][0] for cid, sinr in valid) / total_w
    est_y = sum((sinr**power) * GNB_COORDS[cid][1] for cid, sinr in valid) / total_w
    return est_x, est_y

# ===== 파라미터 조합 테스트 =====
cutoffs = [5, 10, 15, 20, 25, 30]
top_ns  = [3, 4, 5, 6]
powers  = [1, 2, 3]

print(f"{'cutoff':>8} {'top_n':>6} {'power':>6} {'success':>8} {'mean_err':>10} {'RMSE':>10}")
print("-" * 55)

best_rmse   = float('inf')
best_params = None

for cutoff in cutoffs:
    for top_n in top_ns:
        for power in powers:
            errs = []
            for ue_id in UE_LIST:
                if ue_id not in cache_actual:
                    continue
                result = estimate(ue_id, cutoff, top_n, power)
                if result is None:
                    continue
                ax, ay = cache_actual[ue_id]
                err = math.sqrt((result[0]-ax)**2 + (result[1]-ay)**2)
                errs.append(err)

            if len(errs) < 5:
                continue

            mean_e = sum(errs) / len(errs)
            rmse   = math.sqrt(sum(e**2 for e in errs) / len(errs))

            marker = " ◀ BEST" if rmse < best_rmse else ""
            if rmse < best_rmse:
                best_rmse   = rmse
                best_params = (cutoff, top_n, power)

            print(f"{cutoff:>8} {top_n:>6} {power:>6} {len(errs):>8} {mean_e:>10.1f} {rmse:>10.1f}{marker}")

print(f"\n최적 파라미터: cutoff={best_params[0]}, top_n={best_params[1]}, power={best_params[2]}")
print(f"최적 RMSE: {best_rmse:.1f}m")