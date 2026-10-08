#!/usr/bin/env python3
"""InnProxy - a portable port of the INN Barn proxy (InnProxy.exe 1.1k).

The ImagiNation Network client for DOS, running in DOSBox, dials
"localhost:30000" through DOSBox's emulated modem.  This program answers
that call, speaks INN's framed link protocol with the client and relays its
messages to the INN Barn server over TCP, and the server's replies back.

It is a port of the Windows program InnProxy.exe, version 1.1k (x64, built
February 2021), reconstructed from its machine code.  Python 3.8 or later,
standard library only; runs on macOS, Linux and Windows.  README.md says how
to use it, NOTES.md describes the protocol and where this port deviates.

Copyright (c) 2026 Gunther Schmidl.  MIT License (see LICENSE):
https://github.com/gschmidl/innproxy
"""

import argparse
import collections
import errno
import os
import selectors
import signal
import socket
import struct
import sys
import threading
import time

VERSION = '1.0'                 # this port
ORIG_VERSION = '1.1k'           # the InnProxy.exe it is a port of; its hello says so
VER_MAJOR, VER_MINOR, VER_LETTER = 1, 1, ord('k')
APP = 'InnProxy'

DEFAULT_BARN = 'cdnsrv.innbarn.com'
DEFAULT_BARN_PORT = 29000
DEFAULT_CLIENT = '127.0.0.1:30000'
DEFAULT_CLIENT_PORT = 30000
NUM_CHANNELS = 16

# log bits, as the original uses them (default mask 0x3f; XLogBit=n gives 2**(n+1)-1)
L_ALERT, L_ERR, L_INFO, L_DEBUG, L_TRACE = 0x01, 0x04, 0x10, 0x20, 0x40
L_MSGS, L_FRAMES = 0x200, 0x400

# host message types the proxy handles itself (header type >= 0x100)
PROXY_HELLO, NETWORK_STATUS, MULTI_MSG = 0x100, 0x101, 0x102
PROXY_INFO, DISCONNECT, PING, HOST_TEXT = 0x103, 0x104, 0x105, 0x107

MSG_NAMES = {
    0x000: 'AckMsg', 0x001: 'NakMsg', 0x002: 'SendMsg', 0x003: 'RSendMsg',
    0x004: 'ObjLockMsg', 0x005: 'LockQueueMsg', 0x006: 'ObjUnlockMsg',
    0x007: 'ObjNewMsg', 0x008: 'ObjIdMsg', 0x009: 'ObjFreeMsg',
    0x00a: 'GrpJoinMsg', 0x00b: 'GrpDelMsg', 0x00c: 'GrpMemMsg',
    0x00d: 'ObjSetIntMsg', 0x00e: 'ObjSetStrMsg', 0x00f: 'SetByteArrayMsg',
    0x010: 'SetIntArrayMsg', 0x011: 'GetMsg', 0x012: 'GetObjMsg',
    0x013: 'AddMsg', 0x014: 'SetEqMsg', 0x015: 'SetNeMsg', 0x016: 'LoginMsg',
    0x017: 'GameMsg', 0x018: 'GameKillMsg', 0x019: 'RmiMsg',
    0x01a: 'UserSetSidMsg', 0x01b: 'BBSMsg', 0x01c: 'SendManyMsg',
    0x01d: 'GameVerMsg', 0x01e: 'RegisterMsg', 0x01f: 'GrpPropMsg',
    0x020: 'EGetMsg', 0x021: 'SetMsg', 0x022: 'AccountingMsg',
    0x023: 'XferMsg', 0x024: 'HostInfoMsg', 0x025: 'EMailMsg',
    0x026: 'HintsMsg', 0x027: 'FileMsg', 0x028: 'UserInfoMsg',
    0x029: 'ObjExistsMsg', 0x02a: 'ServLoginMsg', 0x02b: 'LogoutMsg',
    0x02c: 'PasswordMsg', 0x02d: 'USRequestMsg', 0x02e: 'LoginVersionMsg',
    0x02f: 'WaitRoomRequestMsg', 0x030: 'SendUnsolicitedMsg',
    0x031: 'UserFindMsg', 0x032: 'MuteUnMuteMsg', 0x033: 'ObjNewListMsg',
    0x034: 'ObjFreeListMsg', 0x035: 'LoginVersion2.0Msg',
    0x036: 'PSessionMsg', 0x037: 'OLSignupMsg', 0x038: 'RemoteStatusMsg',
    0x039: 'SetIntGrpMsg', 0x03a: 'SetStrGrpMsg', 0x03b: 'LoginProdigyMsg',
    0x092: 'SysopLoginMsg', 0x093: 'SysopMsg', 0x094: 'DebugMsg',
    0x0ca: 'Golf3D_202Msg', 0x0cb: 'Golf3D_203Msg', 0x0cc: 'Golf3D_204Msg',
    0x0cd: 'Golf3D_205Msg', 0x0ff: 'ErrMsg', 0x100: 'ProxyHelloMsg',
    0x101: 'NetworkStatusMsg', 0x102: 'MultiMsg', 0x103: 'ProxyInfoMsg',
    0x104: 'DisconnectMsg', 0x105: 'PingMsg',
}

# client link framing
SOF, EOF, ESC = 0x81, 0x82, 0x1b
ESCAPED = (ESC, SOF, EOF)
CLIENT_HOSTS = (b'SIERRA', b'imagine', b'InnBarn', b'Owl')   # "c <host>\r" names
CLIENT_PING = b'\x00\x03\x00\x00\x00'

OFFLINE_TEXT = 'INN Barn OFFLINE!'
IDLE_TEXT = 'Idle so shutting Down!'

# the original's timings, in milliseconds
T_CONNECT_RETRY = 3000      # wait after a refused, timed-out or unreachable connect
T_CONNECT_ERROR = 1000      # wait after any other connect error
T_CONNECT_TIMEOUT = 21000   # what a blocking connect() takes to give up on Windows
T_FIRST_KEEPALIVE = 10000   # the first PingMsg goes this much earlier than T_KEEPALIVE
T_KEEPALIVE = 180000        # PingMsg to the host
T_IDLE = 600000             # no client and no client traffic: "Idle so shutting Down!"
T_OFFLINE_TICK = 500        # offline: one tick per this, or per connect attempt
OFFLINE_TICKS = 120         # ...and quit after this many
T_RESEND_GIVEUP = 3000      # an unacknowledged frame is dropped from the window
T_CLIENT_PING = 15000       # a quiet client gets a ping frame

# connect() errors after which the original waits 3 s and tries again quietly
_RETRY_QUIET = {errno.ETIMEDOUT, errno.ECONNREFUSED, errno.ENETUNREACH,
                getattr(errno, 'EHOSTUNREACH', -1), 10060, 10061, 10051, 10065}
_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                getattr(errno, 'WSAEWOULDBLOCK', 10035), 10035, 10036}


def now_ms():
    return int(time.monotonic() * 1000)


# ---------------------------------------------------------------- checksums

def _ccitt_table():
    t = []
    for b in range(256):
        x = b ^ (b >> 4)
        t.append(((x << 12) ^ (x << 5) ^ x) & 0xffff)
    return t


_CCITT = _ccitt_table()


def crc_link_update(crc, b):
    """One byte of CRC-16/CCITT-FALSE (poly 0x1021, MSB first): the link frames' CRC."""
    return ((crc << 8) & 0xffff) ^ _CCITT[(crc >> 8) ^ b]


