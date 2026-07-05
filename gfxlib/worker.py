"""gfxlib.worker — detached 캡처 워커 CLI.

용법: python3 -m gfxlib.worker --job <job.json>

job.json 스키마:
  {"kind": "live"|"ss"|"burst",
   "rom": "<rom 경로>",            # live/burst 필수
   "out_dir": "<frame_dir>",       # 필수
   "keyseq": "key[:hold[:wait]],..." ,  # dump_screen.py 문법 (초 단위)
   "boot": 11.0,                   # 부팅 대기(초)
   "freeze_at": 0,                 # live: 마지막 입력 후 N초 뒤 정지·덤프
   "burst": 0,                     # burst: 덤프 횟수 / live: 스크린샷-only 횟수
   "burst_int": 0.5,               # 버스트 간격(초)
   "burst_start": 0,               # 버스트 시작 전 대기(초)
   "ss_path": "<state.ss>",        # ss 필수
   "deadline_s": 0,                # 0/생략 = 무제한. 초과 시 FAILED.json
   # 선택 오버라이드(게임 무관 실행환경):
   "mgba": "/usr/games/mgba", "display": ":99", "gdb_port": 2345,
   "shot": false}                  # ss: PNG래핑이면 screenshot.png 추가

동작(원본 dump_screen.py 시맨틱 복사-추출):
  live/burst: Xvfb(부재 시 기동+기록) + mgba -3 -g <rom> + xdotool 키시퀀스
              → GdbClient 덤프 → capture.write_frame.
  burst 는 bd_NN.png + bd_NN_<region>.bin 기록 후 마지막 프레임을
  대표 프레임으로 write_frame(완료 마커 capture.json).
  ss: capture.parse_ss → write_frame (에뮬레이터 불필요).

정리 규율: 자기가 띄운 프로세스 pid 만 <out_dir>/.pids 에 기록하고,
/proc/<pid>/cmdline 마커 검증 후 TERM→KILL. 광역 pkill 절대 금지.
deadline_s 초과 시 자식 정리 후 FAILED.json(사유) 기록.
stdout 은 최종 1줄 — 로그는 <out_dir>/worker.log.
"""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

from . import capture

WORKER_VERSION = "1.0.0"

_DEF = {"keyseq": "", "boot": 11.0, "freeze_at": 0.0, "burst": 0,
        "burst_int": 0.5, "burst_start": 0.0, "deadline_s": 0.0,
        "mgba": "/usr/games/mgba", "display": ":99", "gdb_port": 2345,
        "shot": False}


class DeadlineExceeded(Exception):
    pass


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Log:
    """<out_dir>/worker.log 전용 로거 (stdout 오염 금지)."""

    def __init__(self, out_dir):
        self.path = os.path.join(out_dir, "worker.log")

    def __call__(self, msg):
        with open(self.path, "a") as f:
            f.write("[%s] %s\n" % (_now(), msg))


# ── pid 원장(.pids): 자기 자식만 정리 ───────────────────────────────────

def _pids_path(out_dir):
    return os.path.join(out_dir, ".pids")


def register_pid(out_dir, pid, marker):
    """자기가 띄운 프로세스 기록. marker 는 /proc cmdline 검증용 부분문자열."""
    with open(_pids_path(out_dir), "a") as f:
        f.write("%d\t%s\n" % (pid, marker))


def _proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        return None


def cleanup_pids(out_dir, log=None):
    """<out_dir>/.pids 의 pid 들만 정리. cmdline 에 marker 가 있어야만 kill.

    검증 실패(이미 죽음/pid 재사용으로 다른 프로세스)는 건드리지 않는다.
    """
    path = _pids_path(out_dir)
    if not os.path.exists(path):
        return []
    killed = []
    with open(path) as f:
        entries = [ln.rstrip("\n").split("\t", 1) for ln in f if ln.strip()]
    for ent in entries:
        try:
            pid = int(ent[0])
        except ValueError:
            continue
        marker = ent[1] if len(ent) > 1 else ""
        cmdline = _proc_cmdline(pid)
        if cmdline is None or (marker and marker not in cmdline):
            continue  # 이미 종료됐거나 pid 재사용 — 절대 kill 금지
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        deadline = time.time() + 5
        while time.time() < deadline and _proc_cmdline(pid) is not None:
            time.sleep(0.1)
        if _proc_cmdline(pid) is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        killed.append(pid)
        if log:
            log("cleanup pid=%d (%s)" % (pid, marker))
    try:
        os.remove(path)
    except OSError:
        pass
    return killed


