#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
photo_rescue.py —— 照片找回工具（Windows / macOS / Linux，Python 3.8+）

五个能力：
  1) archive  把这台电脑所有年代的图片一次性收集起来，按年份归档（默认模式）
  2) hidden   扫描磁盘上"隐藏/系统"属性的照片，以及藏在 AppData、缓存目录里的照片
  3) recycle  从 Windows 回收站（$Recycle.Bin）还原还能捞回来的照片（含原始路径）
  4) carve    按文件头特征裸扫磁盘，找回已被删除、回收站里也没有了的照片
              （原理：删除只是删除目录项，数据块还在磁盘上；没被新数据覆盖就能救回来）
  5) vss      从"还原点/以前的版本"（卷影副本）里捞回删除前的照片

典型用法（Windows 管理员命令行 / PowerShell 管理员模式）：
    py photo_rescue.py --mode archive --out D:\照片归档
    py photo_rescue.py --mode all     --out D:\恢复结果
    py photo_rescue.py --mode carve   --drive C: --out E:\恢复结果
    py photo_rescue.py --mode hidden  --out D:\恢复结果
    py photo_rescue.py --mode recycle --out D:\恢复结果
    py photo_rescue.py --mode vss     --out D:\恢复结果

重要提醒（务必看）：
  * 恢复删除的文件时，输出目录千万不要放在"正在扫描的那块磁盘"上，否则会写入同一块盘、
    把待恢复的数据覆盖掉，再也救不回来。用另一块硬盘 / U 盘最稳妥。
  * 发现误删后立刻停止在该盘上装软件、下文件、拍新照片；越早扫，成功率越高。
  * 裸扫磁盘必须管理员权限（Windows: 以管理员身份打开终端；Linux: sudo）。
"""

import argparse
import csv
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf",
    ".raf", ".rw2", ".pef", ".srw", ".jfif", ".ico",
}
RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2", ".pef", ".srw"}

FILE_ATTR_HIDDEN = 0x2
FILE_ATTR_SYSTEM = 0x4

SKIP_DIR_NAMES = {"$Recycle.Bin", "System Volume Information", "node_modules", ".git"}


# -----------------------------------------------------------------------------
# 通用小工具
# -----------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024


def pil_verify(data, need_pixels=16 * 16):
    """用 Pillow 校验这段字节是不是一张能解码的完整图片。没装 Pillow 就放行。"""
    try:
        from PIL import Image  # noqa
    except Exception:
        return True
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
        im = Image.open(io.BytesIO(data))
        im.load()
        w, h = im.size
        return w >= need_pixels, h >= need_pixels
    except Exception:
        return False


def sniff_ext(data):
    """按文件头判断图片类型，用于识别没有扩展名的照片（缓存文件、缩略图库等）。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if len(data) > 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM" and len(data) > 26:
        return ".bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    if len(data) > 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"hevc", b"avif"):
            return ".heic"
    return None


def is_admin():
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def win_drives():
    """返回 Windows 上所有本地固定磁盘盘符，如 ['C:', 'D:']。"""
    if os.name != "nt":
        return ["/"]
    drives = []
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                letter = f"{chr(65 + i)}:"
                # 3 = 本地磁盘
                if ctypes.windll.kernel32.GetDriveTypeW(letter + "\\") == 3:
                    drives.append(letter)
    except Exception:
        pass
    return drives or ["C:"]


def same_volume(path, drive):
    """判断输出目录是否落在被扫描的那块盘上。"""
    try:
        if os.name == "nt":
            return os.path.abspath(path)[:2].upper() == drive.upper()[:2]
        dev = os.stat(path).st_dev
        return dev == os.stat(drive).st_dev
    except Exception:
        return False


