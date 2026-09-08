#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baseline: 단순 centroid (균등 가중) vs WCL 비교
- 단순 centroid: SINR ≥ -20 dB 인 모든 기지국 좌표의 산술평균
- WCL: SINR을 linear로 변환한 가중 중심점
"""

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')

import os, json, math, sys
sys.path.insert(0, str(Path(__file__).parent))

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
CELL_IDS = ["2", "3", "4", "5", "6", "7", "8"]


def run_flux(flux):
    try:
        tables = query_api.query(flux)
        rows = []
        for t in tables:
            for r in t.records:
                row = {"time": r.get_time().isoformat() if r.get_time() else None,
                       "field": r.get_field(), "value": r.get_value()}
                row.update({k: v for k, v in r.values.items()
                             if k not in ("_start","_stop","_time","_value",
                                          "_field","_measurement","result","table")})
                rows.append(row)
        return rows
    except:
        return []


def get_sinr_map(ue_id):
    ue_padded = ue_id.zfill(5)
    sinr_map = {}
    for cell_id in CELL_IDS:
        meas = f"SINR_cell_{cell_id}_ue_{ue_padded}_cp"
        rows = run_flux(f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "{meas}")
  |> filter(fn: (r) => r._field == "value")
  |> last()
''')
        if rows:
            val = rows[0].get("value")
            if val is not None:
                sinr_map[cell_id] = float(val)
    return sinr_map


def get_actual(ue_id):
    rows = run_flux(f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "UE_Position")
  |> filter(fn: (r) => r.ue == "{ue_id}")
  |> filter(fn: (r) => r._field == "x" or r._field == "y")
  |> last()
''')
    pos = {}
    for r in rows:
        pos[r.get("field")] = r.get("value")
    if "x" in pos and "y" in pos:
        return (pos["x"], pos["y"])
    return None


def estimate_simple(sinr_map):
    """단순 centroid: SINR ≥ -20 인 셀들의 좌표 산술평균"""
    valid = [cid for cid, s in sinr_map.items() if s >= -20]
    if len(valid) < 3:
        return None
    n = len(valid)
    x = sum(GNB_COORDS[cid][0] for cid in valid) / n
    y = sum(GNB_COORDS[cid][1] for cid in valid) / n
    return (x, y)


def estimate_wcl(sinr_map):
    """WCL: SINR linear 변환 가중 중심점"""
    valid = [(cid, s) for cid, s in sinr_map.items() if s >= -20]
    valid.sort(key=lambda x: -x[1])
    valid = valid[:6]
    if len(valid) < 3:
        return None
    w = [(cid, 10 ** (s/10)) for cid, s in valid]
    total = sum(wi for _, wi in w)
    x = sum(wi * GNB_COORDS[cid][0] for cid, wi in w) / total
    y = sum(wi * GNB_COORDS[cid][1] for cid, wi in w) / total
    return (x, y)


print("Baseline (단순 centroid) vs WCL 비교")
print("=" * 70)

simple_errs = []
wcl_errs = []

for ue_id in [str(i) for i in range(1, 50)]:
    sinr_map = get_sinr_map(ue_id)
    actual = get_actual(ue_id)
    if not sinr_map or not actual:
        continue

    simple = estimate_simple(sinr_map)
    wcl = estimate_wcl(sinr_map)
    if simple is None or wcl is None:
        continue

    e_simple = math.sqrt((simple[0]-actual[0])**2 + (simple[1]-actual[1])**2)
    e_wcl    = math.sqrt((wcl[0]-actual[0])**2 + (wcl[1]-actual[1])**2)

    simple_errs.append(e_simple)
    wcl_errs.append(e_wcl)

    print(f"UE {ue_id:>3}: simple={e_simple:6.1f}m, WCL={e_wcl:6.1f}m, "
          f"개선={e_simple-e_wcl:+7.1f}m")

print("\n" + "=" * 70)

n = len(simple_errs)
if n == 0:
    print("유효한 결과 없음 (SINR 또는 실제 위치 데이터를 찾지 못함).")
    sys.exit(1)

print(f"{'method':>20} {'count':>8} {'mean':>10} {'RMSE':>10}")
print("-" * 60)

print(f"{'단순 centroid':>20} {n:>8} {sum(simple_errs)/n:>10.1f} "
      f"{math.sqrt(sum(e**2 for e in simple_errs)/n):>10.1f}")
print(f"{'WCL (제안)':>20} {n:>8} {sum(wcl_errs)/n:>10.1f} "
      f"{math.sqrt(sum(e**2 for e in wcl_errs)/n):>10.1f}")

improvement = (sum(simple_errs) - sum(wcl_errs)) / sum(simple_errs) * 100
print(f"\n평균 오차 개선율: {improvement:.1f}%")