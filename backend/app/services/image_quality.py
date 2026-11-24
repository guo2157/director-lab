"""图片质量检查服务（P3）：生成图片落库后的自动验收。

分层检查（对应质量验收与自动返工第一/二层）：
- 第一层 文件与技术检查：文件存在、大小合理、可解码、宽高符合下限、非空白/损坏
- 第二层 主体纯净度启发式检查（PIL 实现，不依赖外部视觉模型）：
  * 纯色占比（近单色画面 → 疑似空白/失败产物）
  * 边框/裁切检测：四条边内侧条带自身均匀、且与相邻内侧区域亮度明显突变，
    或条带几乎全是纯黑/纯白像素 → 疑似边框、拼贴或主体被裁切
  * 人物存在性检测（2026-11-02 新增）：场景基准图/道具锚定图要求画面中没有人。
    项目无图生图（edit/inpaint）能力，人物混入场景只能整图重采样，故必须在
    生成后前移拦截。实现为 YCbCr 肤色掩码在 16x16 网格上的聚集块判定，
    并对整体暖色调画面（沙地/木墙/夕阳）做防误报保护，属启发式筛查而非视觉模型。
  其余语义级检查（畸形肢体、错误道具等）超出本地启发式能力，
  记为 not_applicable，不伪造通过结论。

按资产类型差异化（2026-10-29 修订）：
- 场景基准图（scene）、分镜关键帧（keyframe）、视频片段（clip/video）为满画面构图，
  天空、地面、植被等自然分层会让边缘灰度本身离散。旧判据统计『边缘像素与其均值的偏离占比』，
  在这类资产上恒接近 100%（实测场景基准图 98.4%，角色定妆图 0.0%），
  实际只能区分『纯色背景』与『非纯色背景』，无法识别边框，属误判，故不参与边框裁切判定；
- 角色定妆图（character）、道具锚定图（prop）要求主体居中、背景干净，保留边框裁切判定。

返回结构：{"passed": bool, "issues": [str...], "checks": {name: result}}
检查失败不静默通过：issues 非空即 passed=False，由执行器决定重试或标记失败。
"""
from pathlib import Path

from app.core.config import stored_path

# 文件大小下限（字节）：低于此值视为可疑产物
_MIN_FILE_BYTES = 4_096
# 图片最短边下限（像素）
_MIN_SIDE = 256
# 纯色判定：单色像素占比阈值
_FLAT_COLOR_RATIO = 0.99

# --- 边框/裁切判定参数 ---
# 条带自身灰度标准差上限：低于此值视为『均匀条带』（真实画面条带标准差通常远高于此）
_BAND_STD_MAX = 12.0
# 条带与相邻内侧区域灰度均值差下限：低于此值说明条带与画面自然衔接，不是边框
_BAND_CONTRAST_MIN = 30.0
# 条带内接近纯黑/纯白的像素占比阈值：超过视为纯色边条
_BAND_EXTREME_RATIO = 0.95
_BAND_EXTREME_DARK = 12
_BAND_EXTREME_LIGHT = 250
# 条带均值须近乎纯黑/纯白才可能是边框；浅灰均匀条带属正常画面留白（实测留白常见 240）
_BAND_SOLID_DARK_MAX = 40
_BAND_SOLID_LIGHT_MIN = 245
# 不参与边框裁切判定的资产类型（满画面构图，边缘自然离散）
_NO_BORDER_CHECK_TYPES = {"clip", "video"}

