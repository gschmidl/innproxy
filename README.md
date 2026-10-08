# innproxy

A portable port of **InnProxy.exe 1.1k**, the INN Barn proxy that lets the
DOS client of *The Sierra Network / The ImagiNation Network* (1991), running in
DOSBox, reach the [INN Barn](https://www.innbarn.com/) server.

The original is a 64-bit Windows program. On macOS and Linux it has had to run
under Wine, which on Apple Silicon also needs Rosetta 2, and Apple is phasing
Rosetta out. This port is a single Python 3 script (standard library only) that
runs natively on macOS (Apple Silicon or Intel), Linux and Windows.

It was rebuilt from the original's machine code. A test that drives the same
scripted session through both (see [Tests](#tests)) gets the same bytes from
both on both sides. [NOTES.md](NOTES.md) describes the protocol.

Download: [releases](https://github.com/gschmidl/innproxy/releases), with a
patch that installs it into eXoDOS (see [Patch for eXo](#patch-for-exo)).

## How it fits together

```
INN client (DOS) ──serial──> DOSBox modem ──TCP──> innproxy ──TCP──> INN Barn server
                             "ATDTlocalhost:30000"  127.0.0.1:30000   cdnsrv.innbarn.com:29000
```

The client's `LSCI.CFG` dials `ATDTlocalhost:30000`; DOSBox (`serial1=modem`)
turns that into a TCP connection to the proxy. The proxy answers the client's
packet-network commands (`@D`, `c SIERRA`), then speaks the client's framed link
protocol (CRC, acknowledgements, an 8-frame window) and relays every message to
the server and back.

## Requirements

Python 3.8 or later. macOS: the `python3` of Apple's Command Line Tools
(`xcode-select --install`) or Homebrew works. Linux: the distribution's
`python3`. Windows: any python.org install; WSL is not involved.

## Running it

From the folder that holds (or should hold) `innProxy.ini`. Under eXo that is
the `eXo` folder, the same place the original runs from; the patch puts the
script into `util/innproxy`:

```bash
python3 util/innproxy/innproxy.py
```

Leave it running while you play. Stop it with Ctrl+C or `kill`. Like the
original it also exits by itself:

- after about 120 failed attempts to reach the server, 3 seconds apart
  (6 minutes or more);
- when no client is connected and the server has sent nothing for one for
  10 minutes, a minute after printing "Idle so shutting Down!"
  (`DisableShutdown=1` turns this one off).

| Option | Meaning |
|---|---|
| `-C`, `--directory DIR` | work in DIR: `innProxy.ini`, `Log/` and `INN/LSCI.CFG` are found there |
| `-c`, `--config FILE` | settings file (default `innProxy.ini`) |
| `--barn HOST[:PORT]` | INN Barn server, overrides `BarnIP=` |
| `--listen ADDR[:PORT]` | address and port the DOSBox modem dials, overrides `ClientIP=` |
| `--log-dir DIR` | where the log goes (default `Log`) |
| `--no-log` | no log file |
| `--log-level N` | log detail, like `XLogBit=N`: 0 = default, 10 = every frame |
| `-q`, `--quiet` | nothing on the console |
| `--pid-file FILE` | write the process id to FILE while running, for a launcher that stops it |
| `--version`, `-h`, `--help` | |

On the console it reports the connection to the server, clients coming and
going, messages from the server operators, and how many players are online.
The log file is the original's: `Log/InnProxy-YYYY-Mon-DD.log`, same line
format, same message texts.

## Addresses and ports

The original has **no IP address baked in**. Its defaults are a host name and
two ports, all of which its `innProxy.ini` could already override, and the port
reads the same keys:

```ini
[InnBarn]
BarnIP=cdnsrv.innbarn.com:29000   ; the server: host name or IP, port optional (29000)
ClientIP=127.0.0.1:30000          ; where the DOSBox modem dials in, port optional (30000)
```

`--barn` and `--listen` override both from the command line. If you change
the listen port, change the number in the client's `ATDTlocalhost:30000` in
`LSCI.CFG` to match. A server name that resolves to several addresses is
handled as in the original: a public address is preferred over a private or
loopback one. The name is looked up again on every connection attempt, where
the original only looked it up once at start.

Other keys it reads: `DisableShutdown`, `XLogBit`, `NoMutex` (allow a second
copy on the same port: it then waits for the port instead of quitting), and
`ClientNet` (only `Sock` ever worked in the original; anything else is
reported and treated as `Sock`). `WatchDogReboot` is read and ignored: in the
original it reboots Windows when the server goes quiet. `ComputerName` and
`WinChannel0..16` were window-position bookkeeping and are neither read nor
written.

## Patch for eXo

`innproxy-port-<version>.zip` (`make_release.py` builds it in `release/`) is a
folder with `install_port.py` and the port:

```bash
python install_port.py [--check | --revert] EXO_FOLDER
```

EXO_FOLDER is eXoDOS's `eXo` folder, the one with `eXoDOS`, `emulators` and
`util` in it. `--check` shows what would change, `--revert` undoes it.

- `util\innproxy` gets `innproxy.py`, README.md, NOTES.md and LICENSE, so
  reinstalling the game does not remove the port.
- The game's launcher, `eXoDOS\!dos\INNBarn\exception.bat`, starts
  `innproxy.py` instead of InnProxy.exe when the `py` launcher and the script
  are there, InnProxy.exe otherwise. After the game it stops exactly the
  process it started: `--pid-file` writes the proxy's process id once it owns
  the port, and only a Python process with that id is ended.
  `exception.bat.orig` keeps the old launcher. The game zip is not touched.

The original launcher does this:

```bat
start /min .\eXoDOS\INNBarn\InnProxy.exe
...dosbox...
taskkill /IM InnProxy.exe
```

and with the patch:

```bat
where py >nul 2>nul
if errorlevel 1 goto innproxy_exe
if not exist .\util\innproxy\innproxy.py goto innproxy_exe
start "InnProxy" /min py .\util\innproxy\innproxy.py --pid-file innproxy.pid
goto innproxy_started
:innproxy_exe
start /min .\eXoDOS\INNBarn\InnProxy.exe
:innproxy_started
...dosbox...
if exist innproxy.pid for /f %%p in (innproxy.pid) do taskkill /F /FI "PID eq %%p" /FI "IMAGENAME eq python*" >nul 2>&1
if exist innproxy.pid del innproxy.pid
taskkill /IM InnProxy.exe
```

The macOS and Linux launch scripts are not part of the patch. Where they start
InnProxy.exe under Wine (the working directory is the `eXo` folder), start the
port instead and stop it the same way:

```bash
python3 ./util/innproxy/innproxy.py --quiet &
innproxy_pid=$!
# ... run DOSBox as before ...
kill "$innproxy_pid"
```

The eXo scripts afterwards delete the log folder as `LOG`; the folder is
called `Log`, which matters on a case-sensitive file system.

## What it does not do

Things of the original that are left out:

- the status window: the console lines replace it;
- renaming DOSBox windows to "ImagiNation Network - n", saving and restoring
  their positions, minimizing the "DOSBox Status Window", and closing the DOSBox
  window when the server ends a session (all Windows-only; the port closes the
  connection, and the game sees the line drop);
- the sound when someone joins;
- rebooting the computer (`WatchDogReboot`).

## Where it deliberately differs

Whatever the game or the server can see is kept exactly as in the original,
bugs included: the server depends on some of them. When a land program ends,
DOSBox's modem hangs up and the next program dials in again; the original
does not notice the clean hang-up and only reports the channel closed when
its next ping to it bounces, about 15 seconds later, after the server has
moved the session to the new call. Reporting the hang-up at once (as an early
version of this port did) makes the server drop the session, and the game
then hangs at "Net Connect". The original's `+++` bug, which leaves the
channel taking no further frames, is kept for the same reason.

These differences are invisible to both ends; NOTES.md has the details.

- After losing contact with the server it closes the client sessions, as the
  original does, then reconnects. The original's gateway thread stops there
  for good.
- A client that connects during the "Idle so shutting Down!" minute cancels
  the shutdown.
- Messages the server sends for a channel with no client are dropped. The
  original queued them for whoever got that channel next.
- Bytes are buffered until they can be sent. The original dropped the rest of
  a message when a socket was momentarily full, and its message buffers had
  fixed sizes.

## Tests

`tests/` holds a fake INN Barn server and a fake INN client, both in Python:

- `scenario.py` runs a scripted session (modem commands, every kind of frame,
  host messages, window and resend give-up, client ping, `+++`, disconnects)
  through any proxy command and writes what both ends received;
  `compare.py` compares two such transcripts. The port's transcript is
  identical to the original exe's, the late hang-up report included.
- `edgecases.py` runs the port with shortened timings: server offline at start,
  lost contact, a second copy, the operators' shutdown message, keep-alive
  pings, idle and offline exits, a hang-up and call-back, `+++`, a full
  channel table, the member id in `LSCI.CFG`.
- `test_patches.py EXO_FOLDER` checks install_port.py on a scratch copy of
  that eXo's launcher: the changes, `--check`, a second run, `--revert`; on
  Windows it also runs the new launcher lines with cmd (a stub proxy that must
  be started with `--pid-file` and stopped again; without `py` or without the
  script, the InnProxy.exe line).

```bash
python3 tests/edgecases.py
python3 tests/scenario.py --workdir /tmp/innp --out port.json -- python3 innproxy.py
```

They pass on Windows (Python 3.8, 3.9, 3.10, 3.14) and on Linux (Python 3.11).
They were not run on macOS: it was not available. The port has been played
with in eXoDOS on Windows: login, a session, and leaving.
