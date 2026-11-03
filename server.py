"""Compatibility entry point; new deployments may use backend.app.main:app."""
import sys
from backend.app.api import application
sys.modules[__name__] = application
