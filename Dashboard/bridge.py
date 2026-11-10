"""A narrowly scoped adapter for the existing PC bridge's read-only report request."""
import ctypes
import json
import os
import time
import threading
from ctypes import wintypes

from collector import discover_pc_connection

PIPE_NAME = r"\\.\pipe\StzbHelperZXCFLX"
UNION_REPORT_COMMAND = 92


def union_report_request(cursor):
    if isinstance(cursor, bool) or not isinstance(cursor, int) or not 0 <= cursor <= 9223372036854775807:
        raise ValueError("战报游标须为非负整数")
    # Captured from this account's normal game UI. Do not invent filter values.
    return [[1, 2], [1, 3], [-1], cursor, 0]


def _exchange(command, connection_provider=discover_pc_connection, timeout=3):
    if os.name != "nt":
        raise RuntimeError("PC读取桥接仅支持Windows")
    connection = connection_provider()
    if not connection:
        raise RuntimeError("PC游戏连接未就绪")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel.WaitNamedPipeW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.PeekNamedPipe.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    if not kernel.WaitNamedPipeW(PIPE_NAME, int(timeout * 1000)):
        raise RuntimeError("PC读取桥接未就绪或正忙")
    handle = kernel.CreateFileW(PIPE_NAME, 0xC0000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        server_pid = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(server_pid)):
            raise ctypes.WinError(ctypes.get_last_error())
        if server_pid.value != int(connection["pid"]):
            raise RuntimeError("管道所属进程与已核验PC客户端不一致，读取请求已取消")
        payload = (command + "\n").encode("utf-8")
        buffer = ctypes.create_string_buffer(payload)
        written = wintypes.DWORD()
        if not kernel.WriteFile(handle, buffer, len(payload), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(payload):
            raise RuntimeError("读取请求未完整发送")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            available = wintypes.DWORD()
            if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if available.value:
                break
            time.sleep(.02)
        else:
            raise TimeoutError("PC读取桥接响应超时")
        reply = ctypes.create_string_buffer(4096)
        read = wintypes.DWORD()
        if not kernel.ReadFile(handle, reply, 4096, ctypes.byref(read), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return reply.raw[:read.value].decode("utf-8").strip()
    finally:
        kernel.CloseHandle(handle)


class ReadOnlyBridge:
    def __init__(self, exchange=None):
        self.exchange = exchange or _exchange
        self.lock = threading.Lock()

    def _call(self, command):
        with self.lock:
            return self.exchange(command)

    def ping(self):
        response = self._call("__pipe_ping")
        if response != "OK:managed-pipe-bridge":
            raise RuntimeError("读取桥接的响应不符合已核验版本")
        return response

    def fetch_union_page(self, cursor):
        body = union_report_request(cursor)
        response = self._call("__net_send 92 " + json.dumps(body, separators=(",", ":")))
        if response != "OK:sent cmd=92":
            raise RuntimeError("PC读取请求未被接受：" + response[:160])
        return {"command": UNION_REPORT_COMMAND, "body": body, "acknowledged": True}

    def fetch_profiles(self, battle_ids):
        if not isinstance(battle_ids, list) or not 1 <= len(battle_ids) <= 15:
            raise ValueError("一次只能读取1～15个已观测战报")
        for bid in battle_ids:
            if type(bid) is not int or not 0 < bid <= 9223372036854775807:
                raise ValueError("战报编号无效")
        # Current client's ReportProfileData.RequestProfileByBattleIdList defaults serverId to 0.
        body = [battle_ids, 0, 0]
        response = self._call("__net_send 10 " + json.dumps(body, separators=(",", ":")))
        if response != "OK:sent cmd=10":
            raise RuntimeError("战报摘要读取未被接受：" + response[:120])
        return {"command": 10, "body": body, "acknowledged": True}

    def fetch_battle_detail(self, battle_id):
        if type(battle_id) is not int or not 0 < battle_id <= 9223372036854775807:
            raise ValueError("战报编号无效")
        # Body captured from this same client's normal report-detail UI.
        body = [battle_id, 1, 0]
        response = self._call("__net_send 6231 " + json.dumps(body, separators=(",", ":")))
        if response != "OK:sent cmd=6231":
            raise RuntimeError("战报全文读取未被接受：" + response[:120])
        return {"command": 6231, "body": body, "acknowledged": True}
