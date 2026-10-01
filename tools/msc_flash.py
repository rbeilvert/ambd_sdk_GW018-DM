#!/usr/bin/env python3
"""Program EFR32MG21 flash over SWD by driving the MSC directly.

pyOCD has no flash algorithm for this part and Silicon Labs gates the CMSIS
pack, so this writes through the Memory System Controller by hand.
Registers and geometry taken from efr32mg21_msc.h / efr32mg21a020f768im32.h.
"""
import sys, time, argparse
from intelhex import IntelHex
from pyocd.core.helpers import ConnectHelper

MSC        = 0x40030000
WRITECTRL  = MSC + 0x0C
WRITECMD   = MSC + 0x10
ADDRB      = MSC + 0x14
WDATA      = MSC + 0x18
STATUS     = MSC + 0x1C
LOCK       = MSC + 0x3C

UNLOCK_KEY = 0x1B71
WREN       = 1 << 0
ERASEPAGE  = 1 << 1
WRITEEND   = 1 << 2

ST_BUSY       = 1 << 0
ST_LOCKED     = 1 << 1
ST_INVADDR    = 1 << 2
ST_WDATAREADY = 1 << 3
ST_REGLOCK    = 1 << 16

PAGE  = 0x2000
FLASH = 0xC0000


def wait(t, mask, want, what, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        s = t.read32(STATUS)
        if s & ST_INVADDR:
            raise RuntimeError(f"{what}: INVADDR (status=0x{s:08x})")
        if s & ST_LOCKED:
            raise RuntimeError(f"{what}: LOCKED (status=0x{s:08x})")
        if (s & mask) == want:
            return s
    raise RuntimeError(f"{what}: timeout (status=0x{t.read32(STATUS):08x})")


def unlock(t):
    t.write32(LOCK, UNLOCK_KEY)
    s = t.read32(STATUS)
    if s & ST_REGLOCK:
        raise RuntimeError(f"MSC registers still locked (status=0x{s:08x})")
    t.write32(WRITECTRL, t.read32(WRITECTRL) | WREN)


def relock(t):
    t.write32(WRITECTRL, t.read32(WRITECTRL) & ~WREN)
    t.write32(LOCK, 0)


def erase_page(t, addr):
    t.write32(ADDRB, addr)
    t.write32(WRITECMD, ERASEPAGE)
    wait(t, ST_BUSY, 0, f"erase 0x{addr:06x}")


def write_block(t, addr, data):
    """data: bytes, multiple of 4, within one page."""
    t.write32(ADDRB, addr)
    for i in range(0, len(data), 4):
        wait(t, ST_WDATAREADY, ST_WDATAREADY, f"wdata 0x{addr+i:06x}")
        t.write32(WDATA, int.from_bytes(data[i:i+4], "little"))
    t.write32(WRITECMD, WRITEEND)
    wait(t, ST_BUSY, 0, f"writeend 0x{addr:06x}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hexfile")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--min-addr", type=lambda x: int(x, 0), default=0)
    a = ap.parse_args()

    ih = IntelHex(a.hexfile)
    segs = [(s, e) for s, e in ih.segments()]
    print("hex segments:")
    for s, e in segs:
        print(f"  0x{s:06x} - 0x{e-1:06x}  ({e-s} bytes)")

    with ConnectHelper.session_with_chosen_probe(target_override="cortex_m",
                                                 options={"resume_on_disconnect": False}) as session:
        t = session.target
        t.halt()
        print("core halted")

        if not a.verify_only:
            unlock(t)
            print("MSC unlocked, WREN set")
            pages = sorted({addr & ~(PAGE - 1)
                            for s, e in segs for addr in range(s, e, 4)
                            if addr >= a.min_addr})
            pages = [p for p in pages if p < FLASH]
            print(f"erasing {len(pages)} pages")
            for p in pages:
                erase_page(t, p)
            print("erase done")

            for s, e in segs:
                if e <= a.min_addr:
                    continue
                s = max(s, a.min_addr)
                data = ih.tobinarray(start=s, size=e - s).tobytes()
                if len(data) % 4:
                    data += b"\xff" * (4 - len(data) % 4)
                # split on page boundaries
                off = 0
                while off < len(data):
                    addr = s + off
                    room = PAGE - (addr & (PAGE - 1))
                    chunk = data[off:off + room]
                    write_block(t, addr, chunk)
                    off += len(chunk)
                    print(f"  wrote 0x{addr:06x} +{len(chunk)}", end="\r", flush=True)
            print("\nwrite done")
            relock(t)

        # verify
        bad = 0
        for s, e in segs:
            if e <= a.min_addr:
                continue
            s = max(s, a.min_addr)
            want = ih.tobinarray(start=s, size=e - s).tobytes()
            got = bytes(t.read_memory_block8(s, len(want)))
            if got != want:
                for i in range(len(want)):
                    if got[i] != want[i]:
                        print(f"  MISMATCH at 0x{s+i:06x}: got {got[i]:02x} want {want[i]:02x}")
                        bad += 1
                        if bad > 5:
                            break
            if bad > 5:
                break
        print("VERIFY:", "OK" if bad == 0 else f"{bad}+ mismatches")
        return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
