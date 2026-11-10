"""Observe the selected game's traffic; outgoing inspection is restricted to report paging."""
import ipaddress
import json
import os
import struct
import subprocess
import threading
import time
from pathlib import Path

from capture_decoder import MAX_FRAME, decode_body

ROOT = Path(__file__).resolve().parent
DUMPCAP = r"F:\Program Files\Wireshark\dumpcap.exe"
MUMU_CLI = r"F:\Program Files\Netease\MuMu\nx_main\mumu-cli.exe"
PC_EXECUTABLE = r"E:\Program Files (x86)\stzb\stzb.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class MessageFramer:
    def __init__(self, on_message, on_error, client=False, accepted_ids=None):
        self.buffer = bytearray()
        self.on_message, self.on_error = on_message, on_error
        self.client, self.accepted_ids = client, accepted_ids

    def reset(self):
        self.buffer.clear()

    def feed(self, payload, observed_at):
        self.buffer.extend(payload)
        if len(self.buffer) > 16 * 1024 * 1024:
            self.reset()
            self.on_error()
            return
        offset = 0
        header = 57 if self.client else 13
        while len(self.buffer) - offset >= 12:
            total = struct.unpack_from(">I", self.buffer, offset)[0] + 4
            if total == 12:
                offset += 12
                continue
            if not header <= total <= MAX_FRAME:
                offset += 1
                continue
            if len(self.buffer) - offset < header:
                break
            encoding = self.buffer[offset + header - 1]
            message_id = struct.unpack_from(">I", self.buffer, offset + (44 if self.client else 4))[0]
            if encoding not in (2, 3, 5) or message_id > 1000000:
                offset += 1
                continue
            if len(self.buffer) - offset < total:
                break
            if total == header or (self.accepted_ids is not None and message_id not in self.accepted_ids):
                offset += total
                continue
            frame = bytes(self.buffer[offset:offset + total])
            try:
                value = decode_body(frame, self.client)
            except ValueError:
                self.on_error()
                offset += 1
                continue
            self.on_message(message_id, value, observed_at)
            offset += total
        if offset:
            del self.buffer[:offset]


class TCPStream:
    def __init__(self, framer, on_gap):
        self.framer, self.on_gap = framer, on_gap
        self.next_seq = None
        self.pending = {}
        self.hole_since = None
        self.last_seen = time.monotonic()

    def feed(self, seq, payload, observed_at, syn=False):
        self.last_seen = time.monotonic()
        if syn:
            self.next_seq = (seq + 1) & 0xFFFFFFFF
            self.pending.clear()
            self.hole_since = None
            self.framer.reset()
            seq = self.next_seq
        if not payload:
            return
        if self.next_seq is None:
            self.next_seq = seq
        ahead = (seq - self.next_seq) & 0xFFFFFFFF
        if ahead and ahead < 0x80000000:
            self.pending.setdefault(seq, (payload, observed_at))
            if self.hole_since is None:
                self.hole_since = self.last_seen
            if len(self.pending) > 64 or self.last_seen - self.hole_since > 3:
                self.on_gap()
                self.framer.reset()
                self.next_seq = min(self.pending, key=lambda value: (value - self.next_seq) & 0xFFFFFFFF)
                self.hole_since = None
                self._drain()
            return
        self._append(seq, payload, observed_at)
        self._drain()

    def _append(self, seq, payload, observed_at):
        behind = (self.next_seq - seq) & 0xFFFFFFFF
        if behind >= len(payload):
            return
        new_payload = payload[behind:]
        self.next_seq = (seq + len(payload)) & 0xFFFFFFFF
        self.framer.feed(new_payload, observed_at)

    def _drain(self):
        while self.pending:
            available = [seq for seq in self.pending if (self.next_seq - seq) & 0xFFFFFFFF < 0x80000000]
            if not available:
                break
            for seq in available:
                payload, observed_at = self.pending.pop(seq)
                self._append(seq, payload, observed_at)
        if not self.pending:
            self.hole_since = None


def read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            return None
        chunks.extend(block)
    return bytes(chunks)