def crc_host(data):
    """CRC-16/MCRF4XX (poly 0x8408 reflected, init 0xFFFF): the host messages' CRC."""
    crc = 0xffff
    for b in data:
        crc = (crc >> 4) ^ (((crc ^ b) & 0xf) * 0x1081)
        crc = (crc >> 4) ^ (((crc ^ (b >> 4)) & 0xf) * 0x1081)
    return crc


def encode_frame(seq, payload):
    """A data frame to the client: 81 crc crc seq LEN data.. 82, LEN/data escaped."""
    out = bytearray((SOF, 0, 0, seq))
    crc = crc_link_update(0xffff, seq)

    def put(b):
        nonlocal crc
        if b in ESCAPED:
            out.append(ESC)
            crc = crc_link_update(crc, ESC)
        out.append(b)
        crc = crc_link_update(crc, b)

    n = len(payload)
    if n < 0xff:
        put(n)
    else:
        out.append(0xff)
        crc = crc_link_update(crc, 0xff)
        put(n & 0xff)
        put((n >> 8) & 0xff)
    for b in payload:
        put(b)
    out.append(EOF)
    out[1] = crc & 0xff
    out[2] = crc >> 8
    return bytes(out)


def short_frame(code):
    """81 crc crc code 82: ACK (0x90|seq) or NAK (0x80|seq)."""
    crc = crc_link_update(0xffff, code)
    return bytes((SOF, crc & 0xff, crc >> 8, code, EOF))


def host_msg(mtype, chan, body=b'', word8=0, word12=0):
    """A message to the barn: 16-byte header (type, crc, length, channel, two dwords) + body."""
    m = bytearray(struct.pack('<HHHHII', mtype, 0, (16 + len(body)) & 0xffff,
                              chan & 0xffff, word8 & 0xffffffff, word12 & 0xffffffff))
    m += body
    struct.pack_into('<H', m, 2, crc_host(m))
    return m


def u16(b, off):
    return struct.unpack_from('<H', b, off)[0] if off + 2 <= len(b) else 0


def u32(b, off):
    return struct.unpack_from('<I', b, off)[0] if off + 4 <= len(b) else 0


def cstr(b):
    k = b.find(b'\0')
    return bytes(b if k < 0 else b[:k]).decode('latin-1')


# ------------------------------------------------------------------ logging

class Log:
    """The original's CLogFile: Log/InnProxy-YYYY-Mon-DD.log, CRLF lines,
    a new file each day and every 25,000,000 bytes (suffix A..Z)."""

    MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
    LIMIT = 25000000

    def __init__(self, directory, mask=0x3f, name=APP):
        self.dir = directory
        self.name = name
        self.mask = mask
        self.f = None
        self.idx = 0
        self.size = 0
        self.lines = 0
        self.yday = None
        self.lock = threading.RLock()
        if directory is not None:
            self._open()

    def _open(self):
        if self.f:
            try:
                self.f.close()
            except OSError:
                pass
            self.f = None
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError:
            pass
        tm = time.localtime()
        while True:
            fn = '%s-%04u-%s-%02u' % (self.name, tm.tm_year, self.MONTHS[tm.tm_mon - 1], tm.tm_mday)
            if self.idx:
                fn += chr(0x40 + self.idx)
            try:
                f = open(os.path.join(self.dir, fn + '.log'), 'ab')
            except OSError:
                return
            size = f.tell()
            if size >= self.LIMIT and self.idx <= 25:
                f.close()
                self.idx += 1
                continue
            break
        self.f = f
        self.size = size
        self.lines = 0
        self.yday = tm.tm_yday

    def _emit(self, text, prefix):
        if not self.f:
            return
        tm = time.localtime()
        if self.lines and tm.tm_yday != self.yday:
            self.idx = 0
            self._open()
            if not self.f:
                return
        s = ''
        if prefix:
            s = '%04u-%s-%02u %02u:%02u:%02u - (%6u) {0x%x}' % (
                tm.tm_year, self.MONTHS[tm.tm_mon - 1], tm.tm_mday, tm.tm_hour,
                tm.tm_min, tm.tm_sec, now_ms() & 0xffffffff, threading.get_native_id())
        s += text
        if s.endswith('\n'):
            s = s[:-1]
        data = (s + '\r\n').encode('latin-1', 'replace')
        try:
            self.f.write(data)
            self.f.flush()
        except OSError:
            return
        self.lines += 1
        self.size += len(data)
        if self.size >= self.LIMIT and self.idx < 26:
            self._open()

    def write(self, mask, text):
        if self.f and (self.mask & mask):
            with self.lock:
                self._emit('<0x%4.4x> %s' % (threading.get_native_id(), text), True)

    def dump(self, mask, data, text=None):
        n = len(data)
        eff = mask
        if not ((mask & 0x11) and n < 0x200):
            if (self.mask & 0x300) == 0x100:
                n = min(n, 0x20)
                if mask & 0x200:
                    eff |= 0x100
        if not self.f or not (self.mask & eff):
            return
        with self.lock:
            if text:
                self._emit(text, True)
            self._emit('  Memory Dump: 0x%08X   Size: 0x%x (%u)' % (0, n, n), not text)
            for off in range(0, n, 16):
                row = data[off:off + 16]
                hx = ''
                for k, c in enumerate(row):
                    hx += ' %2.2x' % c
                    if k % 4 == 3:
                        hx += ' '
                asc = ''.join(chr(c) if 0x20 <= c <= 0x7f else '.' for c in row)
                self._emit('      %8.8x %-53s%-16s  {0x%8.8x}' % (off, hx, asc, off), False)
            self._emit('', False)


# ------------------------------------------------------------- configuration

def _find_ci(directory, name):
    """Path of `name` in `directory`, matching the way Windows does (any case)."""
    p = os.path.join(directory, name)
    if os.path.exists(p):
        return p
    try:
        for e in os.listdir(directory or '.'):
            if e.lower() == name.lower():
                return os.path.join(directory, e)
    except OSError:
        pass
    return None


