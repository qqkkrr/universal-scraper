# -*- coding: utf-8 -*-
"""pytest 公共配置：项目根入 sys.path，测试离线可跑。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
