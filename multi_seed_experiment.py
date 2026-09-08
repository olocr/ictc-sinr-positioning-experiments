#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
multi_seed_experiment.py (v3)
===============================
Windows에서 실행. 주요 변경사항:
  - InfluxDB 버킷 재생성 직후 Watchdog을 강제 재시작 (버킷 ID가 바뀌면서
    기존 Watchdog 연결이 끊겨 백그라운드 쓰기 스레드가 죽는 문제 대응)
  - NS-3 크래시(NS_FATAL/NS_ASSERT)는 결정론적이므로 같은 시드 재시도 대신
    대체 시드(seed+100, seed+200)로 전환
  - 데이터 적재를 고정 시간이 아니라 폴링으로 확인, 최종적으로도 안 나타나면
    해당 시드는 평가를 스킵하고 다음 시드로 진행 (ZeroDivisionError 방지)

사전 설치 (Windows): pip install paramiko
"""

import os
import sys
import re
import json
import time
import statistics
import subprocess
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')

try:
    import paramiko
except ImportError:
    raise SystemExit("paramiko가 필요합니다: pip install paramiko")

# ========= 설정 (환경에 맞게 반드시 확인/수정) =========
SEEDS = list(range(11, 21))  # 10개 시드 실행

VM_HOST = "192.168.93.4"     # docker ps에서 확인한 ns3 컨테이너의 실제 호스트 IP
VM_SSH_PORT = 2222           # docker ps의 "0.0.0.0:2222->22/tcp" 매핑값
VM_USER = "root"
VM_PASSWORD = "1234"         # 실제 비밀번호로 교체

NS3_PATH_IN_VM = "/workspace/ns3-mmwave-oran"
WATCHDOG_PATH_IN_VM = "/workspace/ns3-mmwave-oran/InfluxDB"

INFLUX_URL = os.getenv("INFLUX_URL", "http://192.168.93.4:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "mytoken")
INFLUX_ORG = os.getenv("INFLUX_ORG", "oran")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "ns3")

RESULTS_DIR = Path("multi_seed_results")
RESULTS_DIR.mkdir(exist_ok=True)


def ssh_connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(VM_HOST, port=VM_SSH_PORT, username=VM_USER,
                    password=VM_PASSWORD, timeout=30)
    return client


def start_watchdog_background(client: paramiko.SSHClient):
    """watchdog을 컨테이너 안에서 백그라운드로 띄움 (nohup + &)."""
    print("  Watchdog 시작 (백그라운드)...")
    cmd = (
        f"su - ns3user -c "
        f"'cd {WATCHDOG_PATH_IN_VM} && "
        f"nohup python3 sim_watcher_influx.py > /tmp/watchdog.log 2>&1 &'"
    )
    client.exec_command(cmd)
    time.sleep(3)
    print("  Watchdog 기동 완료 (로그: /tmp/watchdog.log, 컨테이너 내부)")


def kill_watchdog(client: paramiko.SSHClient):
    """Watchdog 프로세스를 강제 종료 (버킷 재생성 후 재시작 전 정리 단계)."""
    client.exec_command("pkill -f sim_watcher_influx.py")
    time.sleep(2)


def restart_watchdog(client: paramiko.SSHClient):
    """버킷이 재생성된 뒤, 새 버킷에 대한 새 연결을 맺도록 Watchdog을 완전히 재시작."""
    print("  Watchdog 재시작 중 (새 버킷에 대한 새 연결 필요)...")
    kill_watchdog(client)
    start_watchdog_background(client)


def check_watchdog_alive(client: paramiko.SSHClient) -> bool:
    stdin, stdout, stderr = client.exec_command("pgrep -f sim_watcher_influx.py")
    out = stdout.read().decode().strip()
    return bool(out)


def run_ns3_simulation(client: paramiko.SSHClient, seed: int, max_alt_tries: int = 2):
    """
    su - ns3user로 전환 후 시뮬레이션 실행. 완료까지 블로킹.
    NS-3/O-RAN 모듈 내부 버그(예: buffer.cc의 NS_ASSERT 실패)로 인한 크래시는
    결정론적이라 같은 시드로 재시도해도 100% 같은 지점에서 다시 크래시난다.
    따라서 실패 시 같은 시드를 반복하지 않고 seed+100, seed+200 같은 대체
    시드로 전환한다.

    Returns: (elapsed_sec, actual_seed_used) 또는 (None, None) 전부 실패 시.
    """
    candidates = [seed] + [seed + 100 * i for i in range(1, max_alt_tries + 1)]

    for attempt, try_seed in enumerate(candidates, start=1):
        cmd = (
            f"su - ns3user -c "
            f"'cd {NS3_PATH_IN_VM} && ./ns3 run \"scratch/scenario-three.cc --RngRun={try_seed}\"'"
        )
        print(f"  NS-3 실행 (요청 seed={seed}, 실제 시도 seed={try_seed}, 시도 {attempt}/{len(candidates)})...")
        t0 = time.time()
        stdin, stdout, stderr = client.exec_command(cmd, timeout=3600)
        exit_status = stdout.channel.recv_exit_status()
        out_text = stdout.read().decode(errors="ignore")
        err_text = stderr.read().decode(errors="ignore")
        elapsed = time.time() - t0

        log_path = RESULTS_DIR / f"ns3_log_seed{seed}_try{try_seed}.txt"
        log_path.write_text(out_text + "\n---STDERR---\n" + err_text, encoding="utf-8")

        if exit_status == 0:
            print(f"  NS-3 완료: {elapsed/60:.1f}분 (실제 사용 seed={try_seed})")
            return elapsed, try_seed

        print(f"  !! NS-3 실행 실패 (exit={exit_status}, seed={try_seed}). 로그: {log_path}")
        print(f"     stderr 마지막 300자: ...{err_text[-300:]}")

    print(f"  !! 요청 seed {seed}: 대체 시드({candidates})까지 모두 실패, 스킵")
    return None, None


def run_local_eval_script(script_name: str, out_path: Path) -> bool:
    print(f"  [로컬] {script_name} 실행...")
    result = subprocess.run([sys.executable, script_name], capture_output=True, text=True, timeout=1800)
    out_path.write_text(result.stdout + "\n---STDERR---\n" + result.stderr, encoding="utf-8")
    if result.returncode != 0:
        print(f"  !! {script_name} 실패. 로그: {out_path}")
        print(f"     stderr 마지막 500자: ...{result.stderr[-500:]}")
        return False
    print(f"  {script_name} 완료.")
    return True


def wait_for_data_ready(max_wait_sec: int = 300, poll_interval_sec: int = 15) -> bool:
    """
    고정 시간 대기 대신, ue_position 측정값이 실제로 InfluxDB에 나타날 때까지
    폴링한다.
    """
    from influxdb_client import InfluxDBClient

    print(f"  데이터 적재 확인 중 (최대 {max_wait_sec}초, {poll_interval_sec}초 간격 폴링)...")
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=30_000)
    query_api = client.query_api()

    elapsed = 0
    try:
        while elapsed < max_wait_sec:
            flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5y)
  |> filter(fn: (r) => r._measurement == "UE_Position")
  |> limit(n: 1)
'''
            try:
                tables = query_api.query(flux)
                n_rows = sum(len(t.records) for t in tables)
            except Exception:
                n_rows = 0

            if n_rows > 0:
                print(f"  데이터 확인됨 ({elapsed}초 경과). 안전을 위해 15초 더 대기 후 진행.")
                time.sleep(15)
                return True

            time.sleep(poll_interval_sec)
            elapsed += poll_interval_sec
            print(f"    ...아직 데이터 없음 ({elapsed}초 경과)")

        print(f"  !! {max_wait_sec}초 동안 데이터가 나타나지 않았습니다. Watchdog 상태를 확인하세요.")
        return False
    finally:
        client.close()


def clear_influxdb_bucket(ssh_client: paramiko.SSHClient):
    """
    버킷 삭제 후 재생성 (predicate delete보다 훨씬 빠르고 확실함).
    버킷을 재생성하면 내부 버킷 ID가 바뀌면서 기존 Watchdog 연결이 깨질 수
    있으므로, 재생성 직후 Watchdog을 반드시 재시작한다.
    """
    from influxdb_client import InfluxDBClient

    print("  InfluxDB 버킷 삭제 후 재생성 중...")
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=30_000)
    try:
        orgs_api = client.organizations_api()
        org_obj = orgs_api.find_organizations(org=INFLUX_ORG)[0]

        buckets_api = client.buckets_api()
        existing = buckets_api.find_bucket_by_name(INFLUX_BUCKET)
        if existing:
            buckets_api.delete_bucket(existing)

        buckets_api.create_bucket(bucket_name=INFLUX_BUCKET, org_id=org_obj.id)
        print("  완료 (버킷 재생성됨).")
    except Exception as e:
        print(f"  !! 실패: {e} -- 다음 시드 데이터와 섞일 수 있음. 수동 확인 필요.")
    finally:
        client.close()

    # 버킷이 바뀌었으니 Watchdog을 새로 연결시켜야 함
    restart_watchdog(ssh_client)


def parse_wcl_output(text: str):
    result = {}
    for label, key in [("단순 centroid", "centroid"), (r"WCL \(제안\)", "wcl")]:
        m = re.search(rf"{label}\s+(\d+)\s+([\d.]+)\s+([\d.]+)", text)
        if m:
            result[key] = {"n": int(m.group(1)), "mean_error_m": float(m.group(2)),
                           "rmse_m": float(m.group(3))}
    return result


def main():
    print("=" * 60)
    print(f"다중 시드 실험 시작: {len(SEEDS)}개 시드")
    print(f"예상 NS-3 소요시간: 약 {28 * len(SEEDS)}분 (+ 평가 시간)")
    print("=" * 60)

    client = ssh_connect()
    start_watchdog_background(client)
    if not check_watchdog_alive(client):
        print("!! Watchdog이 기동되지 않은 것 같습니다. 수동 확인 후 재시도하세요.")
        return

    all_wcl, all_centroid, all_pure_llm = [], [], []

    try:
        for seed in SEEDS:
            print(f"\n########## SEED {seed} ##########")

            if not check_watchdog_alive(client):
                print("!! Watchdog이 죽어있습니다. 재기동...")
                start_watchdog_background(client)

            elapsed, actual_seed = run_ns3_simulation(client, seed)
            if elapsed is None:
                continue

            print("  Watchdog 데이터 적재 대기 (폴링 방식)...")
            data_ready = wait_for_data_ready(max_wait_sec=300, poll_interval_sec=15)
            if not data_ready:
                print(f"  !! seed {seed}(실제 사용 {actual_seed}): 데이터 미확인, 평가 스킵하고 다음 시드로")
                clear_influxdb_bucket(client)
                continue

            wcl_out = RESULTS_DIR / f"wcl_seed{seed}.txt"
            if run_local_eval_script("baseline_eval.py", wcl_out):
                parsed = parse_wcl_output(wcl_out.read_text(encoding="utf-8"))
                if "wcl" in parsed:
                    all_wcl.append((seed, parsed["wcl"]["rmse_m"]))
                if "centroid" in parsed:
                    all_centroid.append((seed, parsed["centroid"]["rmse_m"]))

            if run_local_eval_script("pure_llm_baseline.py", RESULTS_DIR / f"purellm_log_seed{seed}.txt"):
                pjson = Path("pure_llm_results.json")
                if pjson.exists():
                    data = json.loads(pjson.read_text(encoding="utf-8"))
                    all_pure_llm.append((seed, data["summary"]["rmse_m"]))
                    pjson.replace(RESULTS_DIR / f"pure_llm_seed{seed}.json")
                else:
                    print(f"  !! pure_llm_results.json을 못 찾았습니다 (현재 폴더: {Path.cwd()}).")

            clear_influxdb_bucket(client)
            print(f"########## SEED {seed} 완료 ##########")
    finally:
        client.close()

    def summarize(name, pairs):
        if not pairs:
            print(f"\n{name}: 결과 없음")
            return
        vals = [v for _, v in pairs]
        mean = statistics.mean(vals)
        std = statistics.stdev(vals) if len(vals) > 1 else 0.0
        print(f"\n=== {name} ({len(vals)} seeds) ===")
        for s, v in pairs:
            print(f"  seed {s}: RMSE={v}")
        print(f"  --> RMSE = {mean:.1f} ± {std:.1f} m (min={min(vals):.1f}, max={max(vals):.1f})")

    print("\n" + "=" * 60 + "\n전체 결과 요약\n" + "=" * 60)
    summarize("Centroid", all_centroid)
    summarize("WCL (time-avg)", all_wcl)
    summarize("Pure-LLM", all_pure_llm)

    (RESULTS_DIR / "summary.json").write_text(json.dumps({
        "centroid": all_centroid, "wcl": all_wcl, "pure_llm": all_pure_llm,
    }, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {RESULTS_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()