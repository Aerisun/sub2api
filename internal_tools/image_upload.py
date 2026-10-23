"""Decode recognition multipart uploads entirely in memory; never create files."""
import base64
from email.parser import BytesParser
from email.policy import default

from internal_tools.errors import ToolError


def invalid(message, param=None):
    return ToolError(400, message, "invalid_arguments", param=param)


def recognition_upload(raw, content_type):
    if not isinstance(content_type, str) or any(c in content_type for c in "\r\n"):
        raise invalid("无效的 multipart Content-Type")
    try:
        prefix = ("MIME-Version: 1.0\r\nContent-Type: " + content_type + "\r\n\r\n").encode("ascii")
        message = BytesParser(policy=default).parsebytes(prefix + raw)
    except (ValueError, UnicodeError):
        raise invalid("上传格式无效") from None
    if message.get_content_type() != "multipart/form-data" or not message.is_multipart() or message.defects:
        raise invalid("请使用 multipart/form-data 上传图片")
    arguments, images = {}, []
    parts = list(message.iter_parts())
    if len(parts) > 4:
        raise invalid("每次仅上传一张图片，以及 prompt、max_output_tokens、stream")
    for part in parts:
        name = part.get_param("name", header="content-disposition")
        if part.is_multipart() or part.defects or part.get_content_disposition() != "form-data":
            raise invalid("上传字段格式无效")
        value = part.get_payload(decode=True)
        if not isinstance(value, bytes):
            raise invalid("上传内容无效", name)
        if name in ("image", "images"):
            if not part.get_filename() or not 0 < len(value) <= 16 * 1024 * 1024 or images:
                raise invalid("每次仅上传一张图片，不超过 16 MiB；多图请并行调用工具", "image")
            media = ("image/png" if value.startswith(b"\x89PNG\r\n\x1a\n") else
                     "image/jpeg" if value.startswith(b"\xff\xd8\xff") else
                     "image/webp" if value.startswith(b"RIFF") and value[8:12] == b"WEBP" else None)
            if media is None:
                raise invalid("请选择 PNG、JPG 或 WebP 图片", "image")
            images.append("data:" + media + ";base64," + base64.b64encode(value).decode("ascii"))
        elif name in ("prompt", "max_output_tokens", "stream") and name not in arguments and part.get_filename() is None:
            if len(value) > 32000:
                raise invalid("提示词过长", name)
            try:
                text = value.decode("utf-8")
                if name == "stream":
                    if text not in ("true","false"):raise ValueError("Expected boolean")
                    arguments[name] = text == "true"
                else:arguments[name] = int(text) if name == "max_output_tokens" else text
            except (UnicodeError, ValueError):
                raise invalid("字段格式无效", name) from None
        else:
            raise invalid("仅支持 image、prompt、max_output_tokens、stream", name)
    if not images:
        raise invalid("请上传图片文件，字段名为 image", "image")
    return {**arguments, "image": images[0]}
