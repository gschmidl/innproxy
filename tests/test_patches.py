"""Test install_port.py on a scratch copy (EXO_FOLDER is only read).

    py test_patches.py EXO_FOLDER

EXO_FOLDER's eXoDOS\\!dos\\INNBarn\\exception.bat (its exception.bat.orig if
it is patched) is copied; then --check, the patch, a second patch (no
change), and --revert (a byte-identical launcher again). On Windows the
launcher's InnProxy part is also run with cmd, the DOSBox line replaced by a
wait: with py, a stub innproxy.py records its arguments and pid file, and the
stop lines must end it and remove the pid file; without py (PATH without it)
and without innproxy.py, the old InnProxy.exe line must run (replaced by an
echo; InnProxy.exe itself is never started, and the launcher's
"taskkill /IM InnProxy.exe" is not run).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'exo'))
import install_port  # noqa: E402

fails = 0


def check(what, got, want):
    global fails
    if got != want:
        fails += 1
        print('FAIL %s:\n     got  %r\n     want %r' % (what, got, want))
    else:
        print('ok   %s' % what)


def run(cmd):
    r = subprocess.run([sys.executable, install_port.__file__] + cmd,
                       capture_output=True, text=True)
    return r.stdout


def alive(pid):
    r = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'],
                       capture_output=True)           # bytes: its text is localized
    return (' %d ' % pid).encode() in r.stdout


def run_block(exo, with_py, with_script):
    """Run the patched launcher's InnProxy part with cmd; what ran?"""
    bat = os.path.join(exo, *install_port.LAUNCHER.parts)
    with open(bat, encoding='latin-1', newline='') as f:
        lines = f.read().split('\r\n')
    out = os.path.join(exo, 'ran.txt')
    test = []
    for line in lines:
        if line.startswith('".\\emulators\\dosbox\\'):
            # instead of DOSBox: wait (at most 10 s) until a proxy has said what it is
            test += ['for /l %%i in (1,1,20) do if not exist ran.txt ping -n 2 127.0.0.1 >nul',
                     'ping -n 2 127.0.0.1 >nul']
        elif line.strip() == install_port.EXE_LINE:
            test.append('echo exe> ran.txt')
        elif line.strip() == install_port.KILL_LINE:
            test.append('goto end_of_test')
        else:
            test.append(line)
    path = os.path.join(exo, 'launcher_test.bat')
    with open(path, 'w', encoding='latin-1', newline='') as f:
        f.write('\r\n'.join(['@echo off', 'cd /d "%s"' % exo] + test + [':end_of_test', '']))
    script = os.path.join(exo, 'util', 'innproxy', 'innproxy.py')
    if os.path.exists(out):
        os.remove(out)
    if with_script:
        # stands in for the proxy: says how it was started, writes its pid file, waits
        with open(script, 'w') as f:
            f.write('import os, sys, time\n'
                    'a = sys.argv[1:]\n'
                    'open(a[a.index("--pid-file") + 1], "w").write("%%d\\n" %% os.getpid())\n'
                    'open(%r, "w").write("port %%d " %% os.getpid() + " ".join(a) + " in " + os.getcwd())\n'
                    'time.sleep(60)\n' % out)
    elif os.path.exists(script):
        os.remove(script)
    env = dict(os.environ)
    env.pop('NoDefaultCurrentDirectoryInExePath', None)
    if not with_py:
        env['PATH'] = os.path.join(os.environ['SystemRoot'], 'System32')
    subprocess.run(['cmd', '/c', path], env=env, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60)
    if not os.path.exists(out):
        return None, None
    with open(out) as f:
        text = f.read().replace(exo, '<exo>').strip()
    if text.startswith('port '):
        pid = int(text.split()[1])
        time.sleep(0.5)
        return text.replace(' %d ' % pid, ' ', 1), (alive(pid), os.path.exists(
            os.path.join(exo, install_port.PID_FILE)))
    return text, None


def main():
    src = sys.argv[1]
    exo = tempfile.mkdtemp(prefix='innproxy-exo-')
    try:
        d = os.path.join(exo, 'eXoDOS', '!dos', 'INNBarn')
        os.makedirs(d)
        orig = os.path.join(src, 'eXoDOS', '!dos', 'INNBarn', 'exception.bat')
        if os.path.exists(orig + '.orig'):      # already patched there
            orig += '.orig'
        bat = os.path.join(d, 'exception.bat')
        shutil.copyfile(orig, bat)
        with open(bat, 'rb') as f:
            original = f.read()
        out = run(['--check', exo])
        check('--check lists 5 changes', len(out.splitlines()), 5)
        check('--check changes nothing', (os.path.exists(os.path.join(exo, 'util')),
                                          open(bat, 'rb').read() == original), (False, True))
        run([exo])
        with open(bat, 'rb') as f:
            new = f.read()
        check('launcher patched', install_port.ours().encode() in new, True)
        check('stop lines added', all(s.encode() in new for s in install_port.stop_block()), True)
        check('CRLF kept', new.count(b'\n'), new.count(b'\r\n'))
        check('.orig', open(bat + '.orig', 'rb').read(), original)
        check('port installed', sorted(os.listdir(os.path.join(exo, 'util', 'innproxy'))),
              sorted(install_port.FILES))
        check('second run: nothing to do', run([exo]), '')
        if os.name == 'nt':
            got, state = run_block(exo, True, True)
            check('runs the port', got, 'port --pid-file innproxy.pid in <exo>')
            check('port stopped, pid file gone', state, (False, False))
            check('without py: the exe', run_block(exo, False, True)[0], 'exe')
            check('without innproxy.py: the exe', run_block(exo, True, False)[0], 'exe')
            run([exo])          # put the real files back
        run(['--revert', exo])
        check('reverted', open(bat, 'rb').read(), original)
        check('.orig gone', os.path.exists(bat + '.orig'), False)
        check('util/innproxy gone', os.path.exists(os.path.join(exo, 'util', 'innproxy')), False)
    finally:
        shutil.rmtree(exo, ignore_errors=True)
    print('%d failure(s)' % fails)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
