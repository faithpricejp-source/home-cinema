"""pytest 根配置：把项目根加入 sys.path，使 `import homecinema` 生效。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
