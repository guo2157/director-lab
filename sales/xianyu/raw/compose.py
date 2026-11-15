"""临时脚本：合成闲鱼上架用图片（1080x1440）。"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = r"E:\project\director-lab"
RAW = os.path.join(ROOT, "sales", "xianyu", "raw")
OUT = os.path.join(ROOT, "sales", "xianyu", "images")
W, H = 1080, 1440
BG = (11, 17, 19)
FG = (232, 240, 238)
MUTED = (150, 168, 166)
ACCENT = (89, 224, 197)
LINE = (35, 52, 58)

SERIF = "C:/Windows/Fonts/simsun.ttc"
SANS = "C:/Windows/Fonts/msyh.ttc"
SANS_BOLD = "C:/Windows/Fonts/msyhbd.ttc"


def font(path, size):
    return ImageFont.truetype(path, size)


def rounded(img, radius=18):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0], img.size[1]], radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def frame(img, radius=18, border=LINE, width=2, shadow=False):
    img = rounded(img.convert("RGB"), radius)
    canvas = Image.new("RGBA", (img.size[0] + width * 2, img.size[1] + width * 2), (0, 0, 0, 0))
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, canvas.size[0] - 1, canvas.size[1] - 1], radius + width, fill=255
    )
    ImageDraw.Draw(canvas).rounded_rectangle(
        [0, 0, canvas.size[0] - 1, canvas.size[1] - 1],
        radius + width,
        outline=border + (255,),
        width=width,
    )
    canvas.alpha_composite(img, (width, width))
    return canvas


def crop_shot(name, box=None, target_ratio=None):
    img = Image.open(os.path.join(RAW, f"{name}.png")).convert("RGB")
    if box:
        img = img.crop(box)
    if target_ratio:
        w, h = img.size
        ratio = w / h
        if ratio > target_ratio:
            nw = int(h * target_ratio)
            left = (w - nw) // 2
            img = img.crop((left, 0, left + nw, h))
        else:
            nh = int(w / target_ratio)
            img = img.crop((0, 0, w, nh))
    return img


def base():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # 顶部细线，代替边框装饰
    d.line([(0, 0), (W, 0)], fill=LINE, width=2)
    return img, d


def eyebrow(d, text, y, color=MUTED, size=30):
    d.text((80, y), text, font=font(SANS, size), fill=color)


def save(img, name):
    img.convert("RGB").save(os.path.join(OUT, name), quality=94)
    print(name, img.size)


# ---------- 01 封面 ----------
def cover():
    img, d = base()
    eyebrow(d, "本地部署 · 一次买断 · 无需剪辑基础", 84)
    d.text((80, 138), "一句话剧本", font=font(SERIF, 108), fill=FG)
    d.text((80, 258), "生成整部短剧", font=font(SERIF, 108), fill=ACCENT)
    d.line([(80, 424), (1000, 424)], fill=LINE, width=2)
    bullets = [
        "剧本自动拆集拆镜，分镜表带对白与动作",
        "角色定妆图锁定形象，全片人物不崩脸",
        "逐镜出视频，原生音轨对口型，自动合成成片",
    ]
    y = 462
    for b in bullets:
        d.rectangle([78, y + 14, 88, y + 24], fill=ACCENT)
        d.text((110, y), b, font=font(SANS, 34), fill=FG)
        y += 62
    shot = frame(crop_shot("storyboard", target_ratio=1440 / 900), radius=16)
    shot.thumbnail((960, 620))
    img.paste(shot, (60, 690), shot)
    d.text((80, 1360), "Windows 10 / 11 免安装版 · 浏览器操作 · 送图文手册",
           font=font(SANS, 30), fill=MUTED)
    save(img, "01-cover.png")


# ---------- 02-04 详情图 ----------
def detail(raw_name, title, subtitle, bullets, out_name, y_text=880):
    img, d = base()
    eyebrow(d, "Director Lab", 76)
    shot = frame(crop_shot(raw_name, target_ratio=1440 / 900), radius=14)
    shot.thumbnail((980, 660))
    img.paste(shot, ((W - shot.size[0]) // 2, 150), shot)
    d.line([(80, y_text), (1000, y_text)], fill=LINE, width=2)
    d.text((80, y_text + 44), title, font=font(SERIF, 66), fill=FG)
    d.text((80, y_text + 138), subtitle, font=font(SANS, 32), fill=MUTED)
    y = y_text + 216
    for b in bullets:
        d.rectangle([80, y + 12, 88, y + 20], fill=ACCENT)
        d.text((108, y), b, font=font(SANS, 30), fill=FG)
        y += 54
    save(img, out_name)


def flow():
    img, d = base()
    eyebrow(d, "制作流程", 84)
    d.text((80, 136), "六个环节", font=font(SERIF, 84), fill=FG)
    d.text((80, 240), "全部自动串联", font=font(SERIF, 84), fill=ACCENT)
    steps = [
        ("01", "剧本", "粘贴小说或大纲，自动拆集、拆镜"),
        ("02", "解析", "生成分镜表：景别、动作、对白、声音"),
        ("03", "资产", "角色 / 场景 / 道具参考图统一画风"),
        ("04", "分镜图", "逐镜出画面，单镜可重生成与替换"),
        ("05", "分镜视频", "逐镜出视频，原生音轨对口型"),
        ("06", "合成导出", "拼接、字幕、片头片尾，出 1080P 成片"),
    ]
    y = 400
    d.line([(104, y), (104, y + len(steps) * 150 - 40)], fill=LINE, width=2)
    for num, t, desc in steps:
        d.ellipse([97, y + 20, 111, y + 34], fill=ACCENT)
        d.text((150, y), num, font=font(SANS, 30), fill=ACCENT)
        d.text((226, y - 6), t, font=font(SERIF, 46), fill=FG)
        d.text((226, y + 56), desc, font=font(SANS, 28), fill=MUTED)
        y += 150
    d.line([(80, 1352), (1000, 1352)], fill=LINE, width=2)
    d.text((80, 1378), "同一套人设与世界观，续写下一集不跑偏",
           font=font(SANS, 30), fill=MUTED)
    save(img, "05-flow.png")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    cover()
    detail("storyboard", "逐镜出视频", "分镜看板里批量生成、单镜重生成，进度一目了然",
           ["每镜独立成片，不满意只重跑那一镜", "可选参考图与尾帧，镜头衔接更自然", "原生音轨对口型，无需另配 TTS"],
           "02-storyboard.png")
    detail("storyboard_images", "人物不崩脸", "角色定妆图 + 槽位绑定，多角色同镜不串脸",
           ["角色 / 场景 / 道具各自独立生成", "分镜图逐镜确认，可替换上传", "一键重生成，保留原有设定"],
           "03-images.png")
    detail("assets", "资产自动提取", "从剧本里认出角色、场景与道具，统一画风",
           ["同一角色全片共用一张定妆图", "道具、场景按集复用，续写自动继承", "资产未通过审核前会阻断生成"],
           "04-assets.png")
    flow()
