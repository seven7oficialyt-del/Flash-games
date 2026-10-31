#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swf2openfl - Pipeline SWF -> proyecto OpenFL (Haxe), listo para hacer el port.

Pasos que automatiza:
  1. Lee la cabecera del SWF (tamano, fps, AS2/AS3, clase de documento, simbolos).
  2. Extrae con JPEXS Free Flash Decompiler (FFDec): scripts, imagenes, shapes,
     sonidos, fuentes, textos y binarios.
  3. Convierte el ActionScript 3 a Haxe con as3hx.
  4. Genera un proyecto OpenFL (project.xml, src/, assets/) que compila a
     HTML5, Windows, Android, iOS, etc.

Uso:
  python3 swf2openfl.py setup                  # instala FFDec y las libs de Haxe
  python3 swf2openfl.py info juego.swf         # solo analiza el SWF
  python3 swf2openfl.py convert juego.swf      # pipeline completo
  python3 swf2openfl.py convert juego.swf --dry-run   # muestra los comandos

Requisitos: Python 3.8+, Java 8+ y Haxe 4.x (haxe.org/download).
Solo usa la libreria estandar de Python.
"""
from __future__ import annotations

import argparse
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("SWF2OPENFL_HOME", Path.home() / ".swf2openfl"))
FFDEC_DIR = HOME / "tools" / "ffdec"
FFDEC_API = "https://api.github.com/repos/jindrapetrik/jpexs-decompiler/releases/latest"
HAXELIBS = ["lime", "openfl", "as3hx"]

EXPORT_TYPES = "script,image,shape,sound,binaryData,text,font"
EXPORT_FORMATS = "script:as,image:png_gif_jpeg,shape:svg,sound:mp3_wav_flv,font:ttf"

# Codigos de tag SWF que nos interesan
TAG_END = 0
TAG_DO_ACTION = 12
TAG_DO_INIT_ACTION = 59
TAG_FILE_ATTRIBUTES = 69
TAG_SYMBOL_CLASS = 76
TAG_DO_ABC = 72
TAG_DO_ABC2 = 82


# --------------------------------------------------------------------------
# 1. Lectura de la cabecera SWF
# --------------------------------------------------------------------------
@dataclass
class SwfInfo:
    path: Path
    version: int = 0
    width: float = 0.0
    height: float = 0.0
    fps: float = 24.0
    frames: int = 0
    as3: bool = False
    as2: bool = False
    doc_class: str | None = None
    symbols: dict = field(default_factory=dict)  # id -> nombre de clase


def _decompress(data: bytes) -> bytes:
    sig = data[:3]
    if sig == b"FWS":
        return data[8:]
    if sig == b"CWS":
        return zlib.decompress(data[8:])
    if sig == b"ZWS":
        # 8 bytes cabecera, 4 bytes tamano comprimido, 5 bytes props LZMA, stream
        props = data[12:17]
        stream = data[17:]
        size = struct.unpack("<I", data[4:8])[0] - 8
        return lzma.decompress(props + struct.pack("<Q", size) + stream,
                               format=lzma.FORMAT_ALONE)
    raise ValueError("No parece un archivo SWF (firma desconocida)")


def _read_rect(body: bytes):
    nbits = body[0] >> 3
    nbytes = (5 + 4 * nbits + 7) // 8
    bits = int.from_bytes(body[:nbytes], "big")
    vals, pos = [], 5
    for _ in range(4):
        shift = nbytes * 8 - pos - nbits
        v = (bits >> shift) & ((1 << nbits) - 1)
        if v >> (nbits - 1):
            v -= 1 << nbits
        vals.append(v)
        pos += nbits
    return vals, nbytes


def _iter_tags(body: bytes, pos: int):
    while pos + 2 <= len(body):
        (hdr,) = struct.unpack_from("<H", body, pos)
        pos += 2
        code, length = hdr >> 6, hdr & 0x3F
        if length == 0x3F:
            (length,) = struct.unpack_from("<I", body, pos)
            pos += 4
        yield code, body[pos:pos + length]
        pos += length
        if code == TAG_END:
            break


def parse_swf(path: Path) -> SwfInfo:
    data = Path(path).read_bytes()
    info = SwfInfo(path=Path(path), version=data[3])
    body = _decompress(data)
    (xmin, xmax, ymin, ymax), off = _read_rect(body)
    info.width = (xmax - xmin) / 20.0
    info.height = (ymax - ymin) / 20.0
    info.fps = body[off + 1] + body[off] / 256.0
    (info.frames,) = struct.unpack_from("<H", body, off + 2)

    for code, tag in _iter_tags(body, off + 4):
        if code == TAG_FILE_ATTRIBUTES and tag and tag[0] & 0x08:
            info.as3 = True
        elif code in (TAG_DO_ABC, TAG_DO_ABC2):
            info.as3 = True
        elif code in (TAG_DO_ACTION, TAG_DO_INIT_ACTION):
            info.as2 = True
        elif code == TAG_SYMBOL_CLASS and len(tag) >= 2:
            (count,) = struct.unpack_from("<H", tag, 0)
            p = 2
            for _ in range(count):
                (sid,) = struct.unpack_from("<H", tag, p)
                end = tag.index(b"\x00", p + 2)
                name = tag[p + 2:end].decode("utf-8", "replace").replace("::", ".")
                info.symbols[sid] = name
                p = end + 1
    info.doc_class = info.symbols.get(0)
    return info


def print_info(info: SwfInfo) -> None:
    lang = "ActionScript 3" if info.as3 else ("ActionScript 1/2" if info.as2 else "sin codigo")
    print(f"  Archivo        : {info.path.name}")
    print(f"  Version SWF    : {info.version}")
    print(f"  Tamano         : {info.width:.0f} x {info.height:.0f} px")
    print(f"  FPS            : {info.fps:.1f}  ({info.frames} frames en la linea principal)")
    print(f"  Lenguaje       : {lang}")
    print(f"  Clase principal: {info.doc_class or '(no definida)'}")
    print(f"  Simbolos       : {len(info.symbols)}")


# --------------------------------------------------------------------------
# 2. Utilidades de ejecucion
# --------------------------------------------------------------------------
def run(cmd, dry: bool) -> int:
    print("  $ " + " ".join(str(c) for c in cmd))
    if dry:
        return 0
    try:
        return subprocess.call([str(c) for c in cmd])
    except FileNotFoundError as exc:
        print(f"  ! No se pudo ejecutar: {exc}")
        return 127


def find_ffdec(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    if FFDEC_DIR.exists():
        for jar in FFDEC_DIR.rglob("ffdec.jar"):
            return jar
    return None


# --------------------------------------------------------------------------
# 3. setup: instala FFDec y las librerias de Haxe
# --------------------------------------------------------------------------
def setup(dry: bool) -> int:
    ok = True
    if not shutil.which("java"):
        print("! Falta Java. Instala un JRE 8 o superior y vuelve a ejecutar.")
        ok = False

    if find_ffdec(None):
        print("= FFDec ya esta instalado en", FFDEC_DIR)
    else:
        print("> Descargando FFDec (JPEXS Free Flash Decompiler)...")
        if dry:
            print("  (dry-run) se consultaria", FFDEC_API)
        else:
            try:
                headers = {"User-Agent": "swf2openfl"}
                if os.environ.get("GITHUB_TOKEN"):  # evita el limite de la API en GitHub Actions
                    headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
                req = urllib.request.Request(FFDEC_API, headers=headers)
                release = json.load(urllib.request.urlopen(req, timeout=60))
                assets = release.get("assets", [])
                pick = next((a for a in assets
                             if re.fullmatch(r"ffdec_\d[\d.]*\.zip", a["name"])), None)
                if pick is None:
                    pick = next((a for a in assets
                                 if a["name"].startswith("ffdec_")
                                 and "lib" not in a["name"] and a["name"].endswith(".zip")), None)
                if pick is None:
                    raise RuntimeError("no se encontro el zip de FFDec en la ultima release")
                FFDEC_DIR.mkdir(parents=True, exist_ok=True)
                zpath = FFDEC_DIR / pick["name"]
                urllib.request.urlretrieve(pick["browser_download_url"], zpath)
                with zipfile.ZipFile(zpath) as zf:
                    zf.extractall(FFDEC_DIR)
                zpath.unlink()
                print("  FFDec instalado en", FFDEC_DIR)
            except Exception as exc:  # noqa: BLE001
                print(f"! No pude descargar FFDec automaticamente ({exc}).")
                print("  Descargalo a mano de https://github.com/jindrapetrik/jpexs-decompiler/releases")
                print(f"  y descomprimelo en {FFDEC_DIR}  (o usa --ffdec ruta/ffdec.jar).")
                ok = False

    if not shutil.which("haxe") or not shutil.which("haxelib"):
        print("! Falta Haxe. Instalalo desde https://haxe.org/download/ y repite 'setup'.")
        ok = False
    else:
        for lib in HAXELIBS:
            print(f"> haxelib install {lib}")
            if run(["haxelib", "install", lib, "--always"], dry) != 0:
                print(f"  ! Fallo la instalacion de {lib} (puede que ya estuviera instalada).")
        print("> Recuerda ejecutar una vez:  haxelib run lime setup")
    print("\nSetup completo." if ok else "\nSetup incompleto: revisa los avisos de arriba.")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 4. convert: SWF -> proyecto OpenFL
# --------------------------------------------------------------------------
def _project_xml(name: str, main: str, info: SwfInfo) -> str:
    pkg = re.sub(r"[^a-z0-9]", "", name.lower()) or "game"
    if pkg[0].isdigit():
        pkg = "g" + pkg
    w, h = int(round(info.width)) or 800, int(round(info.height)) or 600
    fps = int(round(info.fps)) or 30
    return f"""<?xml version="1.0" encoding="utf-8"?>