def pcap_packets(stream):
    header = read_exact(stream, 24)
    if not header:
        return
    formats = {b"\xd4\xc3\xb2\xa1": ("<", 1e6), b"\xa1\xb2\xc3\xd4": (">", 1e6),
               b"\x4d\x3c\xb2\xa1": ("<", 1e9), b"\xa1\xb2\x3c\x4d": (">", 1e9)}
    if header[:4] not in formats:
        raise ValueError("Unexpected capture format")
    endian, precision = formats[header[:4]]
    linktype = struct.unpack_from(endian + "I", header, 20)[0] & 0xFFFF
    if linktype != 1:
        raise ValueError("This collector requires the verified Ethernet-format interface")
    while True:
        record = read_exact(stream, 16)
        if record is None:
            return
        seconds, fraction, captured, _ = struct.unpack(endian + "IIII", record)
        if captured > 16 * 1024 * 1024:
            raise ValueError("Capture packet exceeds safety bound")
        packet = read_exact(stream, captured)
        if packet is None:
            return
        yield seconds + fraction / precision, packet


def tcp_packet(packet):
    if len(packet) < 14:
        return None
    offset = 14
    ether_type = struct.unpack_from(">H", packet, 12)[0]
    while ether_type in (0x8100, 0x88A8) and len(packet) >= offset + 4:
        ether_type = struct.unpack_from(">H", packet, offset + 2)[0]
        offset += 4
    if ether_type != 0x0800 or len(packet) < offset + 20:
        return None
    ip = packet[offset:]
    ihl = (ip[0] & 15) * 4
    total = struct.unpack_from(">H", ip, 2)[0]
    if ip[0] >> 4 != 4 or ihl < 20 or ip[9] != 6 or len(ip) < total:
        return None
    if struct.unpack_from(">H", ip, 6)[0] & 0x3FFF:
        raise ValueError("Fragmented IP packet requires resynchronization")
    tcp = ip[ihl:total]
    if len(tcp) < 20:
        return None
    size = (tcp[12] >> 4) * 4
    if size < 20 or len(tcp) < size:
        return None
    sport, dport, seq = struct.unpack_from(">HHI", tcp)
    return (str(ipaddress.IPv4Address(ip[12:16])), sport,
            str(ipaddress.IPv4Address(ip[16:20])), dport, seq, tcp[13], tcp[size:])


def discover_connection():
    result = subprocess.run([MUMU_CLI, "info", "--vmindex", "0"], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=10, creationflags=NO_WINDOW)
    devices = json.loads(result.stdout)
    device = devices.get("0", devices)
    if not device.get("is_process_started") or not device.get("pid"):
        return None
    pid = int(device["pid"])
    script = (
        f"$emuPid={pid};"
        "$c=Get-NetTCPConnection -OwningProcess $emuPid -RemotePort 8001 -State Established -ErrorAction SilentlyContinue|Select-Object -First 1;"
        "if($c){$i=Get-NetIPAddress -IPAddress $c.LocalAddress -ErrorAction SilentlyContinue|Select-Object -First 1;"
        "$a=Get-NetAdapter -InterfaceIndex $i.InterfaceIndex -ErrorAction SilentlyContinue;"
        "[pscustomobject]@{peer=$c.RemoteAddress;local=$c.LocalAddress;pid=$emuPid;"
        "local_port=$c.LocalPort;"
        "interface=('\\Device\\NPF_{'+([Guid]$a.InterfaceGuid).ToString('D').ToUpperInvariant()+'}')}|ConvertTo-Json -Compress}"
    )
    # PowerShell only reads the verified emulator process's own TCP connection and interface.
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=15, creationflags=NO_WINDOW)
    if not result.stdout.strip():
        return None
    connection = json.loads(result.stdout)
    ipaddress.IPv4Address(connection["peer"])
    ipaddress.IPv4Address(connection["local"])
    if not 0 < int(connection["local_port"]) < 65536:
        raise ValueError("Invalid observed game port")
    if not connection["interface"].startswith("\\Device\\NPF_{"):
        raise ValueError("Invalid observed interface")
    return connection


