#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')

import json, math, sys
sys.path.insert(0, str(Path(__file__).parent))

from llama_influx_agent import estimate_position, get_actual_position

UE_LIST = [str(i) for i in range(1, 50)]

results = []
errors = []

print("전체 UE 위치 추정 배치 실행")
print("=" * 60)

for ue_id in UE_LIST:
    try:
        est = json.loads(estimate_position(ue_id))
        act = json.loads(get_actual_position(ue_id))

        if "error" in est or "error" in act:
            errors.append({"ue_id": ue_id, "reason": est.get("error") or act.get("error")})
            print(f"UE {ue_id:>3}: 스킵 - {est.get('error') or act.get('error')}")
            continue

        dx = est["estimated_x"] - act["actual_x"]
        dy = est["estimated_y"] - act["actual_y"]
        err = math.sqrt(dx**2 + dy**2)

        results.append({
            "ue_id": ue_id,
            "estimated_x": est["estimated_x"],
            "estimated_y": est["estimated_y"],
            "actual_x": act["actual_x"],
            "actual_y": act["actual_y"],
            "error_m": round(err, 1),
            "cells_used": est.get("cells_used", []),
        })
        print(f"UE {ue_id:>3}: 추정({est['estimated_x']:7.1f}, {est['estimated_y']:7.1f}) "
              f"실제({act['actual_x']:7.1f}, {act['actual_y']:7.1f}) 오차={err:6.1f}m")

    except Exception as e:
        errors.append({"ue_id": ue_id, "reason": str(e)})
        print(f"UE {ue_id:>3}: 오류 - {e}")

if results:
    errs = [r["error_m"] for r in results]
    rmse = math.sqrt(sum(e**2 for e in errs) / len(errs))
    mean_err = sum(errs) / len(errs)

    print("\n" + "=" * 60)
    print("결과 통계")
    print("=" * 60)
    print(f"  성공한 UE 수:  {len(results)}개")
    print(f"  실패한 UE 수:  {len(errors)}개")
    print(f"  평균 오차:     {mean_err:.1f} m")
    print(f"  RMSE:          {rmse:.1f} m")
    print(f"  최소 오차:     {min(errs):.1f} m")
    print(f"  최대 오차:     {max(errs):.1f} m")

    output = {
        "summary": {
            "total": len(results),
            "failed": len(errors),
            "mean_error_m": round(mean_err, 1),
            "rmse_m": round(rmse, 1),
            "min_error_m": round(min(errs), 1),
            "max_error_m": round(max(errs), 1),
        },
        "results": results,
        "errors": errors,
    }

    with open("batch_results.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n결과 저장: batch_results.json")