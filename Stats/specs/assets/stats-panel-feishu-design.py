#!/usr/bin/env python3
"""Stats 界面设计稿 — 按 macOS 真实尺寸渲染，数据取自 2026-07-26 真实语料。"""
from PIL import Image, ImageDraw, ImageFont

R = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
S = 2  # retina

def f(size, bold=False):
    return ImageFont.truetype(B if bold else R, size * S)

# macOS 深色配色
BG      = (30, 30, 32)
CARD    = (44, 44, 47)
CARD2   = (38, 38, 41)
LINE    = (64, 64, 68)
TXT     = (245, 245, 247)
DIM     = (152, 152, 158)
DIM2    = (110, 110, 116)
RED     = (255, 85, 79)
AMBER   = (255, 179, 64)
GREEN   = (72, 207, 122)
BLUE    = (74, 158, 255)
BARBG   = (22, 22, 24)

def rr(d, box, r, fill, outline=None, w=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=w)

def txt(d, xy, s, font, fill):
    d.text((xy[0]*S, xy[1]*S), s, font=font, fill=fill)

def w_of(d, s, font):
    return d.textlength(s, font=font) / S


# ══════════════════ 图 1：菜单栏 ══════════════════
def draw_menubar():
    W, H = 900, 150
    im = Image.new("RGB", (W*S, H*S), BG)
    d = ImageDraw.Draw(im)

    txt(d, (24, 18), "① 菜单栏 — 平时与出事时", f(15, True), TXT)
    txt(d, (24, 40), "红点只在「你不出手就好不了」时出现；其余一律沉进面板", f(11), DIM)

    # 平时
    y = 74
    txt(d, (24, y + 6), "平时", f(11), DIM2)
    rr(d, [(70*S, y*S), (560*S, (y+26)*S)], 5*S, BARBG)
    x = 84
    txt(d, (x, y + 6), "39万  98%", f(12), TXT)
    x += 100
    txt(d, (x, y + 6), "58  4  2", f(12), TXT)
    x += 96
    d.ellipse([(x*S, (y+11)*S), ((x+5)*S, (y+16)*S)], fill=GREEN)
    txt(d, (x + 12, y + 6), "都正常", f(12), DIM)
    txt(d, (500, y + 6), "10:24", f(12), DIM)

    # 出事
    y = 112
    txt(d, (24, y + 6), "出事", f(11), DIM2)
    rr(d, [(70*S, y*S), (560*S, (y+26)*S)], 5*S, BARBG)
    x = 84
    txt(d, (x, y + 6), "39万  98%", f(12), TXT)
    x += 100
    txt(d, (x, y + 6), "58  4  2", f(12), TXT)
    x += 96
    d.ellipse([(x*S, (y+10)*S), ((x+7)*S, (y+17)*S)], fill=RED)
    txt(d, (x + 14, y + 6), "3 件要你出手", f(12, True), RED)
    txt(d, (500, y + 6), "10:24", f(12), DIM)

    return im


