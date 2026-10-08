#!/usr/bin/env python3
"""Scripted session through an InnProxy (the original exe or the port).

A fake INN Barn server and a fake INN client (what DOSBox's modem would be)
drive one proxy through modem commands, frames of every shape, host messages,
window/ack behaviour and disconnects, and write down what each end received.

    scenario.py --workdir DIR [--barn-port N] [--client-port N] [--skip-deviations] \
                --out transcript.json -- COMMAND...

COMMAND is started with DIR as its working directory; the script writes
DIR/innProxy.ini and DIR/INN/LSCI.CFG first.  It stops COMMAND at the end.
"""

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import innproxy as ip  # noqa: E402  (framing and CRC helpers)

LSCI = (b' video          = VGA320.DRV\r\n joyDrv         = NO\r\n modemModel    = Test\r\n'
        b' modem          = ATDTlocalhost:30000!~\r\n id             = 123\r\n hostID = SIERRA\r\n')


class Barn:
    """Fake INN Barn: accepts the proxy, records every message it sends."""

    def __init__(self, port):
        self.ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ls.bind(('127.0.0.1', port))
        self.ls.listen(1)
        self.sock = None
        self.msgs = []
        self.cv = threading.Condition()
        self.closed = False

    def accept(self, timeout=15):
        self.ls.settimeout(timeout)
        self.sock, _ = self.ls.accept()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b''
        while True:
            try:
                d = self.sock.recv(65536)
            except OSError:
                d = b''
            if not d:
                with self.cv:
                    self.closed = True
                    self.cv.notify_all()
                return
            buf += d
            while len(buf) >= 16:
                n = struct.unpack_from('<H', buf, 4)[0]
                if n < 16 or len(buf) < n:
                    break
                with self.cv:
                    self.msgs.append(buf[:n])
                    self.cv.notify_all()
                buf = buf[n:]

    def wait(self, count, timeout=5):
        end = time.time() + timeout
        with self.cv:
            while len(self.msgs) < count and time.time() < end:
                self.cv.wait(0.05)
            return len(self.msgs) >= count

    def send(self, mtype, chan, body=b'', w8=0, w12=0):
        self.sock.sendall(bytes(ip.host_msg(mtype, chan, body, w8, w12)))

    def close(self):
        # shutdown() first: on Linux a plain close() while the reader thread
        # sits in recv() leaves the connection open
        for s in (self.sock, self.ls):
            if s is None:
                continue
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


class Client:
    """Fake INN client on the far side of DOSBox's modem."""

    def __init__(self, port):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.sock.settimeout(None)
        self.raw = b''          # unparsed bytes
        self.events = []        # ('text', ...), ('ack', code), ('frame', seq, payload)
        self.auto_ack = True
        self.cv = threading.Condition()
        self.closed = False
        self.framed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                d = self.sock.recv(65536)
            except OSError:
                d = b''
            with self.cv:
                if not d:
                    self.closed = True
                    self.cv.notify_all()
                    return
                self.raw += d
                self._parse()
                self.cv.notify_all()

    def _parse(self):
        while self.raw:
            if not self.framed or self.raw[0] != ip.SOF:
                k = self.raw.find(b'\r')
                if self.framed and self.raw[0] != ip.SOF and k < 0:
                    return
                if not self.framed:
                    if k < 0:
                        return
                    self.events.append(('text', self.raw[:k + 1].decode('latin-1')))
                    self.raw = self.raw[k + 1:]
                    continue
                self.events.append(('junk', self.raw[:k + 1].hex()))
                self.raw = self.raw[k + 1:]
                continue
            if len(self.raw) >= 5 and self.raw[4] == ip.EOF:
                code = self.raw[3]
                self.events.append(('ack', code, self.raw[:5] == ip.short_frame(code)))
                self.raw = self.raw[5:]
                continue
            r = decode(self.raw)
            if r is None:
                return
            used, seq, payload, crc_ok = r
            self.raw = self.raw[used:]
            self.events.append(('frame', seq, payload.hex(), crc_ok))
            if self.auto_ack:
                self.sock.sendall(ip.short_frame(0x90 | (seq & 7)))

    def send(self, data):
        self.sock.sendall(data)

    def wait_events(self, count, timeout=5):
        end = time.time() + timeout
        with self.cv:
            while len(self.events) < count and time.time() < end:
                self.cv.wait(0.05)
            return len(self.events) >= count

    def wait_closed(self, timeout=5):
        end = time.time() + timeout
        with self.cv:
            while not self.closed and time.time() < end:
                self.cv.wait(0.05)
            return self.closed


def decode(buf):
    """Decode one data frame at buf[0]; None if incomplete."""
    i = 4
    crc = ip.crc_link_update(0xffff, buf[3])
    data = bytearray()
    while True:
        if i >= len(buf):
            return None
        b = buf[i]
        if b == ip.EOF:
            break
        crc = ip.crc_link_update(crc, b)
        if b == ip.ESC:
            i += 1
            if i >= len(buf):
                return None
            b = buf[i]
            crc = ip.crc_link_update(crc, b)
        data.append(b)
        i += 1
    if data[:1] == b'\xff':
        n = data[1] | data[2] << 8
        payload = bytes(data[3:])
    else:
        n = data[0]
        payload = bytes(data[1:])
    assert n == len(payload), (n, len(payload), buf[:i + 1].hex())
    return i + 1, buf[3], payload, crc == (buf[1] | buf[2] << 8)