# ── Xvfb: 떠 있으면 재사용, 아니면 자기가 기동+기록 ─────────────────────

def ensure_display(out_dir, display, log):
    """display(예 ':99') 가용 보장. 이미 떠 있으면 재사용(정리 대상 아님)."""
    num = display.lstrip(":").split(".")[0]
    xsock = "/tmp/.X11-unix/X%s" % num
    if os.path.exists(xsock):
        log("Xvfb %s 재사용" % display)
        return
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "640x480x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    register_pid(out_dir, proc.pid, "Xvfb %s" % display)
    log("Xvfb %s 기동 pid=%d" % (display, proc.pid))
    for _ in range(50):
        if os.path.exists(xsock):
            return
        time.sleep(0.1)
    raise RuntimeError("Xvfb %s 기동 실패" % display)


# ── live/burst 경로 (dump_screen.py 시맨틱) ─────────────────────────────

def _sh(cmd, display):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          env=dict(os.environ, DISPLAY=display))


def run_live(job, log):
    out = job["out_dir"]
    display = job["display"]
    rom = job["rom"]
    ensure_display(out, display, log)

    env = dict(os.environ, SDL_AUDIODRIVER="dummy", DISPLAY=display)
    proc = subprocess.Popen([job["mgba"], "-3", "-g", rom], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    register_pid(out, proc.pid, os.path.basename(job["mgba"]))
    log("mgba pid=%d rom=%s" % (proc.pid, rom))

    cli = capture.GdbClient(port=job["gdb_port"])
    try:
        cli.drain()
        cli.cont()
        time.sleep(2)
        win = _sh("xdotool search --class mgba | head -1", display).stdout.strip()
        if not win:
            raise RuntimeError("mgba 창 없음")
        _sh("xdotool windowfocus %s" % win, display)

        def key(k, hold=0.35, wait=0.8):
            _sh("xdotool keydown --window %s %s" % (win, k), display)
            time.sleep(hold)
            _sh("xdotool keyup --window %s %s" % (win, k), display)
            time.sleep(wait)

        time.sleep(float(job["boot"]))  # boot
        _sh("import -window root %s/step0_boot.png" % out, display)
        keyseq = job["keyseq"]
        if keyseq:
            for i, tok in enumerate(keyseq.split(",")):
                parts = tok.split(":")
                k = parts[0]
                hold = float(parts[1]) if len(parts) > 1 else 0.35
                wait = float(parts[2]) if len(parts) > 2 else 0.8
                key(k, hold, wait)
                _sh("import -window root %s/step%d_%s.png" % (out, i + 1, k),
                    display)

        meta = {"mode": job["kind"], "rom": rom, "rom_md5": _md5_file(rom),
                "keyseq": keyseq, "ss_sha1": None, "boot": float(job["boot"]),
                "backend": "mgba-gdb", "started_at": job["_started_at"],
                "worker_version": WORKER_VERSION}

        if job["kind"] == "burst":
            # 매 간격 일시정지→스샷+덤프→재개 (BURST_DUMP=1 경로)
            n = int(job["burst"])
            bint = float(job["burst_int"])
            start = float(job["burst_start"])
            if start > 0:
                time.sleep(start)
            regions = {}
            for b in range(n):
                time.sleep(bint)
                cli.halt(settle=0.15)
                _sh("import -window root %s/bd_%02d.png" % (out, b), display)
                regions = {}
                for name, addr, ln in capture.REGIONS:
                    data = cli.read_mem(addr, ln)
                    with open("%s/bd_%02d_%s.bin" % (out, b, name), "wb") as f:
                        f.write(data)
                    regions[name] = data
                cli.resume()
                log("burst %d/%d" % (b + 1, n))
            meta["finished_at"] = _now()
            meta["burst"] = n
            # 마지막 버스트 프레임 = 대표 프레임
            return capture.write_frame(out, regions, meta)

        # kind == live
        freeze_at = float(job["freeze_at"])
        if freeze_at > 0:
            time.sleep(freeze_at)
        else:
            # 버스트: 입력 없이 N회 timed 스크린샷
            burst = int(job["burst"])
            bint = float(job["burst_int"])
            for b in range(burst):
                time.sleep(bint)
                _sh("import -window root %s/burst_%02d.png" % (out, b), display)

        cli.halt(settle=0.3)
        _sh("import -window root %s/frozen.png" % out, display)
        with open(os.path.join(out, "regs.txt"), "w") as f:
            f.write(cli.regs() + "\n")
        regions = {}
        for name, addr, ln in capture.REGIONS + (capture.IWRAM_REGION,):
            regions[name] = cli.read_mem(addr, ln)
            log("%s: %#x bytes" % (name, ln))
        meta["finished_at"] = _now()
        return capture.write_frame(out, regions, meta)
    finally:
        cli.close()
        cleanup_pids(out, log)


# ── ss 경로 (에뮬레이터 불필요) ─────────────────────────────────────────

def run_ss(job, log):
    out = job["out_dir"]
    ss_path = job["ss_path"]
    regions = capture.parse_ss(ss_path)
    meta = {"mode": "ss", "rom": job.get("rom"),
            "rom_md5": _md5_file(job["rom"]) if job.get("rom") else None,
            "keyseq": None, "ss_sha1": _sha1_file(ss_path), "boot": None,
            "backend": "ss", "started_at": job["_started_at"],
            "ss_path": ss_path, "worker_version": WORKER_VERSION}
    if job.get("shot"):
        try:
            from PIL import Image
            Image.open(ss_path).convert("RGB").save(
                os.path.join(out, "screenshot.png"))
        except Exception as e:  # PNG래핑이 아니면 스킵
            log("screenshot 실패: %s" % e)
    meta["finished_at"] = _now()
    log("ss parsed: %s" % ss_path)
    return capture.write_frame(out, regions, meta)


# ── 엔트리 ───────────────────────────────────────────────────────────────

def _write_failed(out_dir, reason, job):
    doc = {"error": reason, "kind": job.get("kind"), "at": _now(),
           "worker_version": WORKER_VERSION}
    final = os.path.join(out_dir, "FAILED.json")
    tmp = final + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.rename(tmp, final)
    return final


def load_job(path):
    with open(path) as f:
        raw = json.load(f)
    job = dict(_DEF)
    job.update(raw)
    if "kind" not in job or job["kind"] not in ("live", "ss", "burst"):
        raise ValueError("job.kind 는 live|ss|burst 필수")
    if "out_dir" not in job:
        raise ValueError("job.out_dir 필수")
    if job["kind"] == "ss":
        if not job.get("ss_path"):
            raise ValueError("kind=ss 는 ss_path 필수")
    else:
        if not job.get("rom"):
            raise ValueError("kind=%s 는 rom 필수" % job["kind"])
    return job


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gfxlib.worker",
                                 description="detached 캡처 워커")
    ap.add_argument("--job", required=True, help="job.json 경로")
    args = ap.parse_args(argv)

    job = load_job(args.job)
    out = job["out_dir"]
    os.makedirs(out, exist_ok=True)
    log = Log(out)
    job["_started_at"] = _now()
    log("job start kind=%s" % job["kind"])

    deadline_s = float(job.get("deadline_s") or 0)
    if deadline_s > 0:
        def _on_alarm(signum, frame):
            raise DeadlineExceeded("deadline_s=%s 초과" % deadline_s)
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, deadline_s)

    try:
        if job["kind"] == "ss":
            marker = run_ss(job, log)
        else:
            marker = run_live(job, log)
        print("[OK] %s" % marker)
        return 0
    except DeadlineExceeded as e:
        cleanup_pids(out, log)
        p = _write_failed(out, str(e), job)
        log("FAILED: %s" % e)
        print("[FAIL] %s" % p)
        return 1
    except Exception as e:
        cleanup_pids(out, log)
        p = _write_failed(out, "%s: %s" % (type(e).__name__, e), job)
        log("FAILED: %s: %s" % (type(e).__name__, e))
        print("[FAIL] %s" % p)
        return 1
    finally:
        if deadline_s > 0:
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    sys.exit(main())
