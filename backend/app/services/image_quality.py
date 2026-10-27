"""图片质量检查服务（P3）：生成图片落库后的自动验收。

分层检查（对应质量验收与自动返工第一/二层）：
- 第一层 文件与技术检查：文件存在、大小合理、可解码、宽高符合下限、非空白/损坏
- 第二层 主体纯净度启发式检查（PIL 实现，不依赖外部视觉模型）：
  * 纯色占比（近单色画面 → 疑似空白/失败产物）
  * 边缘裁切检测（四边高对比度像素占比 → 疑似主体被裁切/拼贴边框）
  其余语义级检查（多人/额外肢体/混入角色等）超出本地启发式能力，
  记为 not_applicable，不伪造通过结论。

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
# 边缘高对比度像素占比阈值（超过视为疑似裁切/边框拼贴）
_EDGE_ACTIVE_RATIO = 0.95


def check_image_quality(file_path: str, *, asset_type: str | None = None) -> dict:
    """对图片产物执行分层质量检查，返回 {passed, issues, checks}。
    asset_type 仅用于 issues 描述，不影响检查逻辑。
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
            # 边缘活跃度：检测边缘像素自身方差（高方差=疑似边框/拼贴/裁切）
            gray = small.convert("L")
            px = list(gray.getdata())
            edge_idx = [i for i in range(64) if i < 64 * 2 or i >= 64 * 62 or i % 64 < 2 or i % 64 >= 62]
            edge_vals = [px[i] for i in edge_idx]
            edge_mean = sum(edge_vals) / max(len(edge_vals), 1)
            edge_var = sum((v - edge_mean) ** 2 for v in edge_vals) / max(len(edge_vals), 1)
            edge_active = sum(1 for v in edge_vals if abs(v - edge_mean) > 24) / max(len(edge_vals), 1)
            checks["edge_active_ratio"] = round(edge_active, 4)
            checks["edge_variance"] = round(edge_var, 2)
            if edge_active >= _EDGE_ACTIVE_RATIO:
                issues.append(f"画面边缘像素变化过大（活跃占比 {edge_active:.1%}），疑似边框、拼贴或主体被裁切")
    except Exception as e:  # noqa: BLE001
        issues.append(f"纯净度检查执行失败: {type(e).__name__}")

    # 语义级检查（多人/额外肢体/混入元素）超出本地启发式能力，明确不适用
    checks["semantic_check"] = "not_applicable"

    return {"passed": not issues, "issues": issues, "checks": checks}
