"""pytest 公共配置：把项目根目录加入 sys.path（仓库根运行 pytest 即可）."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
