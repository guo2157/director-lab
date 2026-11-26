"""回归：历史 Windows 反斜杠资产路径在 Linux 上的可用性。

缺陷背景（真实 keyframe 出图端到端验证暴露，两处独立缺陷）：
1. executor 上传参考图时直接 open(local_path)；历史 asset.file_path 落库为
   Windows 反斜杠形态，Linux 不把反斜杠当分隔符，上传阶段必然 ENOENT，
   所有出图退化为 comfy_execution_error。
2. keyframe_image_qc._image_to_data_uri 直接 Path(file_path).exists()，
   同样的路径会让参考图被全部丢弃，图像级质检静默降级为
   reference_images_missing（image_qc_ok 仍为 True，缺陷不易察觉）。

修复口径：统一经 app.core.config.stored_path 归一回正斜杠。
"""
import inspect
import struct
import zlib


def _png_bytes() -> bytes:
    """1×1 有效 PNG 字节。"""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", crc)
    raw = zlib.compress(b"\x00\xff\x00\x00")
    crc2 = zlib.crc32(b"IDAT" + raw) & 0xFFFFFFFF
    idat = struct.pack(">I", len(raw)) + b"IDAT" + raw + struct.pack(">I", crc2)
    return sig + ihdr + idat


class TestImageToDataUriBackslashPath:
    """keyframe_image_qc._image_to_data_uri 必须能吃反斜杠路径。"""

    def test_backslash_path_is_readable(self, tmp_path, monkeypatch):
        media = tmp_path / "data" / "media"
        media.mkdir(parents=True)
        (media / "frame.png").write_bytes(_png_bytes())
        monkeypatch.chdir(tmp_path)
        from app.services.keyframe_image_qc import _image_to_data_uri

        out = _image_to_data_uri("data\\media\\frame.png")
        assert out is not None, "反斜杠路径未被归一化，参考图会被静默丢弃"
        # 实现会统一缩放并转 JPEG（控制评图请求体大小），故 MIME 固定为 jpeg
        assert out.startswith("data:image/jpeg;base64,")

    def test_forward_slash_path_unchanged(self, tmp_path, monkeypatch):
        media = tmp_path / "data" / "media"
        media.mkdir(parents=True)
        (media / "frame.png").write_bytes(_png_bytes())
        monkeypatch.chdir(tmp_path)
        from app.services.keyframe_image_qc import _image_to_data_uri

        assert _image_to_data_uri("data/media/frame.png") is not None

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from app.services.keyframe_image_qc import _image_to_data_uri

        assert _image_to_data_uri("data\\media\\absent.png") is None


class TestExecutorNormalizesMediaPaths:
    """executor 必须在 upload_image 之前完成反斜杠归一化。"""

    def test_media_normalized_before_upload(self):
        from app.worker import executor

        src = inspect.getsource(executor._execute_generate_job_once)
        assert "stored_path as _norm_stored_path" in src, "缺少 stored_path 归一化导入"
        assert "_norm_stored_path(_v)" in src, "media_files 未做反斜杠归一化"
        norm_at = src.index("_norm_stored_path(_v)")
        upload_at = src.index("client.upload_image")
        assert norm_at < upload_at, "归一化必须发生在 upload_image 之前"

    def test_stored_path_normalizes_backslash(self):
        from app.core.config import stored_path

        # 用 as_posix 断言：Windows 上 Path 的 str() 会把分隔符还原成反斜杠，
        # 这里校验的是「分隔符已归一为正斜杠」这一语义
        assert stored_path("data\\media\\x.png").as_posix() == "data/media/x.png"
