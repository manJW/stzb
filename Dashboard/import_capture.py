"""Import authorized local captures with original packet times; never mark them live."""
import argparse
import collections
import json
import subprocess
from pathlib import Path
from archive import Archive
from collector import MessageFramer, TCPStream, pcap_packets, tcp_packet, DECODER, NO_WINDOW


def import_capture(archive, path):
    stats = collections.Counter()
    streams = {}
    def on_message(mid, value, at):
        archive.ingest(mid, value, at, source="capture:" + path.name)
        stats["messages"] += 1
    def error():
        stats["decode_errors"] += 1
    process = subprocess.Popen([DECODER.TSHARK, "-r", str(path), "-Y", "tcp.srcport==8001",
        "-F", "pcap", "-w", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    try:
        for at, packet in pcap_packets(process.stdout):
            seg = tcp_packet(packet)
            if not seg:
                continue
            src, sport, dst, dport, seq, flags, payload = seg
            key = (src, sport, dst, dport)
            if key not in streams:
                streams[key] = TCPStream(MessageFramer(on_message, error), lambda: stats.update(["gaps"]))
            streams[key].feed(seq, payload, at, bool(flags & 2))
        if process.wait(timeout=15):
            raise RuntimeError("Capture import failed")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
    return dict(stats)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    archive = Archive(Path(__file__).resolve().parent)
    try:
        stats = import_capture(archive, args.capture)
        print(json.dumps({"import": stats, "counts": archive.summary()["counts"]}, ensure_ascii=True))
    finally:
        archive.close()