def client_frame(seq, payload, length=None, bad_crc=False):
    """A frame as the INN client sends it; `length` overrides the length byte."""
    body = bytes((len(payload) if length is None else length,)) + payload
    out = bytearray((ip.SOF, 0, 0, seq))
    crc = ip.crc_link_update(0xffff, seq)
    for b in body:
        if b in ip.ESCAPED:
            out.append(ip.ESC)
            crc = ip.crc_link_update(crc, ip.ESC)
        out.append(b)
        crc = ip.crc_link_update(crc, b)
    out.append(ip.EOF)
    if bad_crc:
        crc ^= 0x5555
    out[1] = crc & 0xff
    out[2] = crc >> 8
    return bytes(out)


def norm_barn(m):
    m = bytearray(m)
    mtype = struct.unpack_from('<H', m, 0)[0]
    if mtype == ip.PING:
        m[0x10:0x38] = bytes(0x28)       # QueryPerformanceCounter values
        m[2:4] = b'\0\0'
    return m.hex()


def run(cmd, workdir, barn_port, client_port, skip_dev):
    os.makedirs(os.path.join(workdir, 'INN'), exist_ok=True)
    with open(os.path.join(workdir, 'innProxy.ini'), 'w', newline='\r\n') as f:
        f.write('[InnBarn]\nBarnIP=127.0.0.1:%d\nClientIP=127.0.0.1:%d\nNoMutex=1\nXLogBit=10\n'
                % (barn_port, client_port))
    with open(os.path.join(workdir, 'INN', 'LSCI.CFG'), 'wb') as f:
        f.write(LSCI)
    t = {'barn': [], 'client1': [], 'client2': [], 'notes': []}
    barn = Barn(barn_port)
    kw = {}
    if os.name == 'nt':                  # the original is a GUI program: keep it minimized
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7               # SW_SHOWMINNOACTIVE
        kw['startupinfo'] = si
    proc = subprocess.Popen(cmd, cwd=workdir, **kw)
    note = t['notes'].append
    note('pid %d' % proc.pid)
    try:
        barn.accept()
        barn.wait(1)
        pid_w12 = 0x11223344
        title = b'Test Barn\0'
        barn.send(ip.PROXY_HELLO, 0, struct.pack('<I', 4242) + title + bytes(16), 0, pid_w12)
        barn.send(ip.PROXY_INFO, 0, struct.pack('<7I', 0, 1, 3, 3600, 0, 50, 400), 0, pid_w12)
        ping = bytearray(41)
        ping[40] = 0
        barn.send(ip.PING, 0, bytes(ping), 0x55667788, pid_w12)
        note('ping echo %s' % barn.wait(2))
        time.sleep(0.5)

        c = Client(client_port)
        time.sleep(0.3)
        for cmd_text, expect in ((b'AT&F\r', 1), (b'ATDT5551212\r', 1), (b'@D\r', 1),
                                 (b'c Nowhere\r', 0), (b'c SIERRA\r', 1)):
            n = len(c.events)
            c.send(cmd_text)
            ok = c.wait_events(n + expect, 3) if expect else (time.sleep(0.4) or True)
            note('modem %r -> %s' % (cmd_text, ok))
        c.framed = True
        time.sleep(0.2)

        seq = [0]

        def frame_to_barn(payload, length=None, bad=False, expect_barn=1, expect_client=1):
            nb = len(barn.msgs)
            nc = len(c.events)
            c.send(client_frame(seq[0] & 7, payload, length, bad))
            seq[0] += 1
            okc = c.wait_events(nc + expect_client, 3)
            okb = barn.wait(nb + expect_barn, 3) if expect_barn else (time.sleep(0.4) or True)
            note('frame %d bytes -> client %s barn %s' % (len(payload), okc, okb))

        frame_to_barn(bytes([0x16]) + bytes(range(1, 21)))                     # LoginMsg
        frame_to_barn(bytes([0x17, 0x81, 0x82, 0x1b]) + bytes(23))              # length 0x1b
        frame_to_barn(bytes([0x02]) + bytes((i * 7) & 0xff for i in range(128)))  # length 0x81
        rec = bytes([5, 0x0d, 1, 2, 3, 4, 6, 0x0e, 9, 8, 7, 6, 5])
        frame_to_barn(rec[1:], length=rec[0])                                     # 2 records
        s = bytes([10]) + bytes([0x11] + list(range(9))) + bytes([20]) + bytes([0x12] + list(range(40, 59)))
        frame_to_barn(s[1:20], length=s[0])                                       # split, part 1
        frame_to_barn(s[21:32], length=s[20])                                     # split, part 2
        frame_to_barn(bytes([0x22, 1, 2, 3]), bad=True, expect_barn=0)            # bad crc -> NAK
        seq[0] -= 1
        nb = len(barn.msgs)
        c.send(b'xyz' + client_frame(seq[0] & 7, bytes([0x23, 9, 9])))            # junk first
        seq[0] += 1
        note('junk+frame barn %s' % barn.wait(nb + 1, 3))
        frame_to_barn(b'', length=0)                                              # empty
        frame_to_barn(bytes([3, 1, 2, 3, 0, 4]), length=4, expect_barn=0)         # zero record
        nb = len(barn.msgs)
        c.send(client_frame(seq[0] & 7, bytes([0x24, 1])) + client_frame((seq[0] + 1) & 7, bytes([0x25, 2])))
        seq[0] += 2
        note('two frames in one write barn %s' % barn.wait(nb + 2, 3))

        # host -> client
        for n in (5, 27, 129, 130, 254, 255, 300):
            nc = len(c.events)
            barn.send(0x17, 1, bytes([0x17]) + bytes((i * 31 + 0x7e) & 0xff for i in range(n - 1)), 0, pid_w12)
            note('host msg %d -> %s' % (n, c.wait_events(nc + 1, 3)))
        barn.send(0x99, 1, b'\x99abc', 0, pid_w12)       # unknown type: tossed
        time.sleep(0.3)

        # window: no acks, 10 messages, give-up after 3 s
        c.auto_ack = False
        nc = len(c.events)
        for k in range(10):
            barn.send(0x02, 1, bytes([0x02, k]), 0, pid_w12)
        c.wait_events(nc + 8, 3)
        note('window: %d frames before give-up' % (len(c.events) - nc))
        c.wait_events(nc + 10, 6)
        note('window: %d frames after give-up' % (len(c.events) - nc))
        c.auto_ack = True
        for e in c.events[nc:]:
            if e[0] == 'frame':
                c.send(ip.short_frame(0x90 | (e[1] & 7)))
        time.sleep(0.3)

        # idle ping from the proxy after 15 s
        nc = len(c.events)
        note('client ping %s' % c.wait_events(nc + 1, 18))

        # +++ back to modem mode, then reconnect to the host
        c.framed = False
        nc = len(c.events)
        c.send(b'+++')
        time.sleep(1.2)
        c.send(b'ATH0\r')
        note('ATH0 %s' % c.wait_events(nc + 1, 3))
        nc = len(c.events)
        c.send(b'c SIERRA\r')
        note('reconnect %s' % c.wait_events(nc + 1, 3))
        c.framed = True
        time.sleep(0.2)
        frame_to_barn(bytes([0x26, 5, 5]))

        # host drops channel 1
        nb = len(barn.msgs)
        barn.send(ip.DISCONNECT, 1, b'', 0, pid_w12)
        note('client closed %s' % c.wait_closed(5))
        note('disconnect echoed %s' % barn.wait(nb + 1, 3))
        time.sleep(0.3)
        t['client1'] = c.events

        # second session: channel 2
        c2 = Client(client_port)
        time.sleep(0.3)
        c2.send(b'c SIERRA\r')
        note('session 2 connect %s' % c2.wait_events(1, 3))
        c2.framed = True
        time.sleep(0.2)
        nb = len(barn.msgs)
        c2.send(client_frame(0, bytes([0x16, 7, 7, 7])))
        note('session 2 frame %s' % barn.wait(nb + 1, 3))
        time.sleep(0.3)
        if not skip_dev:
            # A clean FIN, as DOSBox's modem hangs up: not reported at once; the proxy's
            # next frame to the client (its ping after 15 s quiet) meets a reset, and only
            # that ends the channel.
            nb = len(barn.msgs)
            c2.sock.shutdown(socket.SHUT_RDWR)
            c2.sock.close()
            t0 = time.time()
            note('hang-up reported at once %s' % barn.wait(nb + 1, 3))
            got = barn.wait(nb + 1, 22)
            note('hang-up reported after the ping %s%s' % (got, ' (%ds)' % round(time.time() - t0) if got else ''))
        t['client2'] = c2.events
    finally:
        time.sleep(0.3)
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(0.3)
        t['barn'] = [norm_barn(m) for m in barn.msgs]
        try:
            with open(os.path.join(workdir, 'INN', 'LSCI.CFG'), 'rb') as f:
                t['lsci'] = f.read().decode('latin-1')
        except OSError as e:
            t['lsci'] = str(e)
        for s in (barn.sock, barn.ls):
            try:
                s.close()
            except Exception:
                pass
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--barn-port', type=int, default=29571)
    ap.add_argument('--client-port', type=int, default=30571)
    ap.add_argument('--skip-deviations', action='store_true')
    ap.add_argument('--out', required=True)
    ap.add_argument('cmd', nargs='+')
    a = ap.parse_args()
    t = run(a.cmd, a.workdir, a.barn_port, a.client_port, a.skip_deviations)
    with open(a.out, 'w') as f:
        json.dump(t, f, indent=1)
    for n in t['notes']:
        print(n)


if __name__ == '__main__':
    main()
