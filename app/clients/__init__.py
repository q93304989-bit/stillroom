"""客户端层：把每个外部服务封装成一个薄对象。

只做三件事：拼请求、解析响应、抛类型化异常。**不做重试编排、不碰界面、不写文件**
（图床读文件是唯一例外，因为它本来就是「上传本地文件」这件事）。
"""

from app.clients.image_client import IMAGE_MODELS, IMAGE_SIZES, ImageClient, ImageRequest
from app.clients.image_host import GH_GUIDE, SEE_GUIDE, upload_to_image_host
from app.clients.llm_client import LlmClient
from app.clients.video_client import (
    ASPECT_RATIOS,
    SECONDS_OPTIONS,
    VIDEO_MODELS,
    VideoClient,
    VideoRequest,
)

__all__ = [
    "ImageClient",
    "ImageRequest",
    "IMAGE_MODELS",
    "IMAGE_SIZES",
    "VideoClient",
    "VideoRequest",
    "VIDEO_MODELS",
    "ASPECT_RATIOS",
    "SECONDS_OPTIONS",
    "LlmClient",
    "upload_to_image_host",
    "GH_GUIDE",
    "SEE_GUIDE",
]