# ══════════════════ 图 2：展开面板 ══════════════════
def draw_panel():
    W, H = 900, 940
    im = Image.new("RGB", (W*S, H*S), BG)
    d = ImageDraw.Draw(im)

    PX, PW = 24, 852   # panel x, width

    txt(d, (PX, 16), "② 展开面板 — 三段式", f(15, True), TXT)
    txt(d, (PX, 38), "顺序即优先级：要你出手 → 我在盯 → 已经过去。数据为 2026-07-26 真实语料", f(11), DIM)

    y = 68
    rr(d, [(PX*S, y*S), ((PX+PW)*S, (H-20)*S)], 10*S, CARD2)

    # ─ header ─
    hy = y + 18
    txt(d, (PX+20, hy), "PULSE", f(15, True), TXT)
    d.ellipse([((PX+90)*S, (hy+5)*S), ((PX+97)*S, (hy+12)*S)], fill=RED)
    txt(d, (PX+104, hy+1), "3 件要你出手", f(12, True), RED)
    tm = "北京时间 07-26 10:24"
    txt(d, (PX+PW-20-w_of(d, tm, f(11)), hy+3), tm, f(11), DIM)
    sub = "数据 40 秒前 · 生产机 vultr"
    txt(d, (PX+PW-20-w_of(d, sub, f(10)), hy+20), sub, f(10), DIM2)
    d.line([((PX+16)*S, (hy+42)*S), ((PX+PW-16)*S, (hy+42)*S)], fill=LINE, width=1*S)

    # ─── 第一段：要你出手 ───
    y = hy + 58
    d.ellipse([((PX+20)*S, (y+4)*S), ((PX+27)*S, (y+11)*S)], fill=RED)
    txt(d, (PX+34, y), "要你出手", f(13, True), RED)
    txt(d, (PX+110, y+2), "3 件", f(11), DIM)
    hint = "自己好不了，且已经影响到人或活"
    txt(d, (PX+PW-20-w_of(d, hint, f(10)), y+2), hint, f(10), DIM2)

    y += 26
    cards = [
        ("三个监控探针自己塌了，已经两天",
         "ack-probe、账本体检、心跳看门狗都在非零退出。这三个塌了，等于协作面现在没人盯——"
         "别的告警不响不代表没事，可能只是没人在测。",
         "07-24 13:00 起 · 最近一次 10:15 · 33 次",
         "查 journalctl --user -u ack-probe.service -n 120"),
        ("Amanda 日巡连续 6 次没留下收尾证据",
         "跑完了报成功，但没有产出、没有台账。日巡等于空转，S1 台账停在 07-24。",
         "07-25 13:03 → 14:03 · 6 次",
         "已飞书通知过，未见处理"),
        ("两个 wi 卡在 hestia 手里超过 SLA 一倍",
         "wi_20260712_057 挂 12.5 天、wi_20260716_315 挂 9.4 天，SLA 是 7 天。"
         "执行人跨机不可达，自动回收够不着，只能你或 hestia 拍。",
         "持续 12 天 · 期间重复告警 140 次",
         "harness reassign 或直接判停"),
    ]
    for title, body, meta, act in cards:
        ch = 100
        rr(d, [((PX+16)*S, y*S), ((PX+PW-16)*S, (y+ch)*S)], 7*S, CARD)
        d.rectangle([((PX+16)*S, y*S), ((PX+19)*S, (y+ch)*S)], fill=RED)
        txt(d, (PX+32, y+11), title, f(12, True), TXT)
        # body 折行
        ty = y + 32
        line, words = "", body
        maxw = PW - 76
        cur = ""
        for ch_ in words:
            if w_of(d, cur+ch_, f(11)) > maxw:
                txt(d, (PX+32, ty), cur, f(11), DIM)
                ty += 16
                cur = ch_
            else:
                cur += ch_
        if cur:
            txt(d, (PX+32, ty), cur, f(11), DIM)
            ty += 16
        txt(d, (PX+32, y+ch-22), meta, f(10), DIM2)
        aw = w_of(d, act, f(10))
        rr(d, [((PX+PW-36-aw-16)*S, (y+ch-26)*S), ((PX+PW-32)*S, (y+ch-8)*S)], 4*S, (58,58,62))
        txt(d, (PX+PW-36-aw-8, y+ch-24), act, f(10), BLUE)
        y += ch + 8

    # ─── 第二段：我在盯 ───
    y += 12
    d.ellipse([((PX+20)*S, (y+4)*S), ((PX+27)*S, (y+11)*S)], fill=AMBER)
    txt(d, (PX+34, y), "我在盯", f(13, True), AMBER)
    txt(d, (PX+104, y+2), "5 件", f(11), DIM)
    hint2 = "有情况但我自己能处理，你可看可不看"
    txt(d, (PX+PW-20-w_of(d, hint2, f(10)), y+2), hint2, f(10), DIM2)

    y += 26
    rows = [
        ("账本有 5 条违例", "ho_20260722_359 挂 46 小时未接，另 4 条卡在 in_progress", "07-25 11:01"),
        ("退化密集一次", "15 分钟内命中 11 次，集中在 steward-vultr", "07-24 23:17"),
        ("Relay 非计划重启 2 次", "检测器没命中退化，原因可能无关", "07-26 10:20"),
        ("handoff 超时未接 1 笔", "ho_20260722_359 → amanda，已挂 2763 分钟", "07-24 15:30"),
        ("Codex 额度快满", "98%，10 小时后重置", "持续"),
    ]
    for t, sub_, when in rows:
        rr(d, [((PX+16)*S, y*S), ((PX+PW-16)*S, (y+38)*S)], 6*S, CARD)
        d.ellipse([((PX+30)*S, (y+16)*S), ((PX+36)*S, (y+22)*S)], fill=AMBER)
        txt(d, (PX+46, y+7), t, f(11, True), TXT)
        txt(d, (PX+46, y+22), sub_, f(10), DIM)
        txt(d, (PX+PW-32-w_of(d, when, f(10)), y+14), when, f(10), DIM2)
        y += 42

    # ─── 第三段：已经过去 ───
    y += 12
    d.ellipse([((PX+20)*S, (y+4)*S), ((PX+27)*S, (y+11)*S)], fill=GREEN)
    txt(d, (PX+34, y), "已经过去", f(13, True), GREEN)
    txt(d, (PX+118, y+2), "近 48 小时", f(11), DIM)
    hint3 = "留档不丢，不推送"
    txt(d, (PX+PW-20-w_of(d, hint3, f(10)), y+2), hint3, f(10), DIM2)

    y += 26
    rr(d, [((PX+16)*S, y*S), ((PX+PW-16)*S, (y+62)*S)], 6*S, CARD)
    txt(d, (PX+32, y+11), "自动干完 8 件 · 自动回收 3 个卡死执行人 · 限流退避 106 次", f(11), DIM)
    txt(d, (PX+32, y+30), "245 条原始事件收敛成 28 件事，其中 20 件是同一对象重复", f(11), DIM)
    txt(d, (PX+32, y+46), "全部可查，不因为 20 条上限被挤掉", f(10), DIM2)
    y += 74

    # footer
    d.line([((PX+16)*S, y*S), ((PX+PW-16)*S, y*S)], fill=LINE, width=1*S)
    y += 12
    txt(d, (PX+20, y), "19 项能力：9 项在测 · 10 项未接（已在界面标灰，不冒充绿）", f(10), DIM2)
    ftr = "上次看过：07-25 21:30 · 之后新增 4 件"
    txt(d, (PX+PW-20-w_of(d, ftr, f(10)), y), ftr, f(10), BLUE)

    return im


