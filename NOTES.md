# InnProxy protocol notes

What InnProxy.exe 1.1k does, as read from its machine code (x64, linked
2021-02-11, `INN Proxy ver: 1.1.k`). Addresses are the original's virtual
addresses (image base 0x140000000), for anyone who wants to check.

## Overview

- One **gateway** connection to the INN Barn server, by default
  `cdnsrv.innbarn.com:29000` (`BarnIP`).
- A **listener** on `127.0.0.1:30000` (`ClientIP`). Each accepted connection is
  one **channel**, numbered 1 to 16. Free channels are handed out first in,
  first out: one client after another gets 1, 2, ... 16, 1, ...
- Everything the client sends is ACKed, unwrapped and sent to the host with
  the channel number. Everything the host sends for a channel is framed and
  sent to that client.

## Client side

### Modem phase (sub_140004890)

A new channel starts in "modem mode", answering what a modem and a
packet-network PAD would:

| Client sends | Proxy answers |
|---|---|
| `\r` or `+` | nothing (dropped one byte at a time) |
| `@D` | `@D\r` |
| `AT...\r` | `OK\r` |
| `ATDT...\r` | `CONNECT\r` (the mode does not change) |
| `c NAME\r`, NAME one of `SIERRA` `imagine` `InnBarn` `Owl`, gateway up | `NAME CONNECTED\r`, then data mode |
| the same with the gateway down | `NO ANSWER\r` |
| `c` with any other name | nothing |
| anything else | dropped up to and including the next `\r` |

Under DOSBox the AT commands go to DOSBox's own modem; what reaches the
proxy is what the client sends after `CONNECT` (`hostID = SIERRA` in
`LSCI.CFG`).

### Frames (sub_140003e10, sub_140003170)

```
81  crc-lo crc-hi  seq  LEN  data...  82
```

- `0x81`, `0x82` and `0x1B` inside LEN or data are sent as `1B xx`. The CRC
  and seq bytes are never escaped.
- LEN is the data length; from 255 bytes up it is `FF lo hi`.
- The CRC is CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, MSB first, no final
  XOR) over seq and every byte after it up to the `82`, escape bytes included,
  stored little-endian.
- `81 crc crc code 82` (5 bytes) is an acknowledgement, CRC over `code`. The
  proxy sends `0x90|seq` for a good frame and `0x80|seq` (NAK) for a bad one,
  where seq is then the last good one plus 1. From the client, any 5-byte frame
  acknowledges proxy frame `code & 7`.

Data frames from the client:

- One message: LEN equals the decoded length. The host gets it with the first
  data byte (the INN message type) as the header type.
- Otherwise the frame is part of a stream of length-prefixed records
  `[n][n bytes]...`. LEN is just its first byte, and records may continue in
  the next frame. Complete records go to the host as one `MultiMsg` (0x102);
  an incomplete tail is kept for the next frame. A record length of 0 drops
  the lot.
- The proxy does not detect duplicates; a frame sent again is forwarded again.

To the client:

- Sequence numbers run 0..7 per channel. At most 8 frames are unacknowledged;
  the next one goes out when the slot of its sequence number is free.
- No resending. When the slot is still taken 3 s after its frame went out, the
  frame is dropped from the window ("NUKE") and the next one takes the slot.
- One frame per pass of the channel loop (about every 2 ms).
- After 15 s without anything from the client (in data mode) the proxy sends
  the 5-byte message `00 03 00 00 00` as a ping frame and expects its ACK.

## Host side

Every message has a 16-byte header, little-endian:

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | type: an INN message type (< 0x100) or a proxy message (>= 0x100) |
| 2 | 2 | CRC-16/MCRF4XX (poly 0x8408 reflected, init 0xFFFF, no final XOR) over the whole message with this field 0 |
| 4 | 2 | total length, header included |
| 6 | 2 | channel (0 for the proxy itself) |
| 8 | 4 | proxy to host: the proxy id; host to proxy: varies |
| 12 | 4 | proxy to host: 0; host to proxy: the proxy id |

The proxy takes its id from offset 12 of every proxy message (>= 0x100) the
host sends, and puts it at offset 8 of everything it sends. The proxy never
checks the CRC of what it receives.

Client messages from the host (type < 0x100) are framed with data = the bytes
after the header. Types outside the name table (0x00-0x3B, 0x92-0x94,
0xCA-0xCD, 0xFF) are dropped ("Tossing unknown INN message").

Proxy messages:

