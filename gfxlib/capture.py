"""gfxlib.capture — 캡처 계층: .ss 파서, GDB RSP 클라이언트, frame_dir 기록.

원본(복사-추출, 알고리즘 무변경):
  - ss_dump.py   : mGBA 세이브스테이트(.ss, PNG래핑 gbAs 청크) → 메모리 블록 추출
  - dump_screen.py : GDB RSP 패킷 인코딩/수신/드레인/청크 메모리 읽기

frame_dir 규약(기존 바이트호환):
  io.bin(0x400) palette.bin(0x400) vram.bin(0x18000) oam.bin(0x400)
  + 선택 iwram.bin(0x8000) wram.bin(0x40000) + *.png
  + capture.json (완료 마커 — 임시명 기록 후 os.rename 원자 커밋)

게임/프로젝트 특화 상수 없음 — 경로·포트는 전부 인자.
"""
import hashlib
import json
import os
import socket
import struct
import time
import zlib

# ── mGBA 세이브스테이트(.ss) 오프라인 파서 ──────────────────────────────
# gbAs 청크 = zlib 압축된 struct GBASerializedState(GBA, 0x61000).
# 헤더 0x400 뒤 메모리블록 연속:
#   io@0x400 palette@0x800 vram@0xC00 oam@0x18C00 iwram@0x19000 wram@0x21000
_SS_HDR = 0x400
_SS_BLK = [("io", 0x400), ("palette", 0x400), ("vram", 0x18000),
           ("oam", 0x400), ("iwram", 0x8000), ("wram", 0x40000)]
_SS_STATE_SIZE = 0x61000
_PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")


def read_gbas(path):
    """.ss 파일에서 직렬화 상태(raw) 바이트를 얻는다 (ss_dump.py read_gbas 동일).

    PNG래핑이면 gbAs 청크를 zlib 해제, 아니면 원시 상태로 간주해 앞 0x61000B.
    """
    d = open(path, "rb").read()
    if d[:8] != _PNG_MAGIC:
        # 압축 안 된 원시 상태거나 다른 포맷
        if len(d) >= _SS_STATE_SIZE:
            return d[:_SS_STATE_SIZE]
        raise ValueError("PNG도 원시상태도 아님: %s" % path)
    i = 8
    gbas = None
    while i < len(d):
        ln = struct.unpack(">I", d[i:i + 4])[0]
        typ = d[i + 4:i + 8]
        if typ == b"gbAs":
            gbas = zlib.decompress(d[i + 8:i + 8 + ln])
        i += 12 + ln
        if typ == b"IEND":
            break
    if gbas is None:
        raise ValueError("gbAs 청크 없음: %s" % path)
    return gbas


def parse_ss(path):
    """mGBA .ss 세이브스테이트 → 메모리 영역 dict (ss_dump.py 바이트호환).

    리턴: {"io","palette","vram","oam","iwram","wram"}: bytes
    (ss_dump.py 가 <outDir>/<name>.bin 으로 쓰는 바이트와 동일)
    """
    raw = read_gbas(path)
    regions = {}
    off = _SS_HDR
    for name, sz in _SS_BLK:
        regions[name] = raw[off:off + sz]
        off += sz
    return regions


# ── frame_dir 기록 ───────────────────────────────────────────────────────

def write_frame(out_dir, regions, meta):
    """regions(.bin)들을 기록한 뒤 capture.json 을 원자 기록한다.

    Args:
        out_dir: frame_dir 경로(없으면 생성).
        regions: {"io": bytes, "palette": bytes, ...} — 각각 <name>.bin 으로 기록.
        meta: capture.json 에 병합할 메타(mode/rom/rom_md5/keyseq/ss_sha1/boot/
              backend/started_at/finished_at/worker_version 등).
    Returns:
        capture.json 경로(str). capture.json 의 "regions" 는 name→sha1(hex).
    """
    os.makedirs(out_dir, exist_ok=True)
    sha = {}
    for name, data in regions.items():
        with open(os.path.join(out_dir, name + ".bin"), "wb") as f:
            f.write(data)
        sha[name] = hashlib.sha1(data).hexdigest()
    doc = dict(meta)
    doc["regions"] = sha
    final = os.path.join(out_dir, "capture.json")
    tmp = final + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, final)  # 원자 커밋 = 완료 마커
    return final


