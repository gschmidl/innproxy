#!/usr/bin/env python3
"""The portable InnProxy (innproxy.py) for eXoDOS's The Sierra Network.

The game's launcher (eXoDOS\\!dos\\INNBarn\\exception.bat) then starts
innproxy.py, the Python port of InnProxy.exe, with Python ("py") when there
is one, InnProxy.exe otherwise, as before; after the game it stops the one
it started.

- util\\innproxy: innproxy.py, README.md, NOTES.md, LICENSE.
- exception.bat: the line that starts InnProxy.exe starts innproxy.py first;
  exception.bat.orig keeps the launcher as it was.

usage: python install_port.py [--check | --revert] EXO_FOLDER

EXO_FOLDER is eXoDOS's "eXo" folder, the one with eXoDOS, emulators and util
in it. --check shows what would change, --revert undoes it. The port lives in
util\\innproxy, so reinstalling the game does not remove it.
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ('innproxy.py', 'README.md', 'NOTES.md', 'LICENSE')
DEST = Path('util') / 'innproxy'
SCRIPT = '.\\util\\innproxy\\innproxy.py'
LAUNCHER = Path('eXoDOS') / '!dos' / 'INNBarn' / 'exception.bat'
# the launcher runs from the eXo folder
EXE_LINE = 'start /min .\\eXoDOS\\INNBarn\\InnProxy.exe'
KILL_LINE = 'taskkill /IM InnProxy.exe'
PID_FILE = 'innproxy.pid'


def ours():
    return 'start "InnProxy" /min py %s --pid-file %s' % (SCRIPT, PID_FILE)


def start_block():
    return ['where py >nul 2>nul',
            'if errorlevel 1 goto innproxy_exe',
            'if not exist %s goto innproxy_exe' % SCRIPT,
            ours(),
            'goto innproxy_started',
            ':innproxy_exe',
            EXE_LINE,
            ':innproxy_started']


def stop_block():
    # only the process innproxy.py wrote into the pid file, and only if it is a Python
    return ['if exist %s for /f %%%%p in (%s) do taskkill /F /FI "PID eq %%%%p" '
            '/FI "IMAGENAME eq python*" >nul 2>&1' % (PID_FILE, PID_FILE),
            'if exist %s del %s' % (PID_FILE, PID_FILE)]


def convert(text):
    """the launcher starting and stopping the port; None if it has no InnProxy lines"""
    nl = '\r\n' if '\r\n' in text else '\n'
    out, started, stopped = [], False, False
    for line in text.split(nl):
        if not started and line.strip().lower() == EXE_LINE.lower():
            started = True
            out += start_block()
            continue
        if started and not stopped and line.strip().lower() == KILL_LINE.lower():
            stopped = True
            out += stop_block()
        out.append(line)
    return nl.join(out) if started and stopped else None


def source(name):
    """NAME from the release zip (beside this script) or the project (one up)"""
    for p in (HERE / name, HERE.parent / name):
        if p.is_file():
            return p
    sys.exit(f'{name} is missing (it belongs beside install_port.py)')


class Patcher:
    def __init__(self, exo, check):
        self.exo, self.check = exo, check

    def say(self, text):
        print(('would ' if self.check else '') + text)

    def apply(self):
        for name in FILES:
            data = source(name).read_bytes()
            dest = self.exo / DEST / name
            if dest.is_file() and dest.read_bytes() == data:
                continue
            self.say(f'write {DEST / name}')
            if not self.check:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        bat = self.exo / LAUNCHER
        orig = bat.with_name('exception.bat.orig')
        if not bat.is_file():
            print(f'{LAUNCHER} is missing: launcher not changed')
            return
        text = bat.read_bytes().decode('latin-1')
        if ours() in text:
            return
        new = convert(text)
        if new is None:
            print(f'{LAUNCHER} has no "{EXE_LINE}" and "{KILL_LINE}" lines: '
                  f'launcher not changed')
            return
        self.say(f'switch {LAUNCHER} to innproxy.py (old one kept as {orig.name})')
        if not self.check:
            orig.write_bytes(text.encode('latin-1'))
            bat.write_bytes(new.encode('latin-1'))

    def revert(self):
        bat = self.exo / LAUNCHER
        orig = bat.with_name('exception.bat.orig')
        if bat.is_file() and ours() in bat.read_bytes().decode('latin-1'):
            if orig.is_file():
                self.say(f'restore {LAUNCHER}')
                if not self.check:
                    bat.write_bytes(orig.read_bytes())
                    orig.unlink()
            else:
                print(f'warning: {LAUNCHER} has no {orig.name}; left as it is')
        folder = self.exo / DEST
        for name in FILES:
            if (folder / name).is_file():
                self.say(f'delete {DEST / name}')
                if not self.check:
                    (folder / name).unlink()
        if folder.is_dir() and not self.check and not any(folder.iterdir()):
            folder.rmdir()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    group = ap.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true',
                       help='only show what would change')
    group.add_argument('--revert', action='store_true', help='undo the patch')
    ap.add_argument('exo', metavar='EXO_FOLDER')
    args = ap.parse_args()
    exo = Path(args.exo)
    if not (exo / 'eXoDOS').is_dir():
        sys.exit(f'{exo} is not eXoDOS\'s "eXo" folder (no eXoDOS in it)')
    p = Patcher(exo, args.check)
    p.revert() if args.revert else p.apply()
    return 0


if __name__ == '__main__':
    sys.exit(main())