| Type | Name | Direction / meaning |
|---|---|---|
| 0x100 | ProxyHelloMsg | to host on connecting: data `01 01 6B 00` (version 1.1k), 20 bytes. From host: +16 member id, +20 a title string (if the message is longer than 32 bytes); a non-zero id is written into `INN\LSCI.CFG` as `id = 0<id>` |
| 0x101 | NetworkStatusMsg | not handled |
| 0x102 | MultiMsg | to host: records from a client, see above |
| 0x103 | ProxyInfoMsg | from host: +16 players, +20 hosts, +24 proxies, +28 host run time (s), +36 max proxies, +40 max players, from +44 a list of strings ending in an empty one |
| 0x104 | DisconnectMsg | from host: drop that channel's client. To host: a channel closed (header channel), or the proxy is leaving (channel 0) |
| 0x105 | PingMsg | 57 bytes: five 8-byte timer values from +16, a count at +56. The proxy sends one every 3 minutes with the count 1; each echo gets the next timer value and the two words at +8 and +12 swapped, and goes back until the count reaches 5 (the round trip time is shown) |
| 0x107 | (no name) | from host: +16 flag, +17 text, shown to the user; flag set = close every client and quit |

## Timers and exits

- Connect attempts to the host: after a refused, timed-out or unreachable
  connect the proxy waits 3 s, after other errors 1 s. While it is offline,
  every attempt counts one tick; at 120 ticks it quits.
- With the host connected, nothing for any client for 10 minutes, no client
  connected and `DisableShutdown` not set: "Idle so shutting Down!", then one
  tick per 0.5 s and quit at 120 (a minute).
- A host message for a client resets both.
- First PingMsg 3 min 10 s after connecting, then every 3 minutes.

## Bugs of the original

Kept, because the game and the server see them (see "Hang-ups" below):

1. **`+++`**: when a frame-mode buffer starts with `+` the parser sets the
   modem flag, and its exit code writes the entry value back (0x140004862).
   The `+` stays at the front of the buffer and the channel never processes
   another byte; it still sends pings and host messages.
2. **Clean hang-up ignored**: `recv()` returning 0 is skipped (0x140004f95), so
   a client that closes normally stays connected. Send errors count as success
   (0x1400030e0 tests only for 0). Only a reset seen by `recv()` ends the channel.
4. **Escaped first record length**: in a multi-record frame whose first length
   is 0x81 or 0x82 (sent as `1B 81`), the original forwards `1B` as the length.

### Hang-ups

Each land is its own DOS program. When one ends, DOSBox's modem hangs up
(a clean close) and the next program dials in again: a new channel. Because
of bug 2 the original doesn't notice; 15 s later its ping to the old channel
meets a reset, and only then does it send DisconnectMsg for it. By then the
host has moved the session to the new channel. An early version of this port
reported the hang-up at once, and the host dropped the session with
`ObjFreeMsg`: the game hung at "Net Connect". The port now behaves like the
original: the channel stays, the next frame (ping or host message) provokes
the reset, and that ends it. Linux reports such a reset only in `SO_ERROR`,
not from `recv()`, so the port checks both.

Fixed, as nothing the game or the server relies on changes (after 3 the server
just sees the proxy connect again):

3. **Gateway gone for good**: after a receive error the gateway thread closes
   every client and ends (0x140002ca1 clears its run flag). The proxy stays up
   without a server until restarted. A graceful close by the server (`recv()`
   = 0) is not noticed at all. The port treats both as lost contact and
   reconnects.
5. **Partial modem input**: the modem parser consumes bytes without moving the
   rest to the front of the buffer, so a command split across two reads is
   garbled. The port keeps an incomplete command until its `\r` arrives.
6. **Format string**: when it drops junk in modem mode the original passes the
   received bytes as the printf format of its log line.
7. **`ClientIP` without a port** sets the *gateway* port to 30000 and listens on
   port 0 (0x140005d93). The port uses 30000 for the listener.
8. **Fixed buffers**: 1024 bytes for host data (a longer message never
   completes and the gateway stalls) and about 1 KB per client; a full
   non-blocking send drops the rest of the message. The port buffers both ways
   without limits.
9. Messages for a channel with no client are queued and delivered to the next
   client given that channel, and channel numbers from the host are used
   unchecked as array indexes. The port drops them.
10. A client arriving during the idle minute is shut down with everybody. The
    port cancels the idle shutdown.
11. With `WatchDogReboot` set, the first gateway pass reboots Windows (on a
    machine up for more than 4 minutes), because the "last received" time
    starts at 0.

## Not ported

The status window and its list box; renaming DOSBox windows ("ImagiNation
Network - n") found through EnumWindows by "cpu speed" in the title, hiding the
"DOSBox Status Window", saving and restoring their positions (`WinChannel0..16`,
`ComputerName`), and sending them WM_CLOSE when their session ends; the WAVE
resource played when the player count rises; the reboot watchdog; the `Pipe`
and `Com1` client modes, which the original reads from the ini but never
implemented (its listener and channel loop only work with `Sock`).