def new_path(outdir, sub, name):
    p = os.path.join(outdir, sub, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def unique_path(outdir, sub, name):
    p = new_path(outdir, sub, name)
    if not os.path.exists(p):
        return p
    base, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = new_path(outdir, sub, f"{base}_{i}{ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


# -----------------------------------------------------------------------------
# 1) 隐藏 / 系统属性的照片
# -----------------------------------------------------------------------------
def scan_hidden(roots, outdir, copy_files=True, csv_path=None, sniff_unknown=True):
    rows = []
    copied = 0
    seen = 0
    log(f"[隐藏扫描] 开始：{', '.join(roots)}")

    for root in roots:
        stack = [root]
        while stack:
            cur = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except (PermissionError, OSError):
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name in SKIP_DIR_NAMES:
                            continue
                        stack.append(e.path)
                        continue
                    st = e.stat(follow_symlinks=False)
                except (OSError, PermissionError):
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                seen += 1
                ext = os.path.splitext(e.name)[1].lower()
                kind = None
                if ext in PHOTO_EXTS:
                    kind = "photo"
                elif sniff_unknown and ext == "" and st.st_size >= 4096:
                    # 无扩展名的小文件，按文件头猜（缓存缩略图常常没有扩展名）
                    try:
                        with open(e.path, "rb") as f:
                            head = f.read(64)
                        if sniff_ext(head):
                            kind = "photo-sniffed"
                    except OSError:
                        kind = None
                if kind is None:
                    continue
                attr = getattr(st, "st_file_attributes", 0)
                hidden = bool(attr & FILE_ATTR_HIDDEN) or e.name.startswith(".")
                system = bool(attr & FILE_ATTR_SYSTEM)
                # 只报告隐藏/系统的，以及藏在 AppData、缓存目录里的照片
                segs = {s.lower() for s in re.split(r"[\\/]+", e.path) if s}
                in_cache = bool(segs & {
                    "appdata", "cache", "caches", "thumbcache", "thumbnails",
                    "wechat", "weixin", "tencent", "qq", "wps", "browser", "chrome",
                    "edge", "firefox", "packages", "onedrive", "icloud",
                })
                if not (hidden or system or in_cache):
                    continue
                tag = "隐藏" if hidden else ""
                tag = (tag + ("+系统" if system else "")) if tag else ("系统" if system else "缓存目录")
                rows.append([tag, e.path, round(st.st_size / 1048576, 3),
                             datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")])
                if copy_files:
                    name = os.path.basename(e.path)
                    if not os.path.splitext(name)[1]:
                        with open(e.path, "rb") as f:
                            name += sniff_ext(f.read(64)) or ".jpg"
                    dst = unique_path(outdir, "hidden_photos",
                                      f"{tag.replace('+', '_')}_{name}")
                    try:
                        shutil.copy2(e.path, dst)
                        copied += 1
                    except (OSError, PermissionError):
                        pass

    if rows:
        cpath = csv_path or os.path.join(outdir, "隐藏照片清单.csv")
        os.makedirs(os.path.dirname(cpath) or ".", exist_ok=True)
        with open(cpath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["类型", "路径", "大小MB", "修改时间"])
            w.writerows(rows)
        log(f"[隐藏扫描] 共发现 {len(rows)} 张隐藏/缓存照片，清单：{cpath}")
    else:
        log("[隐藏扫描] 未发现隐藏照片。")
    if copy_files:
        log(f"[隐藏扫描] 已复制 {copied} 个文件到 {os.path.join(outdir, 'hidden_photos')}")
    log(f"[隐藏扫描] 共检查文件 {seen} 个。")
    return rows


# -----------------------------------------------------------------------------
# 2) Windows 回收站（$Recycle.Bin）还原
# -----------------------------------------------------------------------------
def parse_recycle_index(ipath):
    """解析 $Ixxxxxx 索引文件，返回 (原始路径, 删除时间, 大小)。失败返回 (None,None,None)。"""
    try:
        with open(ipath, "rb") as f:
            data = f.read()
        if len(data) < 24:
            return None, None, None
        ver = struct.unpack("<Q", data[0:8])[0]
        size = struct.unpack("<Q", data[8:16])[0]
        dt = None
        if ver == 2:
            try:
                ticks = struct.unpack("<Q", data[16:24])[0]
                dt = datetime(1601, 1, 1) + __import__("datetime").timedelta(microseconds=ticks / 10)
            except Exception:
                dt = None
            raw = data[24:]
        else:
            raw = data[16:]
        text = raw.decode("utf-16-le", errors="ignore").rstrip("\x00")
        text = text.replace("\x00", "")
        if not text and len(raw) >= 520:
            text = raw[:520].decode("utf-16-le", errors="ignore").rstrip("\x00")
        return text or None, dt, size
    except Exception:
        return None, None, None


def scan_recycle(outdir, copy_files=True):
    if os.name != "nt":
        log("[回收站] 当前不是 Windows，跳过。")
        return []
    rows = []
    copied = 0
    for drive in win_drives():
        bin_dir = os.path.join(drive + "\\", "$Recycle.Bin")
        if not os.path.isdir(bin_dir):
            continue
        for sid in os.listdir(bin_dir):
            sdir = os.path.join(bin_dir, sid)
            if not os.path.isdir(sdir):
                continue
            try:
                names = os.listdir(sdir)
            except (PermissionError, OSError):
                continue
            for name in names:
                if not name.startswith("$R"):
                    continue
                rpath = os.path.join(sdir, name)
                ext = os.path.splitext(name)[1].lower()
                if ext not in PHOTO_EXTS:
                    # 无扩展名的 $R 文件按文件头判断
                    try:
                        with open(rpath, "rb") as f:
                            if not sniff_ext(f.read(64)):
                                continue
                    except OSError:
                        continue
                ipath = os.path.join(sdir, "$I" + name[2:])
                orig, dtime, _ = parse_recycle_index(ipath) if os.path.exists(ipath) else (None, None, None)
                try:
                    sz = os.path.getsize(rpath)
                except OSError:
                    continue
                rows.append([orig or "(未知)",
                             dtime.strftime("%Y-%m-%d %H:%M:%S") if dtime else
                             datetime.fromtimestamp(os.path.getmtime(rpath)).strftime("%Y-%m-%d %H:%M:%S"),
                             round(sz / 1048576, 3), rpath])
                if copy_files:
                    base = os.path.basename(orig) if orig else f"recycle_{name}{ext or '.jpg'}"
                    dst = unique_path(outdir, "recycle_photos", base)
                    try:
                        shutil.copy2(rpath, dst)
                        copied += 1
                    except (OSError, PermissionError):
                        pass
    if rows:
        cpath = os.path.join(outdir, "回收站照片清单.csv")
        with open(cpath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["原始路径", "删除时间", "大小MB", "回收站文件"])
            w.writerows(rows)
        log(f"[回收站] 找到 {len(rows)} 个照片，已还原 {copied} 个到 recycle_photos，清单：{cpath}")
    else:
        log("[回收站] 未找到可还原的照片。")
    return rows


# -----------------------------------------------------------------------------
# 3) 裸扫磁盘 carve（找回已删除、回收站也没有的照片）
# -----------------------------------------------------------------------------
MAX_PHOTO = 24 * 1024 * 1024     # 单张照片最大按 24MB 处理
CHUNK = 4 * 1024 * 1024
PNG_END = b"IEND\xaeB`\x82"
SIGS = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
)


def _gif_end(buf, pos):
    """返回 GIF 结束位置；数据不够返回 None；结构非法返回 -1。"""
    try:
        p = pos + 13
        if buf[pos + 10] & 0x80:                      # global color table
            p += 3 * (2 ** ((buf[pos + 10] & 7) + 1))
        while p < len(buf):
            b = buf[p]
            if b == 0x3B:                             # trailer
                return p + 1
            if b == 0x21:                             # extension
                p += 2
                while p < len(buf):
                    sz = buf[p]; p += 1
                    if sz == 0:
                        break
                    p += sz
            elif b == 0x2C:                           # image descriptor
                packed = buf[p + 9]
                p += 10
                if packed & 0x80:
                    p += 3 * (2 ** ((packed & 7) + 1))
                p += 1                                # LZW min code size
                while p < len(buf):
                    sz = buf[p]; p += 1
                    if sz == 0:
                        break
                    p += sz
            else:
                return -1                             # 结构非法
        return None
    except IndexError:
        return None


def find_end(buf, pos, ext, verify):
    """在 buf 中从 pos 找图片结束位置。
    返回 (状态, 结束下标)：状态 ok / need_more / invalid。"""
    if ext == ".jpg":
        limit = min(len(buf), pos + MAX_PHOTO)
        p = pos + 3
        tries = 0
        while True:
            i = buf.find(b"\xff\xd9", p, limit)
            if i == -1:
                if limit < pos + MAX_PHOTO and len(buf) < pos + MAX_PHOTO:
                    return "need_more", None
                return "invalid", None
            if not verify or pil_verify(buf[pos:i + 2]):
                return "ok", i + 2
            tries += 1
            if tries >= 6:
                return "invalid", None
            p = i + 2
    if ext == ".png":
        limit = min(len(buf), pos + MAX_PHOTO)
        i = buf.find(PNG_END, pos + 8, limit)
        if i == -1:
            if len(buf) < pos + MAX_PHOTO:
                return "need_more", None
            return "invalid", None
        return "ok", i + 8
    if ext == ".gif":
        r = _gif_end(buf, pos)
        if r is None:
            return "need_more", None
        if r == -1:
            return "invalid", None
        return "ok", r
    if ext == ".bmp":
        if len(buf) < pos + 6:
            return "need_more", None
        size = struct.unpack("<I", buf[pos + 2:pos + 6])[0]
        if not (26 <= size <= MAX_PHOTO):
            return "invalid", None
        if len(buf) < pos + size:
            return "need_more", None
        return "ok", pos + size
    if ext == ".webp":
        if len(buf) < pos + 12:
            return "need_more", None
        if buf[pos + 8:pos + 12] != b"WEBP":
            return "invalid", None
        size = struct.unpack("<I", buf[pos + 4:pos + 8])[0] + 8
        if len(buf) < pos + size:
            return "need_more", None
        return "ok", pos + size
    # tif / 其他：先取到窗口末尾或下一个签名处，再用 Pillow 校验
    limit = min(len(buf), pos + MAX_PHOTO)
    nxt = None
    for marker, _ in SIGS:
        j = buf.find(marker, pos + 8, limit)
        if j != -1 and (nxt is None or j < nxt):
            nxt = j
    end = nxt or limit
    if end >= limit and len(buf) < pos + MAX_PHOTO:
        return "need_more", None
    if pil_verify(buf[pos:end]):
        return "ok", end
    return "invalid", None


def open_raw(target):
    """打开裸设备或普通文件。"""
    if os.name == "nt":
        # 裸设备必须用 \\.\ 前缀；去掉尾部反斜杠
        dev = target
        if re.match(r"^[A-Za-z]:\\?$", target):
            dev = "\\\\.\\" + target[0].upper() + ":"
        return open(dev, "rb", buffering=0)
    return open(target, "rb", buffering=0)


def carve(target, outdir, start=0, max_bytes=None, verify=True, ext_filter=None,
          min_size=2048, verbose_every=60):
    """裸扫 target（裸设备或镜像文件），按特征切出照片。"""
    out_sub = "carved_photos"
    os.makedirs(os.path.join(outdir, out_sub), exist_ok=True)
    hashes = {}
    pending = []
    buf = b""
    buf_start = 0
    scan_abs = start
    total_read = 0
    saved = 0
    last_log = time.time()
    f = None
    tag = re.sub(r"[^0-9A-Za-z]", "", target.split(":")[0]) or "img"

    try:
        f = open_raw(target)
        if start:
            try:
                f.seek(start)
            except OSError:
                start = 0
                scan_abs = 0
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            total_read += len(chunk)
            if max_bytes and total_read > max_bytes:
                chunk = chunk[:max(0, max_bytes - (total_read - len(chunk)))]
                buf += chunk
                total_read = len(buf)
                eof = True
            else:
                buf += chunk
                eof = False

            # (a) 找新的起点
            pos = scan_abs - buf_start
            while pos < len(buf):
                hit = None
                for marker, ext in SIGS:
                    j = buf.find(marker, pos)
                    if j == -1:
                        continue
                    if marker == b"RIFF":
                        if j + 12 > len(buf) or buf[j + 8:j + 12] != b"WEBP":
                            continue
                    if marker == b"BM":
                        # BM 太容易误判，要求后面像位图头
                        if j + 26 > len(buf) or buf[j + 6:j + 10] == b"\x00\x00\x00\x00":
                            continue
                    if hit is None or j < hit[0]:
                        hit = (j, ext)
                if hit is None:
                    break
                j, ext = hit
                abs_pos = buf_start + j
                scan_abs = abs_pos + 2
                pos = j + 2
                if ext_filter and ext not in ext_filter:
                    continue
                pending.append({"start": abs_pos, "ext": ext})
                if len(pending) > 400:
                    pending.pop(0)

            # (b) 检查待定项能否收尾
            if pending:
                keep = []
                for item in pending:
                    rel = item["start"] - buf_start
                    if rel < 0:
                        continue                     # 已滑出窗口，放弃
                    state, end = find_end(buf, rel, item["ext"], verify)
                    if state == "ok":
                        data = buf[rel:end]
                        if len(data) >= min_size:
                            h = hashlib.md5(data[:65536]).hexdigest() + str(len(data))
                            if h not in hashes:
                                hashes[h] = True
                                name = f"rec_{tag}_{item['start']:012d}_{h[:8]}{item['ext']}"
                                dst = unique_path(outdir, out_sub, name)
                                with open(dst, "wb") as o:
                                    o.write(data)
                                saved += 1
                    elif state == "need_more" and not eof:
                        keep.append(item)
                pending = keep

            # (c) 回收缓冲
            if len(buf) > MAX_PHOTO + 2 * CHUNK:
                cut = len(buf) - (MAX_PHOTO + CHUNK)
                buf_start += cut
                buf = buf[cut:]

            now = time.time()
            if now - last_log > verbose_every:
                pct = ""
                if max_bytes:
                    pct = f" ({total_read * 100.0 / max_bytes:.1f}%)"
                log(f"[carve] {target} 已扫 {human(total_read)}{pct}，已恢复 {saved} 张")
                last_log = now
            if eof:
                break
    except PermissionError:
        log(f"[carve] 没有权限读取 {target}。请用管理员身份运行。")
    except FileNotFoundError as e:
        log(f"[carve] 设备/文件不存在：{e}")
    except OSError as e:
        log(f"[carve] 读取 {target} 出错：{e}（该盘可能正在被系统占用，可先关闭程序再试）")
    finally:
        if f:
            try:
                f.close()
            except Exception:
                pass

    log(f"[carve] 完成：{target} 共扫描 {human(total_read)}，恢复 {saved} 张 -> {os.path.join(outdir, out_sub)}")
    return saved


# -----------------------------------------------------------------------------
# 4) 卷影副本（还原点 / 以前的版本）
# -----------------------------------------------------------------------------
def scan_vss(outdir, copy_files=True):
    if os.name != "nt":
        log("[卷影副本] 仅 Windows 支持，跳过。")
        return []
    try:
        out = subprocess.run(["vssadmin", "list", "shadows"],
                             capture_output=True, text=True, encoding="utf-8", errors="ignore")
        text = (out.stdout or "") + (out.stderr or "")
    except Exception as e:
        log(f"[卷影副本] 调用 vssadmin 失败：{e}")
        return []
    ids = sorted(set(int(x) for x in re.findall(r"HarddiskVolumeShadowCopy(\d+)", text)))
    if not ids:
        log("[卷影副本] 没找到还原点/卷影副本。可以打开『此电脑-属性-以前的版本』确认是否开启过保护。")
        return []
    rows = []
    for sid in ids:
        link = os.path.join(os.environ.get("TEMP", "."), f"_shadow_{sid}")
        try:
            if not os.path.exists(link):
                subprocess.run(["cmd", "/c", "mklink", "/d", link,
                                f"\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy{sid}"],
                               capture_output=True, text=True)
            if not os.path.isdir(link):
                log(f"[卷影副本] 挂载 #{sid} 失败（可能不是管理员）。")
                continue
            log(f"[卷影副本] 已挂载 #{sid} -> {link}，开始扫描照片…")
            found = scan_hidden([link], outdir, copy_files=copy_files,
                                csv_path=os.path.join(outdir, f"卷影{sid}_照片清单.csv"),
                                sniff_unknown=False)
            rows.extend(found)
        except Exception as e:
            log(f"[卷影副本] #{sid} 处理失败：{e}")
    return rows


# -----------------------------------------------------------------------------
# 5) 全量归档：把这台电脑所有年代的图片一次性收集出来，按年份分文件夹
# -----------------------------------------------------------------------------
def exif_datetime(path):
    """读 EXIF 拍摄时间；读不到返回 None。"""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            if not ex:
                return None
            for tag in (36867, 36868, 306):      # 拍摄时间 / 数字化时间 / 修改时间
                val = ex.get(tag)
                if not val:
                    continue
                txt = str(val).strip().replace("-", ":")
                try:
                    return datetime.strptime(txt[:19], "%Y:%m:%d %H:%M:%S")
                except Exception:
                    continue
    except Exception:
        return None
    return None


def file_hash(path):
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                blk = f.read(1024 * 1024)
                if not blk:
                    break
                h.update(blk)
    except OSError:
        return None
    return h.hexdigest()


def scan_archive(roots, outdir, copy_files=True, min_year=None, max_year=None,
                 dedupe=False, csv_name="全部照片清单.csv"):
    """收集 roots 下所有年代的图片，按年份归档并生成清单。"""
    rows, stats = [], {}
    copied = dup = too_small = failed = checked = 0
    hashes = set()
    t0 = time.time()

    log(f"[归档] 开始扫描：{', '.join(roots)}")
    log(f"[归档] 时间范围：{min_year or '最早'} — {max_year or '至今'}；"
        f"复制={'开' if copy_files else '关（只出清单）'}；去重={'开' if dedupe else '关'}")

    for root in roots:
        stack = [root]
        while stack:
            cur = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except (PermissionError, OSError):
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name in SKIP_DIR_NAMES:
                            continue
                        stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except (OSError, PermissionError):
                    continue
                checked += 1
                ext = os.path.splitext(e.name)[1].lower()
                if ext in PHOTO_EXTS:
                    pass
                elif ext == "" and st.st_size >= 4096:
                    try:
                        with open(e.path, "rb") as f:
                            if not sniff_ext(f.read(64)):
                                continue
                    except OSError:
                        continue
                else:
                    continue
                if st.st_size < 1024:            # 过滤图标、表情这类极小图
                    too_small += 1
                    continue
                when = exif_datetime(e.path) or datetime.fromtimestamp(st.st_mtime)
                year = when.year
                if (min_year and year < min_year) or (max_year and year > max_year):
                    continue
                if dedupe:
                    h = file_hash(e.path)
                    if h is None:
                        failed += 1
                        continue
                    if h in hashes:
                        dup += 1
                        continue
                    hashes.add(h)
                archived = ""
                if copy_files:
                    try:
                        dst = unique_path(outdir, os.path.join("照片归档", str(year)), e.name)
                        shutil.copy2(e.path, dst)
                        archived = dst
                        copied += 1
                    except (OSError, PermissionError, shutil.SameFileError):
                        failed += 1
                rows.append([year, when.strftime("%Y-%m-%d %H:%M:%S"),
                             round(st.st_size / 1048576, 3), e.path, archived])
                stats[year] = stats.get(year, 0) + 1

            if time.time() - t0 > 60:
                log(f"[归档] 已检查 {checked} 个文件，命中图片 {len(rows)} 张，已复制 {copied} 张…")
                t0 = time.time()

    rows.sort(key=lambda r: (r[0], r[1]))
    if rows:
        cpath = os.path.join(outdir, csv_name)
        with open(cpath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["年份", "拍摄/修改时间", "大小MB", "原路径", "归档文件"])
            w.writerows(rows)
        with open(os.path.join(outdir, "年度统计.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["年份", "照片数"])
            for y in sorted(stats):
                w.writerow([y, stats[y]])
        log(f"[归档] 共找到 {len(rows)} 张图片，时间跨度 {min(stats)}—{max(stats)} 年。")
        log(f"[归档] 清单：{cpath}")
        log("[归档] 各年份张数：")
        for y in sorted(stats):
            log(f"        {y} 年：{stats[y]} 张")
    else:
        log("[归档] 指定范围里没有找到图片。")
    log(f"[归档] 检查文件 {checked} 个；过小跳过 {too_small}；重复跳过 {dup}；失败 {failed}；"
        f"共复制 {copied} 张 -> {os.path.join(outdir, '照片归档')}")
    return rows


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def num0(s):
    """把 "0x100000" / "1M"/"2G" 之类的参数转成字节数。"""
    s = str(s).strip().replace("_", "")
    unit = s[-1:].upper()
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}.get(unit, 1)
    if unit in "KMG":
        s = s[:-1]
    return int(float(s) * mult) if ("." in s) else int(s, 0) * mult


def main():
    ap = argparse.ArgumentParser(
        description="照片找回工具：全量归档 / 隐藏照片 / 回收站还原 / 已删除裸扫 / 还原点",
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mode", default="archive",
                    choices=["archive", "hidden", "recycle", "carve", "vss", "all"],
                    help="archive=把这台电脑所有年代的图片全部收集归档（默认）\n"
                         "hidden=找隐藏照片  recycle=回收站还原  carve=已删除裸扫  vss=还原点\n"
                         "all=上面全做：归档+隐藏+回收站+还原点+已删除裸扫")
    ap.add_argument("--drive", default=None,
                    help="裸扫目标，如 C: 或 /dev/sda1 或镜像文件路径；默认所有本地盘")
    ap.add_argument("--out", default="./照片归档", help="输出目录（归档/恢复都建议放在另一块磁盘）")
    ap.add_argument("--roots", default=None,
                    help="扫描的根目录，多个用 ; 分隔；默认本机所有磁盘。\n"
                         "只想收某个文件夹的照片：--roots \"D:\\我的照片\"")
    ap.add_argument("--min-year", type=int, default=None, help="只要这一年及以后的照片，如 --min-year 2015")
    ap.add_argument("--max-year", type=int, default=None, help="只要这一年及以前的照片，如 --max-year 2020")
    ap.add_argument("--dedupe", action="store_true", help="按内容去重（同一张图存了多份时只留一份）")
    ap.add_argument("--start", type=num0, default=0, help="裸扫起始偏移（字节）")
    ap.add_argument("--max-bytes", type=num0, default=None, help="裸扫最多读取字节数")
    ap.add_argument("--no-verify", action="store_true", help="关闭 Pillow 校验（更快但可能有坏图）")
    ap.add_argument("--no-copy", action="store_true", help="只列清单，不复制文件")
    args = ap.parse_args()

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    log(f"输出目录：{outdir}")
    log(f"管理员权限：{'是' if is_admin() else '否'}")
    log(f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    log("-" * 60)

    copy_files = not args.no_copy
    mode = args.mode

    # 输出目录不能与被扫磁盘同盘，否则会覆盖待恢复数据
    drives = win_drives() if os.name == "nt" else ["/"]
    scan_targets = [args.drive] if args.drive else drives
    vols = [t[:2] if os.name == "nt" and len(t) <= 3 else t for t in scan_targets]
    for vol in vols:
        if os.path.isdir(vol) or (os.name == "nt" and len(vol) == 2):
            if os.name == "nt" and same_volume(outdir, vol):
                log(f"!! 警告：输出目录和被扫描的 {vol} 是同一块磁盘。")
                log("!! 恢复时写入同一块盘会覆盖待恢复照片，建议改到另一块硬盘/U 盘。")
                log("!! 若坚持继续，只能保证尽量少写（本次仅写恢复出的小文件）。")

    all_roots = ([r.strip() for r in args.roots.split(";") if r.strip()] if args.roots else
                 ([d + "\\" for d in drives] if os.name == "nt" else ["/"]))

    if mode in ("archive", "all"):
        scan_archive(all_roots, outdir, copy_files=copy_files,
                     min_year=args.min_year, max_year=args.max_year, dedupe=args.dedupe)

    if mode in ("hidden", "all"):
        scan_hidden(all_roots, outdir, copy_files=copy_files)

    if mode in ("recycle", "all"):
        scan_recycle(outdir, copy_files=copy_files)

    if mode in ("vss", "all"):
        scan_vss(outdir, copy_files=copy_files)

    if mode in ("carve", "all"):
        if not is_admin():
            log("[carve] 提示：未获得管理员权限，裸设备可能打不开。请用管理员身份重跑。")
        ext_filter = {".jpg", ".png", ".gif", ".bmp", ".webp", ".tif"}
        total = 0
        for t in scan_targets:
            if os.name == "nt" and not t.endswith("\\") and len(t) <= 2:
                t = t + ":"
            total += carve(t, outdir, start=args.start, max_bytes=args.max_bytes,
                           verify=not args.no_verify, ext_filter=ext_filter)
        log(f"[carve] 本轮共恢复照片 {total} 张。")

    log("-" * 60)
    log(f"全部完成。请到 {outdir} 查看：照片归档\\年份\\ 、全部照片清单.csv 、年度统计.csv 、"
        f"隐藏照片清单.csv 、回收站照片清单.csv 、carved_photos")
    log("建议：先双击打开清单，再用看图软件逐张检查；部分照片可能是缩略图（分辨率较小）。")
    log("提示：这次没找回的照片，说明数据块已被覆盖，通常无法再恢复。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n已中断。")
        sys.exit(1)
