#!/usr/bin/env python3
"""以明確的 argv 清單執行品質檢查；任何必要檢查失敗即失敗。"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REDACT = re.compile(r"(?i)(gh[pousr]_[a-z0-9_]{12,}|glpat-[a-z0-9_-]{12,}|sk-[a-z0-9_-]{16,}|(token|password|secret|api[_-]?key)\s*[:=]\s*[^\s]+)")

def run_gate(root: Path, mode: str, write_report=True) -> tuple[int, dict]:
    # 受管理的必要檢查永遠先載入；Local 設定只能追加檢查。
    default_conf = root / '.paef' / 'quality.json'
    local_conf = root / '.paef' / 'quality.local.json'
    if not default_conf.is_file():
        return 2, {'status': 'FAIL', 'error': 'Missing required quality.json', 'checks': []}
    try:
        base = json.loads(default_conf.read_text(encoding='utf-8'))
        required = base['checks']
        if not isinstance(required, list) or not required:
            raise ValueError('required checks must be nonempty')
        names = [c.get('name') for c in required]
        if any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
            raise ValueError('required check names must be unique')
        extras = []
        if local_conf.exists():
            local = json.loads(local_conf.read_text(encoding='utf-8'))
            # v1.1 可使用 checks 複製原檔；v1.2 只相容未改動的重複必要檢查。
            extras = local.get('extra_checks', local.get('checks', []))
            if not isinstance(extras, list):
                raise ValueError('extra_checks must be a list')
        required_by_name = {c['name']: c for c in required}
        additions = []
        for item in extras:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                raise ValueError('extra check must have a name')
            name = item['name']
            if name in required_by_name:
                if item != required_by_name[name]:
                    raise ValueError(f'local config must not override required check: {name}')
                continue  # v1.1 的完整複製檔仍可沿用，未重複執行
            if any(x['name'] == name for x in additions):
                raise ValueError(f'duplicate extra check: {name}')
            additions.append(item)
        checks = required + additions
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return 2, {'status': 'FAIL', 'error': f'Invalid quality config: {exc}', 'checks': []}
    results=[]
    for entry in checks:
        if not isinstance(entry, dict):
            results.append({"name":"configuration","status":"FAIL","reason":"invalid check object"})
            continue
        name = str(entry.get("name", "unnamed"))
        when = entry.get("modes", ["quick", "full"])
        if not isinstance(when, list) or not when or not set(when) <= {"quick", "full"}:
            results.append({"name":name,"status":"FAIL","reason":"invalid modes"})
            continue
        if mode not in when:
            continue
        argv = entry.get("argv")
        if (not isinstance(argv, list) or not argv or
                any(not isinstance(x, str) or not x for x in argv)):
            results.append({"name":name,"status":"FAIL","reason":"Invalid argv"})
            continue
        cwd_setting = entry.get("cwd", ".")
        if not isinstance(cwd_setting, str):
            results.append({"name":name,"status":"FAIL","reason":"invalid cwd"})
            continue
        wd = (root / cwd_setting).resolve()
        if not wd.is_relative_to(root.resolve()):
            results.append({"name":name,"status":"FAIL","reason":"cwd outside project"})
            continue
        if not wd.is_dir():
            results.append({"name":name,"status":"FAIL","reason":"cwd missing"})
            continue
        timeout = entry.get("timeout_sec", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (1 <= timeout <= 3600):
            results.append({"name":name,"status":"FAIL","reason":"invalid timeout"})
            continue
        start = time.monotonic()
        try:
            cp = subprocess.run(argv, cwd=wd, capture_output=True, text=True,
                                errors="replace", timeout=timeout, shell=False,
                                env=os.environ.copy())
            result = "PASS" if cp.returncode == 0 else "FAIL"
            output = (cp.stdout + "\n" + cp.stderr)[-3000:]
            results.append({"name":name, "status":result,"exit_code":cp.returncode,
                            "duration_sec":round(time.monotonic()-start,2),
                            "output_tail":REDACT.sub('[REDACTED]',output)})
        except FileNotFoundError:
            results.append({"name":name,"status":"FAIL","reason":f"Executable not found: {argv[0]}"})
        except subprocess.TimeoutExpired:
            results.append({"name":name,"status":"FAIL","reason":f"Timeout after {timeout}s"})
    if not results:
        results.append({"name":"configuration","status":"FAIL","reason":f"No checks configured for mode {mode}"})
    status = "PASS" if all(c["status"]=="PASS" for c in results) else "FAIL"
    report={"schema_version":1,"status":status,"mode":mode,
            "timestamp_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "checks":results}
    if write_report:
        out = root / ".paef" / "reports"
        out.mkdir(parents=True,exist_ok=True)
        # 只紀錄檢查證據，不紀錄環境變數；報告預設被 .gitignore 忽略。
        (out / f"latest-{mode}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return (0 if status=="PASS" else 1),report

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--mode",choices=["quick","full"],default="quick")
    p.add_argument("--root",type=Path,default=Path.cwd())
    p.add_argument("--no-report",action="store_true")
    a=p.parse_args()
    rc,report=run_gate(a.root.resolve(),a.mode,not a.no_report)
    for c in report.get("checks",[]):
        print(f"[{c['status']}] {c['name']} {c.get('reason','')}")
    print(f"Quality gate: {report['status']} ({a.mode})")
    if report.get("error"):
        print(report["error"],file=sys.stderr)
    return rc
if __name__ == "__main__":
    sys.exit(main())