# --- 人物存在性判定参数（仅场景/道具：要求画面中没有人）---
# 必须在生成后拦截：项目无 edit/inpaint 图生图能力，人物混入后只能整图重采样。
# 判定依据放在『肤色聚集块』而非『全图肤色占比』：暖色地面、木墙、夕阳会让
# 全图肤色占比整体偏高，属背景色调；人物则表现为局部密集聚集。
_SKIN_GRID = 16
_SKIN_CELL_PX = 8
_SKIN_CELL_RATIO = 0.35        # 单格肤色像素占比达到此值记为肤色格
_SKIN_MIN_BLOCK = 4            # 相邻肤色格连通块不小于此值才判为人物
_SKIN_MIN_BLOCK_SIDE = 2       # 连通块外接框宽高至少各占此值（排除一维细条误报）
_SKIN_FULL_FRAME_RATIO = 0.55  # 全图肤色占比超过此值视为整体暖色调背景，不报
_SKIN_LUMA_MIN = 40            # 过暗区域（阴影/黑色物体）不计入肤色
_SKIN_LUMA_MAX = 235
# 形状约束（2026-11-24 新增）：肤色聚集块还必须「像人」，否则暖色背景会被判成人物。
# 实测两处误报（E2E prj_7717c671b15c，同一批 7 张场景/道具图命中 2 张）：
#   * 城市天台 ast_90c242520e2e：湿水泥地的橙色反光构成 8 格宽 × 3 格高的横向条带，
#     判「混入人物 16 格 / 肤色 13.0%」→ 编译阻断 → 分镜图回退旧版单人提示词；
#   * 水泥楼梯间 ast_e42d88e0c876：米白色墙面占满 13×13 格（填充率 0.61），
#     判「混入人物 103 格 / 肤色 36.1%」→ 同样的连锁回退。
# 人物形态是「竖长、不铺满画幅」：外接框高宽比 ≥ 0.8，且宽高都不接近整幅。
_SKIN_MIN_ASPECT = 0.8         # 外接框 高/宽 下限：横向条带（反光/地面/条状物）达不到
_SKIN_MAX_SPAN_RATIO = 0.75    # 外接框 宽或高 占网格比例上限：整墙/整片背景超限
# 饱和度约束（2026-11-24 新增，第 3 处误报）：黄铜/古铜等金属色与湿地面霓虹反光的
# 色相几乎和肤色重合，形状也竖长，前两条判据都拦不住。实测道具「旧钥匙」
# （ast_9e52f5fe03bf，黄铜钥匙竖放在米白背景）被判「混入人物 27 格」，
# 进而阻断所有引用该道具的分镜图编译。
# 真肤色饱和度低：实测程野 S 中位数 48、林夏 46、合成图 90；
# 而黄铜钥匙 159、湿地面霓虹反光 165 —— 高饱和是材质/光源，不是皮肤。
_SKIN_MAX_SATURATION_MEDIAN = 130
_NO_PERSON_CHECK_TYPES = {"scene", "prop"}


def _detect_border(gray) -> str | None:
    """检测四条边是否存在纯色边框/裁切痕迹。

    仅当条带『自身均匀』且『与内侧区域明显突变』，或条带几乎全为纯黑/纯白时判定为边框。
    返回问题描述，未命中返回 None。
    """
    w, h = gray.size
    band = max(2, int(min(w, h) * 0.015))
    inner = band * 3
    edges = {
        "上边缘": ((0, 0, w, band), (0, band, w, min(band + inner, h))),
        "下边缘": ((0, h - band, w, h), (0, max(h - band - inner, 0), w, h - band)),
        "左边缘": ((0, 0, band, h), (band, 0, min(band + inner, w), h)),
        "右边缘": ((w - band, 0, w, h), (max(w - band - inner, 0), 0, w - band, h)),
    }
    for name, (band_box, inner_box) in edges.items():
        b_vals = list(gray.crop(band_box).getdata())
        i_vals = list(gray.crop(inner_box).getdata())
        if not b_vals or not i_vals:
            continue
        b_mean = sum(b_vals) / len(b_vals)
        b_std = (sum((v - b_mean) ** 2 for v in b_vals) / len(b_vals)) ** 0.5
        i_mean = sum(i_vals) / len(i_vals)
        extreme = sum(1 for v in b_vals if v <= _BAND_EXTREME_DARK or v >= _BAND_EXTREME_LIGHT) / len(b_vals)
        # 极端条带还必须与内侧存在明显亮度跃变才算边框：
        # 纯白/纯黑背景的定妆图（角色、道具）整条边缘本就是同一种背景色，
        # 白色会连续向内延伸，与内侧均值差接近 0，属正常留白而非拼贴边框。
        if extreme >= _BAND_EXTREME_RATIO and abs(b_mean - i_mean) >= _BAND_CONTRAST_MIN:
            return (
                f"{name}存在纯黑/纯白边条（极端像素占比 {extreme:.1%}，"
                f"与内侧均值差 {abs(b_mean - i_mean):.1f}），疑似边框或拼贴"
            )
        # 均匀条带只有在自身接近纯黑/纯白时才算边框：
        # 浅灰均匀条带（如 240 留白）与内侧有内容形成对比属正常构图，不是边框或裁切
        band_solid = b_mean <= _BAND_SOLID_DARK_MAX or b_mean >= _BAND_SOLID_LIGHT_MIN
        if band_solid and b_std <= _BAND_STD_MAX and abs(b_mean - i_mean) >= _BAND_CONTRAST_MIN:
            return (
                f"{name}存在均匀边框条带（条带标准差 {b_std:.1f}，与内侧均值差 "
                f"{abs(b_mean - i_mean):.1f}），疑似边框、拼贴或主体被裁切"
            )
    return None


