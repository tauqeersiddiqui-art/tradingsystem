"""Research backtest engine - clean room implementation mirroring live logic."""
from .research_engine import ResearchEngine
from .parity_test import ParityTestWrapper as ParityTest

__all__ = ["ResearchEngine", "ParityTest"]