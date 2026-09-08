#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_measurements.py
========================
InfluxDB 버킷에 실제로 어떤 measurement가 있는지 확인해서
measurements.txt 파일로 저장한다.

실행 (Windows):
    python check_measurements.py
"""

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')

import os
from influxdb_client import InfluxDBClient

INFLUX_URL    = os.getenv("INFLUX_URL",    "http://192.168.93.4:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "mytoken")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "oran")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "ns3")

OUTPUT_PATH = Path("measurements.txt")


def main():
    print(f"연결 대상: {INFLUX_URL} (org={INFLUX_ORG}, bucket={INFLUX_BUCKET})")

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query_api = client.query_api()

    flux = f'''
import "influxdata/influxdb/schema"
schema.measurements(bucket: "{INFLUX_BUCKET}")
'''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        print(f"!! 쿼리 실패: {e}")
        client.close()
        return

    names = []
    for table in tables:
        for record in table.records:
            names.append(record.get_value())

    client.close()

    names.sort()

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(f"버킷: {INFLUX_BUCKET}\n")
        f.write(f"총 measurement 개수: {len(names)}\n")
        f.write("=" * 50 + "\n")
        for n in names:
            f.write(n + "\n")

    print(f"총 {len(names)}개 measurement 발견")
    print(f"결과 저장: {OUTPUT_PATH.resolve()}")


if __name__ == "__main__":
    main()