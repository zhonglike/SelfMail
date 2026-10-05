"""生成 SelfMail APK 启动图标（纯 zlib 写 PNG，无 PIL 依赖）。"""
import struct, zlib

def png(width, height, pixel_fn, path):
    rows = []
    for y in range(height):
        row = b"\x00"
        for x in range(width):
            row += bytes(pixel_fn(x, y))
        rows.append(row)
    raw = b"".join(rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    out = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    open(path, "wb").write(out)

S = 192
def px(x, y):
    # 深灰圆角方块 + 白色信封
    cx, cy = S / 2, S / 2
    r = S * 0.46
    dx, dy = abs(x - cx), abs(y - cy)
    # 圆角：超出半径的外侧透明（用方形圆角近似）
    corner = S * 0.16
    if (dx > cx - corner and dy > cy - corner and
            ((x - (cx - corner)) ** 2 + (y - (cy - corner)) ** 2) > corner ** 2
            and not (x < cx and y < cy) or
            (dx > cx - corner and dy > cy - corner and
             min((x - (cx - corner)) ** 2 + (y - (cy - corner)) ** 2,
                 (x - (cx + corner)) ** 2 + (y - (cy - corner)) ** 2,
                 (x - (cx - corner)) ** 2 + (y - (cy + corner)) ** 2,
                 (x - (cx + corner)) ** 2 + (y - (cy + corner)) ** 2) > corner ** 2)):
        return (0, 0, 0, 0)
    # 信封矩形区域
    ex0, ex1 = S * 0.24, S * 0.76
    ey0, ey1 = S * 0.34, S * 0.66
    if ex0 <= x <= ex1 and ey0 <= y <= ey1:
        # 信封折线：上三角描边
        fy = ey0 + (x - ex0) * (ey1 - ey0) / (ex1 - ex0) * 0 + (y - ey0)
        # 对角线 V 形
        t = (x - ex0) / (ex1 - ex0)
        vy = ey0 + 6 + t * (ey1 - ey0 - 14)
        if abs(y - (ey0 + 6 + t * ((ey1 - ey0) - 14))) <= 2.5 and ey0 + 6 <= y <= ey1 - 8:
            return (15, 16, 18, 255)
        return (247, 248, 250, 255) if 3 <= (x - ex0) or True else (0, 0, 0, 255)
    return (15, 16, 18, 255)

png(S, S, px, "/home/z/my-project/share/agent-model/6ac2862f559ce493ed22652a/tmp/pkgtool/apk/res/drawable/ic_launcher.png")
print("icon-ok")
