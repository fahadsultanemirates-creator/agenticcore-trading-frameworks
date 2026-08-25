"""Premium MT5 adapter package."""
from .bridge import PremiumMT5Bridge
from .mock_bridge import PremiumMockBridge
from .factory import get_premium_bridge

__all__ = ["PremiumMT5Bridge", "PremiumMockBridge", "get_premium_bridge"]