<project>
	<meta title="{name}" package="com.example.{pkg}" version="1.0.0" company="" />
	<app main="{main}" path="Export" file="{name}" />
	<window width="{w}" height="{h}" fps="{fps}" background="#FFFFFF" resizable="true" />
	<source path="src" />
	<haxelib name="openfl" />
	<assets path="assets" rename="assets" />
</project>
"""


STUB_MAIN = """import openfl.display.Sprite;

/**
 * Stub generado por swf2openfl: el SWF original usa ActionScript 1/2, que as3hx
 * no puede convertir. El codigo extraido esta en _extracted/scripts para portarlo a mano.
 */
class Main extends Sprite {
	public function new() {
		super();
		// TODO: portar la logica desde _extracted/scripts
	}
}
"""


def _build_manifest(info: SwfInfo, assets: Path, proj: Path) -> list:
    by_id: dict[int, list[str]] = {}
    if assets.exists():
        for f in assets.rglob("*"):
            if f.is_file():
                m = re.match(r"(\d+)(?:\D|$)", f.stem)
                if m:
                    by_id.setdefault(int(m.group(1)), []).append(
                        f.relative_to(proj).as_posix())
    rows = []
    for sid, name in sorted(info.symbols.items()):
        if sid == 0:
            continue
        rows.append({"id": sid, "class": name, "files": sorted(by_id.get(sid, []))})
    return rows


def convert(swf: Path, out_base: Path, ffdec: str | None, dry: bool) -> int:
    print(f"\n=== {swf.name} ===")
    info = parse_swf(swf)
    print_info(info)

    stem = re.sub(r"\W+", "_", swf.stem).strip("_") or "game"
    proj = out_base / f"{stem}_openfl"
    extracted, src, assets = proj / "_extracted", proj / "src", proj / "assets"
    if not dry:
        for d in (extracted, src, assets):
            d.mkdir(parents=True, exist_ok=True)

    # --- Paso 1: extraer con FFDec --------------------------------------
    print("\n[1/4] Extrayendo recursos y codigo con FFDec")
    jar = find_ffdec(ffdec)
    if jar is None and not dry:
        print("! No encuentro FFDec. Ejecuta primero:  python3 swf2openfl.py setup")
        return 1
    jar = jar or Path("ffdec.jar")
    cmd = ["java", "-jar", jar, "-format", EXPORT_FORMATS,
           "-export", EXPORT_TYPES, extracted, swf]
    if run(cmd, dry) != 0:
        print("  Reintentando sin -format (otra version de FFDec)...")
        if run(["java", "-jar", jar, "-export", EXPORT_TYPES, extracted, swf], dry) != 0:
            print("! FFDec fallo. Revisa la salida de arriba.")
            return 1

    # --- Paso 2: copiar assets ------------------------------------------
    print("\n[2/4] Copiando assets al proyecto")
    if not dry and extracted.exists():
        for child in sorted(extracted.iterdir()):
            if child.is_dir() and child.name.lower() != "scripts":
                shutil.copytree(child, assets / child.name, dirs_exist_ok=True)
                print(f"  assets/{child.name}")

    # --- Paso 3: AS3 -> Haxe --------------------------------------------
    print("\n[3/4] Convirtiendo ActionScript a Haxe")
    main_class = info.doc_class or "Main"
    todo_count = hx_count = 0
    if info.as3:
        if shutil.which("haxelib") or dry:
            run(["haxelib", "run", "as3hx", extracted / "scripts", src], dry)
        else:
            print("! haxelib no esta disponible; se omite as3hx.")
        if not dry and src.exists():
            for f in src.rglob("*.hx"):
                hx_count += 1
                todo_count += f.read_text(encoding="utf-8", errors="replace").count("TODO")
    else:
        main_class = "Main"
        print("  El SWF no es AS3: as3hx no aplica. Se crea un Main.hx vacio.")
        if not dry:
            (src / "Main.hx").write_text(STUB_MAIN, encoding="utf-8")

    # --- Paso 4: proyecto OpenFL ----------------------------------------
    print("\n[4/4] Generando proyecto OpenFL")
    if not dry:
        (proj / "project.xml").write_text(_project_xml(stem, main_class, info), encoding="utf-8")
        manifest = _build_manifest(info, assets, proj)
        (proj / "assets_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print("  project.xml, assets_manifest.json")

    print("\nListo." if not dry else "\n(dry-run: no se escribio nada)")
    print(f"  Proyecto : {proj}")
    if info.as3 and not dry:
        print(f"  Haxe     : {hx_count} archivos .hx, {todo_count} marcas TODO de as3hx")
    print("  Siguiente:")
    print(f"    cd {proj}")
    print("    haxelib run openfl test html5")
    print("  Notas:")
    print("    - assets_manifest.json une cada clase del SWF con sus archivos extraidos;")
    print("      sirve para reemplazar los [Embed] / clases de simbolos del AS original.")
    print("    - Si el juego es online, falta reconstruir el servidor (no se automatiza).")
    return 0


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pipeline SWF -> proyecto OpenFL (Haxe)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="instala FFDec y las librerias de Haxe")
    p_setup.add_argument("--dry-run", action="store_true")

    p_info = sub.add_parser("info", help="analiza un SWF sin convertirlo")
    p_info.add_argument("swf", nargs="+", type=Path)

    p_conv = sub.add_parser("convert", help="convierte uno o varios SWF")
    p_conv.add_argument("swf", nargs="+", type=Path)
    p_conv.add_argument("-o", "--out", type=Path, default=Path("."),
                        help="carpeta base de salida (por defecto, la actual)")
    p_conv.add_argument("--ffdec", help="ruta a ffdec.jar (si no se usa el de 'setup')")
    p_conv.add_argument("--dry-run", action="store_true",
                        help="muestra los comandos sin ejecutarlos")

    args = ap.parse_args(argv)

    if args.cmd == "setup":
        return setup(args.dry_run)

    rc = 0
    for swf in args.swf:
        if not swf.is_file():
            print(f"! No existe: {swf}")
            rc = 1
            continue
        try:
            if args.cmd == "info":
                print_info(parse_swf(swf))
                print()
            else:
                rc |= convert(swf, args.out, args.ffdec, args.dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"! Error con {swf}: {exc}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
