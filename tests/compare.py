#!/usr/bin/env python3
"""Compare two transcripts written by scenario.py (e.g. the original exe vs the port)."""

import json
import sys


def show(label, a, b):
    same = True
    n = max(len(a), len(b))
    for i in range(n):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            same = False
            print('%s[%d]:\n  A %s\n  B %s' % (label, i, x, y))
    print('%s: %d vs %d entries, %s' % (label, len(a), len(b), 'identical' if same else 'DIFFERENT'))
    return same


def main():
    a = json.load(open(sys.argv[1]))
    b = json.load(open(sys.argv[2]))
    ok = True
    for k in ('barn', 'client1', 'client2'):
        ok &= show(k, a[k], b[k])
    if a.get('lsci') != b.get('lsci'):
        ok = False
        print('LSCI.CFG differs:\n  A %r\n  B %r' % (a.get('lsci'), b.get('lsci')))
    else:
        print('LSCI.CFG identical')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