def read_ini(path):
    """{section: {key: value}}, lower-case names, first key wins - like GetPrivateProfileString."""
    data = {}
    try:
        with open(path, 'rb') as f:
            text = f.read().decode('latin-1')
    except (OSError, TypeError):
        return data
    sec = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(';'):
            continue
        if s.startswith('['):
            end = s.find(']')
            sec = (s[1:end] if end > 0 else s[1:]).strip().lower()
            data.setdefault(sec, {})
            continue
        if sec is None or '=' not in s:
            continue
        k, v = s.split('=', 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
            v = v[1:-1]
        data[sec].setdefault(k.strip().lower(), v)
    return data


def _atoi(s):
    s = s.lstrip(' \t\r\n\v\f')
    i = 1 if s[:1] in ('+', '-') else 0
    j = i
    while j < len(s) and '0' <= s[j] <= '9':
        j += 1
    v = int(s[i:j]) if j > i else 0
    return -v if s[:1] == '-' else v


def parse_addr(text):
    """'host[:port]' -> (host, port or 0), as the original splits BarnIP/ClientIP."""
    k = text.find(':')
    if k < 0:
        return text, 0
    return text[:k], _atoi(text[k + 1:]) & 0xffffffff


def _dotted(host):
    """The original's sscanf("%u.%u.%u.%u") path for a name that starts with a digit."""
    parts = [0, 0, 0, 0]
    i = 0
    for k in range(4):
        j = i
        while j < len(host) and '0' <= host[j] <= '9':
            j += 1
        if j == i:
            break
        parts[k] = int(host[i:j]) & 0xffffffff
        i = j
        if k < 3:
            if i >= len(host) or host[i] != '.':
                break
            i += 1
    a, b, c, d = parts
    v = d
    for x in (c, b, a):
        v = ((v << 8) | x) & 0xffffffff
    if v == 0xffffffff:
        return None
    return '%d.%d.%d.%d' % (v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff, v >> 24)


def resolve(host):
    """IPv4 address for BarnIP/ClientIP, picked the way the original picks it:
    a public address first, then a private or loopback one, then a private x.x.x.1."""
    if host[:1].isdigit():
        ip = _dotted(host)
        if ip is not None:
            return ip
    try:
        infos = socket.getaddrinfo(host or socket.gethostname(), None,
                                   socket.AF_INET, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    pub = other = dot1 = None
    for info in infos:
        ip = info[4][0]
        a = [int(x) for x in ip.split('.')]
        if a[1] == a[2] == a[3] == 0:
            continue
        if ip == '127.0.0.1':
            other = ip
            continue
        if (a[0] == 192 and a[1] == 168) or a[0] == 10 or (a[0] == 172 and 16 <= a[1] <= 31):
            if a[3] == 1:
                dot1 = ip
            elif other is None:
                other = ip
        elif pub is None:
            pub = ip
    return pub or other or dot1


class Config:
    def __init__(self, ini_path, args):
        sec = read_ini(ini_path).get('innbarn', {})

        def gint(key):
            v = sec.get(key.lower())
            return max(_atoi(v), 0) if v is not None else 0

        def gstr(key, default):
            v = sec.get(key.lower())
            return default if v is None else v

        self.disable_shutdown = gint('DisableShutdown') != 0
        xlog = gint('XLogBit') if args.log_level is None else max(args.log_level, 0)
        self.log_mask = ((1 << ((xlog + 1) & 31)) - 1) & 0xffffffff if xlog else 0x3f
        self.watchdog = gint('WatchDogReboot') != 0
        self.client_net = gstr('ClientNet', '')
        self.no_mutex = gint('NoMutex') != 0
        barn = args.barn if args.barn is not None else gstr('BarnIP', DEFAULT_BARN)
        self.barn_host, self.barn_port = parse_addr(barn)
        if not self.barn_port:
            self.barn_port = DEFAULT_BARN_PORT
        self.barn_port &= 0xffff
        client = args.listen if args.listen is not None else gstr('ClientIP', DEFAULT_CLIENT)
        self.client_host, self.client_port = parse_addr(client)
        if not self.client_port:
            # the original sets the *barn* port to 30000 here and leaves this one 0
            self.client_port = DEFAULT_CLIENT_PORT
        self.client_port &= 0xffff


# ------------------------------------------------------------------ channels

class Channel:
    """One connected INN client (a DOSBox modem call)."""

    def __init__(self, num):
        self.num = num
        self.sock = None
        self.reset(0)

    def reset(self, t):
        self.state = 0              # 0 disconnected, 1 modem mode, 2 data mode
        self.modem = True           # bytes go to the modem/PAD emulation
        self.rx = bytearray()
        self.out = bytearray()
        self.queue = collections.deque()   # payloads from the host, not yet framed
        self.unacked = [None] * 8   # frames sent, by sequence number
        self.sent_at = [0] * 8
        self.txseq = 0
        self.rxseq = 7
        self.partial = b''          # incomplete multi-message record
        self.last_activity = t
        self.last_tick = t
        self.login_id = 0
        self.closing = False
        self.peer_closed = False    # the client closed (FIN): unnoticed, as in the original
        self.probe_at = 0           # when to look for the reset that our next frame provokes
        self.probes_left = 0

    def unacked_count(self):
        return sum(1 for f in self.unacked if f is not None)


# --------------------------------------------------------------------- proxy

class Proxy:
    def __init__(self, cfg, log, workdir, quiet=False):
        self.cfg = cfg
        self.log = log
        self.workdir = workdir
        self.quiet = quiet
        self.sel = selectors.DefaultSelector()
        self.chans = [None] + [Channel(i) for i in range(1, NUM_CHANNELS + 1)]
        self.free = collections.deque(range(1, NUM_CHANNELS + 1))
        self.lsock = None
        self.next_listen = 0
        self.pid_file = None
        self.quit = False
        self.quit_reason = ''
        # barn (host gateway) state
        self.barn = None
        self.barn_up = False        # data_140035888: hello sent, relaying
        self.barn_connecting = False
        self.connect_deadline = 0
        self.next_connect = 0
        self.barn_rx = bytearray()
        self.barn_out = bytearray()
        self.proxy_id = 0           # given by the host in every proxy message (+0x0c)
        self.member_id = 0
        self.title = ''
        self.players = self.max_players = self.proxies = self.max_proxies = 0
        self.hosts = self.host_runtime = 0
        self.player_list = []
        self.offline_text = ''
        self.offline_since = 0
        self.offline_count = 0
        self.last_rx = 0
        t = now_ms()
        self.client_msg_time = t    # last client-bound message from the host
        self.keepalive_mark = t + 3 * T_FIRST_KEEPALIVE

    # ----------------------------------------------------------- utilities

    def say(self, text):
        if self.quiet:
            return
        try:
            print(time.strftime('%H:%M:%S ') + text, flush=True)
        except (OSError, ValueError):
            pass

    def _reg(self, sock, data, write, read=True):
        events = (selectors.EVENT_READ if read else 0) | (selectors.EVENT_WRITE if write else 0)
        try:
            key = self.sel.get_key(sock)
        except (KeyError, ValueError):
            self.sel.register(sock, events, data)
            return
        if key.events != events:
            self.sel.modify(sock, events, data)

    def _unreg(self, sock):
        try:
            self.sel.unregister(sock)
        except (KeyError, ValueError):
            pass

    def log_msg(self, direction, msg):
        """sub_140002150: names a host message in the log."""
        mtype = u16(msg, 0)
        name = MSG_NAMES.get(mtype)
        n = min(u16(msg, 4), len(msg))
        if name is None:
            self.log.dump(L_ALERT, msg[:n], '%s  @@@ Unknown Message 0x%x (%u)' % (direction, mtype, mtype))
            return
        if not (self.log.mask & L_MSGS):
            return
        text = '%s  %s' % (direction, name)
        if mtype <= 1 and len(msg) > 0x14 and msg[0x14] in MSG_NAMES:
            text += '  %s' % MSG_NAMES[msg[0x14]]
        self.log.dump(L_MSGS, msg[:n], text)

    # ------------------------------------------------------------- barn link

    def send_barn(self, msg):
        if not (self.barn and (self.barn_up or u16(msg, 0) == PROXY_HELLO)):
            return False
        if len(msg) < 16:
            self.log.dump(L_ERR, msg, 'Bad message length')
            return False
        self.barn_out += msg
        self._flush_barn()
        self.log_msg('Host <-- Proxy', msg)
        return self.barn is not None

    def _flush_barn(self):
        while self.barn_out and self.barn:
            try:
                n = self.barn.send(self.barn_out)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as e:
                self.log.write(L_ERR, 'failed send to host error: %d  remaining:%u'
                               % (e.errno or 0, len(self.barn_out)))
                self.barn_lost(e.errno or 0)
                return
            del self.barn_out[:n]
        if self.barn:
            self._reg(self.barn, self.on_barn, bool(self.barn_out) or self.barn_connecting)

    def barn_try_connect(self):
        self.gate_iteration()
        if self.quit:
            return
        t = now_ms()
        ip = resolve(self.cfg.barn_host)
        if ip is None:
            self.connect_failed(errno.EADDRNOTAVAIL)
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            self.log.write(L_ERR, "Couldn't create socket for Gateway connection.")
            self.next_connect = t + 1000
            return
        s.setblocking(False)
        self.barn = s
        self.barn_rx.clear()
        self.barn_out.clear()
        err = s.connect_ex((ip, self.cfg.barn_port))
        if err == 0:
            self.barn_connected()
        elif err in _IN_PROGRESS:
            self.barn_connecting = True
            self.connect_deadline = t + T_CONNECT_TIMEOUT
            self._reg(s, self.on_barn, True)
        else:
            self.connect_failed(err)

    def connect_failed(self, err):
        t = now_ms()
        if not self.offline_since:
            self.offline_text = OFFLINE_TEXT
            self.offline_since = t | 1
            self.offline_count = 1
            self.say('%s (can\'t reach %s:%u)' % (OFFLINE_TEXT, self.cfg.barn_host, self.cfg.barn_port))
        if self.barn:
            self._unreg(self.barn)
            self.barn.close()
        self.barn = None
        self.barn_connecting = False
        if err in _RETRY_QUIET:
            self.next_connect = t + T_CONNECT_RETRY
        else:
            self.log.write(L_ERR, "Couldn't connect to Host Gateway.\n")
            self.next_connect = t + T_CONNECT_ERROR

    def barn_connected(self):
        t = now_ms()
        self.barn_connecting = False
        self.offline_text = ''
        self.offline_since = 0
        self.offline_count = 1
        self._reg(self.barn, self.on_barn, False)
        body = bytes((VER_MAJOR, VER_MINOR, VER_LETTER, 0))
        self.send_barn(host_msg(PROXY_HELLO, 0, body, self.proxy_id, 0))
        if not self.barn:
            return
        self.barn_up = True
        self.keepalive_mark = t + T_FIRST_KEEPALIVE
        self.say('Connected to INN Barn %s:%u' % (self.cfg.barn_host, self.cfg.barn_port))

    def barn_lost(self, err):
        """Lost contact: the original closes every client and stops its gateway thread."""
        if self.barn is None:
            return
        self.log.write(L_ERR, '@@@  gate socket: 0x%x rx failure.  err:%d\n' % (self.barn.fileno(), err))
        self._unreg(self.barn)
        try:
            self.barn.close()
        except OSError:
            pass
        self.barn = None
        was_up = self.barn_up
        self.barn_up = False
        self.barn_connecting = False
        self.barn_rx.clear()
        self.barn_out.clear()
        closed = self.close_all('lost contact with host')
        if closed:
            self.say('Lost Contact with host. INN App sessions are non-restorable so shutdown!')
        elif was_up:
            self.say('Lost contact with INN Barn')
        # unlike the original (which then stays dead until restarted), try again
        self.next_connect = now_ms() + 1000

    def on_barn(self, mask):
        if self.barn_connecting:
            err = self.barn.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err:
                self.connect_failed(err)
            elif mask & selectors.EVENT_WRITE:
                self.barn_connected()
            return
        if mask & selectors.EVENT_WRITE:
            self._flush_barn()
        if mask & selectors.EVENT_READ and self.barn:
            self.barn_readable()

    def barn_readable(self):
        try:
            data = self.barn.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            self.barn_lost(e.errno or 0)
            return
        if not data:
            self.barn_lost(0)
            return
        self.last_rx = now_ms()
        buf = self.barn_rx
        buf += data
        off = 0
        while len(buf) - off >= 16:
            mlen = u16(buf, off + 4)
            if mlen < 16:
                self.log.dump(L_ERR, buf[off:off + 16], 'Bad message length')
                self.barn_lost(0)
                return
            if off + mlen > len(buf):
                break
            msg = bytearray(buf[off:off + mlen])
            off += mlen
            self.host_message(msg)
            if self.barn is None or self.quit:
                return
        del buf[:off]

    def host_message(self, msg):
        mtype = u16(msg, 0)
        num = u16(msg, 6)
        if mtype >= 0x100:
            self.log_msg('Host --> Proxy', msg)
            self.proxy_message(msg)
            return
        ch = self.chans[num] if 1 <= num <= NUM_CHANNELS else None
        if mtype == 0 and len(msg) > 0x14 and msg[0x14] == 0x16 and u32(msg, 8) and ch:
            ch.login_id = u32(msg, 8)
        if mtype not in MSG_NAMES:
            self.log.dump(L_ERR, msg, 'Tossing unknown INN message %u' % mtype)
            return
        self.client_msg_time = now_ms()
        self.offline_since = 0
        self.log_msg('Host --> Proxy', msg)
        if ch is None or ch.sock is None:
            # the original queues it anyway (for a later session on that channel)
            self.log.write(L_INFO, 'chan: %u is not connected, dropping %s' % (num, MSG_NAMES[mtype]))
            return
        ch.queue.append(bytes(msg[16:]))
        self.log.write(L_TRACE, 'chan: %u  un-ack-frames:(%u)  has   %u gate-q-msgs'
                       % (num, ch.unacked_count(), len(ch.queue)))

    def proxy_message(self, msg):
        """sub_140002cb0: messages for the proxy itself."""
        mtype = u16(msg, 0)
        self.proxy_id = u32(msg, 0x0c)
        if mtype == PROXY_HELLO:
            self.member_id = u32(msg, 0x10)
            if u16(msg, 4) > 0x20:
                self.title = cstr(msg[0x14:])
                self.say('INN Barn: %s' % self.title)
            if self.member_id:
                self.say('Update Member Id.  id=%u' % self.member_id)
                self.update_lsci(self.member_id)
        elif mtype == DISCONNECT:
            num = u16(msg, 6)
            if 1 <= num <= NUM_CHANNELS and self.chans[num].sock is not None:
                self.close_channel(self.chans[num], 'host disconnected it')
        elif mtype == PING:
            if len(msg) < 0x39:
                return
            ts = int(time.perf_counter_ns() // 100)      # QueryPerformanceCounter at 10 MHz
            idx = msg[0x38]
            if idx < 5:
                struct.pack_into('<Q', msg, 0x10 + idx * 8, ts & 0xffffffffffffffff)
            msg[0x38] = (idx + 1) & 0xff
            if msg[0x38] < 5:
                a, b = u32(msg, 8), u32(msg, 0x0c)
                struct.pack_into('<II', msg, 8, b, a)
                n = u16(msg, 4)
                struct.pack_into('<H', msg, 2, 0)
                struct.pack_into('<H', msg, 2, crc_host(msg[:n]))
                self.send_barn(msg[:n])
            else:
                span = (((struct.unpack_from('<Q', msg, 0x30)[0] - struct.unpack_from('<Q', msg, 0x10)[0]
                          + 1) & 0xffffffffffffffff) >> 1)
                freq = 10000000
                self.log.write(L_DEBUG, 'Msg Speed  %u.%3.3ums' % (span // freq, (span % freq) * 1000 // freq))
        elif mtype == PROXY_INFO:
            old = self.players
            self.players = u32(msg, 0x10)
            self.hosts = u32(msg, 0x14)
            self.proxies = u32(msg, 0x18)
            self.host_runtime = u32(msg, 0x1c)
            self.max_proxies = u32(msg, 0x24)
            self.max_players = u32(msg, 0x28)
            names = []
            if u16(msg, 4) > 0x2c and self.players:
                p = 0x2c
                while p < len(msg) and msg[p]:
                    s = cstr(msg[p:])
                    names.append(s)
                    p += len(s) + 1
            self.player_list = names
            self.log.write(L_DEBUG, 'Number of Players: %u  max: %u   Number of Proxys: %u  max: %u   '
                                    'Number of Hosts: %u'
                           % (self.players, self.max_players, self.proxies, self.max_proxies, self.hosts))
            if self.players != old:
                line = 'INN Barn: %u player%s online' % (self.players, '' if self.players == 1 else 's')
                if names:
                    line += ': ' + ', '.join(names)
                self.say(line)
        elif mtype == HOST_TEXT:
            flag = msg[0x10] if len(msg) > 0x10 else 0
            text = cstr(msg[0x11:])
            self.log.write(L_INFO, 'Host Message: %s' % text)
            self.say('Host Message: %s' % text)
            if flag:
                self.close_all('host shut the proxy down')
                self.stop('the host asked the proxy to shut down')

    def update_lsci(self, member_id):
        """sub_1400025e0: writes 'id = 0<member id>' into INN\\LSCI.CFG below the working directory."""
        d = _find_ci(self.workdir, 'INN')
        path = _find_ci(d, 'LSCI.CFG') if d else None
        if not path:
            return False
        try:
            with open(path, 'rb') as f:
                lines = f.read().splitlines(keepends=True)
        except OSError:
            return False
        new = b'id = 0%u\r\n' % member_id
        out = []
        found = False
        for line in lines:
            if not found and _is_id_line(line):
                out.append(new)
                found = True
            else:
                out.append(line)
        if not found:
            if out and not out[-1].endswith(b'\n'):
                out[-1] += b'\r\n'
            out.append(new)
        tmp = os.path.join(d, 'LSCI%u.cfg' % member_id)
        try:
            with open(tmp, 'wb') as f:
                f.write(b''.join(out))
            os.replace(tmp, path)
        except OSError:
            return False
        return True

    def gate_iteration(self):
        """The checks at the top of the original's gateway loop."""
        t = now_ms()
        if self.offline_since and t - self.offline_since >= T_OFFLINE_TICK:
            self.offline_since = t | 1
            self.offline_count += 1
            if self.offline_count >= OFFLINE_TICKS:
                self.stop(self.offline_text or OFFLINE_TEXT)

    def gate_timers(self):
        t = now_ms()
        if not self.barn_up:
            return
        self.gate_iteration()
        if t - self.keepalive_mark >= T_KEEPALIVE:
            self.keepalive_mark = t
            body = bytearray(41)
            struct.pack_into('<Q', body, 0, int(time.perf_counter_ns() // 100))
            body[40] = 1
            self.send_barn(host_msg(PING, 0, bytes(body), self.proxy_id, 0))
        if (self.barn_up and not self.offline_since and t - self.client_msg_time >= T_IDLE
                and not self.cfg.disable_shutdown):
            self.client_msg_time = t
            if all(self.chans[i].sock is None for i in range(1, NUM_CHANNELS + 1)):
                self.offline_text = IDLE_TEXT
                self.offline_since = t | 1
                self.offline_count = 1
                self.say(IDLE_TEXT)

    # ---------------------------------------------------------- client side

    def open_listener(self):
        t = now_ms()
        host = resolve(self.cfg.client_host) or '0.0.0.0'
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            self.log.write(L_ERR, 'Create ListenSock failed')
            self.next_listen = t + 1000
            return None
        try:
            if os.name == 'nt':
                s.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_EXCLUSIVEADDRUSE', -5), 1)
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        try:
            s.bind((host, self.cfg.client_port))
        except OSError as e:
            self.log.write(L_ERR, "ListenSock 'bind' failed\n")
            s.close()
            self.next_listen = t + 1000
            return e
        try:
            s.listen(5)
        except OSError as e:
            self.log.write(L_ERR, "ListenSock 'listen' failed.\n")
            s.close()
            self.next_listen = t + 1000
            return e
        s.setblocking(False)
        self.lsock = s
        self._reg(s, self.on_accept, False)
        return None

    def on_accept(self, mask):
        try:
            s, _ = self.lsock.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.log.write(L_ERR, 'accept failed\n')
            return
        if not self.free:
            s.close()
            self.log.write(L_ALERT, 'All channels are already full!')
            self.say('Inn Proxy - ERROR - Channels Full: All message channels are full! Try again later!')
            return
        num = self.free.popleft()
        ch = self.chans[num]
        t = now_ms()
        ch.reset(t)
        ch.sock = s
        ch.state = 1
        s.setblocking(False)
        self.log.write(L_DEBUG, '***  %u) Socket# 0x%x Connection established to innApp' % (num, s.fileno()))
        self._reg(s, (self.on_client, ch), False)
        if self.offline_since and self.offline_text == IDLE_TEXT:
            self.offline_since = 0          # someone came after all: don't quit under them
        self.say('INN client connected (channel %u)' % num)

    def send_client(self, ch, data):
        if ch.sock is None or ch.closing:
            return False
        ch.out += data
        self._flush_client(ch)
        return not ch.closing

    def _flush_client(self, ch):
        sent = False
        while ch.out and ch.sock is not None:
            try:
                n = ch.sock.send(ch.out)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as e:
                self.log.write(L_ERR, 'client send has an error: %u' % (e.errno or 0))
                ch.closing = True
                return
            del ch.out[:n]
            sent = True
        if ch.sock is None:
            return
        if ch.peer_closed:
            # nothing more to read; the frame just sent makes the far end answer with a reset
            if sent:
                ch.probe_at = now_ms() + 100
                ch.probes_left = 1
            if ch.out:
                self._reg(ch.sock, (self.on_client, ch), True, read=False)
            else:
                self._unreg(ch.sock)
            return
        self._reg(ch.sock, (self.on_client, ch), bool(ch.out))

    def client_read_failed(self, ch, err):
        self.log.write(L_ERR, 'chan: %u  iDEV::SOCK 0x%x failed read' % (ch.num, ch.sock.fileno()))
        self.log.write(L_ERR, 'chan: %u) Read failed   bFail=true  err=%d\n' % (ch.num, err))
        ch.closing = True

    def probe_client(self, ch):
        """After the client closed: a reset in reply to our last frame ends the channel.

        Windows reports the reset on the next recv(), which is how the original sees it;
        BSD/macOS too. Linux keeps returning end-of-file there and only sets SO_ERROR."""
        ch.probe_at = 0
        try:
            err = ch.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except OSError as e:
            err = e.errno or -1
        if not err:
            try:
                ch.sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                pass
            except OSError as e:
                err = e.errno or -1
        if err:
            self.client_read_failed(ch, err)
        elif ch.probes_left:
            ch.probes_left -= 1
            ch.probe_at = now_ms() + 900

    def on_client(self, ch, mask):
        if ch.sock is None:
            return
        if mask & selectors.EVENT_WRITE:
            self._flush_client(ch)
        if mask & selectors.EVENT_READ and ch.sock is not None and not ch.closing and not ch.peer_closed:
            try:
                data = ch.sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                self.client_read_failed(ch, e.errno or 0)
                return
            if not data:
                # The INN client hung up cleanly. Like the original (which skips a
                # recv() of 0) the proxy doesn't notice: the channel stays, nothing goes
                # to the host, and only the reset that its next frame (a ping after 15 s,
                # or a host message) provokes closes the channel. The client calls back
                # in between and the host moves the session to the new channel; telling
                # the host at once makes it drop the session.
                ch.peer_closed = True
                self.log.write(L_DEBUG, 'chan: %u  innApp closed the connection' % ch.num)
                self._flush_client(ch)
                return
            ch.last_activity = now_ms()
            ch.rx += data
            self.client_data(ch)
        if len(ch.rx) > 65536:
            self.log.dump(L_ERR, ch.rx[:64], 'chan: %u throwing away %u of %u bytes' % (ch.num, len(ch.rx), len(ch.rx)))
            ch.rx.clear()

    def client_data(self, ch):
        """The original channel thread's loop after a recv()."""
        while not ch.closing:
            if not ch.modem:
                ret, need_more = self.process_frames(ch)
                if ret <= 0 or need_more or not ch.modem:
                    break
            if not self.process_modem(ch) or ch.modem or not ch.rx:
                break

    def process_modem(self, ch):
        """sub_140004890: what a modem and a packet network's PAD would answer."""
        buf = ch.rx
        num = ch.num
        i = 0
        ok = True
        while i < len(buf):
            b = buf[i]
            rem = len(buf) - i
            if b in (0x0d, 0x2b):
                self.log.dump(L_DEBUG, buf[i:], 'chan: %u  in modem: strip a <CR> or <+>\n' % num)
                i += 1
                continue
            if b == 0x40 and rem >= 2 and buf[i + 1] == 0x44:
                self.log.write(L_DEBUG, '%u) modem: rcvd @D<cr>    so twx @D<cr>\n' % num)
                self.send_client(ch, b'@D\r')
                i += 2
                continue
            if rem <= 2:
                break
            if b == 0x63 and buf[i + 1] == 0x20:          # "c <host>\r"
                j = buf.find(b'\r', i + 2)
                if j < 0:
                    break
                name = bytes(buf[i + 2:j])
                i = j + 1
                if name not in CLIENT_HOSTS:
                    continue
                if self.barn_up:
                    reply = name + b' CONNECTED\r'
                    ch.state = 2
                    ch.modem = False
                    self.log.dump(L_FRAMES, reply, 'chan: %u  modem: rcvd CONNECT so twx to innApp' % num)
                    if not self.send_client(ch, reply):
                        self.log.write(L_ERR, 'chan: %u Error sending raw.' % num)
                    self.say('INN client on channel %u logged on to %s' % (num, name.decode('latin-1')))
                    break                                  # the rest is framed data
                reply = b'NO ANSWER\r'
                self.log.dump(L_FRAMES, reply, 'modem: No host so rejecting connect attempt from innApp')
                self.send_client(ch, reply)
                continue
            if b == 0x41 and buf[i + 1] == 0x54:          # "AT...\r"
                j = i + 2
                if buf[j:j + 2] == b'DT':
                    reply = b'CONNECT\r'
                    j += 2
                    self.log.write(L_DEBUG, '%u) ATDT command\n' % num)
                else:
                    reply = b'OK\r'
                    self.log.write(L_DEBUG, '%u) AT command\n' % num)
                k = buf.find(b'\r', j)
                if k < 0:
                    break
                cmd = bytes(buf[i:k + 1])
                i = k + 1
                self.log.write(L_DEBUG, '%u) remainder  %d\n' % (num, len(buf) - i))
                self.log.dump(L_FRAMES, cmd + b'\0', 'modem: innProxy --> innApp')
                if not self.send_client(ch, reply):
                    self.log.write(L_ERR, '%d) Write pipe failed\n' % num)
                    ok = False
                    break
                continue
            self.log.write(L_DEBUG, '%d) Tossing bytes\n' % num)
            k = buf.find(b'\r', i)
            i = len(buf) if k < 0 else k + 1
            self.log.dump(L_FRAMES, buf[i:], ' post toss remainders')
        del buf[:i]
        return ok

    def process_frames(self, ch):
        """sub_140003e10: frames from the INN client."""
        buf = ch.rx
        num = ch.num
        need_more = False
        ret = 1
        if len(buf) < 5:
            self.log.dump(L_TRACE, buf, 'PRE loop < 5 bytes, bypass to refill!')
        mark = '<'
        while len(buf) >= 5 and not ch.closing:
            n = len(buf)
            self.log.dump(L_TRACE, buf, '%s%s%s top - chan: %u  proxy <-- innApp (remaining bytes in buffer)'
                          % (mark, mark, mark, num))
            mark = '.'
            if n == 5 and buf[4] == ESC:
                need_more = True
                break
            b0 = buf[0]
            if b0 == SOF:
                if buf[4] == EOF:                         # ACK of one of our frames
                    seq = buf[3]
                    crc = crc_link_update(0xffff, seq)
                    rx = buf[1] | (buf[2] << 8)
                    if crc != rx:
                        self.log.dump(L_ERR, buf, 'chan %u-%u - ERRROR Crc calc: 0x%x vs 0x%x in Super Frame '
                                      'message DUMPING BUFFER\n' % (num, seq & 7, crc, rx))
                    ch.unacked[seq & 7] = None
                    ch.sent_at[seq & 7] = 0
                    del buf[:5]
                    continue
                r = self.data_frame(ch)
                if r == 'more':
                    need_more = True
                    break
                if r == 'next':
                    continue
                ret = r
                break
            if b0 == 0x2b:
                # The original sets the modem flag here and its exit code puts the old value
                # back, so the '+' stays at the front and the channel takes no more frames;
                # it keeps sending (pings, host messages), which is what eventually ends it
                # after a hang-up. Kept as is: the host is used to exactly that.
                self.log.dump(L_INFO, buf, "chan: %u '+' found, switching to modem" % num)
                break
            i = 0
            while i < n:
                c = buf[i]
                if c == SOF:
                    break
                if c == ESC:
                    if n - i == 1:
                        break
                    if buf[i + 1] == SOF:
                        i += 2
                        continue
                i += 1
            self.log.dump(L_ERR, buf, 'chan: %u throwing away %u of %u bytes' % (num, i, n))
            del buf[:i]
        if buf:
            self.log.dump(L_TRACE, buf, '>>> bot - chan: %u  nAvail:%u  proxy <-- innApp (remaining bytes in buffer '
                          'xit)  bModem:%d fill:%d  ret:%d' % (num, len(buf), ch.modem, need_more, ret))
        return ret, need_more

    def data_frame(self, ch):
        """One data frame at the start of ch.rx -> 'next', 'more', 0 or -1."""
        buf = ch.rx
        num = ch.num
        n = len(buf)
        crc = crc_link_update(0xffff, buf[3])
        length = raw_length = buf[4]
        crc = crc_link_update(crc, length)
        i = 5
        if length == ESC:
            length = buf[5]
            crc = crc_link_update(crc, length)
            i = 6
        out = bytearray()
        while True:
            if i >= n:
                return 'more'
            d = buf[i]
            if d == EOF:
                break
            if d == ESC:
                crc = crc_link_update(crc, ESC)
                i += 1
                if i >= n:
                    return 'more'
                d = buf[i]
                if d not in ESCAPED:
                    self.log.dump(L_ERR, buf, 'chan: %u 0x81...0x81 Bad ESCAPE  tossing %u bytes' % (num, i))
                    del buf[:i]
                    return 'next'
            elif d == SOF:
                self.log.dump(L_ERR, buf, 'chan: %u 0x81...0x81 Bad start  tossing %u bytes' % (num, i))
                del buf[:i]
                return 'next'
            out.append(d)
            crc = crc_link_update(crc, d)
            i += 1
        end = i + 1
        if crc != (buf[1] | (buf[2] << 8)):
            ch.rxseq = (ch.rxseq + 1) & 7
            text = 'chan: %u-%u ...bad crc innProxy Super Frame ***NAK*** --> innApp, rcvd message' % (num, ch.rxseq)
            frame = short_frame(0x80 | ch.rxseq)
            ok = self.send_client(ch, frame)
            self.log.dump(L_FRAMES, frame, text)
            if not ok:
                self.log.dump(L_ERR, frame, 'chan: %u-%u - ERROR Send Super Frame to Client error. DUMPING IT'
                              % (num, ch.rxseq))
                return -1
            self.log.dump(L_ERR, buf, text)
            del buf[:end]
            return 'next'
        seq = buf[3] & 7
        ch.rxseq = seq
        frame = short_frame(0x90 | seq)
        ok = self.send_client(ch, frame)
        self.log.dump(L_FRAMES, frame, 'chan: %u-%u  ...innProxy Super Frame ACK --> innApp, rcvd message' % (num, seq))
        if not ok:
            self.log.dump(L_ERR, frame, 'chan: %u-%u - ERROR Send Super Frame to Client error. DUMPING IT' % (num, seq))
            return -1
        del buf[:end]
        cnt = len(out)
        if length == cnt:                                 # one message
            mtype = out[0] if cnt else EOF
            body = bytes(out)
        else:                                             # length-prefixed records, maybe split
            # the first record's length goes in as it came, escape byte and all (as in the original)
            records = ch.partial + bytes((raw_length,)) + bytes(out)
            ch.partial = b''
            p = 0
            total = len(records)
            while True:
                rlen = records[p]
                if rlen == 0:
                    self.log.dump(L_ERR, bytes(16) + records, '@@@ Error!!! %u-%u multi-msg packet with sub (0) '
                                  'length!' % (num, seq))
                    return 'next'
                q = p + 1 + rlen
                if q > total:
                    break
                p = q
                if p == total:
                    break
            if p < total:
                ch.partial = records[p:]
            mtype = MULTI_MSG
            body = records[:p]
        if self.barn_up:
            if not self.send_barn(host_msg(mtype, num, body, self.proxy_id, 0)):
                self.log.write(L_ERR, 'chan: %u ERROR Send to host failed' % num)
                return 0
        return 'next'

    def channel_tick(self, ch):
        """The window, resend give-up and keep-alive part of the channel loop."""
        t = now_ms()
        ch.last_tick = t
        if ch.peer_closed and ch.probe_at and t >= ch.probe_at:
            self.probe_client(ch)
            if ch.closing:
                return
        slot = ch.txseq
        if ch.unacked[slot] is not None and t - ch.sent_at[slot] > T_RESEND_GIVEUP:
            ch.unacked[slot] = None
            ch.sent_at[slot] = 0
            self.log.write(L_TRACE, ' chan: %u-%u innProxy --> innApp (NUKE)' % (ch.num, slot))
        if ch.unacked[slot] is not None:
            return
        if ch.queue:
            self.log.write(L_TRACE, 'channel #%u-%u has %u messages to send' % (ch.num, slot, len(ch.queue)))
            payload = ch.queue.popleft()
            ch.unacked[slot] = payload
            ch.txseq = (slot + 1) & 7
            frame = encode_frame(slot, payload)
            self.log.dump(L_FRAMES, frame, 'chan: %u-%u  innProxy --> innApp' % (ch.num, slot))
            self.send_client(ch, frame)
            ch.sent_at[slot] = now_ms()
        elif t - ch.last_activity > T_CLIENT_PING:
            ch.last_activity = t
            if ch.modem:
                return
            ch.unacked[slot] = CLIENT_PING
            ch.sent_at[slot] = t
            ch.txseq = (slot + 1) & 7
            frame = encode_frame(slot, CLIENT_PING)
            self.log.dump(L_FRAMES, frame, 'chan: %u-%u ping (is innapp still alive?)' % (ch.num, slot))
            if not self.send_client(ch, frame):
                ch.unacked[slot] = None
                ch.sent_at[slot] = 0
                self.log.write(L_ERR, 'chan: %u Frame Ping Failure so dead!.' % ch.num)

    def channel_deadline(self, ch):
        slot = ch.txseq
        if ch.unacked[slot] is not None:
            t = ch.sent_at[slot] + T_RESEND_GIVEUP + 1
        elif ch.queue:
            t = ch.last_tick + 2
        else:
            t = ch.last_activity + T_CLIENT_PING + 1
        if ch.peer_closed and ch.probe_at:
            t = min(t, ch.probe_at)
        return t

    def close_channel(self, ch, why):
        if ch.sock is None:
            return
        num = ch.num
        self.log.write(L_INFO, 'Shutting down app channel#%d' % num)
        self._unreg(ch.sock)
        try:
            ch.sock.close()
        except OSError:
            pass
        ch.sock = None
        self.log.write(L_DEBUG, 'Channel %u thread exit' % num)
        if self.barn_up:
            self.send_barn(host_msg(DISCONNECT, num, b'', self.proxy_id, 0))
        ch.reset(now_ms())
        self.free.append(num)
        self.say('INN client on channel %u disconnected (%s)' % (num, why))

    def close_all(self, why):
        n = 0
        for i in range(1, NUM_CHANNELS + 1):
            if self.chans[i].sock is not None:
                self.close_channel(self.chans[i], why)
                n += 1
        return n

    # ------------------------------------------------------------ main loop

    def stop(self, reason):
        if not self.quit:
            self.quit = True
            self.quit_reason = reason

    def run(self):
        self.log.write(L_INFO, '\n\n____________________________________________________')
        self.log.write(L_INFO, '====================================================\n\n')
        self.log.write(L_INFO, 'INN Proxy ver: %u.%u.%c' % (VER_MAJOR, VER_MINOR, VER_LETTER))
        self.log.write(L_INFO, 'innproxy.py %s, Python %s on %s'
                       % (VERSION, sys.version.split()[0], sys.platform))
        if self.cfg.watchdog:
            self.log.write(L_INFO, 'WatchDogReboot is ignored: this port never reboots the computer')
        if self.cfg.client_net not in ('', 'Sock'):
            self.log.write(L_INFO, "ClientNet=%s is not supported, using 'Sock'" % self.cfg.client_net)
        err = self.open_listener()
        if err is not None and not self.cfg.no_mutex:
            # the original's single-instance mutex; here the listening port plays that part
            busy = err.errno in (errno.EADDRINUSE, 10048, 10013)
            if busy:
                msg = "'%s' is already running. Killing this instance! (%s:%u is in use)" % (
                    APP, self.cfg.client_host, self.cfg.client_port)
            else:
                msg = "can't listen on %s:%u: %s" % (self.cfg.client_host, self.cfg.client_port, err.strerror)
            self.log.write(L_INFO, msg)
            self.say(msg)
            return 0 if busy else 1
        self.say('innproxy.py %s listening on %s:%u, INN Barn is %s:%u'
                 % (VERSION, self.cfg.client_host, self.cfg.client_port, self.cfg.barn_host, self.cfg.barn_port))
        if self.pid_file:
            # only once the port is ours, so a second copy can't overwrite the first one's id
            try:
                with open(self.pid_file, 'w') as f:
                    f.write('%d\n' % os.getpid())
            except OSError as e:
                self.say("can't write %s: %s" % (self.pid_file, e.strerror))
                self.pid_file = None
        while not self.quit:
            t = now_ms()
            if self.lsock is None and t >= self.next_listen:
                self.open_listener()
            if self.barn is None and t >= self.next_connect:
                self.barn_try_connect()
            elif self.barn_connecting and t >= self.connect_deadline:
                self.connect_failed(errno.ETIMEDOUT)
            deadline = t + 500
            if self.barn_up and self.offline_since:
                deadline = min(deadline, self.offline_since + T_OFFLINE_TICK)
            if self.barn is None:
                deadline = min(deadline, self.next_connect)
            elif self.barn_connecting:
                deadline = min(deadline, self.connect_deadline)
            if self.lsock is None:
                deadline = min(deadline, self.next_listen)
            for i in range(1, NUM_CHANNELS + 1):
                ch = self.chans[i]
                if ch.sock is not None:
                    deadline = min(deadline, self.channel_deadline(ch))
            timeout = max(deadline - now_ms(), 0) / 1000.0
            try:
                events = self.sel.select(timeout) if self.sel.get_map() else time.sleep(timeout) or []
            except InterruptedError:
                events = []
            for key, mask in events:
                cb = key.data
                if isinstance(cb, tuple):
                    cb[0](cb[1], mask)
                else:
                    cb(mask)
                if self.quit:
                    break
            for i in range(1, NUM_CHANNELS + 1):
                ch = self.chans[i]
                if ch.sock is None:
                    continue
                if ch.closing:
                    self.close_channel(ch, 'hung up')
                    continue
                if now_ms() - ch.last_tick >= 2:
                    self.channel_tick(ch)
                if ch.closing:
                    self.close_channel(ch, 'hung up')
            self.gate_timers()
        self.shutdown()
        return 0

    def shutdown(self):
        self.log.write(L_DEBUG, 'GateThread Exit')
        if self.quit_reason:
            self.say('Shutting down: %s' % self.quit_reason)
        if self.barn_up:
            # The original's gateway thread would send DisconnectMsg (channel 0) here, but its
            # main thread has already called WSACleanup(), so in practice the host only sees
            # the connection close. Do the same; just let anything already queued go out.
            end = now_ms() + 500
            while self.barn and self.barn_out and now_ms() < end:
                try:
                    self.barn.setblocking(True)
                    self.barn.settimeout(max(end - now_ms(), 1) / 1000.0)
                    n = self.barn.send(self.barn_out)
                    del self.barn_out[:n]
                except OSError:
                    break
        for i in range(1, NUM_CHANNELS + 1):
            ch = self.chans[i]
            if ch.sock is not None:
                try:
                    ch.sock.close()
                except OSError:
                    pass
                ch.sock = None
        for s in (self.barn, self.lsock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self.barn = self.lsock = None
        if self.pid_file:
            try:
                with open(self.pid_file) as f:
                    mine = f.read().strip() == str(os.getpid())
                if mine:
                    os.remove(self.pid_file)
            except OSError:
                pass
            self.pid_file = None


def _is_id_line(line):
    """Is this LSCI.CFG line 'id = ...'? (the original's tokenizer)"""
    s = line.decode('latin-1')
    k = 0
    while k < len(s) and s[k] in ' \t':
        k += 1
    if k >= len(s) or s[k] in ';\r\n':
        return False
    j = k
    while j < len(s) and s[j] not in '\t\n\r ;=':
        j += 1
    if s[k:j] != 'id':
        return False
    if j < len(s) and s[j] in '\n\r;':
        return False
    while j < len(s) and s[j] in ' \t':
        j += 1
    return j < len(s) and s[j] == '='


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='innproxy',
        description='Links the ImagiNation Network client in DOSBox (which dials localhost:30000 through '
                    "DOSBox's modem) to the INN Barn server. A portable port of InnProxy.exe %s." % ORIG_VERSION,
        allow_abbrev=False)
    ap.add_argument('-C', '--directory', metavar='DIR',
                    help='work in DIR: innProxy.ini, Log/ and INN/LSCI.CFG are looked up there '
                         '(default: the current directory, as the original does)')
    ap.add_argument('-c', '--config', metavar='FILE', help='settings file (default: innProxy.ini)')
    ap.add_argument('--barn', metavar='HOST[:PORT]',
                    help='INN Barn server, overrides BarnIP= (default: %s:%u)' % (DEFAULT_BARN, DEFAULT_BARN_PORT))
    ap.add_argument('--listen', metavar='ADDR[:PORT]',
                    help='address the DOSBox modem dials, overrides ClientIP= (default: %s)' % DEFAULT_CLIENT)
    ap.add_argument('--log-dir', metavar='DIR', help='write the log file in DIR (default: Log)')
    ap.add_argument('--no-log', action='store_true', help='write no log file')
    ap.add_argument('--log-level', metavar='N', type=int,
                    help='log detail like XLogBit=N: 0 is the default, 10 logs every frame')
    ap.add_argument('-q', '--quiet', action='store_true', help='print nothing on the console')
    ap.add_argument('--pid-file', metavar='FILE',
                    help='write the process id to FILE while running (for a launcher that stops it)')
    ap.add_argument('--version', action='version',
                    version='innproxy.py %s (port of InnProxy.exe %s)' % (VERSION, ORIG_VERSION))
    args = ap.parse_args(argv)

    if args.directory:
        try:
            os.chdir(args.directory)
        except OSError as e:
            ap.error("can't change to %s: %s" % (args.directory, e.strerror))
    workdir = os.getcwd()
    ini = args.config or _find_ci(workdir, 'innProxy.ini') or os.path.join(workdir, 'innProxy.ini')
    cfg = Config(ini, args)
    logdir = None if args.no_log else (args.log_dir or os.path.join(workdir, 'Log'))
    log = Log(logdir, cfg.log_mask)
    proxy = Proxy(cfg, log, workdir, quiet=args.quiet)
    proxy.pid_file = os.path.abspath(args.pid_file) if args.pid_file else None

    def on_signal(signum, frame):
        proxy.stop('signal %d' % signum)

    for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK'):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (OSError, ValueError):
                pass
    try:
        return proxy.run()
    except KeyboardInterrupt:
        proxy.stop('interrupted')
        proxy.shutdown()
        return 0


if __name__ == '__main__':
    sys.exit(main())