def discover_pc_connection(executable=None):
    executable = str(executable or PC_EXECUTABLE)
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$g=Get-CimInstance Win32_Process|Where-Object { $_.Name -eq 'stzb.exe' -and "
        "$_.ExecutablePath -eq $env:STLAB_CAPTURE_EXECUTABLE "
        "}|Select-Object -First 1;"
        "if($g){$c=Get-NetTCPConnection -OwningProcess $g.ProcessId -RemotePort 8001 -State Established "
        "-ErrorAction SilentlyContinue|Select-Object -First 1;"
        "if($c){$i=Get-NetIPAddress -IPAddress $c.LocalAddress -ErrorAction SilentlyContinue|Select-Object -First 1;"
        "$a=Get-NetAdapter -InterfaceIndex $i.InterfaceIndex -ErrorAction SilentlyContinue;"
        "[pscustomobject]@{peer=$c.RemoteAddress;local=$c.LocalAddress;pid=$g.ProcessId;"
        "executable=$g.ExecutablePath;source='pc';local_port=$c.LocalPort;"
        "connection_started=if($c.CreationTime){$c.CreationTime.ToUniversalTime().Ticks}else{0};"
        "interface=('\\Device\\NPF_{'+([Guid]$a.InterfaceGuid).ToString('D').ToUpperInvariant()+'}')}|ConvertTo-Json -Compress}}"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=20, creationflags=NO_WINDOW,
                            env={**os.environ, "STLAB_CAPTURE_EXECUTABLE": executable})
    if not result.stdout.strip():
        return None
    connection = json.loads(result.stdout)
    ipaddress.IPv4Address(connection["peer"])
    ipaddress.IPv4Address(connection["local"])
    if str(connection.get("executable", "")).casefold() != executable.casefold() or int(connection["pid"]) <= 0:
        raise ValueError("PC process identity does not match the configured game executable")
    if not 0 < int(connection["local_port"]) < 65536 or not connection["interface"].startswith("\\Device\\NPF_{"):
        raise ValueError("Invalid observed PC game connection")
    return connection


