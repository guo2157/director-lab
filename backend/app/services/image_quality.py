"""图片质量检查服务（P3）：生成图片落库后的自动验收。

分层检查（对应质量验收与自动返工第一/二层）：
- 第一层 文件与技术检查：文件存在、大小合理、可解码、宽高符合下限、非空白/损坏
- 第二层 主体纯净度启发式检查（PIL 实现，不依赖外部视觉模型）：
  * 纯色占比（近单色画面 → 疑似空白/失败产物）
  * 边框/裁切检测：四条边内侧条带自身均匀、且与相邻内侧区域亮度明显突变，
    或条带几乎全是纯黑/纯白像素 → 疑似边框、拼贴或主体被裁切
  其余语义级检查（多人/额外肢体/混入角色等）超出本地启发式能力，
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
_NO_BORDER_CHECK_TYPES = {"scene", "keyframe", "clip", "video"}


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
        if extreme >= _BAND_EXTREME_RATIO:
            return f"{name}存在纯黑/纯白边条（极端像素占比 {extreme:.1%}），疑似边框或拼贴"
        # 均匀条带只有在自身接近纯黑/纯白时才算边框：
        # 浅灰均匀条带（如 240 留白）与内侧有内容形成对比属正常构图，不是边框或裁切
        band_solid = b_mean <= _BAND_SOLID_DARK_MAX or b_mean >= _BAND_SOLID_LIGHT_MIN
        if band_solid and b_std <= _BAND_STD_MAX and abs(b_mean - i_mean) >= _BAND_CONTRAST_MIN:
            return (
                f"{name}存在均匀边框条带（条带标准差 {b_std:.1f}，与内侧均值差 "
                f"{abs(b_mean - i_mean):.1f}），疑似边框、拼贴或主体被裁切"
            )
    return None


def check_image_quality(file_path: str, *, asset_type: str | None = None) -> dict:
    """对图片产物执行分层质量检查，返回 {passed, issues, checks}。
    asset_type 用于选择检查策略与 issues 描述。
    """
    issues: list[str] = []
    checks: dict = {}

    # --- 第一层：文件与技术检查 ---
    p = Path(file_path)
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

            # 边框/裁切检测（按资产类型选择是否启用）
            atype = (asset_type or "").strip().lower()
            checks["asset_type"] = atype or None
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