def _detect_person(rgb) -> str | None:
    """启发式检测画面中是否存在人物（肤色聚集块）。

    返回问题描述，未命中返回 None。
    这是启发式筛查而非视觉模型：命中后由执行器换 seed 重新采样，
    不宣称已精确识别『几个人』或『是谁』。

    三级判据（任一不满足即放行）：
    1. 肤色聚集块足够大（≥ _SKIN_MIN_BLOCK 格，外接框宽高各 ≥ 2 格）；
    2. 形状像人：外接框高宽比 ≥ _SKIN_MIN_ASPECT 且不铺满画幅
       —— 排除横向反光条带与整片墙面；
    3. 饱和度像皮肤：肤色像素 S 中位数 ≤ _SKIN_MAX_SATURATION_MEDIAN
       —— 排除黄铜/古铜等金属色与霓虹反光。
    """
    size = _SKIN_GRID * _SKIN_CELL_PX
    small = rgb.resize((size, size))
    ycbcr = small.convert('YCbCr')
    y_data = ycbcr.getchannel('Y').tobytes()
    cb_data = ycbcr.getchannel('Cb').tobytes()
    cr_data = ycbcr.getchannel('Cr').tobytes()

    cells = [0] * (_SKIN_GRID * _SKIN_GRID)
    skin_total = 0
    sat_values: list[int] = []
    for idx in range(size * size):
        y = y_data[idx]
        if y < _SKIN_LUMA_MIN or y > _SKIN_LUMA_MAX:
            continue
        if 77 <= cb_data[idx] <= 127 and 133 <= cr_data[idx] <= 173:
            skin_total += 1
            row = idx // size // _SKIN_CELL_PX
            col = idx % size // _SKIN_CELL_PX
            cells[row * _SKIN_GRID + col] += 1

    if skin_total == 0:
        return None
    skin_ratio = skin_total / (size * size)
    if skin_ratio >= _SKIN_FULL_FRAME_RATIO:
        # 整体暖色调画面（沙地、木墙、夕照），不是局部人物
        return None

    # 饱和度判据：高饱和 = 金属材质或强色光源，不是皮肤。
    # 只在命中候选块后计算会更省，但这里已遍历过一遍，顺手取样即可。
    hsv = small.convert('HSV')
    sat_data = hsv.getchannel('S').tobytes()
    for idx in range(size * size):
        y = y_data[idx]
        if y < _SKIN_LUMA_MIN or y > _SKIN_LUMA_MAX:
            continue
        if 77 <= cb_data[idx] <= 127 and 133 <= cr_data[idx] <= 173:
            sat_values.append(sat_data[idx])
    if sat_values:
        sat_values.sort()
        sat_median = sat_values[len(sat_values) // 2]
        if sat_median > _SKIN_MAX_SATURATION_MEDIAN:
            return None

    per_cell = _SKIN_CELL_PX * _SKIN_CELL_PX
    hot = {i for i, n in enumerate(cells) if n / per_cell >= _SKIN_CELL_RATIO}
    if len(hot) < _SKIN_MIN_BLOCK:
        return None

    seen: set[int] = set()
    largest = 0
    largest_side = 0
    # 最大连通块的外接框必须单独记录：min_r/max_r/min_c/max_c 是每个连通块的
    # 循环内局部量，循环结束后残留的是「最后一块」而不是「最大块」的包围盒。
    best_box = (0, 0, 0, 0)  # (min_r, max_r, min_c, max_c)
    for start in hot:
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        block = 0
        min_r = max_r = start // _SKIN_GRID
        min_c = max_c = start % _SKIN_GRID
        while stack:
            cur = stack.pop()
            block += 1
            r, c = divmod(cur, _SKIN_GRID)
            min_r, max_r = min(min_r, r), max(max_r, r)
            min_c, max_c = min(min_c, c), max(max_c, c)
            for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if 0 <= nr < _SKIN_GRID and 0 <= nc < _SKIN_GRID:
                    nxt = nr * _SKIN_GRID + nc
                    if nxt in hot and nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
        if block > largest:
            largest = block
            largest_side = min(max_c - min_c + 1, max_r - min_r + 1)
            best_box = (min_r, max_r, min_c, max_c)

    # 一维细条不算人物：米白色纸张边缘、木条、椅腿等浅色窄带会落入肤色区间，
    # 实测道具图 ast_25f8313d91c3 的书口被识别成 1 格宽 × 10 格高的竖条并误报。
    # 人物是二维聚集块，外接框宽高至少各占 2 格。
    if largest < _SKIN_MIN_BLOCK or largest_side < _SKIN_MIN_BLOCK_SIDE:
        return None

    # 形状约束：仅「像人」的聚集块才算人物（2026-11-24）。
    # 上面两处 E2E 误报分别是横向条带（高宽比 0.375）与铺满画幅的墙面
    # （13×13 格 / 81% 画幅），都在这里被排除，不再触发编译阻断连锁。
    min_r, max_r, min_c, max_c = best_box
    box_w = max_c - min_c + 1
    box_h = max_r - min_r + 1
    aspect = box_h / box_w
    span = max(box_w, box_h) / _SKIN_GRID
    if aspect < _SKIN_MIN_ASPECT or span > _SKIN_MAX_SPAN_RATIO:
        return None

    return (
        f'疑似混入人物（肤色聚集块 {largest} 格，外接框 {box_w}×{box_h} 格，'
        f'全图肤色 {skin_ratio:.1%}），场景/道具图应为空场，需重采样'
    )


def check_image_quality(file_path: str, *, asset_type: str | None = None) -> dict:
    """对图片产物执行分层质量检查，返回 {passed, issues, checks}。
    asset_type 用于选择检查策略与 issues 描述。
    """
    issues: list[str] = []
    checks: dict = {}

    # --- 第一层：文件与技术检查 ---
    p = stored_path(file_path)
    checks["file_exists"] = p.exists()
    if not p.exists():
        return {"passed": False, "issues": ["文件不存在"], "checks": checks}

    size = p.stat().st_size
    checks["file_bytes"] = size
    if size < _MIN_FILE_BYTES:
        issues.append(f"文件过小（{size} 字节），疑似空白或损坏产物")

    try:
        from PIL import Image
    except ImportError:
        # PIL 不可用时只完成文件级检查，不伪造通过
        return {
            "passed": not issues,
            "issues": issues + (["PIL 不可用，无法执行解码与纯净度检查"] if not issues else []),
            "checks": checks,
        }

    try:
        with Image.open(p) as img:
            img.load()
            width, height = img.size
            checks["size"] = [width, height]
    except Exception as e:  # noqa: BLE001
        return {"passed": False, "issues": [f"图片无法解码: {type(e).__name__}"], "checks": checks}

    if min(width, height) < _MIN_SIDE:
        issues.append(f"图片尺寸过小（{width}x{height}），低于下限 {_MIN_SIDE}px")

    # --- 第二层：主体纯净度启发式检查 ---
    try:
        with Image.open(p) as img:
            rgb = img.convert("RGB")
            small = rgb.resize((64, 64))
            # 纯色占比：量化到 16 级后统计众数颜色占比
            quant = small.quantize(colors=16)
            hist = quant.getcolors(64 * 64) or []
            top = max(hist)[0] if hist else 0
            flat_ratio = top / (64 * 64)
            checks["flat_color_ratio"] = round(flat_ratio, 4)
            if flat_ratio >= _FLAT_COLOR_RATIO:
                issues.append(f"画面接近纯色（占比 {flat_ratio:.1%}），疑似空白或失败产物")

            atype = (asset_type or "").strip().lower()
            checks["asset_type"] = atype or None

            # 人物存在性检测（场景/道具要求画面中没有人；无 edit 能力只能前移拦截）
            if atype in _NO_PERSON_CHECK_TYPES:
                person_hit = _detect_person(rgb)
                checks["person_check"] = person_hit or "ok"
                if person_hit:
                    issues.append(person_hit)
            else:
                checks["person_check"] = "not_applicable"

            # 边框/裁切检测（按资产类型选择是否启用）
            if atype in _NO_BORDER_CHECK_TYPES:
                checks["border_check"] = "skipped_full_frame_asset"
            else:
                gray = rgb.convert("L").resize((256, 256))
                hit = _detect_border(gray)
                checks["border_check"] = hit or "ok"
                if hit:
                    issues.append(hit)
    except Exception as e:  # noqa: BLE001
        issues.append(f"纯净度检查执行失败: {type(e).__name__}")

    # 语义级检查（多人/额外肢体/混入元素）超出本地启发式能力，明确不适用
    checks["semantic_check"] = "not_applicable"

    return {"passed": not issues, "issues": issues, "checks": checks}
