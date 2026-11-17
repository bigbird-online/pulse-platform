#!/usr/bin/env python3
"""改造结果图 — 全部取自 2026-07 全月真语料回算，非估计。"""
from PIL import Image, ImageDraw, ImageFont

R = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
S = 2

def f(size, bold=False):
    return ImageFont.truetype(B if bold else R, int(size * S))

BG    = (30, 30, 32)
CARD  = (44, 44, 47)
TXT   = (245, 245, 247)
DIM   = (152, 152, 158)
DIM2  = (110, 110, 116)
RED   = (255, 85, 79)
AMBER = (255, 179, 64)
GREEN = (72, 207, 122)
BLUE  = (74, 158, 255)

def rr(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)

def txt(d, xy, s, font, fill):
    d.text((xy[0]*S, xy[1]*S), s, font=font, fill=fill)

def w_of(d, s, font):
    return d.textlength(s, font=font) / S

# 真实数据（07-01 .. 07-26）
DAYS = [
    ("07-01",8,0),("07-03",11,0),("07-04",6,1),("07-05",2,0),("07-06",6,0),
    ("07-07",4,1),("07-08",8,2),("07-09",12,2),("07-10",7,1),("07-11",7,1),
    ("07-12",10,1),("07-13",16,1),("07-14",6,2),("07-15",9,1),("07-16",18,2),
    ("07-17",38,4),("07-18",9,0),("07-19",24,2),("07-20",63,4),("07-21",69,2),
    ("07-22",117,6),("07-23",53,0),("07-24",74,4),("07-25",41,7),
]

W, H = 900, 660
im = Image.new("RGB", (W*S, H*S), BG)
d = ImageDraw.Draw(im)

txt(d, (24, 18), "改造结果 — 2026-07 全月真语料回算", f(15, True), TXT)
txt(d, (24, 40), "不是估计：拿 1464 条真实告警事件按新规则重跑", f(10), DIM)

# ── 三个大数 ──
y = 68
stats = [
    ("全月推送", "619", "47", "降 92%"),
    ("日均", "24.8", "1.9", ""),
    ("峰值日 07-22", "117", "6", ""),
]
bw = 276
for i, (label, old, new, note) in enumerate(stats):
    bx = 24 + i * (bw + 12)
    rr(d, [(bx*S, y*S), ((bx+bw)*S, (y+92)*S)], 8*S, CARD)
    txt(d, (bx+18, y+13), label, f(10), DIM)
    # old
    txt(d, (bx+18, y+34), old, f(26, True), DIM2)
    ow = w_of(d, old, f(26, True))
    d.line([((bx+16)*S, (y+50)*S), ((bx+20+ow)*S, (y+50)*S)], fill=RED, width=2*S)
    # arrow
    txt(d, (bx+24+ow, y+40), "→", f(15), DIM2)
    # new
    txt(d, (bx+52+ow, y+34), new, f(26, True), GREEN)
    if note:
        txt(d, (bx+18, y+70), note, f(11, True), GREEN)

# ── 每日对照柱 ──
y += 112
txt(d, (24, y), "每天推给大鸟多少条", f(12, True), TXT)
txt(d, (200, y+2), "上=改前  下=改后", f(9), DIM2)
y += 24

maxv = 117
chart_w = 852
bar_w = chart_w / len(DAYS)
top_h = 130
for i, (day, old, new) in enumerate(DAYS):
    bx = 24 + i * bar_w
    # old bar (down from top)
    oh = max(1, top_h * old / maxv)
    rr(d, [((bx+2)*S, (y+top_h-oh)*S), ((bx+bar_w-3)*S, (y+top_h)*S)], 2*S,
       RED if old >= 40 else (196, 74, 70))
    # new bar
    nh = max(1, top_h * new / maxv)
    rr(d, [((bx+2)*S, (y+top_h+4)*S), ((bx+bar_w-3)*S, (y+top_h+4+nh)*S)], 2*S, GREEN)

d.line([(24*S, (y+top_h+2)*S), ((24+chart_w)*S, (y+top_h+2)*S)], fill=(80,80,84), width=1*S)

# 标注峰值
peak_i = [i for i,(dd,o,n) in enumerate(DAYS) if o == 117][0]
px = 24 + peak_i * bar_w
txt(d, (px-24, y-2), "117", f(10, True), RED)
txt(d, (px-14, y+top_h+26), "6", f(10, True), GREEN)

# 标注事故日
inc_i = len(DAYS) - 1
ix = 24 + inc_i * bar_w
txt(d, (ix-46, y+top_h+42), "事故日 41→7", f(9), AMBER)
txt(d, (ix-46, y+top_h+56), "没降到 0", f(9), AMBER)

# x 轴
for i, (day, _, _) in enumerate(DAYS):
    if i % 4 == 0:
        txt(d, (24 + i*bar_w - 6, y+top_h+12), day[3:], f(8), DIM2)

y += top_h + 78

# ── 关键说明 ──
rr(d, [(24*S, y*S), ((W-24)*S, (y+128)*S)], 8*S, CARD)
txt(d, (44, y+14), "两件事必须同时成立", f(11, True), TXT)
lines = [
    ("噪音降九成", "619 → 47 条。同一件事重复喊不再占位，「干完了」不推送。", GREEN),
    ("真事故一条不少", "07-25 事故日 41 → 7，不是 0。只看降幅会掉进「全部静音」的陷阱。", AMBER),
]
ty = y + 38
for head, body, col in lines:
    d.ellipse([(44*S, (ty+4)*S), (50*S, (ty+10)*S)], fill=col)
    txt(d, (58, ty), head, f(10, True), col)
    txt(d, (150, ty), body, f(10), DIM)
    ty += 22

ty += 6
txt(d, (44, ty), "真实事故回归（A6 硬门）：07-25 当天 104 条事件收敛成 18 件，", f(10), DIM)
ty += 16
txt(d, (44, ty), "Amanda 日巡失败、探针塌掉两场事故都判进「要你出手」——当天真实发生的是 Stats 一无所知。", f(10), DIM)

import os
out = "/Users/owner/Coevolution/Bigbird/PULSE/vultr-pulse-space/.relay/outbox"
os.makedirs(out, exist_ok=True)
p = f"{out}/stats-4-result.png"
im.save(p)
print(f"{p}  {W}x{H}pt")
