"""Local browser-discovered API adapters. Runtime never launches a browser."""
from .client import Client
from .product import Product

__all__ = ["Client", "Product"]