# ── GDB RSP 클라이언트 (dump_screen.py 함수 승격) ───────────────────────

# 라이브 덤프 기본 영역 (dump_screen.py REGIONS 동일)
REGIONS = (("io", 0x04000000, 0x400),
           ("palette", 0x05000000, 0x400),
           ("oam", 0x07000000, 0x400),
           ("vram", 0x06000000, 0x18000))

# 정지 후 최종 덤프에 추가되는 영역 (dump_screen.py 최종 덤프 튜플의 iwram)
IWRAM_REGION = ("iwram", 0x03000000, 0x8000)


def encode_packet(cmd):
    """RSP 패킷 인코딩: $<cmd>#<checksum2hex> (dump_screen.py send_packet 동일).

    체크섬 = sum(cmd 바이트) & 0xff.
    """
    b = cmd.encode()
    cs = sum(b) & 0xff
    return b'$' + b + b'#' + ('%02x' % cs).encode()


class GdbClient:
    """mgba -g GDB stub 용 최소 RSP 클라이언트 (dump_screen.py 승격).

    connect 재시도 내장. 소켓 프로토콜 시맨틱은 원본과 동일:
    send_packet/recv_packet/drain/read_mem(0x200 청크).
    """

    def __init__(self, port=2345, host="127.0.0.1", timeout=5,
                 retries=30, retry_wait=0.5):
        self.port = port
        self.host = host
        self.timeout = timeout
        self.sock = None
        for _ in range(retries):
            try:
                self.sock = socket.create_connection((host, port), timeout=1)
                break
            except OSError:
                time.sleep(retry_wait)
        if self.sock is None:
            raise ConnectionError("gdb stub 연결 실패: %s:%d" % (host, port))

    # -- 저수준 프로토콜 --------------------------------------------------
    def send_packet(self, cmd):
        self.sock.sendall(encode_packet(cmd))

    def recv_packet(self, timeout=None):
        sock = self.sock
        sock.settimeout(self.timeout if timeout is None else timeout)
        while True:
            c = sock.recv(1)
            if not c:
                raise ConnectionError("closed")
            if c == b'$':
                break
        data = b''
        while True:
            c = sock.recv(1)
            if c == b'#':
                break
            data += c
        sock.recv(2)
        sock.sendall(b'+')
        return data.decode(errors='replace')

    def drain(self):
        self.sock.settimeout(0.2)
        try:
            while self.sock.recv(256):
                pass
        except (socket.timeout, OSError):
            pass

    # -- 고수준 동작 ------------------------------------------------------
    def read_mem(self, addr, length):
        """0x200 청크로 m 패킷 반복 (dump_screen.py read_mem 동일)."""
        out = b''
        while length > 0:
            n = min(0x200, length)
            self.send_packet("m%x,%x" % (addr, n))
            r = self.recv_packet()
            if r.startswith('E'):
                raise RuntimeError("read %#x,%x: %s" % (addr, n, r))
            out += bytes.fromhex(r)
            addr += n
            length -= n
        return out

    def cont(self):
        """실행 재개(c) — 초기 진입용, drain 없음 (dump_screen.py 부팅 경로)."""
        self.send_packet("c")

    def resume(self):
        """실행 재개(c) + drain (dump_screen.py 버스트 재개 경로)."""
        self.send_packet("c")
        self.drain()

    def halt(self, settle=0.3):
        """Ctrl-C(0x03) 인터럽트 → settle 대기 → drain.

        dump_screen.py: 최종 정지 settle=0.3, 버스트 정지 settle=0.15.
        """
        self.sock.sendall(b'\x03')
        time.sleep(settle)
        self.drain()

    def regs(self):
        """g 패킷 — 레지스터 덤프 문자열."""
        self.send_packet("g")
        return self.recv_packet()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
