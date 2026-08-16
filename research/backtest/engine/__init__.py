"""Research backtest engine - clean room implementation mirroring live logic."""

# Lazy imports to avoid circular import issues
def __getattr__(name):
    if name == "ResearchEngine":
        from .research_engine import ResearchEngine
        return ResearchEngine
    if name == "ParityTest":
        from .parity_test import ParityTestWrapper as ParityTest
        return ParityTest
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["ResearchEngine", "ParityTest"]