"""配置层：路径、密钥、偏好。

约定（三条硬规则的一部分）：

- **密钥只放 `.env`**，由 `credentials` 独占读写；
- **偏好只放 `settings.json`**，由 `settings` 独占读写；
- **路径只由 `paths` 解析**，其他模块不再自己拼 `sys._MEIPASS` / exe 目录。

任何模块要配置，都从这里取，不允许再出现第二处 `load_dotenv`。
"""

from app.config import paths, settings

__all__ = ["paths", "settings"]