# ══════════════════ 图 3：飞书通知 ══════════════════
def draw_feishu():
    W, H = 900, 620
    im = Image.new("RGB", (W*S, H*S), BG)
    d = ImageDraw.Draw(im)

    txt(d, (24, 16), "③ 飞书通知 — 只有「要你出手」才发", f(15, True), TXT)
    txt(d, (24, 38), "一件事一条，状态变了才再发；干完了、我在盯的，一律不推", f(11), DIM)

    # 卡片
    CX, CW = 24, 500
    y = 70
    rr(d, [(CX*S, y*S), ((CX+CW)*S, (y+228)*S)], 9*S, (250, 250, 252))
    # 红顶条
    d.rounded_rectangle([(CX*S, y*S), ((CX+CW)*S, (y+34)*S)], radius=9*S, fill=(217, 58, 51))
    d.rectangle([(CX*S, (y+22)*S), ((CX+CW)*S, (y+34)*S)], fill=(217, 58, 51))
    txt(d, (CX+16, y+9), "要你出手", f(12, True), (255, 255, 255))
    txt(d, (CX+CW-16-w_of(d, "PULSE", f(11)), y+10), "PULSE", f(11), (255, 220, 218))

    ty = y + 48
    txt(d, (CX+16, ty), "三个监控探针自己塌了，已经两天", f(13, True), (28, 28, 30))
    ty += 26
    for ln in ["ack-probe、账本体检、心跳看门狗都在非零退出。",
               "这三个塌了，等于协作面现在没人盯——别的告警不响，",
               "不代表没事，可能只是没人在测。"]:
        txt(d, (CX+16, ty), ln, f(11), (72, 72, 76))
        ty += 17
    ty += 6
    txt(d, (CX+16, ty), "07-24 13:00 起，最近一次 10:15", f(10), (140, 140, 146))
    ty += 22
    rr(d, [((CX+16)*S, ty*S), ((CX+150)*S, (ty+26)*S)], 5*S, (26, 122, 245))
    txt(d, (CX+30, ty+6), "查 journal", f(11), (255, 255, 255))
    rr(d, [((CX+158)*S, ty*S), ((CX+280)*S, (ty+26)*S)], 5*S, (232, 232, 236))
    txt(d, (CX+172, ty+6), "我知道了", f(11), (72, 72, 76))

    # 右侧对照
    RX = 560
    y2 = 70
    txt(d, (RX, y2), "对照：现在的样子", f(12, True), DIM)
    y2 += 24
    rr(d, [(RX*S, y2*S), (876*S, (y2+150)*S)], 7*S, CARD)
    txt(d, (RX+14, y2+12), "07-22 一天推了 117 条", f(11, True), RED)
    yy = y2 + 34
    for ln in ["其中 170 条是同一个 wi 在喊",
               "111 条是「干完了」",
               "195 条推送失败，静默",
               "文案是 A reviewed structured",
               "escalation rule matched"]:
        txt(d, (RX+14, yy), "· " + ln, f(10), DIM)
        yy += 17
    txt(d, (RX+14, y2+128), "看的人无法据此行动", f(10), DIM2)

    # 底部：量的对照
    y3 = 330
    txt(d, (24, y3), "改完之后的量（按 7 月真实语料回算）", f(12, True), TXT)
    y3 += 24
    bars = [
        ("现在 · 每天推送", 117, RED, "峰值 07-22"),
        ("改后 · 每天推送", 3, GREEN, "只剩「要你出手」"),
    ]
    for label, val, color, note in bars:
        txt(d, (24, y3 + 4), label, f(11), DIM)
        bx = 180
        maxw = 480
        w = max(6, int(maxw * val / 117))
        rr(d, [(bx*S, y3*S), ((bx+maxw)*S, (y3+22)*S)], 4*S, CARD)
        rr(d, [(bx*S, y3*S), ((bx+w)*S, (y3+22)*S)], 4*S, color)
        txt(d, (bx + w + 10, y3 + 4), f"{val} 条", f(11, True), color)
        txt(d, (bx + maxw + 60, y3 + 5), note, f(10), DIM2)
        y3 += 34

    y3 += 12
    rr(d, [(24*S, y3*S), (876*S, (y3+96)*S)], 7*S, CARD)
    txt(d, (40, y3 + 12), "三条规矩", f(11, True), TXT)
    for i, ln in enumerate([
        "一件事只通知一次 —— 重复扫到不再喊，状态变了（恶化 / 解决）才再发一条",
        "默认不推送 —— 要推送的一方举证「你不出手它好不了」，证不出来就沉进面板",
        "推送失败要能被看见 —— 106 条限流失败现在是静默的，改后进「要你出手」",
    ]):
        txt(d, (40, y3 + 34 + i*20), f"{i+1}. {ln}", f(10), DIM)

    return im


import os
out = "/Users/owner/Coevolution/Bigbird/PULSE/vultr-pulse-space/.relay/outbox"
os.makedirs(out, exist_ok=True)
for name, fn in [("stats-1-menubar", draw_menubar),
                 ("stats-2-panel", draw_panel),
                 ("stats-3-feishu", draw_feishu)]:
    img = fn()
    p = f"{out}/{name}.png"
    img.save(p)
    print(f"{p}  {img.size[0]//S}x{img.size[1]//S}pt")
