#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
出版商特定处理模块

包含不同出版商（Nature、Science等）的论文提取处理器
"""

from .nature import NatureHandler
from .aps import APSHandler
from .aip import AIPHandler
from .cambridge import CambridgeHandler
from .iop import IOPHandler
from .optica import OpticaHandler
from .science import ScienceHandler
from .sciencedirect import ScienceDirectHandler

__all__ = [
    'NatureHandler', 'APSHandler', 'AIPHandler', 'CambridgeHandler',
    'IOPHandler', 'OpticaHandler', 'ScienceHandler', 'ScienceDirectHandler',
]
