#!/usr/bin/env python3
"""Edge cases of the port that the scripted session does not reach.

Runs innproxy.py with shortened timings against the fake barn and client
from scenario.py:  offline start, lost contact, a second instance, the host's
shutdown message, keep-alive pings, idle and offline exits, the player list,
a full channel table and the LSCI.CFG member id.

    edgecases.py [--python EXE]     (uses a fresh temporary directory)
"""

import argparse
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))
import innproxy as ip  # noqa: E402
from scenario import Barn, Client, client_frame  # noqa: E402

PB, PC = 29581, 30581
RESULTS = []
STARTED = []                 # every proxy process, killed at the end whatever happens


def check(name, cond, extra=''):
    RESULTS.append((name, bool(cond)))
    print('%-4s %s %s' % ('ok' if cond else 'FAIL', name, extra))


def start(py, workdir, timings=None, args=()):
    code = ('import sys; sys.path.insert(0, %r); import innproxy as m\n' % os.path.join(HERE, '..'))
    for k, v in (timings or {}).items():
        code += 'm.%s = %d\n' % (k, v)
    code += 'sys.exit(m.main(%r))\n' % (['-C', workdir, '--barn', '127.0.0.1:%d' % PB,
                                         '--listen', '127.0.0.1:%d' % PC] + list(args))
    p = subprocess.Popen([py, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    STARTED.append(p)
    return p


def finish(p, timeout=5):
    try:
        out = p.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        out = p.communicate()[0]
    return out.decode('latin-1')


def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def msgs_of(barn, mtype):
    return [m for m in barn.msgs if struct.unpack_from('<H', m, 0)[0] == mtype]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--python', default=sys.executable)
    a = ap.parse_args()
    py = a.python
    work = tempfile.mkdtemp(prefix='innproxy-edge-')
    try:
        run_all(py, work)
    finally:
        for p in STARTED:
            if p.poll() is None:
                p.kill()
                p.wait()
        shutil.rmtree(work, ignore_errors=True)
    bad = [n for n, ok in RESULTS if not ok]
    print('%d checks, %d failed%s' % (len(RESULTS), len(bad), (': ' + ', '.join(bad)) if bad else ''))
    sys.exit(1 if bad else 0)


def run_all(py, work):
    # --- offline start, then the barn appears; lost contact; reconnect
    p = start(py, work, {'T_CONNECT_RETRY': 300, 'T_CONNECT_ERROR': 300})
    time.sleep(1.0)
    c = Client(PC)
    time.sleep(0.2)
    c.send(b'c SIERRA\r')
    check('offline: c SIERRA -> NO ANSWER', c.wait_events(1, 3) and c.events[0] == ('text', 'NO ANSWER\r'), c.events)
    barn = Barn(PB)
    barn.accept(5)
    check('offline: connects when the barn comes up', barn.wait(1, 3) and msgs_of(barn, ip.PROXY_HELLO))
    barn.send(ip.PROXY_INFO, 0, struct.pack('<7I', 2, 1, 3, 3600, 0, 50, 400) + b'Alice\0Bob\0\0', 0, 0x0abc)
    time.sleep(0.2)
    c.send(b'c SIERRA\r')
    check('online: c SIERRA -> CONNECTED', c.wait_events(2, 3) and c.events[1] == ('text', 'SIERRA CONNECTED\r'))
    c.framed = True
    # second instance on the same port
    p2 = start(py, work)
    out2 = finish(p2, 5)
    check('second instance exits', p2.returncode == 0 and 'already running' in out2, out2.strip().splitlines()[-1:])
    # lost contact: the barn goes away with a client logged on
    barn.close()
    check('lost contact closes the client', c.wait_closed(3))
    barn = Barn(PB)
    barn.accept(5)
    check('reconnects after lost contact', barn.wait(1, 3) and u32(barn.msgs[0], 8) == 0x0abc,
          'proxy id %#x' % u32(barn.msgs[0], 8) if barn.msgs else '')
    # keep-alive is long here; the host's shutdown text
    barn.send(ip.HOST_TEXT, 0, b'\x01Server going down\0', 0, 0x0abc)
    out = finish(p, 5)
    check('host shutdown message ends the proxy', p.returncode == 0 and 'Host Message: Server going down' in out,
          '' if 'Host Message' in out else 'rc=%s %r' % (p.returncode, out[-400:]))
    check('player list printed', 'INN Barn: 2 players online: Alice, Bob' in out,
          '' if 'Alice' in out else 'rc=%s %r' % (p.returncode, out[-600:]))
    check('no proxy disconnect on exit', not wait_for(lambda: msgs_of(barn, ip.DISCONNECT), 1))
    barn.close()

    # --- keep-alive pings and the host echoing them
    barn = Barn(PB)
    p = start(py, work, {'T_KEEPALIVE': 800, 'T_FIRST_KEEPALIVE': 100})
    barn.accept(5)
    ok = wait_for(lambda: msgs_of(barn, ip.PING), 4)
    pings = msgs_of(barn, ip.PING)
    check('keep-alive PingMsg', ok and len(pings[0]) == 0x39 and pings[0][0x38] == 1)
    if ok:
        m = bytearray(pings[0])
        for k in range(2, 5):
            n = len(msgs_of(barn, ip.PING))
            barn.sock.sendall(bytes(m))
            wait_for(lambda n=n: len(msgs_of(barn, ip.PING)) > n, 2)
            m = bytearray(msgs_of(barn, ip.PING)[-1])
            check('ping echo %d' % k, m[0x38] == k and struct.unpack_from('<Q', m, 0x10 + (k - 1) * 8)[0] != 0)
    p.terminate()
    finish(p)
    barn.close()

    # --- idle shutdown, and a client arriving in time cancels it
    barn = Barn(PB)
    p = start(py, work, {'T_IDLE': 600, 'T_OFFLINE_TICK': 100, 'OFFLINE_TICKS': 8})
    barn.accept(5)
    t0 = time.time()
    out = finish(p, 6)
    check('idle shutdown', p.returncode == 0 and 'Idle so shutting Down!' in out, '%.1fs' % (time.time() - t0))
    barn.close()

    # --- offline for good: quits after OFFLINE_TICKS attempts (a refused connect
    #     to a local port takes ~2 s on Windows, none on Linux)
    p = start(py, work, {'T_CONNECT_RETRY': 100, 'T_CONNECT_ERROR': 100, 'OFFLINE_TICKS': 4})
    t0 = time.time()
    out = finish(p, 20)
    check('offline exit', p.returncode == 0 and 'Shutting down: INN Barn OFFLINE!' in out, '%.1fs' % (time.time() - t0))

    # --- a client that hangs up cleanly keeps its channel until the next frame bounces;
    #     a call-back meanwhile gets the next channel; '+++' stops the channel taking frames
    barn = Barn(PB)
    p = start(py, work, {'T_CLIENT_PING': 1500})
    barn.accept(5)
    barn.wait(1, 3)
    c = Client(PC)
    c.send(b'c SIERRA\r')
    c.wait_events(1, 3)
    c.framed = True
    time.sleep(0.2)
    c.send(client_frame(0, bytes([0x16, 1, 2])))
    barn.wait(2, 3)
    c.sock.shutdown(socket.SHUT_RDWR)
    c.sock.close()
    t0 = time.time()
    c2 = Client(PC)
    c2.send(b'c SIERRA\r')
    c2.wait_events(1, 3)
    c2.framed = True
    check('hang-up not reported at once', not wait_for(lambda: msgs_of(barn, ip.DISCONNECT), 0.8))
    ok = wait_for(lambda: msgs_of(barn, ip.DISCONNECT), 5)
    d = msgs_of(barn, ip.DISCONNECT)
    check('hang-up reported when the ping bounces', ok and struct.unpack_from('<H', d[0], 6)[0] == 1,
          '%.1fs' % (time.time() - t0))
    nb = len(barn.msgs)
    c2.send(client_frame(0, bytes([0x16, 7])))
    check('call-back works on channel 2', barn.wait(nb + 1, 3) and struct.unpack_from('<H', barn.msgs[-1], 6)[0] == 2)
    nb = len(barn.msgs)
    c2.send(b'+++')
    time.sleep(0.3)
    c2.send(client_frame(1, bytes([0x16, 8])))
    check("after '+++' no more frames are taken",
          not barn.wait(nb + 1, 1.5) and sum(1 for e in c2.events if e[0] == 'ack') == 1, c2.events)
    p.terminate()
    finish(p)
    barn.close()

    # --- 16 channels, the 17th is refused; member id into inn/lsci.cfg (any case)
    os.makedirs(os.path.join(work, 'inn'), exist_ok=True)
    with open(os.path.join(work, 'inn', 'lsci.cfg'), 'wb') as f:
        f.write(b' video = VGA320.DRV\r\n id = 123\r\n hostID = SIERRA')
    barn = Barn(PB)
    p = start(py, work)
    barn.accept(5)
    barn.wait(1, 3)
    barn.send(ip.PROXY_HELLO, 0, struct.pack('<I', 777), 0, 1)
    clients = []
    for _ in range(17):
        clients.append(Client(PC))
        time.sleep(0.05)
    check('17th client refused', clients[16].wait_closed(3) and not any(x.closed for x in clients[:16]))
    with open(os.path.join(work, 'inn', 'lsci.cfg'), 'rb') as f:
        lsci = f.read()
    # the original replaces the whole line, indentation included
    check('member id written', lsci == b' video = VGA320.DRV\r\nid = 0777\r\n hostID = SIERRA', lsci)
    for x in clients:
        try:
            x.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        x.sock.close()
    p.terminate()
    finish(p)
    barn.close()


def u32(b, off):
    return struct.unpack_from('<I', b, off)[0]


if __name__ == '__main__':
    main()