class Collector:
    def __init__(self, state, archive=None, settings=None):
        self.state = state
        self.archive = archive
        path = self.state.root / "capture-source.json"
        if settings is None:
            settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.source = settings.get("mode", "mumu")
        self.pc_executable = settings.get("executable")
        self.dumpcap = settings.get("dumpcap", DUMPCAP)
        self.incoming_ids = None
        if self.source not in ("pc", "mumu"):
            raise ValueError("采集来源只能是 pc 或 mumu")
        if settings.get("profile_id") and (archive is None or settings["profile_id"] != archive.profile_id):
            raise ValueError("采集来源绑定的账号、赛季与当前档案不一致")
        self.state.health["capture_source"] = self.source
        self.stop_event = threading.Event()
        self.process = None
        self.report_observer = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="game-collector")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def _decode_error(self):
        self.state.decode_error()

    def _message(self, mid, value, observed_at):
        stats = None
        if self.archive is not None:
            try:
                stats = self.archive.ingest(mid, value, observed_at, source=self.source + "-live")
                with self.state.lock:
                    self.state.health["archive_last_saved"] = observed_at
            except Exception as error:
                if mid == 92 and self.report_observer:
                    self.report_observer.fail("战报保存失败，游标未推进")
                with self.state.lock:
                    self.state.health["archive_errors"] = self.state.health.get("archive_errors", 0) + 1
                    self.state.health["archive_error"] = type(error).__name__
                with (self.state.root / "runtime" / "archive-errors.log").open("a", encoding="utf-8") as log:
                    log.write(f"{time.time():.0f} {mid} {type(error).__name__}: {str(error)[:160]}\n")
        if mid == 92 and self.report_observer and stats is not None:
            self.report_observer.on_page(value, observed_at, stats)
        self.state.apply(mid, value, observed_at)

    def _request(self, mid, value, observed_at):
        # No client headers or unrelated request bodies leave the in-memory framer.
        if mid == 92 and self.report_observer:
            self.report_observer.on_request(value, observed_at)

    def run(self):
        while not self.stop_event.is_set():
            try:
                connection = self._discover()
                if not connection:
                    label = "PC版" if self.source == "pc" else "MuMu"
                    self.state.connection("waiting", f"等待{label}中的游戏连接，请保持地图在线")
                    self.stop_event.wait(5)
                    continue
                self.capture(connection)
            except Exception as error:
                self.state.connection("error", "采集暂时中断，正在尝试恢复")
                with (self.state.root / "runtime" / "collector.log").open("a", encoding="utf-8") as log:
                    log.write(f"{time.time():.0f} {type(error).__name__}: {str(error)[:180]}\n")
                self.stop_event.wait(5)

    def _discover(self):
        if self.source == "pc":
            return discover_pc_connection(self.pc_executable) if self.pc_executable else discover_pc_connection()
        return discover_connection()

    def capture(self, connection):
        peer = connection["peer"]
        capture_filter = (f"src host {peer} and tcp src port 8001 and "
                          f"dst host {connection['local']} and tcp dst port {int(connection['local_port'])}")
        observe_requests = self.source == "pc" and self.report_observer is not None
        if observe_requests:
            capture_filter = (f"({capture_filter}) or (src host {connection['local']} and "
                f"tcp src port {int(connection['local_port'])} and dst host {peer} and tcp dst port 8001)")
        # Classic PCAP goes directly to memory; no continuous raw capture file is written.
        command = [self.dumpcap, "-P", "-q", "-i", connection["interface"], "-f", capture_filter,
                   "-s", "65535", "-w", "-"]
        stopped = threading.Event()
        log_path = self.state.root / "runtime" / "capture.log"
        with log_path.open("a", encoding="utf-8") as log:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log,
                                            creationflags=NO_WINDOW, bufsize=0)
            process = self.process
            self.state.connection("live", "等待新的地图消息", connection)
            def monitor():
                while not stopped.wait(15):
                    if self.stop_event.is_set():
                        process.terminate()
                        return
                    try:
                        current = self._discover()
                    except Exception:
                        continue
                    if not current or any(current[key] != connection[key] for key in ("peer", "pid", "interface", "local", "local_port")):
                        process.terminate()
                        return
            threading.Thread(target=monitor, daemon=True, name="connection-monitor").start()
            streams = {}
            saved_at = time.monotonic()
            try:
                for observed_at, packet in pcap_packets(process.stdout):
                    if self.stop_event.is_set():
                        break
                    try:
                        segment = tcp_packet(packet)
                    except ValueError:
                        self.state.gap()
                        continue
                    if not segment:
                        continue
                    src, sport, dst, dport, seq, flags, payload = segment
                    incoming = src == peer and sport == 8001 and dst == connection["local"] and dport == int(connection["local_port"])
                    outgoing = observe_requests and dst == peer and dport == 8001 and src == connection["local"] and sport == int(connection["local_port"])
                    if not incoming and not outgoing:
                        raise ValueError("Capture scope guard rejected an unexpected packet")
                    if incoming:
                        self.state.packet(observed_at)
                    key = (src, sport, dst, dport)
                    def request_gap():
                        if self.report_observer:
                            self.report_observer.fail("请求观测中断，已暂停翻页")
                    framer = MessageFramer(self._message, self._decode_error, accepted_ids=self.incoming_ids) if incoming else MessageFramer(
                        self._request, request_gap, client=True, accepted_ids={92})
                    stream = streams.setdefault(key, TCPStream(framer, self.state.gap if incoming else request_gap))
                    stream.feed(seq, payload, observed_at, bool(flags & 2))
                    if flags & 5:
                        streams.pop(key, None)
                    if time.monotonic() - saved_at > 10:
                        self.state.persist()
                        saved_at = time.monotonic()
                        streams = {key: value for key, value in streams.items() if time.monotonic() - value.last_seen < 120}
            finally:
                stopped.set()
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
                self.process = None
                self.state.persist()
                if not self.stop_event.is_set():
                    self.state.connection("reconnecting", "正在重新识别游戏连接")
