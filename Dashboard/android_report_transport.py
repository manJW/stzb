"""Loopback-only transport for the source-verified emulator. No game requests."""
import queue
import socket
import struct
import threading
import time
from android_map_recovery import LocalAdb
GUEST_DIR='/data/local/tmp/stzb-readonly-probe-20261112'
GUEST_BINARY=GUEST_DIR+'/frida-server-17.26.2'
GUEST_PORT=27043

class AdbStream:
    """Duplex stream over the already-existing, source-verified emulator port."""
    def __init__(self,port,service):
        self.device=LocalAdb(port)
        self.sock=socket.create_connection(('127.0.0.1',port),timeout=10)
        self.sock.settimeout(15)
        self.send_lock=threading.Lock()
        self.write_lock=threading.Lock()
        self.ack=threading.Event()
        self.closed=threading.Event()
        self.parts=queue.Queue(maxsize=128)
        self.buffer=bytearray()
        self.last_error=None
        self.device._send(self.sock,b'CNXN',0x01000001,1024*1024,b'host::features=cmd;\0')
        if self.device._receive(self.sock)[0]!=b'CNXN':raise RuntimeError('ADB authorization unavailable')
        self.device._send(self.sock,b'OPEN',1,0,service.encode('ascii')+b'\0')
        command,self.remote,local,body=self.device._receive(self.sock)
        if command!=b'OKAY' or local!=1:raise RuntimeError('ADB stream unavailable')
        self.sock.settimeout(None)
        threading.Thread(target=self._receive,daemon=True).start()

    def _send(self,command,body=b''):
        with self.send_lock:self.device._send(self.sock,command,1,self.remote,body)

    def _receive(self):
        try:
            while not self.closed.is_set():
                command,remote,local,body=self.device._receive(self.sock)
                if remote!=self.remote or local!=1:raise RuntimeError('ADB stream identity changed')
                if command==b'WRTE':
                    self.parts.put(body,timeout=10);self._send(b'OKAY')
                elif command==b'OKAY':self.ack.set()
                elif command==b'CLSE':break
                else:raise RuntimeError('Unexpected ADB command')
        except (OSError,RuntimeError,queue.Full) as error:self.last_error=str(error)[:250]
        finally:self.closed.set();self.ack.set()

    def write(self,data):
        with self.write_lock:
            for offset in range(0,len(data),65536):
                if self.closed.is_set():raise RuntimeError('ADB stream closed')
                self.ack.clear();self._send(b'WRTE',data[offset:offset+65536])
                if not self.ack.wait(15) or self.closed.is_set():raise RuntimeError('ADB write not acknowledged')

    def read_some(self,timeout=20):
        deadline=time.monotonic()+timeout
        while True:
            try:return self.parts.get(timeout=.5)
            except queue.Empty:
                if self.closed.is_set():return b''
                if time.monotonic()>=deadline:raise RuntimeError('ADB read timed out')

    def read(self,size):
        while len(self.buffer)<size:
            part=self.read_some()
            if not part:raise RuntimeError('ADB read ended early')
            self.buffer.extend(part)
        result=bytes(self.buffer[:size]);del self.buffer[:size]
        return result

    def close(self):
        if not self.closed.is_set():
            try:self._send(b'CLSE')
            except OSError:pass
        self.closed.set()
        try:self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.sock.close()


def push_tool(port,path):
    stream=AdbStream(port,'sync:')
    try:
        name=(GUEST_BINARY+','+str(0o100700)).encode('ascii')
        stream.write(b'SEND'+struct.pack('<I',len(name))+name)
        with path.open('rb') as source:
            for part in iter(lambda:source.read(65536),b''):
                stream.write(b'DATA'+struct.pack('<I',len(part))+part)
        stream.write(b'DONE'+struct.pack('<I',int(time.time())))
        status,size=struct.unpack('<4sI',stream.read(8))
        if status!=b'OKAY' or size!=0:raise RuntimeError('Tool transfer not accepted')
    finally:stream.close()


class LocalTunnel:
    def __init__(self,adb_port):
        self.adb_port=adb_port
        self.closed=threading.Event()
        self.connections=[]
        self.errors=[]
        self.traffic={'opened':0,'up':0,'down':0}
        self.listener=socket.socket()
        self.listener.bind(('127.0.0.1',0));self.listener.listen(4);self.listener.settimeout(.5)
        self.port=self.listener.getsockname()[1]
        threading.Thread(target=self._accept,daemon=True).start()

    def _accept(self):
        while not self.closed.is_set():
            try:client,_=self.listener.accept()
            except socket.timeout:continue
            except OSError:return
            threading.Thread(target=self._forward,args=(client,),daemon=True).start()

    def _forward(self,client):
        remote=None
        try:
            remote=AdbStream(self.adb_port,f'tcp:{GUEST_PORT}')
            self.traffic['opened']+=1
            self.connections.append((client,remote))
            def upload():
                try:
                    while not self.closed.is_set():
                        part=client.recv(65536)
                        if not part:break
                        remote.write(part)
                        self.traffic['up']+=len(part)
                except (OSError,RuntimeError) as error:self.errors.append('up: '+str(error)[:250])
                finally:remote.close()
            threading.Thread(target=upload,daemon=True).start()
            while not self.closed.is_set():
                part=remote.read_some()
                if not part:break
                client.sendall(part)
                self.traffic['down']+=len(part)
        except (OSError,RuntimeError) as error:self.errors.append('down: '+str(error)[:250])
        finally:
            if remote and remote.last_error:self.errors.append('adb: '+remote.last_error)
            if remote:remote.close()
            client.close()

    def close(self):
        self.closed.set();self.listener.close()
        for client,remote in self.connections:
            remote.close()
            try:client.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            client.close()

