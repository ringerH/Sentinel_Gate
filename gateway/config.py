import os

# Database and Cache configurations
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./governance.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Security configurations
# Fallback signing key for signature verification if not provided per agent in DB
DEFAULT_SECRET = os.getenv("DEFAULT_SECRET", "super-secret-key-12345")

# Gateway server settings
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
