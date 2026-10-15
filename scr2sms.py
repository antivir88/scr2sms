#!/usr/bin/env python3
"""Build a bank-switched SMS slideshow from 256x192 ZX Spectrum .scr files."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCR_SIZE = 6912
BANK_SIZE = 0x4000
IMAGE_DATA_SIZE = 6144 * 2  # Mode 2 patterns + one colour byte per scanline
MAX_IMAGES = 254             # banks 0/1 are player/header; FFFD is 8-bit

# The Spectrum palette used by antivir88/scrview.  The bright bit chooses the
# second half exactly as that viewer does: index = colour + 8 * BRIGHT.
ZX_COLOURS = (
    (0x00, 0x00, 0x00), (0x00, 0x00, 0xC0), (0xC0, 0x00, 0x00), (0xC0, 0x00, 0xC0),
    (0x00, 0xC0, 0x00), (0x00, 0xC0, 0xC0), (0xC0, 0xC0, 0x00), (0xC0, 0xC0, 0xC0),
    (0x00, 0x00, 0x00), (0x00, 0x00, 0xFF), (0xFF, 0x00, 0x00), (0xFF, 0x00, 0xFF),
    (0x00, 0xFF, 0x00), (0x00, 0xFF, 0xFF), (0xFF, 0xFF, 0x00), (0xFF, 0xFF, 0xFF),
)

# Fixed TMS9918 colours exposed by the SMS VDP compatibility modes.  Entry 0
# is transparent, so black uses entry 1.
TMS9918_COLOURS = (
    (0x00, 0x00, 0x00), (0x00, 0x00, 0x00), (0x21, 0xC8, 0x42), (0x5E, 0xDC, 0x78),
    (0x54, 0x55, 0xED), (0x7D, 0x76, 0xFC), (0xD4, 0x52, 0x4D), (0x42, 0xEB, 0xF5),
    (0xFC, 0x55, 0x54), (0xFF, 0x79, 0x78), (0xD4, 0xC1, 0x54), (0xE6, 0xCE, 0x80),
    (0x21, 0xB0, 0x3B), (0xC9, 0x5B, 0xBA), (0xCC, 0xCC, 0xCC), (0xFF, 0xFF, 0xFF),
)


def nearest_tms_colour(colour: tuple[int, int, int]) -> int:
    return min(range(1, 16), key=lambda index: sum((left - right) ** 2
                                                     for left, right in zip(colour, TMS9918_COLOURS[index])))


def tms_colour_byte(attribute: int) -> int:
    """Translate one Spectrum ink/paper attribute to a TMS colour byte."""
    bright_offset = 8 if attribute & 0x40 else 0
    ink = nearest_tms_colour(ZX_COLOURS[(attribute & 7) + bright_offset])
    paper = nearest_tms_colour(ZX_COLOURS[((attribute >> 3) & 7) + bright_offset])
    return (ink << 4) | paper


def convert_scr(source: bytes) -> bytes:
    """Produce Mode 2 pattern and colour tables from a standard .scr image."""
    if len(source) != SCR_SIZE:
        raise ValueError(f"expected {SCR_SIZE} bytes, got {len(source)}")
    bitmap, attributes = source[:6144], source[6144:]
    patterns = bytearray()
    colours = bytearray()
    # Spectrum stores scanlines in thirds; VDP Mode 2 stores each 8x8 tile
    # consecutively.  Its colour table accepts one foreground/background byte
    # per tile scanline, so each Spectrum attribute is repeated eight times.
    for character_y in range(24):
        for character_x in range(32):
            colour = tms_colour_byte(attributes[character_y * 32 + character_x])
            for scanline in range(8):
                spectrum_offset = ((character_y & 0x18) << 8) + (scanline << 8) + ((character_y & 7) << 5) + character_x
                patterns.append(bitmap[spectrum_offset])
                colours.append(colour)
    return bytes(patterns + colours)


PLAYER = r'''
        ORG $0000
VDP_DATA EQU $BE
VDP_CTRL EQU $BF
PAGE1    EQU $FFFE             ; Sega mapper slot $4000-$7FFF
CURRENT  EQU $C000
COUNT    EQU {count}

start:  DI
        LD SP,$DFF0
        XOR A
        LD (CURRENT),A
        CALL video_init
        CALL show_current
main:   CALL wait_release
.wait: IN A,(VDP_CTRL)
        BIT 7,A                 ; consume VBlank status
        IN A,($DC)
        BIT 2,A                 ; left is active low
        JR Z,.previous
        BIT 3,A                 ; right is active low
        JR Z,.next
        JR .wait
.previous:
        LD A,(CURRENT)
        OR A
        JR NZ,.dec
        LD A,COUNT-1
        JR .store
.dec:   DEC A
        JR .store
.next:  LD A,(CURRENT)
        INC A
        CP COUNT
        JR C,.store
        XOR A
.store: LD (CURRENT),A
        CALL show_current
        JR main

wait_release:
        IN A,($DC)
        AND $0C
        CP $0C
        JR NZ,wait_release
        RET

show_current:
        LD A,(CURRENT)
        ADD A,2                 ; banks 0/1 contain player and ROM header
        LD (PAGE1),A
        LD HL,$4000
        LD DE,$0000
        LD BC,6144
        CALL vram_copy
        LD HL,$5800
        LD DE,$2000
        LD BC,6144
        JP vram_copy

vram_copy:
        LD A,E
        OUT (VDP_CTRL),A
        LD A,D
        OR $40
        OUT (VDP_CTRL),A
.loop: LD A,(HL)
        OUT (VDP_DATA),A
        INC HL
        DEC BC
        LD A,B
        OR C
        JR NZ,.loop
        RET

video_init:
        LD HL,registers
        LD B,8
        LD C,0
.reg:   LD A,(HL)
        OUT (VDP_CTRL),A
        LD A,C
        OR $80
        OUT (VDP_CTRL),A
        INC HL
        INC C
        DJNZ .reg
        ; Name table: each of the 32x24 cells addresses a distinct pattern.
        LD DE,$1800
        LD A,E
        OUT (VDP_CTRL),A
        LD A,D
        OR $40
        OUT (VDP_CTRL),A
        LD B,3
        LD C,0
.names: LD A,C
        OUT (VDP_DATA),A
        INC C
        JR NZ,.names
        DJNZ .names
        RET

; TMS9918 Graphics II: patterns $0000-$17FF, names $1800-$1AFF,
; colours $2000-$37FF.  It is supported by the SMS VDP in compatibility mode.
registers:
        DB $02,$C0,$06,$FF,$03,$76,$03,$01
        DS $4000-$,0
'''


def collect_screens(directory: Path) -> tuple[list[Path], list[str]]:
    screens: list[Path] = []
    skipped: list[str] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file():
            continue
        if path.suffix.casefold() != ".scr":
            skipped.append(f"{path.name}: not a .scr file")
        elif path.stat().st_size != SCR_SIZE:
            skipped.append(f"{path.name}: {path.stat().st_size} bytes (expected {SCR_SIZE})")
        else:
            screens.append(path)
    return screens, skipped


def build(screens: list[Path], output: Path) -> None:
    if not screens:
        raise ValueError("no valid .scr files found")
    if len(screens) > MAX_IMAGES:
        raise ValueError(f"{len(screens)} screens do not fit: Sega mapper player supports at most {MAX_IMAGES}")
    assembler = shutil.which("sjasmplus")
    if not assembler:
        raise RuntimeError("sjasmplus is required; install it and try again")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scr2sms-") as temporary:
        work = Path(temporary)
        assembly = work / "album.asm"
        for index, screen in enumerate(screens, start=1):
            payload = convert_scr(screen.read_bytes())
            (work / f"bank-{index:03d}.bin").write_bytes(payload + bytes(BANK_SIZE - len(payload)))
        # sjasmplus raw output is a Z80 address space and therefore wraps at
        # 64 KiB.  Assemble only fixed bank 0, then append physical mapper
        # banks in Python below.
        assembly.write_text(PLAYER.format(count=len(screens)), encoding="ascii")
        raw = work / "album.raw"
        subprocess.run([assembler, "--nologo", f"--raw={raw}", str(assembly)], cwd=work, check=True)
        rom = bytearray(raw.read_bytes())
        if len(rom) != BANK_SIZE:
            raise RuntimeError(f"player bank has unexpected size: {len(rom)} bytes")
        rom.extend(bytes(BANK_SIZE))  # bank 1: conventional SMS header at $7FF0
        for index in range(1, len(screens) + 1):
            rom.extend((work / f"bank-{index:03d}.bin").read_bytes())
        target_size = 0x8000
        while target_size < len(rom):
            target_size <<= 1
        rom.extend(bytes(target_size - len(rom)))
        # The checksum is optional for most emulators, but the header makes
        # this a normal SMS ROM rather than a private cartridge image.
        header = 0x7FF0
        checksum = sum(rom[:header]) & 0xFFFF
        rom[header:header + 8] = b"TMR SEGA"
        rom[header + 8] = checksum & 0xFF
        rom[header + 9] = checksum >> 8
        size_code = {0x8000: 0xC, 0x10000: 0xE, 0x20000: 0xF,
                     0x40000: 0x0, 0x80000: 0x1, 0x100000: 0x2,
                     0x200000: 0x3, 0x400000: 0x4}[target_size]
        rom[header + 15] = 0x40 | size_code  # export SMS region
        output.write_bytes(rom)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Sega Master System slideshow from ZX Spectrum .scr files.")
    parser.add_argument("directory", type=Path, help="directory containing .scr files")
    parser.add_argument("-o", "--output", type=Path, required=True, help="output .sms file")
    arguments = parser.parse_args()
    if not arguments.directory.is_dir():
        parser.error(f"not a directory: {arguments.directory}")
    screens, skipped = collect_screens(arguments.directory)
    for message in skipped:
        print(f"skip: {message}", file=sys.stderr)
    try:
        build(screens, arguments.output)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"scr2sms: {error}", file=sys.stderr)
        return 1
    print(f"built {arguments.output}: {len(screens)} screens, {arguments.output.stat().st_size // 1024} KiB, Sega mapper")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
