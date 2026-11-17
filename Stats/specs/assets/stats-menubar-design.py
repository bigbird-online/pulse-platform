#!/usr/bin/env python3
"""菜单栏 v3 — 照 macOS Stats 的上小下大两行结构，合成一个图标。"""
from PIL import Image, ImageDraw, ImageFont

R = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
S = 3   # 高倍，菜单栏元素很小

def f(size, bold=False):
    return ImageFont.truetype(B if bold else R, int(size * S))

BG    = (30, 30, 32)
CARD  = (44, 44, 47)
TXT   = (245, 245, 247)
DIM   = (152, 152, 158)
DIM2  = (110, 110, 116)
RED   = (255, 69, 58)
AMBER = (255, 179, 64)
GREEN = (50, 215, 75)
BARBG = (28, 28, 30)

def rr(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)

def txt(d, xy, s, font, fill):
    d.text((xy[0]*S, xy[1]*S), s, font=font, fill=fill)

def w_of(d, s, font):
    return d.textlength(s, font=font) / S

def ctr(d, cx, y, s, font, fill):
    """居中绘制"""
    d.text(((cx - w_of(d, s, font)/2)*S, y*S), s, font=font, fill=fill)


# 菜单栏一个 item：上小下大两行
LBL = 6.5   # 标签字号
VAL = 11    # 数值字号

def slot(d, x, y, label, value, vcolor=TXT, lcolor=DIM):
    """返回该 slot 宽度。y 为条顶。"""
    wl = w_of(d, label, f(LBL))
    wv = w_of(d, value, f(VAL, True))
    w = max(wl, wv)
    cx = x + w/2
    ctr(d, cx, y + 3.5, label, f(LBL), lcolor)
    ctr(d, cx, y + 12, value, f(VAL, True), vcolor)
    return w


W, H = 760, 500
im = Image.new("RGB", (W*S, H*S), BG)
d = ImageDraw.Draw(im)

txt(d, (20, 14), "① 菜单栏 v3 — 照 Stats 的两行结构，合成一个", f(13, True), TXT)
txt(d, (20, 33), "标签小而灰，数值大而白；一眼只看见数字，标签需要时才读", f(9), DIM)

BAR_H = 30

# ── 参考原图 ──
y = 62
txt(d, (20, y), "你给的参考", f(8.5), DIM2)
y += 14
rr(d, [(20*S, y*S), (250*S, (y+BAR_H)*S)], 4*S, (139, 94, 8))
x = 32
for lb, vl in [("CPU", "9%"), ("RAM", "83%"), ("SSD", "72%")]:
    w = slot(d, x, y, lb, vl, TXT, (230, 210, 170))
    x += w + 16
d.ellipse([(x*S, (y+7)*S), ((x+6)*S, (y+13)*S)], fill=(255, 69, 58))
d.ellipse([(x*S, (y+17)*S), ((x+6)*S, (y+23)*S)], fill=(10, 132, 255))
txt(d, (x + 11, y + 5), "2 KB/s", f(8), TXT)
txt(d, (x + 11, y + 16), "3 KB/s", f(8), TXT)

# ── 新版：平时 ──
y += 58
txt(d, (20, y), "PULSE · 平时", f(8.5), DIM2)
y += 14
rr(d, [(20*S, y*S), (232*S, (y+BAR_H)*S)], 4*S, BARBG)
x = 34
w = slot(d, x, y, "CC", "51%", TXT)
x += w + 20
w = slot(d, x, y, "CODEX", "98%", AMBER)
x += w + 20
d.ellipse([(x*S, (y+12)*S), ((x+7)*S, (y+19)*S)], fill=GREEN)
txt(d, (x + 12, y + 10), "正常", f(10), DIM)
txt(d, (250, y + 4), "配额超 90% 自己变黄，不用你比阈值", f(8.5), DIM2)
txt(d, (250, y + 17), "「正常」= 没有一件事需要你", f(8.5), DIM2)

# ── 新版：出事 ──
y += 58
txt(d, (20, y), "PULSE · 出事", f(8.5), DIM2)
y += 14
rr(d, [(20*S, y*S), (268*S, (y+BAR_H)*S)], 4*S, BARBG)
x = 34
w = slot(d, x, y, "CC", "51%", TXT)
x += w + 20
w = slot(d, x, y, "CODEX", "98%", AMBER)
x += w + 20
d.ellipse([(x*S, (y+11)*S), ((x+8)*S, (y+19)*S)], fill=RED)
txt(d, (x + 13, y + 9), "3 件要你出手", f(10, True), RED)
txt(d, (286, y + 10), "只有右边这一段会变", f(8.5), DIM2)

# ── 新版：配额告急 ──
y += 58
txt(d, (20, y), "PULSE · 配额告急", f(8.5), DIM2)
y += 14
rr(d, [(20*S, y*S), (232*S, (y+BAR_H)*S)], 4*S, BARBG)
x = 34
w = slot(d, x, y, "CC", "94%", RED)
x += w + 20
w = slot(d, x, y, "CODEX", "100%", RED)
x += w + 20
d.ellipse([(x*S, (y+12)*S), ((x+7)*S, (y+19)*S)], fill=GREEN)
txt(d, (x + 12, y + 10), "正常", f(10), DIM)
txt(d, (250, y + 4), "配额和协作状态各管各的", f(8.5), DIM2)
txt(d, (250, y + 17), "额度烧完不等于有事要你干", f(8.5), DIM2)

# ── 说明卡 ──
y += 62
rr(d, [(20*S, y*S), ((W-20)*S, (y+118)*S)], 6*S, CARD)
txt(d, (36, y + 13), "合成一个之后", f(10, True), TXT)
ty = y + 33
for ln in ["· 菜单栏只剩一项，点开是同一个三段式面板",
           "· 两个配额是「要你预判」的数：还能不能干活、要不要省着用",
           "· 右边一句话是「要不要立刻管」：正常 / N 件要你出手",
           "· 原来的 58 / 4 / 2 和 token 总量全部下沉到面板，菜单栏不放统计"]:
    txt(d, (36, ty), ln, f(9), DIM)
    ty += 17

ty += 4
txt(d, (36, ty), "宽度约 210pt（原来两项合计 280pt 预算）", f(8.5), DIM2)

import os
out = "/Users/owner/Coevolution/Bigbird/PULSE/vultr-pulse-space/.relay/outbox"
os.makedirs(out, exist_ok=True)
p = f"{out}/stats-1-menubar.png"
im.save(p)
print(f"{p}  {W}x{H}pt")
