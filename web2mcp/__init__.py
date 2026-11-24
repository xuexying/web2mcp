"""Local browser-discovered API adapters. Runtime never launches a browser."""

from .client import Client
from .product import Product

__version__ = "0.4.0"
__all__ = ["Client", "Product", "__version__"]
