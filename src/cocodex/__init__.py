from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("cocodex")
except PackageNotFoundError:
    __version__ = "0.0.0"
